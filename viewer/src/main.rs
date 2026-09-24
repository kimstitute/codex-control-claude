use std::env;
use std::path::PathBuf;
use std::process::Command;

use anyhow::{Context, Result, bail};
use claude_control_viewer::feed::{Feed, FeedConfig, read_jsonl, read_jsonl_file};
use claude_control_viewer::graph::project;
use claude_control_viewer::headless::{inspect_v1, render_tree};
use claude_control_viewer::model::{DetailStore, Timeline};
use claude_control_viewer::picker::{SessionChoice, choose};
use crossterm::style::force_color_output;
#[derive(Debug)]
struct Args {
    inspect: bool,
    tree: bool,
    python: Option<PathBuf>,
    cli: Option<PathBuf>,
    state_dir: Option<PathBuf>,
    stream_file: Option<PathBuf>,
    follow: bool,
    poll_seconds: f64,
    color: bool,
    local_detail: bool,
    detail_source: Option<String>,
    claude_root: Option<PathBuf>,
    codex_root: Option<PathBuf>,
    detail_current: bool,
    detail_dir: Option<PathBuf>,
    detail_file: Option<PathBuf>,
    detail_id: Option<String>,
}

fn main() {
    if let Err(error) = run() {
        eprintln!("ccc-viewer: {error:#}");
        std::process::exit(2);
    }
}

fn run() -> Result<()> {
    let mut args = parse_args()?;
    if args.local_detail && args.detail_source.is_none() {
        let sessions = catalog_sessions(&args)?;
        let candidates = filter_sessions(&args, &sessions)?;
        args.detail_source = if candidates.len() == 1 {
            Some(candidates[0].selector.clone())
        } else {
            choose(&candidates)?
        };
        if args.detail_source.is_none() {
            args.local_detail = false;
        }
    }
    if !args.inspect && !args.tree {
        // Claude/Codex host processes commonly export NO_COLOR for machine-readable
        // command output. This application is an interactive visual surface, so color
        // remains on unless the viewer-specific flag disables it explicitly.
        force_color_output(args.color);
    }
    let mut timeline = Timeline::default();
    let mut detail = DetailStore::default();
    if let Some(path) = &args.stream_file {
        for event in read_jsonl_file(path)? {
            ingest(&mut timeline, &mut detail, event)?;
        }
    }

    if args.inspect || args.tree {
        if args.stream_file.is_none() {
            for event in snapshot_events(&args)? {
                ingest(&mut timeline, &mut detail, event)?;
            }
        }
        let state = timeline.state_at(timeline.latest_index())?;
        let scene = project(&state);
        if args.tree {
            print!("{}", render_tree(&timeline, &state, &scene));
        } else {
            println!(
                "{}",
                serde_json::to_string_pretty(&inspect_v1(&timeline, &state, &scene))?
            );
        }
        return Ok(());
    }

    let feed = if args.stream_file.is_some() {
        None
    } else {
        Some(Feed::spawn(&feed_config(&args)?)?)
    };
    claude_control_viewer::ui::run(timeline, feed, detail)
}

