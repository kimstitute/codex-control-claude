use std::cmp::min;
use std::io;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use crossterm::cursor::Show;
use crossterm::event::{self, Event, KeyCode, KeyEvent, KeyEventKind};
use crossterm::execute;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use ratatui::Terminal;
use ratatui::backend::CrosstermBackend;
use ratatui::buffer::Buffer;
use ratatui::layout::{Alignment, Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, Borders, Clear, Paragraph, Wrap};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

use crate::feed::{Feed, FeedEvent};
use crate::graph::{Card, Scene, project};
use crate::model::{GraphState, Timeline};

const FRAME_TIME: Duration = Duration::from_millis(50);
const PLAY_TIME: Duration = Duration::from_millis(180);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum CameraMode {
    Overview,
    Follow,
    Manual,
}

impl CameraMode {
    fn label(self) -> &'static str {
        match self {
            Self::Overview => "overview",
            Self::Follow => "follow",
            Self::Manual => "manual",
        }
    }
}

#[derive(Clone, Copy, Debug)]
struct Camera {
    x: f32,
    y: f32,
    zoom: f32,
    mode: CameraMode,
}

impl Default for Camera {
    fn default() -> Self {
        Self {
            x: -2.0,
            y: -2.0,
            zoom: 1.0,
            mode: CameraMode::Overview,
        }
    }
}

pub struct App {
    timeline: Timeline,
    feed: Option<Feed>,
    state: GraphState,
    scene: Scene,
    index: usize,
    live: bool,
    stream_open: bool,
    playing: bool,
    selected: usize,
    camera: Camera,
    show_help: bool,
    show_info: bool,
    show_detail: bool,
    quit: bool,
    status: String,
    error: Option<String>,
    last_play: Instant,
}

impl App {
    pub fn new(timeline: Timeline, feed: Option<Feed>) -> Result<Self> {
        let index = timeline.latest_index();
        let state = timeline.state_at(index)?;
        let scene = project(&state);
        let stream_open = feed.is_some();
        Ok(Self {
            timeline,
            feed,
            state,
            scene,
            index,
            live: stream_open,
            stream_open,
            playing: false,
            selected: 0,
            camera: Camera::default(),
            show_help: false,
            show_info: false,
            show_detail: false,
            quit: false,
            status: "connecting to Claude Control event ledger".to_owned(),
            error: None,
            last_play: Instant::now(),
        })
    }

    fn refresh_projection(&mut self) {
        match self.timeline.state_at(self.index) {
            Ok(state) => {
                let key = self
                    .scene
                    .cards
                    .get(self.selected)
                    .map(|card| card.key.clone());
                self.state = state;
                self.scene = project(&self.state);
                self.selected = key
                    .as_deref()
                    .and_then(|key| self.scene.index_of(key))
                    .unwrap_or(0)
                    .min(self.scene.cards.len().saturating_sub(1));
            }
            Err(error) => self.error = Some(error.to_string()),
        }
    }

    fn update(&mut self) {
        if let Some(feed) = &mut self.feed {
            for event in feed.drain() {
                match event {
                    FeedEvent::Event(value) => match self.timeline.push(value) {
                        Ok(true) => {
                            self.stream_open = true;
                            if self.live {
                                self.index = self.timeline.latest_index();
                                self.refresh_projection();
                            }
                            self.status = format!("live cursor {}", self.timeline.last_cursor());
                        }
                        Ok(false) => {}
                        Err(error) => self.error = Some(error.to_string()),
                    },
                    FeedEvent::Diagnostic(message) => self.status = message,
                    FeedEvent::Error(message) => {
                        self.stream_open = false;
                        self.live = false;
                        self.error = Some(message);
                    }
                    FeedEvent::Eof => {
                        self.stream_open = false;
                        self.live = false;
                        self.status = "event stream ended; replay remains available".to_owned();
                    }
                }
            }
        }
        if self.playing && self.last_play.elapsed() >= PLAY_TIME {
            if self.index < self.timeline.latest_index() {
                self.index += 1;
                self.live = self.index == self.timeline.latest_index();
                self.refresh_projection();
            } else {
                self.playing = false;
                self.live = self.stream_open;
            }
            self.last_play = Instant::now();
        }
    }

