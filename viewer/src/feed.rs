use std::fs::File;
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{self, Receiver, Sender};
use std::thread;

use anyhow::{Context, Result, bail};
use serde_json::Value;

#[derive(Clone, Debug)]
pub struct FeedConfig {
    pub python: PathBuf,
    pub cli: PathBuf,
    pub state_dir: Option<PathBuf>,
    pub after: u64,
    pub follow: bool,
    pub poll_seconds: f64,
}

#[derive(Clone, Debug)]
pub enum FeedEvent {
    Event(Value),
    Diagnostic(String),
    Error(String),
    Eof,
}

pub struct Feed {
    child: Child,
    receiver: Receiver<FeedEvent>,
    exit_reported: bool,
}

impl Feed {
    pub fn spawn(config: &FeedConfig) -> Result<Self> {
        if !config.python.is_file() {
            bail!(
                "Python executable does not exist: {}",
                config.python.display()
            );
        }
        if !config.cli.is_file() {
            bail!(
                "Claude Control CLI does not exist: {}",
                config.cli.display()
            );
        }
        let mut command = Command::new(&config.python);
        command.arg(&config.cli);
        if let Some(state_dir) = &config.state_dir {
            command.arg("--state-dir").arg(state_dir);
        }
        command
            .args([
                "monitor",
                "agui",
                "stream",
                "--after",
                &config.after.to_string(),
                "--limit",
                "256",
                "--poll-seconds",
                &config.poll_seconds.to_string(),
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if config.follow {
            command.arg("--follow");
        }
        let mut child = command.spawn().with_context(|| {
            format!(
                "could not start {} monitor agui stream",
                config.cli.display()
            )
        })?;
        let stdout = child
            .stdout
            .take()
            .context("stream process has no stdout")?;
        let stderr = child
            .stderr
            .take()
            .context("stream process has no stderr")?;
        let (sender, receiver) = mpsc::channel();
        spawn_stdout(stdout, sender.clone());
        spawn_stderr(stderr, sender);
        Ok(Self {
            child,
            receiver,
            exit_reported: false,
        })
    }

    pub fn drain(&mut self) -> Vec<FeedEvent> {
        let mut events = Vec::new();
        while let Ok(event) = self.receiver.try_recv() {
            events.push(event);
        }
        if !self.exit_reported
            && let Ok(Some(status)) = self.child.try_wait()
        {
            self.exit_reported = true;
            if !status.success() {
                events.push(FeedEvent::Error(format!(
                    "event stream exited with {status}"
                )));
            }
        }
        events
    }
}

impl Drop for Feed {
    fn drop(&mut self) {
        if self.child.try_wait().ok().flatten().is_none() {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}

pub fn read_jsonl_file(path: &Path) -> Result<Vec<Value>> {
    let file = File::open(path).with_context(|| format!("could not open {}", path.display()))?;
    read_jsonl(BufReader::new(file))
}

pub fn read_jsonl<R: BufRead>(reader: R) -> Result<Vec<Value>> {
    reader
        .lines()
        .enumerate()
        .filter_map(|(index, line)| match line {
            Ok(line) if line.trim().is_empty() => None,
            Ok(line) => Some(
                serde_json::from_str(&line)
                    .with_context(|| format!("invalid JSON event on line {}", index + 1)),
            ),
            Err(error) => Some(Err(error).context("could not read JSON event stream")),
        })
        .collect()
}

fn spawn_stdout(stdout: impl Read + Send + 'static, sender: Sender<FeedEvent>) {
    thread::spawn(move || {
        for (index, line) in BufReader::new(stdout).lines().enumerate() {
            let line = match line {
                Ok(line) => line,
                Err(error) => {
                    let _ = sender.send(FeedEvent::Error(format!(
                        "event stream read failed: {error}"
                    )));
                    return;
                }
            };
            if line.trim().is_empty() {
                continue;
            }
            match serde_json::from_str(&line) {
                Ok(value) => {
                    if sender.send(FeedEvent::Event(value)).is_err() {
                        return;
                    }
                }
                Err(error) => {
                    let _ = sender.send(FeedEvent::Error(format!(
                        "invalid event JSON on line {}: {error}",
                        index + 1
                    )));
                    return;
                }
            }
        }
        let _ = sender.send(FeedEvent::Eof);
    });
}

fn spawn_stderr(stderr: impl Read + Send + 'static, sender: Sender<FeedEvent>) {
    thread::spawn(move || {
        for line in BufReader::new(stderr).lines() {
            match line {
                Ok(line) if !line.trim().is_empty() => {
                    if sender.send(FeedEvent::Diagnostic(line)).is_err() {
                        return;
                    }
                }
                Ok(_) => {}
                Err(error) => {
                    let _ = sender.send(FeedEvent::Error(format!(
                        "event stream stderr failed: {error}"
                    )));
                    return;
                }
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn jsonl_reader_ignores_blank_lines_and_preserves_order() {
        let values = read_jsonl(BufReader::new(
            b"{\"cursor\":1}\n\n{\"cursor\":2}\n".as_slice(),
        ))
        .unwrap();
        assert_eq!(values.len(), 2);
        assert_eq!(values[1]["cursor"], 2);
    }

    #[test]
    fn jsonl_reader_reports_the_bad_line() {
        let error = read_jsonl(BufReader::new(b"{}\nnot-json\n".as_slice())).unwrap_err();
        assert!(error.to_string().contains("line 2"));
    }
}