fn parse_args() -> Result<Args> {
    let mut values = env::args().skip(1).peekable();
    if values
        .peek()
        .is_some_and(|value| value == "--help" || value == "-h")
    {
        print_help();
        std::process::exit(0);
    }
    if values
        .peek()
        .is_some_and(|value| value == "--version" || value == "-V")
    {
        println!("ccc-viewer {}", env!("CARGO_PKG_VERSION"));
        std::process::exit(0);
    }
    let mode = values.peek().map(String::as_str);
    let inspect = mode == Some("inspect");
    let tree = mode == Some("tree");
    if inspect || tree {
        values.next();
    }
    let mut args = Args {
        inspect,
        tree,
        python: None,
        cli: None,
        state_dir: None,
        stream_file: None,
        follow: !inspect && !tree,
        poll_seconds: 0.25,
        color: true,
        local_detail: false,
        detail_source: None,
        claude_root: None,
        codex_root: None,
        detail_current: false,
        detail_dir: None,
        detail_file: None,
        detail_id: None,
    };
    while let Some(value) = values.next() {
        match value.as_str() {
            "--python" => args.python = Some(PathBuf::from(next_value(&mut values, "--python")?)),
            "--cli" => args.cli = Some(PathBuf::from(next_value(&mut values, "--cli")?)),
            "--state-dir" => {
                args.state_dir = Some(PathBuf::from(next_value(&mut values, "--state-dir")?))
            }
            "--stream-file" => {
                args.stream_file = Some(PathBuf::from(next_value(&mut values, "--stream-file")?))
            }
            "--poll-seconds" => {
                args.poll_seconds = next_value(&mut values, "--poll-seconds")?.parse()?
            }
            "--no-follow" => args.follow = false,
            "--no-color" => args.color = false,
            "--local-detail" => args.local_detail = true,
            "--detail-source" => {
                args.local_detail = true;
                args.detail_source = Some(next_value(&mut values, "--detail-source")?);
            }
            "--claude-root" => {
                args.claude_root = Some(PathBuf::from(next_value(&mut values, "--claude-root")?))
            }
            "--codex-root" => {
                args.codex_root = Some(PathBuf::from(next_value(&mut values, "--codex-root")?))
            }
            "--detail-current" => {
                args.local_detail = true;
                args.detail_current = true;
            }
            "--detail-dir" => {
                args.local_detail = true;
                args.detail_dir = Some(PathBuf::from(next_value(&mut values, "--detail-dir")?));
            }
            "--detail-file" => {
                args.local_detail = true;
                args.detail_file = Some(PathBuf::from(next_value(&mut values, "--detail-file")?));
            }
            "--detail-id" => {
                args.local_detail = true;
                args.detail_id = Some(next_value(&mut values, "--detail-id")?);
            }
            unknown => bail!("unknown argument {unknown:?}; run ccc-viewer --help"),
        }
    }
    if !(0.05..=10.0).contains(&args.poll_seconds) {
        bail!("--poll-seconds must be between 0.05 and 10")
    }
    if args.stream_file.is_none() && (args.python.is_none() || args.cli.is_none()) {
        bail!("--python and --cli are required unless --stream-file is used")
    }
    if args.stream_file.is_some() && args.local_detail {
        bail!("--stream-file cannot be combined with local detail discovery")
    }
    let selector_count = usize::from(args.detail_source.is_some())
        + usize::from(args.detail_current)
        + usize::from(args.detail_dir.is_some())
        + usize::from(args.detail_file.is_some())
        + usize::from(args.detail_id.is_some());
    if selector_count > 1 {
        bail!("choose only one local detail selector")
    }
    if (args.inspect || args.tree) && args.local_detail {
        bail!("headless inspect/tree intentionally remain content-free")
    }
    Ok(args)
}

fn next_value(values: &mut impl Iterator<Item = String>, flag: &str) -> Result<String> {
    values
        .next()
        .with_context(|| format!("{flag} requires a value"))
}

fn feed_config(args: &Args) -> Result<FeedConfig> {
    Ok(FeedConfig {
        python: args.python.clone().context("--python is required")?,
        cli: args.cli.clone().context("--cli is required")?,
        state_dir: args.state_dir.clone(),
        after: 0,
        follow: args.follow,
        poll_seconds: args.poll_seconds,
        local_detail: args.local_detail,
        source: args.detail_source.clone(),
        claude_root: args.claude_root.clone(),
        codex_root: args.codex_root.clone(),
    })
}