    fn seek(&mut self, delta: isize) {
        if self.timeline.is_empty() {
            return;
        }
        let latest = self.timeline.latest_index() as isize;
        self.index = (self.index as isize + delta).clamp(0, latest) as usize;
        self.live = self.index == self.timeline.latest_index() && self.stream_open;
        self.playing = false;
        self.refresh_projection();
    }

    fn handle_key(&mut self, key: KeyEvent) {
        if key.kind != KeyEventKind::Press {
            return;
        }
        if self.show_help || self.show_info || self.show_detail {
            match key.code {
                KeyCode::Esc | KeyCode::Char('q') | KeyCode::Enter => {
                    self.show_help = false;
                    self.show_info = false;
                    self.show_detail = false;
                }
                _ => {}
            }
            return;
        }
        match key.code {
            KeyCode::Char('q') | KeyCode::Esc => self.quit = true,
            KeyCode::Char('?') => self.show_help = true,
            KeyCode::Char('i') => self.show_info = true,
            KeyCode::Char('v') | KeyCode::Enter => self.show_detail = true,
            KeyCode::Char(' ') => {
                self.playing = !self.playing;
                self.live = false;
                self.last_play = Instant::now();
            }
            KeyCode::Left => self.seek(-1),
            KeyCode::Right => self.seek(1),
            KeyCode::Char('[') => self.seek(-10),
            KeyCode::Char(']') => self.seek(10),
            KeyCode::Home => {
                self.index = 0;
                self.live = false;
                self.playing = false;
                self.refresh_projection();
            }
            KeyCode::End => {
                self.index = self.timeline.latest_index();
                self.live = self.stream_open;
                self.playing = false;
                self.refresh_projection();
            }
            KeyCode::Tab | KeyCode::Down => {
                if !self.scene.cards.is_empty() {
                    self.selected = (self.selected + 1) % self.scene.cards.len();
                }
            }
            KeyCode::BackTab | KeyCode::Up => {
                if !self.scene.cards.is_empty() {
                    self.selected = self
                        .selected
                        .checked_sub(1)
                        .unwrap_or(self.scene.cards.len() - 1);
                }
            }
            KeyCode::Char('o') => self.camera.mode = CameraMode::Overview,
            KeyCode::Char('f') => {
                self.camera.mode = CameraMode::Follow;
                self.camera.zoom = self.camera.zoom.max(0.85);
            }
            KeyCode::Char('m') => self.camera.mode = CameraMode::Manual,
            KeyCode::Char('+') | KeyCode::Char('=') => {
                self.camera.mode = CameraMode::Manual;
                self.camera.zoom = (self.camera.zoom + 0.1).min(2.0);
            }
            KeyCode::Char('-') => {
                self.camera.mode = CameraMode::Manual;
                self.camera.zoom = (self.camera.zoom - 0.1).max(0.35);
            }
            KeyCode::Char('w') => self.pan(0.0, -3.0),
            KeyCode::Char('s') => self.pan(0.0, 3.0),
            KeyCode::Char('a') => self.pan(-5.0, 0.0),
            KeyCode::Char('d') => self.pan(5.0, 0.0),
            KeyCode::Char('l') => self.pan(5.0, 0.0),
            KeyCode::Char('h') => self.pan(-5.0, 0.0),
            KeyCode::Char('j') => self.pan(0.0, 3.0),
            KeyCode::Char('k') => self.pan(0.0, -3.0),
            _ => {}
        }
    }

    fn pan(&mut self, x: f32, y: f32) {
        self.camera.mode = CameraMode::Manual;
        self.camera.x += x / self.camera.zoom;
        self.camera.y += y / self.camera.zoom;
    }

