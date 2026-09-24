use std::env;
use std::path::PathBuf;
use std::process::Command;

use anyhow::{Context, Result, bail};
use claude_control_viewer::feed::{Feed, FeedConfig, read_jsonl, read_jsonl_file};
use claude_control_viewer::graph::project;
use claude_control_viewer::headless::{inspect_v1, render_tree};
use claude_control_viewer::model::Timeline;
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
}

fn main() {
    if let Err(error) = run() {
        eprintln!("ccc-viewer: {error:#}");
        std::process::exit(2);
    }
}

fn run() -> Result<()> {
    let args = parse_args()?;
    if !args.inspect && !args.tree {
        // Claude/Codex host processes commonly export NO_COLOR for machine-readable
        // command output. This application is an interactive visual surface, so color
        // remains on unless the viewer-specific flag disables it explicitly.
        force_color_output(args.color);
    }
    let mut timeline = Timeline::default();
    if let Some(path) = &args.stream_file {
        for event in read_jsonl_file(path)? {
            timeline.push(event)?;
        }
    }

    if args.inspect || args.tree {
        if args.stream_file.is_none() {
            for event in snapshot_events(&args)? {
                timeline.push(event)?;
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
    claude_control_viewer::ui::run(timeline, feed)
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
            unknown => bail!("unknown argument {unknown:?}; run ccc-viewer --help"),
        }
    }
    if !(0.05..=10.0).contains(&args.poll_seconds) {
        bail!("--poll-seconds must be between 0.05 and 10")
    }
    if args.stream_file.is_none() && (args.python.is_none() || args.cli.is_none()) {
        bail!("--python and --cli are required unless --stream-file is used")
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
    })
}

fn snapshot_events(args: &Args) -> Result<Vec<serde_json::Value>> {
    let config = feed_config(args)?;
    let mut command = Command::new(&config.python);
    command.arg(&config.cli);
    if let Some(state_dir) = &config.state_dir {
        command.arg("--state-dir").arg(state_dir);
    }
    command.args([
        "monitor", "agui", "stream", "--after", "0", "--limit", "256",
    ]);
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
         -h, --help           Show this help\n  -V, --version        Show version",
        env!("CARGO_PKG_VERSION")
    );
}