fn snapshot_events(args: &Args) -> Result<Vec<serde_json::Value>> {
    let config = feed_config(args)?;
    let mut command = Command::new(&config.python);
    command.arg(&config.cli);
    if let Some(state_dir) = &config.state_dir {
        command.arg("--state-dir").arg(state_dir);
    }
    if config.local_detail {
        command.args(["monitor", "local", "stream"]);
        if let Some(source) = &config.source {
            command.arg("--source").arg(source);
        }
        if let Some(root) = &config.claude_root {
            command.arg("--claude-root").arg(root);
        }
        if let Some(root) = &config.codex_root {
            command.arg("--codex-root").arg(root);
        }
    } else {
        command.args([
            "monitor", "agui", "stream", "--after", "0", "--limit", "256",
        ]);
    }
    let output = command
        .output()
        .context("could not read Claude Control event ledger")?;
    if !output.status.success() {
        bail!(
            "Claude Control event stream failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    read_jsonl(output.stdout.as_slice())
}

fn ingest(
    timeline: &mut Timeline,
    detail: &mut DetailStore,
    value: serde_json::Value,
) -> Result<()> {
    if value.get("type").and_then(serde_json::Value::as_str)
        == Some("CLAUDE_CONTROL_DETAIL_SNAPSHOT")
    {
        detail.apply_snapshot(&value)
    } else if value.get("type").and_then(serde_json::Value::as_str)
        == Some("CLAUDE_CONTROL_DETAIL_RESET")
    {
        *timeline = Timeline::default();
        Ok(())
    } else {
        timeline.push(value).map(|_| ())
    }
}

fn catalog_sessions(args: &Args) -> Result<Vec<SessionChoice>> {
    let config = feed_config(args)?;
    let mut command = Command::new(&config.python);
    command.arg(&config.cli);
    if let Some(state_dir) = &config.state_dir {
        command.arg("--state-dir").arg(state_dir);
    }
    command.args(["monitor", "local", "sessions", "--source", "all"]);
    if let Some(root) = &config.claude_root {
        command.arg("--claude-root").arg(root);
    }
    if let Some(root) = &config.codex_root {
        command.arg("--codex-root").arg(root);
    }
    let output = command
        .output()
        .context("could not discover local sessions")?;
    if !output.status.success() {
        bail!(
            "local session discovery failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    let value: serde_json::Value = serde_json::from_slice(&output.stdout)
        .context("local session catalog was not valid JSON")?;
    if value.get("protocol").and_then(serde_json::Value::as_str)
        != Some("claude-control.local-sessions.v1")
    {
        bail!("local session catalog has an unsupported protocol")
    }
    Ok(value
        .get("sessions")
        .and_then(serde_json::Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|item| {
            Some(SessionChoice {
                selector: item.get("selector")?.as_str()?.to_owned(),
                provider: item.get("provider")?.as_str()?.to_owned(),
                session_id: item
                    .get("session_id")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or_default()
                    .to_owned(),
                title: item
                    .get("title")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or_default()
                    .to_owned(),
                project: item
                    .get("project")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or_default()
                    .to_owned(),
                file: item
                    .get("file")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or_default()
                    .to_owned(),
                live: item
                    .get("live")
                    .and_then(serde_json::Value::as_bool)
                    .unwrap_or(false),
                agents: item
                    .get("agents")
                    .and_then(serde_json::Value::as_u64)
                    .unwrap_or(0),
                events: item.get("events").and_then(serde_json::Value::as_u64),
            })
        })
        .collect())
}

fn filter_sessions(args: &Args, sessions: &[SessionChoice]) -> Result<Vec<SessionChoice>> {
    let current = if args.detail_current {
        Some(std::env::current_dir()?.canonicalize()?)
    } else {
        args.detail_dir
            .as_ref()
            .map(|path| path.canonicalize())
            .transpose()?
    };
    let file = args
        .detail_file
        .as_ref()
        .map(|path| path.canonicalize())
        .transpose()?;
    let filtered = sessions
        .iter()
        .filter(|session| {
            current.as_ref().is_none_or(|expected| {
                PathBuf::from(&session.project)
                    .canonicalize()
                    .is_ok_and(|actual| &actual == expected)
            }) && file.as_ref().is_none_or(|expected| {
                PathBuf::from(&session.file)
                    .canonicalize()
                    .is_ok_and(|actual| &actual == expected)
            }) && args
                .detail_id
                .as_ref()
                .is_none_or(|expected| &session.session_id == expected)
        })
        .cloned()
        .collect::<Vec<_>>();
    if filtered.is_empty() {
        bail!("no local session matched the requested selector")
    }
    Ok(filtered)
}

fn print_help() {
    println!(
        "ccc-viewer {}\n\n\
         Read-only real-time graph and replay viewer for Claude Control.\n\n\
         Usage:\n  ccc-viewer --python PATH --cli PATH [OPTIONS]\n  \
         ccc-viewer inspect --python PATH --cli PATH [OPTIONS]\n  \
         ccc-viewer tree --python PATH --cli PATH [OPTIONS]\n  \
         ccc-viewer [inspect|tree] --stream-file EVENTS.jsonl\n\n\
         Options:\n  --state-dir PATH      Claude Control state directory\n  \
         --no-follow          Stop after the current high-water mark\n  \
         --no-color           Disable the semantic TUI palette\n  \
         --poll-seconds N     Live polling interval (default: 0.25)\n  \
         --stream-file PATH   Replay a saved AG-UI JSONL stream\n  \
         --local-detail       Pick an opt-in local Claude/Codex/managed session\n  \
         --detail-source ID   Open one selector from `monitor local sessions`\n  \
         --claude-root PATH   Override the Claude transcript root\n  \
         --codex-root PATH    Override the Codex transcript root\n  \
         --detail-current     Open session(s) for the current directory\n  \
         --detail-dir PATH    Open session(s) whose recorded cwd is PATH\n  \
         --detail-file PATH   Open one exact transcript file\n  \
         --detail-id ID       Open session(s) with the provider session ID\n  \
         -h, --help           Show this help\n  -V, --version        Show version",
        env!("CARGO_PKG_VERSION")
    );
}