    fn update_camera(&mut self, area: Rect) {
        match self.camera.mode {
            CameraMode::Overview => {
                let width = (self.scene.bounds.max_x - self.scene.bounds.min_x + 5).max(1) as f32;
                let height = (self.scene.bounds.max_y - self.scene.bounds.min_y + 5).max(1) as f32;
                self.camera.zoom = ((area.width as f32 / width).min(area.height as f32 / height))
                    .clamp(0.35, 1.25);
                self.camera.x = self.scene.bounds.min_x as f32 - 2.0;
                self.camera.y = self.scene.bounds.min_y as f32 - 2.0;
            }
            CameraMode::Follow => {
                if let Some(card) = self.scene.cards.get(self.selected) {
                    self.camera.x = card.position.x as f32 + card.width as f32 / 2.0
                        - area.width as f32 / self.camera.zoom / 2.0;
                    self.camera.y = card.position.y as f32 + card.height as f32 / 2.0
                        - area.height as f32 / self.camera.zoom / 2.0;
                }
            }
            CameraMode::Manual => {}
        }
    }
}

pub fn run(timeline: Timeline, feed: Option<Feed>) -> Result<()> {
    let _screen = ScreenGuard::enter()?;
    let stdout = io::stdout();
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend).context("could not initialize terminal")?;
    terminal.clear()?;
    let mut app = App::new(timeline, feed)?;

    while !app.quit {
        app.update();
        terminal.draw(|frame| draw(frame, &mut app))?;
        if event::poll(FRAME_TIME)?
            && let Event::Key(key) = event::read()?
        {
            app.handle_key(key);
        }
    }
    Ok(())
}

struct ScreenGuard;

impl ScreenGuard {
    fn enter() -> Result<Self> {
        enable_raw_mode().context("could not enable terminal raw mode")?;
        let mut stdout = io::stdout();
        if let Err(error) = execute!(stdout, EnterAlternateScreen) {
            let _ = disable_raw_mode();
            return Err(error).context("could not enter alternate screen");
        }
        Ok(Self)
    }
}

impl Drop for ScreenGuard {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = execute!(io::stdout(), LeaveAlternateScreen, Show);
    }
}

fn draw(frame: &mut ratatui::Frame<'_>, app: &mut App) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1),
            Constraint::Min(10),
            Constraint::Length(5),
            Constraint::Length(1),
        ])
        .split(frame.area());
    draw_header(frame, chunks[0], app);
    app.update_camera(chunks[1]);
    draw_graph(frame, chunks[1], app);
    draw_timeline(frame, chunks[2], app);
    draw_footer(frame, chunks[3], app);
    if app.show_help {
        draw_help(frame);
    } else if app.show_info {
        draw_info(frame, app);
    } else if app.show_detail {
        draw_detail(frame, app);
    }
}

fn draw_header(frame: &mut ratatui::Frame<'_>, area: Rect, app: &App) {
    let selected = app.scene.cards.get(app.selected);
    let title = Span::styled(
        " ccc viewer ",
        Style::default()
            .fg(Color::Black)
            .bg(Color::Yellow)
            .add_modifier(Modifier::BOLD),
    );
    let mode = if app.live {
        "LIVE"
    } else if app.playing {
        "PLAY"
    } else {
        "PAUSED"
    };
    let identity = selected
        .map(|card| format!("{} / {}", card.kind, card.title))
        .unwrap_or_else(|| "empty graph".to_owned());
    let line = Line::from(vec![
        title,
        Span::raw(format!(" {mode:<6} ")),
        Span::styled(identity, Style::default().fg(Color::White)),
        Span::raw(format!(
            "  camera:{} {:.0}%",
            app.camera.mode.label(),
            app.camera.zoom * 100.0
        )),
    ]);
    frame.render_widget(
        Paragraph::new(line).style(Style::default().bg(Color::Rgb(18, 18, 18))),
        area,
    );
}

fn draw_graph(frame: &mut ratatui::Frame<'_>, area: Rect, app: &App) {
    let buffer = frame.buffer_mut();
    fill(
        buffer,
        area,
        " ",
        Style::default().bg(Color::Rgb(13, 15, 16)),
    );
    for y in (area.y..area.bottom()).step_by(2) {
        for x in (area.x..area.right()).step_by(4) {
            set(
                buffer,
                area,
                x as i32,
                y as i32,
                ".",
                Style::default().fg(Color::Rgb(46, 51, 53)),
            );
        }
    }

    if app.camera.zoom >= 0.55 {
        for link in &app.scene.links {
            let from = app.scene.cards.iter().find(|card| card.key == link.from);
            let to = app.scene.cards.iter().find(|card| card.key == link.to);
            if let (Some(from), Some(to)) = (from, to) {
                draw_link(buffer, area, app.camera, from, to);
            }
        }
    }
    for (index, card) in app.scene.cards.iter().enumerate() {
        draw_card(buffer, area, app.camera, card, index == app.selected);
    }
    draw_minimap(buffer, area, app);
}

