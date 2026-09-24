use std::cmp::min;
use std::collections::BTreeMap;
use std::io;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use crossterm::cursor::{Hide, Show};
use crossterm::event::{
    self, DisableMouseCapture, EnableMouseCapture, Event, KeyCode, KeyEvent, KeyEventKind,
    MouseButton, MouseEvent, MouseEventKind,
};
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
use crate::graph::{Card, Point, Scene, project_with_positions};
use crate::interaction::{
    ScreenPoint, WorldBounds, WorldPoint, cursor_centered_zoom, drag_pan_delta,
    minimap_point_to_camera_center, point_in_rect, timeline_column_to_index, world_to_screen,
};
use crate::model::{GraphState, Timeline};

const FRAME_TIME: Duration = Duration::from_millis(50);
const PLAY_TIME: Duration = Duration::from_millis(180);
const MIN_ZOOM: f32 = 0.08;
const MAX_ZOOM: f32 = 2.0;
const INSPECTOR_WIDTH: u16 = 44;

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

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Playback {
    Live,
    Paused,
    Playing,
}

impl Playback {
    fn label(self) -> &'static str {
        match self {
            Self::Live => "LIVE",
            Self::Paused => "PAUSED",
            Self::Playing => "PLAYING",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum HitTarget {
    Card(usize),
    Canvas,
    Timeline,
    Minimap,
    Inspector,
    Play,
    Live,
}

#[derive(Clone, Copy, Debug)]
struct PressState {
    target: HitTarget,
    origin: ScreenPoint,
    last: ScreenPoint,
    dragging: bool,
}

#[derive(Clone, Debug, Default)]
struct UiRegions {
    canvas: Rect,
    timeline: Rect,
    timeline_track: Rect,
    inspector: Option<Rect>,
    minimap: Option<Rect>,
    play: Option<Rect>,
    live: Option<Rect>,
    cards: Vec<(usize, Rect)>,
}

impl Default for Camera {
    fn default() -> Self {
        Self {
            x: -2.0,
            y: -2.0,
            zoom: 1.0,
            mode: CameraMode::Follow,
        }
    }
}

pub struct App {
    timeline: Timeline,
    feed: Option<Feed>,
    state: GraphState,
    scene: Scene,
    positions: BTreeMap<String, Point>,
    index: usize,
    stream_open: bool,
    playback: Playback,
    selected: usize,
    camera: Camera,
    show_help: bool,
    show_info: bool,
    show_detail: bool,
    quit: bool,
    status: String,
    error: Option<String>,
    last_play: Instant,
    started: Instant,
    regions: UiRegions,
    press: Option<PressState>,
    hovered: Option<usize>,
    last_card_click: Option<(usize, Instant)>,
    inspector_scroll: u16,
    mouse_capture: bool,
}

impl App {
    pub fn new(timeline: Timeline, feed: Option<Feed>) -> Result<Self> {
        let index = timeline.latest_index();
        let state = timeline.state_at(index)?;
        let mut positions = BTreeMap::new();
        let scene = project_with_positions(&state, &mut positions);
        let stream_open = feed.is_some();
        Ok(Self {
            timeline,
            feed,
            state,
            scene,
            positions,
            index,
            stream_open,
            playback: if stream_open {
                Playback::Live
            } else {
                Playback::Paused
            },
            selected: 0,
            camera: Camera::default(),
            show_help: false,
            show_info: false,
            show_detail: false,
            quit: false,
            status: "connecting to Claude Control event ledger".to_owned(),
            error: None,
            last_play: Instant::now(),
            started: Instant::now(),
            regions: UiRegions::default(),
            press: None,
            hovered: None,
            last_card_click: None,
            inspector_scroll: 0,
            mouse_capture: true,
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
                self.scene = project_with_positions(&self.state, &mut self.positions);
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
                            if self.playback == Playback::Live {
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
                        if self.playback == Playback::Live {
                            self.playback = Playback::Paused;
                        }
                        self.error = Some(message);
                    }
                    FeedEvent::Eof => {
                        self.stream_open = false;
                        if self.playback == Playback::Live {
                            self.playback = Playback::Paused;
                        }
                        self.status = "event stream ended; replay remains available".to_owned();
                    }
                }
            }
        }
        if self.playback == Playback::Playing && self.last_play.elapsed() >= PLAY_TIME {
            if self.index < self.timeline.latest_index() {
                self.index += 1;
                self.refresh_projection();
            } else {
                self.playback = if self.stream_open {
                    Playback::Live
                } else {
                    Playback::Paused
                };
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
        self.playback = Playback::Paused;
        self.refresh_projection();
    }

    fn seek_to(&mut self, index: usize) {
        if self.timeline.is_empty() {
            return;
        }
        self.index = index.min(self.timeline.latest_index());
        self.playback = Playback::Paused;
        self.refresh_projection();
    }

    fn go_live(&mut self) {
        self.index = self.timeline.latest_index();
        self.playback = if self.stream_open {
            Playback::Live
        } else {
            Playback::Paused
        };
        self.refresh_projection();
    }

    fn toggle_play(&mut self) {
        self.playback = match self.playback {
            Playback::Playing => Playback::Paused,
            Playback::Live | Playback::Paused => Playback::Playing,
        };
        self.last_play = Instant::now();
    }

    fn handle_key(&mut self, key: KeyEvent) {
        if key.kind != KeyEventKind::Press {
            return;
        }
        if self.show_help || self.show_info {
            match key.code {
                KeyCode::Esc | KeyCode::Char('q') | KeyCode::Enter => {
                    self.show_help = false;
                    self.show_info = false;
                }
                _ => {}
            }
            return;
        }
        if self.show_detail && matches!(key.code, KeyCode::Esc | KeyCode::Char('v')) {
            self.show_detail = false;
            return;
        }
        match key.code {
            KeyCode::Char('q') | KeyCode::Esc => self.quit = true,
            KeyCode::Char('?') => self.show_help = true,
            KeyCode::Char('i') => self.show_info = true,
            KeyCode::Char('v') | KeyCode::Enter => self.show_detail = true,
            KeyCode::Char(' ') => self.toggle_play(),
            KeyCode::Left => self.seek(-1),
            KeyCode::Right => self.seek(1),
            KeyCode::Char('[') => self.seek(-10),
            KeyCode::Char(']') => self.seek(10),
            KeyCode::Home => {
                self.index = 0;
                self.playback = Playback::Paused;
                self.refresh_projection();
            }
            KeyCode::End | KeyCode::Char('g') => self.go_live(),
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
            KeyCode::Char('c') => self.center_selected(),
            KeyCode::Char('0') => self.camera.mode = CameraMode::Overview,
            KeyCode::Char('x') => self.mouse_capture = !self.mouse_capture,
            KeyCode::Char('+') | KeyCode::Char('=') => {
                self.camera.mode = CameraMode::Manual;
                self.camera.zoom = (self.camera.zoom + 0.1).min(MAX_ZOOM);
            }
            KeyCode::Char('-') => {
                self.camera.mode = CameraMode::Manual;
                self.camera.zoom = (self.camera.zoom - 0.1).max(MIN_ZOOM);
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

    fn center_selected(&mut self) {
        let Some(card) = self.scene.cards.get(self.selected) else {
            return;
        };
        self.camera.mode = CameraMode::Manual;
        self.camera.zoom = self.camera.zoom.max(0.85);
        self.camera.x = card.position.x as f32 + card.width as f32 / 2.0
            - self.regions.canvas.width as f32 / self.camera.zoom / 2.0;
        self.camera.y = card.position.y as f32 + card.height as f32 / 2.0
            - self.regions.canvas.height as f32 / self.camera.zoom / 2.0;
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
                    .clamp(MIN_ZOOM, 1.25);
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

    fn handle_mouse(&mut self, mouse: MouseEvent) {
        let point = ScreenPoint::new(f32::from(mouse.column), f32::from(mouse.row));
        match mouse.kind {
            MouseEventKind::Moved => {
                self.hovered = self.card_at(point);
            }
            MouseEventKind::Down(MouseButton::Left) => {
                let target = self.target_at(point);
                self.press = Some(PressState {
                    target,
                    origin: point,
                    last: point,
                    dragging: matches!(target, HitTarget::Timeline | HitTarget::Minimap),
                });
                if target == HitTarget::Timeline {
                    self.scrub(point);
                } else if target == HitTarget::Minimap {
                    self.recenter_from_minimap(point);
                }
            }
            MouseEventKind::Drag(MouseButton::Left) => {
                if let Some(mut press) = self.press {
                    if (point.x - press.origin.x).abs() >= 2.0
                        || (point.y - press.origin.y).abs() >= 1.0
                    {
                        press.dragging = true;
                    }
                    match press.target {
                        HitTarget::Timeline => self.scrub(point),
                        HitTarget::Minimap => self.recenter_from_minimap(point),
                        HitTarget::Canvas | HitTarget::Card(_) if press.dragging => {
                            if let Some(delta) = drag_pan_delta(press.last, point, self.camera.zoom)
                            {
                                self.camera.mode = CameraMode::Manual;
                                self.camera.x += delta.x;
                                self.camera.y += delta.y;
                            }
                        }
                        _ => {}
                    }
                    press.last = point;
                    self.press = Some(press);
                }
            }
            MouseEventKind::Up(MouseButton::Left) => {
                if let Some(press) = self.press.take()
                    && !press.dragging
                {
                    self.activate(press.target);
                }
            }
            MouseEventKind::Down(MouseButton::Right) => {
                self.show_detail = false;
            }
            MouseEventKind::ScrollUp => self.scroll(point, -1),
            MouseEventKind::ScrollDown => self.scroll(point, 1),
            _ => {}
        }
    }

    fn target_at(&self, point: ScreenPoint) -> HitTarget {
        if self
            .regions
            .inspector
            .is_some_and(|area| point_in_rect(point, area))
        {
            return HitTarget::Inspector;
        }
        if self
            .regions
            .play
            .is_some_and(|area| point_in_rect(point, area))
        {
            return HitTarget::Play;
        }
        if self
            .regions
            .live
            .is_some_and(|area| point_in_rect(point, area))
        {
            return HitTarget::Live;
        }
        if self.regions.timeline.width > 0 && point_in_rect(point, self.regions.timeline) {
            return HitTarget::Timeline;
        }
        if self
            .regions
            .minimap
            .is_some_and(|area| point_in_rect(point, area))
        {
            return HitTarget::Minimap;
        }
        if let Some(index) = self.card_at(point) {
            return HitTarget::Card(index);
        }
        HitTarget::Canvas
    }

    fn card_at(&self, point: ScreenPoint) -> Option<usize> {
        self.regions
            .cards
            .iter()
            .rev()
            .find_map(|(index, area)| point_in_rect(point, *area).then_some(*index))
    }

    fn activate(&mut self, target: HitTarget) {
        match target {
            HitTarget::Card(index) => {
                let double = self.last_card_click.is_some_and(|(last, at)| {
                    last == index && at.elapsed() <= Duration::from_millis(320)
                });
                self.selected = index;
                self.show_detail = true;
                self.inspector_scroll = 0;
                self.last_card_click = Some((index, Instant::now()));
                if double {
                    self.camera.mode = CameraMode::Follow;
                    self.camera.zoom = self.camera.zoom.max(0.85);
                }
            }
            HitTarget::Canvas => self.show_detail = false,
            HitTarget::Timeline => {}
            HitTarget::Minimap => {}
            HitTarget::Inspector => {}
            HitTarget::Play => self.toggle_play(),
            HitTarget::Live => self.go_live(),
        }
    }

    fn scrub(&mut self, point: ScreenPoint) {
        if let Some(index) = timeline_column_to_index(
            self.regions.timeline_track,
            point.x as u16,
            self.timeline.len(),
        ) {
            self.seek_to(index);
        }
    }

    fn scroll(&mut self, point: ScreenPoint, direction: isize) {
        if self
            .regions
            .inspector
            .is_some_and(|area| point_in_rect(point, area))
        {
            self.inspector_scroll = if direction < 0 {
                self.inspector_scroll.saturating_sub(2)
            } else {
                self.inspector_scroll.saturating_add(2)
            };
            return;
        }
        if point_in_rect(point, self.regions.timeline) {
            self.seek(direction * 10);
            return;
        }
        if point_in_rect(point, self.regions.canvas) {
            let next = if direction < 0 {
                (self.camera.zoom * 1.18).min(MAX_ZOOM)
            } else {
                (self.camera.zoom / 1.18).max(MIN_ZOOM)
            };
            if let Some(origin) = cursor_centered_zoom(
                self.regions.canvas,
                WorldPoint::new(self.camera.x, self.camera.y),
                self.camera.zoom,
                next,
                point,
            ) {
                self.camera.mode = CameraMode::Manual;
                self.camera.x = origin.x;
                self.camera.y = origin.y;
                self.camera.zoom = next;
            }
        }
    }

    fn recenter_from_minimap(&mut self, point: ScreenPoint) {
        let Some(map) = self.regions.minimap else {
            return;
        };
        let bounds = WorldBounds::new(
            WorldPoint::new(
                self.scene.bounds.min_x as f32,
                self.scene.bounds.min_y as f32,
            ),
            WorldPoint::new(
                self.scene.bounds.max_x as f32,
                self.scene.bounds.max_y as f32,
            ),
        );
        if let Some(center) = minimap_point_to_camera_center(map, point, bounds) {
            self.camera.mode = CameraMode::Manual;
            self.camera.x = center.x - self.regions.canvas.width as f32 / self.camera.zoom / 2.0;
            self.camera.y = center.y - self.regions.canvas.height as f32 / self.camera.zoom / 2.0;
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
        if event::poll(FRAME_TIME)? {
            match event::read()? {
                Event::Key(key) => {
                    let mouse_before = app.mouse_capture;
                    app.handle_key(key);
                    if mouse_before != app.mouse_capture {
                        if app.mouse_capture {
                            execute!(terminal.backend_mut(), EnableMouseCapture)?;
                        } else {
                            execute!(terminal.backend_mut(), DisableMouseCapture)?;
                        }
                    }
                }
                Event::Mouse(mouse) if app.mouse_capture => app.handle_mouse(mouse),
                Event::Resize(_, _) => app.camera.mode = CameraMode::Overview,
                _ => {}
            }
        }
    }
    Ok(())
}

struct ScreenGuard;

impl ScreenGuard {
    fn enter() -> Result<Self> {
        enable_raw_mode().context("could not enable terminal raw mode")?;
        let mut stdout = io::stdout();
        if let Err(error) = execute!(stdout, EnterAlternateScreen, EnableMouseCapture, Hide) {
            let _ = disable_raw_mode();
            return Err(error).context("could not enter alternate screen");
        }
        Ok(Self)
    }
}

impl Drop for ScreenGuard {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = execute!(
            io::stdout(),
            DisableMouseCapture,
            LeaveAlternateScreen,
            Show
        );
    }
}

fn draw(frame: &mut ratatui::Frame<'_>, app: &mut App) {
    if frame.area().width < 60 || frame.area().height < 18 {
        frame.render_widget(
            Paragraph::new("terminal too small · need at least 60×18")
                .alignment(Alignment::Center)
                .style(Style::default().fg(Color::Yellow)),
            frame.area(),
        );
        app.regions = UiRegions::default();
        return;
    }
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Min(10),
            Constraint::Length(if frame.area().height >= 32 { 5 } else { 4 }),
            Constraint::Length(1),
        ])
        .split(frame.area());
    let graph_parts = if app.show_detail && chunks[0].width >= 112 {
        Layout::default()
            .direction(Direction::Horizontal)
            .constraints([Constraint::Min(50), Constraint::Length(INSPECTOR_WIDTH)])
            .split(chunks[0])
    } else {
        Layout::default()
            .direction(Direction::Horizontal)
            .constraints([Constraint::Percentage(100), Constraint::Length(0)])
            .split(chunks[0])
    };
    app.regions.canvas = graph_parts[0];
    app.regions.timeline = chunks[1];
    app.regions.cards.clear();
    app.regions.inspector = (graph_parts[1].width > 0).then_some(graph_parts[1]);
    app.update_camera(graph_parts[0]);
    draw_graph(frame, graph_parts[0], app);
    if graph_parts[1].width > 0 {
        draw_inspector(frame, graph_parts[1], app);
    }
    draw_timeline(frame, chunks[1], app);
    draw_footer(frame, chunks[2], app);
    if app.show_help {
        draw_help(frame);
    } else if app.show_info {
        draw_info(frame, app);
    } else if app.show_detail && graph_parts[1].width == 0 {
        draw_detail(frame, app);
    }
}

fn draw_graph(frame: &mut ratatui::Frame<'_>, area: Rect, app: &mut App) {
    let buffer = frame.buffer_mut();
    fill(
        buffer,
        area,
        " ",
        Style::default().bg(Color::Rgb(12, 14, 13)),
    );
    if app.camera.zoom >= 0.22 {
        for y in (area.y..area.bottom()).step_by(4) {
            for x in (area.x..area.right()).step_by(8) {
                set(
                    buffer,
                    area,
                    i32::from(x),
                    i32::from(y),
                    "·",
                    Style::default().fg(Color::Rgb(43, 47, 44)),
                );
            }
        }
    }

    let selected_key = app
        .scene
        .cards
        .get(app.selected)
        .map(|card| card.key.as_str());
    if app.camera.zoom >= 0.32 {
        for link in &app.scene.links {
            let from = app.scene.cards.iter().find(|card| card.key == link.from);
            let to = app.scene.cards.iter().find(|card| card.key == link.to);
            if let (Some(from), Some(to)) = (from, to) {
                draw_link(
                    buffer,
                    area,
                    app.camera,
                    from,
                    to,
                    selected_key,
                    app.started.elapsed(),
                );
            }
        }
    }
    let mut card_regions = Vec::new();
    for (index, card) in app.scene.cards.iter().enumerate() {
        if let Some(rect) = draw_card(
            buffer,
            area,
            app.camera,
            card,
            index == app.selected,
            app.hovered == Some(index),
        ) {
            card_regions.push((index, rect));
        }
    }
    app.regions.cards = card_regions;

    let breadcrumb = app
        .scene
        .cards
        .get(app.selected)
        .map(|card| format!(" CLAUDE CONTROL  ›  {}  ›  {} ", card.kind, card.title))
        .unwrap_or_else(|| " CLAUDE CONTROL ".to_owned());
    set_text(
        buffer,
        area,
        i32::from(area.x) + 1,
        i32::from(area.y),
        i32::from(area.width).saturating_sub(2),
        &breadcrumb,
        Style::default()
            .fg(Color::Rgb(215, 177, 48))
            .bg(Color::Rgb(12, 14, 13))
            .add_modifier(Modifier::BOLD),
    );
    let newer = app.timeline.latest_index().saturating_sub(app.index);
    if newer > 0 {
        let label = format!(" +{newer} newer ");
        set_text(
            buffer,
            area,
            i32::from(area.right()) - label.len() as i32 - 1,
            i32::from(area.y),
            label.len() as i32,
            &label,
            Style::default()
                .fg(Color::Rgb(11, 15, 12))
                .bg(Color::Rgb(112, 190, 101))
                .add_modifier(Modifier::BOLD),
        );
    }
    draw_minimap(buffer, area, app);
}

fn draw_link(
    buffer: &mut Buffer,
    area: Rect,
    camera: Camera,
    from: &Card,
    to: &Card,
    selected: Option<&str>,
    elapsed: Duration,
) {
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
    let endpoint_visible = |point: (i32, i32)| {
        point.0 >= i32::from(area.x).saturating_sub(2)
            && point.0 < i32::from(area.right()).saturating_add(2)
            && point.1 >= i32::from(area.y).saturating_sub(1)
            && point.1 < i32::from(area.bottom()).saturating_add(1)
    };
    if !endpoint_visible(start) || !endpoint_visible(end) {
        return;
    }
    let mid = (start.0 + end.0) / 2;
    let selected_edge = selected.is_some_and(|key| key == from.key || key == to.key);
    let active_edge = active_state(&from.state) || active_state(&to.state);
    let color = if selected_edge {
        Color::Rgb(223, 181, 52)
    } else if active_edge {
        Color::Rgb(88, 158, 91)
    } else {
        Color::Rgb(54, 62, 57)
    };
    let style = Style::default().fg(color);
    line_h(buffer, area, start.0, mid, start.1, "─", style);
    line_v(buffer, area, start.1, end.1, mid, "│", style);
    line_h(buffer, area, mid, end.0, end.1, "─", style);
    set(buffer, area, mid, start.1, "┐", style);
    set(buffer, area, mid, end.1, "└", style);
    set(buffer, area, end.0, end.1, "▶", style);
    if active_edge && (start.0 - mid).abs() > 2 {
        let length = (mid - start.0).unsigned_abs().max(1) as u128;
        let step = (elapsed.as_millis() / 140) % length;
        let direction = if mid >= start.0 { 1 } else { -1 };
        let x = start.0 + direction * step as i32;
        set(
            buffer,
            area,
            x,
            start.1,
            "◆",
            Style::default().fg(Color::Rgb(120, 207, 111)),
        );
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum CardLod {
    Dot,
    Compact,
    Full,
}

fn draw_card(
    buffer: &mut Buffer,
    area: Rect,
    camera: Camera,
    card: &Card,
    selected: bool,
    hovered: bool,
) -> Option<Rect> {
    let (x, y) = screen_point(area, camera, card.position.x as f32, card.position.y as f32);
    let lod = if camera.zoom < 0.28 {
        CardLod::Dot
    } else if camera.zoom < 0.78 {
        CardLod::Compact
    } else {
        CardLod::Full
    };
    let (width, height) = match lod {
        CardLod::Dot => (3, 1),
        CardLod::Compact => (24, 4),
        CardLod::Full => (34, 7),
    };
    let visible = clipped_rect(area, x, y, width, height)?;
    if lod == CardLod::Dot {
        let glyph = if selected { " ◈ " } else { " ◆ " };
        set_text(
            buffer,
            area,
            x,
            y,
            width,
            glyph,
            Style::default().fg(if selected {
                Color::Rgb(229, 186, 48)
            } else {
                state_color(&card.state)
            }),
        );
        return Some(visible);
    }

    let surface = if hovered {
        Color::Rgb(34, 38, 34)
    } else {
        Color::Rgb(25, 28, 25)
    };
    draw_shadow(buffer, area, x, y, width, height);
    fill_rect(buffer, area, x, y, width, height, surface);
    box_border_variant(buffer, area, x, y, width, height, selected, surface);
    set_text(
        buffer,
        area,
        x + 2,
        y + 1,
        width - 4,
        &format!("{}  {}", status_mark(&card.state), card.title),
        Style::default()
            .fg(if selected {
                Color::Rgb(238, 198, 64)
            } else {
                Color::Rgb(224, 226, 219)
            })
            .bg(surface)
            .add_modifier(Modifier::BOLD),
    );
    set_text(
        buffer,
        area,
        x + 2,
        y + 2,
        width - 4,
        &format!(
            "{}  {}",
            card.role.as_deref().unwrap_or(&card.kind),
            card.model.as_deref().unwrap_or("")
        ),
        Style::default().fg(Color::Rgb(112, 183, 190)).bg(surface),
    );
    set_text(
        buffer,
        area,
        x + 2,
        y + 3,
        width - 4,
        &format!("{} · {}", card.state, card.activity),
        Style::default().fg(state_color(&card.state)).bg(surface),
    );
    if lod == CardLod::Full {
        set_text(
            buffer,
            area,
            x + 2,
            y + 4,
            width - 4,
            &format!(
                "{}  ·  {} tokens",
                short_key(&card.key),
                compact_number(card.output_tokens)
            ),
            Style::default().fg(Color::Rgb(143, 146, 137)).bg(surface),
        );
        set_text(
            buffer,
            area,
            x + 2,
            y + 5,
            width - 4,
            "click inspect · drag · dbl follow",
            Style::default().fg(Color::Rgb(91, 96, 89)).bg(surface),
        );
    }
    Some(visible)
}

fn draw_minimap(buffer: &mut Buffer, area: Rect, app: &mut App) {
    app.regions.minimap = None;
    if area.width < 80 || area.height < 20 || app.scene.cards.len() < 8 {
        return;
    }
    let width = if area.width >= 120 { 24 } else { 19 };
    let height = if area.height >= 28 { 9 } else { 7 };
    let x = i32::from(area.right()) - width - 2;
    let y = i32::from(area.y) + 2;
    fill_rect(buffer, area, x, y, width, height, Color::Rgb(20, 22, 20));
    box_border_variant(
        buffer,
        area,
        x,
        y,
        width,
        height,
        false,
        Color::Rgb(20, 22, 20),
    );
    set_text(
        buffer,
        area,
        x + 2,
        y,
        width - 4,
        " overview ",
        Style::default()
            .fg(Color::Rgb(130, 132, 125))
            .bg(Color::Rgb(20, 22, 20)),
    );
    let inner = Rect::new(
        (x + 1) as u16,
        (y + 1) as u16,
        (width - 2) as u16,
        (height - 2) as u16,
    );
    app.regions.minimap = Some(inner);
    let min_x = app.scene.bounds.min_x as f32;
    let min_y = app.scene.bounds.min_y as f32;
    let span_x = (app.scene.bounds.max_x - app.scene.bounds.min_x).max(1) as f32;
    let span_y = (app.scene.bounds.max_y - app.scene.bounds.min_y).max(1) as f32;
    let map_x = |world: f32| {
        f32::from(inner.x)
            + ((world - min_x) / span_x).clamp(0.0, 1.0) * f32::from(inner.width.saturating_sub(1))
    };
    let map_y = |world: f32| {
        f32::from(inner.y)
            + ((world - min_y) / span_y).clamp(0.0, 1.0) * f32::from(inner.height.saturating_sub(1))
    };
    for (index, card) in app.scene.cards.iter().enumerate() {
        set(
            buffer,
            inner,
            map_x(card.position.x as f32).round() as i32,
            map_y(card.position.y as f32).round() as i32,
            if index == app.selected { "◆" } else { "·" },
            Style::default()
                .fg(if index == app.selected {
                    Color::Rgb(232, 188, 48)
                } else {
                    kind_color(&card.kind)
                })
                .bg(Color::Rgb(20, 22, 20)),
        );
    }
    let view_left = map_x(app.camera.x).round() as i32;
    let view_top = map_y(app.camera.y).round() as i32;
    let view_right = map_x(app.camera.x + f32::from(area.width) / app.camera.zoom).round() as i32;
    let view_bottom = map_y(app.camera.y + f32::from(area.height) / app.camera.zoom).round() as i32;
    let view_style = Style::default().fg(Color::Rgb(178, 181, 171));
    line_h(
        buffer, inner, view_left, view_right, view_top, "─", view_style,
    );
    line_h(
        buffer,
        inner,
        view_left,
        view_right,
        view_bottom,
        "─",
        view_style,
    );
    line_v(
        buffer,
        inner,
        view_top,
        view_bottom,
        view_left,
        "│",
        view_style,
    );
    line_v(
        buffer,
        inner,
        view_top,
        view_bottom,
        view_right,
        "│",
        view_style,
    );
}

fn draw_timeline(frame: &mut ratatui::Frame<'_>, area: Rect, app: &mut App) {
    let block = Block::default()
        .title(format!(
            " REPLAY  event {}/{}  cursor {}  {} ",
            if app.timeline.is_empty() {
                0
            } else {
                app.index + 1
            },
            app.timeline.len(),
            app.timeline
                .event(app.index)
                .map(|event| event.cursor)
                .unwrap_or(0),
            app.playback.label()
        ))
        .borders(Borders::ALL)
        .border_style(Style::default().fg(Color::Rgb(75, 78, 72)))
        .style(Style::default().bg(Color::Rgb(18, 20, 18)));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    app.regions.timeline_track = Rect::default();
    if app.timeline.is_empty() || inner.width == 0 {
        frame.render_widget(
            Paragraph::new("waiting for events")
                .alignment(Alignment::Center)
                .style(Style::default().fg(Color::DarkGray)),
            inner,
        );
        return;
    }
    let label = app
        .timeline
        .event(app.index)
        .map(|event| event.summary.as_str())
        .unwrap_or("event");
    let summary = Rect::new(inner.x, inner.y, inner.width, 1);
    let track = Rect::new(inner.x, inner.y.saturating_add(1), inner.width, 1);
    let pointer =
        (inner.height >= 3).then(|| Rect::new(inner.x, inner.y.saturating_add(2), inner.width, 1));
    app.regions.timeline_track = track;
    frame.render_widget(
        Paragraph::new(format!(" {}", label)).style(Style::default().fg(Color::Rgb(184, 187, 178))),
        summary,
    );
    let buffer = frame.buffer_mut();
    let columns = usize::from(track.width.max(1));
    let mut counts = vec![0_usize; columns];
    for event_index in 0..app.timeline.len() {
        let column = if app.timeline.len() <= 1 {
            0
        } else {
            event_index * columns.saturating_sub(1) / app.timeline.len().saturating_sub(1)
        };
        counts[column] += 1;
    }
    let max_count = counts.iter().copied().max().unwrap_or(1).max(1);
    let histogram = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"];
    for (column, count) in counts.into_iter().enumerate() {
        let event_index = column * app.timeline.len() / columns;
        let level = ((count.saturating_sub(1) * (histogram.len() - 1)) / max_count).min(7);
        let color = if event_index <= app.index {
            Color::Rgb(161, 139, 53)
        } else {
            Color::Rgb(62, 67, 61)
        };
        set(
            buffer,
            track,
            i32::from(track.x) + column as i32,
            i32::from(track.y),
            if count == 0 { " " } else { histogram[level] },
            Style::default().fg(color).bg(Color::Rgb(18, 20, 18)),
        );
    }
    let current_x = track.x
        + ((app.index as u64 * track.width.saturating_sub(1) as u64)
            / app.timeline.len().saturating_sub(1).max(1) as u64) as u16;
    set(
        buffer,
        track,
        i32::from(current_x),
        i32::from(track.y),
        "●",
        Style::default().fg(if app.index == app.timeline.latest_index() {
            Color::Rgb(102, 205, 94)
        } else {
            Color::Rgb(245, 193, 37)
        }),
    );
    if app.index != app.timeline.latest_index() {
        set(
            buffer,
            track,
            i32::from(track.right().saturating_sub(1)),
            i32::from(track.y),
            "│",
            Style::default().fg(Color::Rgb(102, 190, 96)),
        );
    }
    if let Some(pointer) = pointer {
        let timestamp = app
            .timeline
            .event(app.index)
            .map(|event| format_clock(event.timestamp))
            .unwrap_or_default();
        set_text(
            buffer,
            pointer,
            i32::from(pointer.x),
            i32::from(pointer.y),
            i32::from(pointer.width.saturating_sub(2)),
            &timestamp,
            Style::default().fg(Color::Rgb(133, 135, 128)),
        );
        set(
            buffer,
            pointer,
            i32::from(current_x),
            i32::from(pointer.y),
            "▲",
            Style::default().fg(Color::Rgb(235, 184, 39)),
        );
    }
}

fn draw_footer(frame: &mut ratatui::Frame<'_>, area: Rect, app: &mut App) {
    let buffer = frame.buffer_mut();
    fill(
        buffer,
        area,
        " ",
        Style::default().bg(Color::Rgb(17, 19, 17)),
    );
    app.regions.play = None;
    app.regions.live = None;
    let mut x = area.x;
    let brand = " CCC VIEWER ";
    set_text(
        buffer,
        area,
        i32::from(x),
        i32::from(area.y),
        brand.len() as i32,
        brand,
        Style::default()
            .fg(Color::Rgb(20, 18, 8))
            .bg(Color::Rgb(230, 184, 41))
            .add_modifier(Modifier::BOLD),
    );
    x = x.saturating_add(brand.len() as u16 + 1);
    let play = if app.playback == Playback::Playing {
        " Ⅱ PAUSE "
    } else {
        " ▶ PLAY "
    };
    app.regions.play = Some(Rect::new(x, area.y, play.len() as u16, 1));
    set_text(
        buffer,
        area,
        i32::from(x),
        i32::from(area.y),
        play.len() as i32,
        play,
        Style::default()
            .fg(Color::Rgb(224, 181, 45))
            .bg(Color::Rgb(35, 35, 29))
            .add_modifier(Modifier::BOLD),
    );
    x = x.saturating_add(play.len() as u16 + 1);
    let live = " ● LIVE ";
    app.regions.live = Some(Rect::new(x, area.y, live.len() as u16, 1));
    set_text(
        buffer,
        area,
        i32::from(x),
        i32::from(area.y),
        live.len() as i32,
        live,
        Style::default()
            .fg(if app.playback == Playback::Live {
                Color::Rgb(128, 219, 112)
            } else {
                Color::Rgb(93, 99, 91)
            })
            .bg(Color::Rgb(28, 31, 27)),
    );
    x = x.saturating_add(live.len() as u16 + 1);
    let agents = app
        .scene
        .cards
        .iter()
        .filter(|card| card.kind == "session")
        .count();
    let tokens = app
        .scene
        .cards
        .iter()
        .filter(|card| card.kind == "session")
        .map(|card| card.output_tokens)
        .sum::<u64>();
    let state = format!(
        "{} agents · {} tokens · {} {:.0}%",
        agents,
        compact_number(tokens),
        app.camera.mode.label(),
        app.camera.zoom * 100.0
    );
    let right = " ? help · q quit ";
    let usable = area
        .right()
        .saturating_sub(x)
        .saturating_sub(right.len() as u16) as usize;
    let message = app.error.as_deref().unwrap_or(&state);
    set_text(
        buffer,
        area,
        i32::from(x),
        i32::from(area.y),
        usable as i32,
        &truncate(message, usable),
        Style::default()
            .fg(if app.error.is_some() {
                Color::LightRed
            } else {
                Color::Rgb(133, 137, 129)
            })
            .bg(Color::Rgb(17, 19, 17)),
    );
    set_text(
        buffer,
        area,
        i32::from(area.right()) - right.len() as i32,
        i32::from(area.y),
        right.len() as i32,
        right,
        Style::default()
            .fg(Color::Rgb(152, 154, 146))
            .bg(Color::Rgb(17, 19, 17)),
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
        Line::raw("Mouse"),
        Line::raw("Click card   select + inspect       Double-click follow"),
        Line::raw("Drag canvas  pan graph              Wheel zoom at pointer"),
        Line::raw("Drag replay  scrub history          Click/drag minimap move"),
        Line::raw("Click PLAY/LIVE transport           Right-click close inspector"),
        Line::raw(""),
        Line::raw("Keyboard"),
        Line::raw("Left/Right  replay one event     [/]  replay ten events"),
        Line::raw("Home/End    first/live event      Space play or pause"),
        Line::raw("Tab/Up/Down select agent          Enter/V details"),
        Line::raw("O/0 overview  F follow   C center   WASD/HJKL pan"),
        Line::raw("+/- zoom      X mouse capture      I info   Q quit"),
        Line::raw(""),
        Line::raw(
            "The viewer is read-only. It never accepts work, edits tasks, or invokes models.",
        ),
    ]);
    overlay(frame, 78, 22, " controls ", text);
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

fn draw_inspector(frame: &mut ratatui::Frame<'_>, area: Rect, app: &App) {
    let Some(card) = app.scene.cards.get(app.selected) else {
        return;
    };
    let block = Block::default()
        .title(format!(" {} ", card.title))
        .borders(Borders::ALL)
        .border_style(Style::default().fg(Color::Rgb(218, 178, 49)))
        .style(Style::default().bg(Color::Rgb(20, 22, 20)));
    frame.render_widget(
        Paragraph::new(card_detail_text(app, card))
            .block(block)
            .scroll((app.inspector_scroll, 0))
            .wrap(Wrap { trim: false }),
        area,
    );
}

fn draw_detail(frame: &mut ratatui::Frame<'_>, app: &App) {
    let Some(card) = app.scene.cards.get(app.selected) else {
        return;
    };
    overlay(
        frame,
        70,
        18,
        &format!(" {} ", card.title),
        card_detail_text(app, card),
    );
}

fn card_detail_text(app: &App, card: &Card) -> Text<'static> {
    let inbound = app
        .scene
        .links
        .iter()
        .filter(|link| link.to == card.key)
        .count();
    let outbound = app
        .scene
        .links
        .iter()
        .filter(|link| link.from == card.key)
        .count();
    Text::from(vec![
        Line::from(vec![
            Span::styled(
                format!("{} ", status_mark(&card.state)),
                Style::default().fg(state_color(&card.state)),
            ),
            Span::styled(
                card.state.to_uppercase(),
                Style::default()
                    .fg(state_color(&card.state))
                    .add_modifier(Modifier::BOLD),
            ),
        ]),
        Line::raw(""),
        Line::raw(format!(
            "Role        {}",
            card.role.as_deref().unwrap_or("—")
        )),
        Line::raw(format!(
            "Model       {}",
            card.model.as_deref().unwrap_or("—")
        )),
        Line::raw(format!("Type        {}", card.kind)),
        Line::raw(format!("Activity    {}", card.activity)),
        Line::raw(format!(
            "Tokens      {}",
            compact_number(card.output_tokens)
        )),
        Line::raw(format!("Connections {inbound} in · {outbound} out")),
        Line::raw(""),
        Line::styled(
            "IDENTITY",
            Style::default()
                .fg(Color::Rgb(215, 177, 48))
                .add_modifier(Modifier::BOLD),
        ),
        Line::raw(card.key.clone()),
        Line::raw(""),
        Line::styled(
            "VIEWER",
            Style::default()
                .fg(Color::Rgb(215, 177, 48))
                .add_modifier(Modifier::BOLD),
        ),
        Line::raw(format!(
            "Event {}/{} · {}",
            app.index.saturating_add(1),
            app.timeline.len(),
            app.playback.label()
        )),
        Line::raw(format!(
            "Camera {} · {:.0}%",
            app.camera.mode.label(),
            app.camera.zoom * 100.0
        )),
        Line::raw(""),
        Line::styled(
            "Read-only semantic metadata. Prompt, reasoning, and tool bodies are never rendered.",
            Style::default().fg(Color::Rgb(112, 116, 108)),
        ),
    ])
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
    world_to_screen(
        area,
        WorldPoint::new(camera.x, camera.y),
        camera.zoom,
        WorldPoint::new(x, y),
    )
    .map(|point| (point.x.round() as i32, point.y.round() as i32))
    .unwrap_or((i32::MIN, i32::MIN))
}

fn clipped_rect(area: Rect, x: i32, y: i32, width: i32, height: i32) -> Option<Rect> {
    let left = x.max(i32::from(area.x));
    let top = y.max(i32::from(area.y));
    let right = (x + width).min(i32::from(area.right()));
    let bottom = (y + height).min(i32::from(area.bottom()));
    (left < right && top < bottom).then_some(Rect::new(
        left as u16,
        top as u16,
        (right - left) as u16,
        (bottom - top) as u16,
    ))
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
    result.push('…');
    result
}

fn format_clock(timestamp: f64) -> String {
    if !timestamp.is_finite() || timestamp < 0.0 {
        return "--:--:--".to_owned();
    }
    let seconds = timestamp as u64 % 86_400;
    format!(
        "{:02}:{:02}:{:02}",
        seconds / 3_600,
        (seconds % 3_600) / 60,
        seconds % 60
    )
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

fn fill_rect(
    buffer: &mut Buffer,
    area: Rect,
    x: i32,
    y: i32,
    width: i32,
    height: i32,
    color: Color,
) {
    let style = Style::default().bg(color);
    for row in y..y + height {
        for column in x..x + width {
            set(buffer, area, column, row, " ", style);
        }
    }
}

fn draw_shadow(buffer: &mut Buffer, area: Rect, x: i32, y: i32, width: i32, height: i32) {
    let style = Style::default().bg(Color::Rgb(6, 7, 6));
    line_h(buffer, area, x + 2, x + width, y + height, " ", style);
    line_v(buffer, area, y + 1, y + height, x + width, " ", style);
}

#[allow(clippy::too_many_arguments)]
fn box_border_variant(
    buffer: &mut Buffer,
    area: Rect,
    x: i32,
    y: i32,
    width: i32,
    height: i32,
    selected: bool,
    surface: Color,
) {
    let color = if selected {
        Color::Rgb(229, 184, 43)
    } else {
        Color::Rgb(72, 76, 70)
    };
    let style = Style::default().fg(color).bg(surface);
    let (horizontal, vertical, top_left, top_right, bottom_left, bottom_right) = if selected {
        ("═", "║", "╔", "╗", "╚", "╝")
    } else {
        ("─", "│", "╭", "╮", "╰", "╯")
    };
    line_h(buffer, area, x + 1, x + width - 2, y, horizontal, style);
    line_h(
        buffer,
        area,
        x + 1,
        x + width - 2,
        y + height - 1,
        horizontal,
        style,
    );
    line_v(buffer, area, y + 1, y + height - 2, x, vertical, style);
    line_v(
        buffer,
        area,
        y + 1,
        y + height - 2,
        x + width - 1,
        vertical,
        style,
    );
    set(buffer, area, x, y, top_left, style);
    set(buffer, area, x + width - 1, y, top_right, style);
    set(buffer, area, x, y + height - 1, bottom_left, style);
    set(
        buffer,
        area,
        x + width - 1,
        y + height - 1,
        bottom_right,
        style,
    );
}

fn compact_number(value: u64) -> String {
    if value >= 1_000_000 {
        format!("{:.1}m", value as f64 / 1_000_000.0)
    } else if value >= 1_000 {
        format!("{:.1}k", value as f64 / 1_000.0)
    } else {
        value.to_string()
    }
}

fn short_key(key: &str) -> String {
    let tail = key.rsplit(':').next().unwrap_or(key);
    truncate(tail, 12)
}

fn active_state(state: &str) -> bool {
    matches!(
        state,
        "pending" | "claimed" | "launching" | "running" | "stopping" | "active"
    )
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
        "running" | "active" | "claimed" | "launching" => "◐",
        "completed" | "accepted" => "✓",
        "failed" | "unknown" | "cancelled" | "blocked" => "×",
        "pending" | "queued" | "awaiting_codex" | "awaiting_leader" => "◆",
        _ => "○",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::KeyModifiers;
    use ratatui::backend::TestBackend;
    use serde_json::{Value, json};

    fn observation(cursor: u64, kind: &str, entity_kind: &str, id: &str, payload: Value) -> Value {
        json!({
            "type": "CUSTOM",
            "name": "claude_control_observation",
            "value": {
                "cursor": cursor,
                "kind": kind,
                "entity_kind": entity_kind,
                "entity_id": id,
                "effective_at": cursor as f64,
                "payload": payload
            },
            "timestamp": cursor as f64,
            "metadata": {"claude-control": {"cursor": cursor}}
        })
    }

    fn sample_app(agent_count: usize) -> App {
        let mut timeline = Timeline::default();
        timeline
            .push(observation(
                1,
                "baseline",
                "store",
                "baseline",
                json!({"fidelity": "exact", "nodes": {}, "edges": {}, "telemetry": []}),
            ))
            .unwrap();
        for index in 0..agent_count {
            let cursor = index as u64 + 2;
            timeline
                .push(observation(
                    cursor,
                    "node_created",
                    "session",
                    &format!("session-{index}"),
                    json!({
                        "id": format!("session-{index}"),
                        "name": format!("agent-{index}"),
                        "state": if index == 0 { "running" } else { "idle" },
                        "role": "editor",
                        "model": "claude-opus-5"
                    }),
                ))
                .unwrap();
        }
        App::new(timeline, None).unwrap()
    }

    fn mouse(kind: MouseEventKind, column: u16, row: u16) -> MouseEvent {
        MouseEvent {
            kind,
            column,
            row,
            modifiers: KeyModifiers::NONE,
        }
    }

    #[test]
    fn renderer_populates_click_regions_at_supported_sizes() {
        for (width, height) in [(160, 45), (100, 30), (60, 18)] {
            let mut app = sample_app(10);
            let backend = TestBackend::new(width, height);
            let mut terminal = Terminal::new(backend).unwrap();
            terminal.draw(|frame| draw(frame, &mut app)).unwrap();
            assert!(app.regions.canvas.width > 0);
            assert!(app.regions.timeline_track.width > 0);
            assert!(!app.regions.cards.is_empty());
            if width >= 80 && height >= 20 {
                assert!(app.regions.minimap.is_some());
            }
        }
    }

    #[test]
    fn card_click_and_canvas_drag_have_distinct_results() {
        let mut app = sample_app(2);
        app.regions.canvas = Rect::new(0, 0, 100, 30);
        app.regions.cards = vec![(1, Rect::new(10, 5, 20, 5))];

        app.handle_mouse(mouse(MouseEventKind::Down(MouseButton::Left), 12, 6));
        app.handle_mouse(mouse(MouseEventKind::Up(MouseButton::Left), 12, 6));
        assert_eq!(app.selected, 1);
        assert!(app.show_detail);

        app.show_detail = false;
        app.regions.cards.clear();
        let before = (app.camera.x, app.camera.y);
        app.handle_mouse(mouse(MouseEventKind::Down(MouseButton::Left), 40, 15));
        app.handle_mouse(mouse(MouseEventKind::Drag(MouseButton::Left), 46, 18));
        app.handle_mouse(mouse(MouseEventKind::Up(MouseButton::Left), 46, 18));
        assert_ne!((app.camera.x, app.camera.y), before);
        assert!(!app.show_detail);
        assert_eq!(app.camera.mode, CameraMode::Manual);
    }

    #[test]
    fn timeline_press_scrubs_and_pauses() {
        let mut app = sample_app(10);
        app.regions.timeline = Rect::new(0, 30, 100, 4);
        app.regions.timeline_track = Rect::new(1, 32, 98, 1);
        app.playback = Playback::Playing;
        app.handle_mouse(mouse(MouseEventKind::Down(MouseButton::Left), 49, 32));
        assert!((4..=7).contains(&app.index));
        assert_eq!(app.playback, Playback::Paused);
    }
}