fn draw_link(buffer: &mut Buffer, area: Rect, camera: Camera, from: &Card, to: &Card) {
    let start = screen_point(
        area,
        camera,
        from.position.x as f32 + from.width as f32,
        from.position.y as f32 + from.height as f32 / 2.0,
    );
    let end = screen_point(
        area,
        camera,
        to.position.x as f32,
        to.position.y as f32 + to.height as f32 / 2.0,
    );
    let mid = (start.0 + end.0) / 2;
    let style = Style::default().fg(Color::Rgb(65, 78, 82));
    line_h(buffer, area, start.0, mid, start.1, "-", style);
    line_v(buffer, area, start.1, end.1, mid, "|", style);
    line_h(buffer, area, mid, end.0, end.1, "-", style);
    set(
        buffer,
        area,
        end.0,
        end.1,
        ">",
        Style::default().fg(Color::Rgb(108, 139, 145)),
    );
}

fn draw_card(buffer: &mut Buffer, area: Rect, camera: Camera, card: &Card, selected: bool) {
    let (x, y) = screen_point(area, camera, card.position.x as f32, card.position.y as f32);
    let width = (card.width as f32 * camera.zoom).round().clamp(10.0, 38.0) as i32;
    let height = (card.height as f32 * camera.zoom).round().clamp(3.0, 6.0) as i32;
    if x + width < area.x as i32
        || y + height < area.y as i32
        || x >= area.right() as i32
        || y >= area.bottom() as i32
    {
        return;
    }
    let color = kind_color(&card.kind);
    let border = if selected {
        Color::Yellow
    } else {
        Color::Rgb(68, 72, 74)
    };
    box_border(buffer, area, x, y, width, height, border);
    let title_style = Style::default().fg(color).add_modifier(Modifier::BOLD);
    set_text(
        buffer,
        area,
        x + 2,
        y,
        width - 4,
        &format!(" {} ", card.title),
        title_style,
    );
    set_text(
        buffer,
        area,
        x + 2,
        y + 1,
        width - 4,
        &format!("{}  {}", status_mark(&card.state), card.state),
        Style::default().fg(state_color(&card.state)),
    );
    if height < 4 {
        return;
    }
    let identity = match (&card.role, &card.model) {
        (Some(role), Some(model)) => format!("{role} / {model}"),
        (Some(role), None) => role.clone(),
        (None, Some(model)) => model.clone(),
        _ => card.kind.clone(),
    };
    set_text(
        buffer,
        area,
        x + 2,
        y + 2,
        width - 4,
        &identity,
        Style::default().fg(Color::Gray),
    );
    if height < 5 {
        return;
    }
    set_text(
        buffer,
        area,
        x + 2,
        y + 3,
        width - 4,
        &card.activity,
        Style::default().fg(Color::Rgb(183, 183, 183)),
    );
    if height < 6 {
        return;
    }
    set_text(
        buffer,
        area,
        x + 2,
        y + 4,
        width - 4,
        &format!("{} output tokens", card.output_tokens),
        Style::default().fg(Color::Rgb(214, 180, 71)),
    );
}

fn draw_minimap(buffer: &mut Buffer, area: Rect, app: &App) {
    if area.width < 55 || area.height < 16 {
        return;
    }
    let width = 20_i32;
    let height = 8_i32;
    let x = area.right() as i32 - width - 1;
    let y = area.y as i32 + 1;
    box_border(buffer, area, x, y, width, height, Color::Rgb(67, 70, 71));
    let span_x = (app.scene.bounds.max_x - app.scene.bounds.min_x + 1).max(1) as f32;
    let span_y = (app.scene.bounds.max_y - app.scene.bounds.min_y + 1).max(1) as f32;
    for card in &app.scene.cards {
        let px = x
            + 1
            + (((card.position.x - app.scene.bounds.min_x) as f32 / span_x) * (width - 3) as f32)
                as i32;
        let py = y
            + 1
            + (((card.position.y - app.scene.bounds.min_y) as f32 / span_y) * (height - 3) as f32)
                as i32;
        set(
            buffer,
            area,
            px,
            py,
            "#",
            Style::default().fg(kind_color(&card.kind)),
        );
    }
    set_text(
        buffer,
        area,
        x + 2,
        y,
        width - 4,
        " map ",
        Style::default().fg(Color::DarkGray),
    );
}

fn draw_timeline(frame: &mut ratatui::Frame<'_>, area: Rect, app: &App) {
    let block = Block::default()
        .title(format!(
            " replay  event {}/{}  cursor {} ",
            if app.timeline.is_empty() {
                0
            } else {
                app.index + 1
            },
            app.timeline.len(),
            app.timeline
                .event(app.index)
                .map(|event| event.cursor)
                .unwrap_or(0)
        ))
        .borders(Borders::ALL)
        .border_style(Style::default().fg(Color::Rgb(76, 80, 81)));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if app.timeline.is_empty() || inner.width == 0 {
        frame.render_widget(
            Paragraph::new("waiting for events").alignment(Alignment::Center),
            inner,
        );
        return;
    }
    let label = app
        .timeline
        .event(app.index)
        .map(|event| event.summary.as_str())
        .unwrap_or("event");
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1),
            Constraint::Length(1),
            Constraint::Length(1),
        ])
        .split(inner);
    frame.render_widget(
        Paragraph::new(label).style(Style::default().fg(Color::Rgb(190, 190, 190))),
        rows[0],
    );
    let buffer = frame.buffer_mut();
    for event_index in 0..app.timeline.len() {
        let x = rows[1].x
            + ((event_index as u64 * rows[1].width.saturating_sub(1) as u64)
                / app.timeline.len().saturating_sub(1).max(1) as u64) as u16;
        let symbol = app
            .timeline
            .event(event_index)
            .map(|event| timeline_mark(&event.summary))
            .unwrap_or("|");
        let color = if event_index <= app.index {
            Color::Rgb(142, 132, 74)
        } else {
            Color::Rgb(70, 73, 74)
        };
        set(
            buffer,
            rows[1],
            x as i32,
            rows[1].y as i32,
            symbol,
            Style::default().fg(color),
        );
    }
    let current_x = rows[2].x
        + ((app.index as u64 * rows[2].width.saturating_sub(1) as u64)
            / app.timeline.len().saturating_sub(1).max(1) as u64) as u16;
    set(
        buffer,
        rows[2],
        current_x as i32,
        rows[2].y as i32,
        "^",
        Style::default().fg(Color::Yellow),
    );
}

fn draw_footer(frame: &mut ratatui::Frame<'_>, area: Rect, app: &App) {
    let message = app.error.as_deref().unwrap_or(&app.status);
    let style = if app.error.is_some() {
        Style::default()
            .fg(Color::LightRed)
            .bg(Color::Rgb(18, 18, 18))
    } else {
        Style::default()
            .fg(Color::DarkGray)
            .bg(Color::Rgb(18, 18, 18))
    };
    let right = "  ? help  space play  arrows replay  tab agent  q quit ";
    let available = area.width.saturating_sub(right.len() as u16) as usize;
    let left = truncate(message, available);
    frame.render_widget(
        Paragraph::new(Line::from(vec![Span::raw(left), Span::raw(right)])).style(style),
        area,
    );
}

fn draw_help(frame: &mut ratatui::Frame<'_>) {
    let text = Text::from(vec![
        Line::from(Span::styled(
            "Viewer controls",
            Style::default()
                .fg(Color::Yellow)
                .add_modifier(Modifier::BOLD),
        )),
        Line::raw(""),
        Line::raw("Left/Right  replay one event     [/]  replay ten events"),
        Line::raw("Home/End    first/live event      Space play or pause"),
        Line::raw("Tab/Up/Down select agent          Enter/V details"),
        Line::raw("O overview   F follow selection   M manual camera"),
        Line::raw("WASD/HJKL pan                     +/- zoom"),
        Line::raw("I session information             Q quit"),
        Line::raw(""),
        Line::raw(
            "The viewer is read-only. It never accepts work, edits tasks, or invokes models.",
        ),
    ]);
    overlay(frame, 74, 15, " help ", text);
}

fn draw_info(frame: &mut ratatui::Frame<'_>, app: &App) {
    let event = app.timeline.event(app.index);
    let text = Text::from(vec![
        Line::raw(format!("fidelity       {}", app.state.fidelity)),
        Line::raw(format!("events         {}", app.timeline.len())),
        Line::raw(format!(
            "ledger cursor  {}",
            event.map(|event| event.cursor).unwrap_or(0)
        )),
        Line::raw(format!("duplicate rows {}", app.timeline.duplicates())),
        Line::raw(format!(
            "nodes / edges  {} / {}",
            app.state.nodes.len(),
            app.state.edges.len()
        )),
        Line::raw(format!(
            "camera         {} at {:.0}%",
            app.camera.mode.label(),
            app.camera.zoom * 100.0
        )),
        Line::raw(format!(
            "stream         {}",
            if app.stream_open {
                "attached"
            } else {
                "closed/offline"
            }
        )),
        Line::raw(""),
        Line::raw("AG-UI metadata is content-free and sourced from the durable local ledger."),
    ]);
    overlay(frame, 68, 14, " session info ", text);
}

fn draw_detail(frame: &mut ratatui::Frame<'_>, app: &App) {
    let Some(card) = app.scene.cards.get(app.selected) else {
        return;
    };
    let raw = app.state.nodes.get(&card.key).map(|node| &node.raw);
    let raw = raw
        .and_then(|raw| serde_json::to_string_pretty(raw).ok())
        .unwrap_or_else(|| "controller projection".to_owned());
    let mut lines = vec![
        Line::raw(format!("kind     {}", card.kind)),
        Line::raw(format!("state    {}", card.state)),
        Line::raw(format!("role     {}", card.role.as_deref().unwrap_or("-"))),
        Line::raw(format!("model    {}", card.model.as_deref().unwrap_or("-"))),
        Line::raw(format!("tokens   {}", card.output_tokens)),
        Line::raw(format!("activity {}", card.activity)),
        Line::raw(""),
    ];
    lines.extend(raw.lines().take(16).map(|line| Line::raw(line.to_owned())));
    overlay(
        frame,
        84,
        24,
        &format!(" {} ", card.title),
        Text::from(lines),
    );
}

fn overlay(frame: &mut ratatui::Frame<'_>, width: u16, height: u16, title: &str, text: Text<'_>) {
    let area = centered_rect(
        width.min(frame.area().width.saturating_sub(2)),
        height.min(frame.area().height.saturating_sub(2)),
        frame.area(),
    );
    frame.render_widget(Clear, area);
    let block = Block::default()
        .title(title)
        .borders(Borders::ALL)
        .border_style(Style::default().fg(Color::Yellow))
        .style(Style::default().bg(Color::Rgb(20, 21, 21)));
    frame.render_widget(
        Paragraph::new(text).block(block).wrap(Wrap { trim: false }),
        area,
    );
}

fn centered_rect(width: u16, height: u16, area: Rect) -> Rect {
    Rect {
        x: area.x + area.width.saturating_sub(width) / 2,
        y: area.y + area.height.saturating_sub(height) / 2,
        width,
        height,
    }
}

fn screen_point(area: Rect, camera: Camera, x: f32, y: f32) -> (i32, i32) {
    (
        area.x as i32 + ((x - camera.x) * camera.zoom).round() as i32,
        area.y as i32 + ((y - camera.y) * camera.zoom).round() as i32,
    )
}

fn fill(buffer: &mut Buffer, area: Rect, symbol: &str, style: Style) {
    for y in area.y..area.bottom() {
        for x in area.x..area.right() {
            buffer[(x, y)].set_symbol(symbol).set_style(style);
        }
    }
}

fn set(buffer: &mut Buffer, area: Rect, x: i32, y: i32, symbol: &str, style: Style) {
    if x >= area.x as i32
        && x < area.right() as i32
        && y >= area.y as i32
        && y < area.bottom() as i32
    {
        buffer[(x as u16, y as u16)]
            .set_symbol(symbol)
            .set_style(style);
    }
}

fn set_text(
    buffer: &mut Buffer,
    area: Rect,
    x: i32,
    y: i32,
    width: i32,
    value: &str,
    style: Style,
) {
    if width <= 0 || y < area.y as i32 || y >= area.bottom() as i32 {
        return;
    }
    let text = truncate(value, width as usize);
    if x >= area.x as i32 && x < area.right() as i32 {
        let available = (area.right() as i32 - x).min(width) as usize;
        buffer.set_stringn(x as u16, y as u16, text, available, style);
    }
}

fn truncate(value: &str, width: usize) -> String {
    if width == 0 {
        return String::new();
    }
    if UnicodeWidthStr::width(value) <= width {
        return value.to_owned();
    }
    let target = width.saturating_sub(1);
    let mut result = String::new();
    let mut used = 0;
    for character in value.chars() {
        let character_width = UnicodeWidthChar::width(character).unwrap_or(0);
        if used + character_width > target {
            break;
        }
        result.push(character);
        used += character_width;
    }
    result.push('~');
    result
}

fn line_h(
    buffer: &mut Buffer,
    area: Rect,
    start: i32,
    end: i32,
    y: i32,
    symbol: &str,
    style: Style,
) {
    for x in min(start, end)..=start.max(end) {
        set(buffer, area, x, y, symbol, style);
    }
}

fn line_v(
    buffer: &mut Buffer,
    area: Rect,
    start: i32,
    end: i32,
    x: i32,
    symbol: &str,
    style: Style,
) {
    for y in min(start, end)..=start.max(end) {
        set(buffer, area, x, y, symbol, style);
    }
}

fn box_border(
    buffer: &mut Buffer,
    area: Rect,
    x: i32,
    y: i32,
    width: i32,
    height: i32,
    color: Color,
) {
    let style = Style::default().fg(color).bg(Color::Rgb(27, 29, 29));
    for row in y..y + height {
        for column in x..x + width {
            set(buffer, area, column, row, " ", style);
        }
    }
    line_h(buffer, area, x + 1, x + width - 2, y, "-", style);
    line_h(
        buffer,
        area,
        x + 1,
        x + width - 2,
        y + height - 1,
        "-",
        style,
    );
    line_v(buffer, area, y + 1, y + height - 2, x, "|", style);
    line_v(
        buffer,
        area,
        y + 1,
        y + height - 2,
        x + width - 1,
        "|",
        style,
    );
    set(buffer, area, x, y, "+", style);
    set(buffer, area, x + width - 1, y, "+", style);
    set(buffer, area, x, y + height - 1, "+", style);
    set(buffer, area, x + width - 1, y + height - 1, "+", style);
}

fn kind_color(kind: &str) -> Color {
    match kind {
        "controller" => Color::Yellow,
        "composition" => Color::LightMagenta,
        "workflow" => Color::LightBlue,
        "workspace" => Color::LightCyan,
        "task" => Color::LightGreen,
        "session" => Color::Rgb(124, 184, 111),
        _ => Color::White,
    }
}

fn state_color(state: &str) -> Color {
    match state {
        "running" | "active" | "claimed" | "launching" => Color::LightGreen,
        "completed" | "accepted" | "idle" => Color::Rgb(121, 157, 112),
        "failed" | "unknown" | "cancelled" | "blocked" => Color::LightRed,
        "pending" | "queued" | "awaiting_codex" | "awaiting_leader" => Color::Yellow,
        _ => Color::Gray,
    }
}

fn status_mark(state: &str) -> &'static str {
    match state {
        "running" | "active" | "claimed" | "launching" => "*",
        "failed" | "unknown" | "cancelled" | "blocked" => "!",
        "pending" | "queued" | "awaiting_codex" | "awaiting_leader" => ">",
        _ => "o",
    }
}

fn timeline_mark(summary: &str) -> &'static str {
    if summary.starts_with("run ") {
        "#"
    } else if summary.starts_with("node") {
        "|"
    } else if summary.starts_with("edge") {
        ":"
    } else if summary.starts_with("baseline") {
        "B"
    } else {
        "."
    }
}
