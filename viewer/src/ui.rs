use std::collections::{BTreeMap, BTreeSet};
use std::io;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use crossterm::cursor::{Hide, Show};
use crossterm::event::{
    self, DisableMouseCapture, EnableMouseCapture, Event, KeyCode, KeyEvent, KeyEventKind,
    KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use crossterm::execute;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use rataflow::{
    Background, BackgroundStyle, BackgroundVariant, EventResponse, FlowEvent, MiniMap,
    MiniMapPosition, MiniMapStyle,
};
use ratatui::Terminal;
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Alignment, Constraint, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Padding, Paragraph, Wrap};

use crate::feed::{Feed, FeedEvent};
use crate::flow_view::{ObserverFlow, compact_number, new_flow, relayout, sync as sync_flow};
use crate::graph::{Card, Link, Point, Scene, project_with_positions};
use crate::interaction::{ScreenPoint, point_in_rect, timeline_column_to_index};
use crate::model::{GraphState, Timeline};
use crate::theme::{
    BORDER, CANVAS, GOLD, GREEN, GRID, MUTED, RED, SUBTLE, SURFACE, SURFACE_RAISED, TEXT,
    state_color,
};

const FRAME_TIME: Duration = Duration::from_millis(32);
const PLAY_TIME: Duration = Duration::from_millis(180);
const PLAYBACK_SPEEDS: [f64; 6] = [0.25, 0.5, 1.0, 2.0, 4.0, 8.0];
const RECENT_NODE_LIMIT: usize = 18;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum CameraMode {
    Overview,
    Follow,
    Manual,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ScopeMode {
    Focus,
    Recent,
    All,
}

impl ScopeMode {
    fn label(self) -> &'static str {
        match self {
            Self::Focus => "focus",
            Self::Recent => "recent",
            Self::All => "all",
        }
    }

    fn next(self) -> Self {
        match self {
            Self::Focus => Self::Recent,
            Self::Recent => Self::All,
            Self::All => Self::Focus,
        }
    }
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

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Playback {
    Live,
    Paused,
    Playing,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum GapMode {
    Uniform,
    Compressed,
}

impl GapMode {
    fn label(self) -> &'static str {
        match self {
            Self::Uniform => "GAP OFF",
            Self::Compressed => "GAP ON",
        }
    }

    fn toggled(self) -> Self {
        match self {
            Self::Uniform => Self::Compressed,
            Self::Compressed => Self::Uniform,
        }
    }
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

#[derive(Clone, Debug, Default)]
struct UiRegions {
    canvas: Rect,
    timeline: Rect,
    timeline_track: Rect,
    inspector: Option<Rect>,
    play: Option<Rect>,
    live: Option<Rect>,
    scope: Option<Rect>,
    speed: Option<Rect>,
    gap: Option<Rect>,
    previous_era: Option<Rect>,
    next_era: Option<Rect>,
}

pub struct App {
    timeline: Timeline,
    feed: Option<Feed>,
    state: GraphState,
    scene: Scene,
    display_scene: Scene,
    positions: BTreeMap<String, Point>,
    flow: ObserverFlow,
    index: usize,
    stream_open: bool,
    playback: Playback,
    camera: CameraMode,
    scope: ScopeMode,
    speed_index: usize,
    gap_mode: GapMode,
    show_help: bool,
    show_info: bool,
    quit: bool,
    status: String,
    error: Option<String>,
    last_play: Instant,
    last_frame: Instant,
    regions: UiRegions,
    mouse_capture: bool,
    inspector_scroll: u16,
    inspector_content_height: u16,
    inspector_viewport_height: u16,
    inspector_follow_latest: bool,
    inspector_selected: Option<String>,
}

impl App {
    pub fn new(timeline: Timeline, feed: Option<Feed>) -> Result<Self> {
        let index = timeline.latest_index();
        let state = timeline.state_at(index)?;
        let mut positions = BTreeMap::new();
        let scene = project_with_positions(&state, &mut positions);
        let display_scene = scope_scene(&state, &scene, ScopeMode::Focus);
        let stream_open = feed.is_some();
        let mut flow = new_flow();
        sync_flow(&mut flow, &display_scene, true);
        flow.request_fit_view();
        Ok(Self {
            timeline,
            feed,
            state,
            scene,
            display_scene,
            positions,
            flow,
            index,
            stream_open,
            playback: if stream_open {
                Playback::Live
            } else {
                Playback::Paused
            },
            camera: CameraMode::Overview,
            scope: ScopeMode::Focus,
            speed_index: 2,
            gap_mode: GapMode::Uniform,
            show_help: false,
            show_info: false,
            quit: false,
            status: "observation ledger ready".to_owned(),
            error: None,
            last_play: Instant::now(),
            last_frame: Instant::now(),
            regions: UiRegions::default(),
            mouse_capture: true,
            inspector_scroll: 0,
            inspector_content_height: 0,
            inspector_viewport_height: 0,
            inspector_follow_latest: false,
            inspector_selected: None,
        })
    }

    fn selected_id(&self) -> Option<String> {
        self.flow.first_selected_node_id()
    }

    fn selected_card(&self) -> Option<&Card> {
        let id = self.selected_id()?;
        self.display_scene.cards.iter().find(|card| card.key == id)
    }

    fn refresh_projection(&mut self) {
        let selected = self.selected_id();
        match self.timeline.state_at(self.index) {
            Ok(state) => {
                self.state = state;
                self.scene = project_with_positions(&self.state, &mut self.positions);
                self.display_scene = scope_scene(&self.state, &self.scene, self.scope);
                let changed = sync_flow(
                    &mut self.flow,
                    &self.display_scene,
                    self.camera != CameraMode::Manual,
                );
                if let Some(id) = selected.filter(|id| self.display_scene.index_of(id).is_some()) {
                    self.flow.select_node(&id);
                }
                if changed && self.camera == CameraMode::Overview {
                    self.flow.request_fit_view();
                } else if self.camera == CameraMode::Follow {
                    self.follow_activity();
                }
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
                            self.status = format!("cursor {}", self.timeline.last_cursor());
                        }
                        Ok(false) => {}
                        Err(error) => self.error = Some(error.to_string()),
                    },
                    FeedEvent::Diagnostic(message) => self.status = message,
                    FeedEvent::Error(message) => {
                        self.stream_open = false;
                        self.playback = Playback::Paused;
                        self.error = Some(message);
                    }
                    FeedEvent::Eof => {
                        self.stream_open = false;
                        self.playback = Playback::Paused;
                        self.status = "stream ended · replay available".to_owned();
                    }
                }
            }
        }
        if self.playback == Playback::Playing {
            let mut advanced = 0;
            while self.index < self.timeline.latest_index()
                && self.last_play.elapsed() >= self.playback_delay()
                && advanced < 512
            {
                let delay = self.playback_delay();
                self.last_play = self
                    .last_play
                    .checked_add(delay)
                    .unwrap_or_else(Instant::now);
                self.index += 1;
                advanced += 1;
            }
            if advanced > 0 {
                self.refresh_projection();
            }
            if self.index >= self.timeline.latest_index() {
                self.playback = if self.stream_open {
                    Playback::Live
                } else {
                    Playback::Paused
                };
            }
        }
        let now = Instant::now();
        let elapsed = now.saturating_duration_since(self.last_frame);
        self.flow.tick_animation(elapsed);
        let _ = self.flow.tick_auto_pan(elapsed);
        self.last_frame = now;
    }

    fn seek(&mut self, delta: isize) {
        if self.timeline.is_empty() {
            return;
        }
        let latest = self.timeline.latest_index() as isize;
        self.seek_to((self.index as isize + delta).clamp(0, latest) as usize);
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

    fn playback_delay(&self) -> Duration {
        let speed = PLAYBACK_SPEEDS[self.speed_index];
        let millis = if self.gap_mode == GapMode::Uniform {
            PLAY_TIME.as_secs_f64() * 1_000.0
        } else {
            let current = self.timeline.event(self.index).map(|event| event.timestamp);
            let next = self
                .timeline
                .event(self.index.saturating_add(1))
                .map(|event| event.timestamp);
            match (current, next) {
                (Some(current), Some(next)) if current.is_finite() && next.is_finite() => {
                    ((next - current).max(0.0) * 1_000.0).clamp(16.0, 2_000.0)
                }
                _ => PLAY_TIME.as_secs_f64() * 1_000.0,
            }
        };
        Duration::from_secs_f64((millis / speed).max(1.0) / 1_000.0)
    }

    fn change_speed(&mut self, delta: isize) {
        self.speed_index = (self.speed_index as isize + delta)
            .clamp(0, PLAYBACK_SPEEDS.len() as isize - 1) as usize;
        self.last_play = Instant::now();
    }

    fn toggle_gap(&mut self) {
        self.gap_mode = self.gap_mode.toggled();
        self.last_play = Instant::now();
    }

    fn seek_era(&mut self, next: bool) {
        let target = if next {
            self.timeline.next_era(self.index)
        } else {
            self.timeline.previous_era(self.index)
        };
        self.seek_to(target);
    }

    fn scroll_inspector(&mut self, delta: isize) {
        let max = self
            .inspector_content_height
            .saturating_sub(self.inspector_viewport_height);
        self.inspector_scroll =
            (self.inspector_scroll as isize + delta).clamp(0, max as isize) as u16;
        self.inspector_follow_latest = self.inspector_scroll == 0;
    }

    fn follow_activity(&mut self) {
        let candidate = self
            .display_scene
            .cards
            .iter()
            .rev()
            .find(|card| active_state(&card.state))
            .or_else(|| self.scene.cards.first())
            .map(|card| card.key.clone());
        if let Some(id) = candidate {
            self.flow.select_node(&id);
            self.flow.center_on_selected();
        }
    }

    fn cycle_scope(&mut self) {
        self.scope = self.scope.next();
        self.display_scene = scope_scene(&self.state, &self.scene, self.scope);
        sync_flow(&mut self.flow, &self.display_scene, true);
        self.camera = CameraMode::Overview;
        self.flow.request_fit_view();
    }

    fn process_flow_response(&mut self, response: EventResponse) {
        for event in response.into_events() {
            match event {
                FlowEvent::ViewportChanged { .. }
                | FlowEvent::NodeDragged { .. }
                | FlowEvent::NodeDragEnded { .. } => self.camera = CameraMode::Manual,
                FlowEvent::NodeClicked { node_id } => self.flow.select_node(&node_id),
                FlowEvent::SelectionChanged { node_ids, .. } if !node_ids.is_empty() => {
                    self.flow.center_on_selected();
                }
                _ => {}
            }
        }
    }

    fn handle_key(&mut self, key: KeyEvent) {
        if key.kind == KeyEventKind::Release {
            return;
        }
        if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
            self.quit = true;
            return;
        }
        if self.show_help || self.show_info {
            if matches!(key.code, KeyCode::Esc | KeyCode::Char('q') | KeyCode::Enter) {
                self.show_help = false;
                self.show_info = false;
            }
            return;
        }
        match key.code {
            KeyCode::Char('q') => self.quit = true,
            KeyCode::Esc => self.flow.clear_selection(),
            KeyCode::Char('?') => self.show_help = true,
            KeyCode::Char('i') => self.show_info = true,
            KeyCode::Char(' ') => self.toggle_play(),
            KeyCode::Char('[') => self.seek(-1),
            KeyCode::Char(']') => self.seek(1),
            KeyCode::Char('{') => self.seek_era(false),
            KeyCode::Char('}') => self.seek_era(true),
            KeyCode::Char(',') => self.change_speed(-1),
            KeyCode::Char('.') => self.change_speed(1),
            KeyCode::Char('z') | KeyCode::Char('Z') => self.toggle_gap(),
            KeyCode::PageUp if self.selected_id().is_some() => {
                self.scroll_inspector(-(self.inspector_viewport_height.max(1) as isize));
            }
            KeyCode::PageDown if self.selected_id().is_some() => {
                self.scroll_inspector(self.inspector_viewport_height.max(1) as isize);
            }
            KeyCode::Home => self.seek_to(0),
            KeyCode::End | KeyCode::Char('g') | KeyCode::Char('G') => self.go_live(),
            KeyCode::Char('o') | KeyCode::Char('O') => {
                self.camera = CameraMode::Overview;
                self.flow.clear_selection();
                self.flow.request_fit_view();
            }
            KeyCode::Char('f') | KeyCode::Char('F') => {
                self.camera = CameraMode::Follow;
                self.follow_activity();
            }
            KeyCode::Char('r') | KeyCode::Char('R') => {
                relayout(&mut self.flow);
                self.camera = CameraMode::Overview;
                self.flow.request_fit_view();
            }
            KeyCode::Char('a') | KeyCode::Char('A') => {
                self.cycle_scope();
            }
            KeyCode::Char('c') => self.flow.center_on_selected(),
            KeyCode::Char('x') => self.mouse_capture = !self.mouse_capture,
            KeyCode::Char('+')
            | KeyCode::Char('=')
            | KeyCode::Char('-')
            | KeyCode::Char('_')
            | KeyCode::Char('0') => {
                self.camera = CameraMode::Manual;
                let response = self.flow.handle_controls_key_event(key);
                self.process_flow_response(response);
            }
            KeyCode::Tab
            | KeyCode::BackTab
            | KeyCode::Up
            | KeyCode::Down
            | KeyCode::Left
            | KeyCode::Right
            | KeyCode::Char('h')
            | KeyCode::Char('j')
            | KeyCode::Char('k')
            | KeyCode::Char('l') => {
                self.camera = CameraMode::Manual;
                let response = self.flow.handle_key_event(key);
                self.process_flow_response(response);
            }
            KeyCode::Enter => {
                if self.selected_id().is_none()
                    && let Some(card) = self.display_scene.cards.first()
                {
                    self.flow.select_node(&card.key);
                    self.flow.center_on_selected();
                }
            }
            _ => {}
        }
    }

    fn handle_mouse(&mut self, mouse: MouseEvent) {
        let point = ScreenPoint::new(f32::from(mouse.column), f32::from(mouse.row));
        if point_in_rect(point, self.regions.timeline_track)
            && matches!(
                mouse.kind,
                MouseEventKind::Down(MouseButton::Left) | MouseEventKind::Drag(MouseButton::Left)
            )
        {
            if let Some(index) = timeline_column_to_index(
                self.regions.timeline_track,
                mouse.column,
                self.timeline.len(),
            ) {
                self.seek_to(index);
            }
            return;
        }
        if matches!(mouse.kind, MouseEventKind::Down(MouseButton::Left)) {
            if self
                .regions
                .play
                .is_some_and(|area| point_in_rect(point, area))
            {
                self.toggle_play();
                return;
            }
            if self
                .regions
                .live
                .is_some_and(|area| point_in_rect(point, area))
            {
                self.go_live();
                return;
            }
            if self
                .regions
                .scope
                .is_some_and(|area| point_in_rect(point, area))
            {
                self.cycle_scope();
                return;
            }
            if self
                .regions
                .speed
                .is_some_and(|area| point_in_rect(point, area))
            {
                self.speed_index = (self.speed_index + 1) % PLAYBACK_SPEEDS.len();
                self.last_play = Instant::now();
                return;
            }
            if self
                .regions
                .gap
                .is_some_and(|area| point_in_rect(point, area))
            {
                self.toggle_gap();
                return;
            }
            if self
                .regions
                .previous_era
                .is_some_and(|area| point_in_rect(point, area))
            {
                self.seek_era(false);
                return;
            }
            if self
                .regions
                .next_era
                .is_some_and(|area| point_in_rect(point, area))
            {
                self.seek_era(true);
                return;
            }
        }
        if self
            .regions
            .inspector
            .is_some_and(|area| point_in_rect(point, area))
        {
            match mouse.kind {
                MouseEventKind::ScrollUp => self.scroll_inspector(-3),
                MouseEventKind::ScrollDown => self.scroll_inspector(3),
                _ => {}
            }
            return;
        }
        if matches!(mouse.kind, MouseEventKind::Down(MouseButton::Right)) {
            self.flow.clear_selection();
            return;
        }
        if point_in_rect(point, self.regions.canvas) {
            let response = self.flow.handle_mouse_event(mouse);
            self.process_flow_response(response);
        }
    }
}

pub fn run(timeline: Timeline, feed: Option<Feed>) -> Result<()> {
    let _screen = ScreenGuard::enter()?;
    let backend = CrosstermBackend::new(io::stdout());
    let mut terminal = Terminal::new(backend).context("could not initialize terminal")?;
    terminal.clear()?;
    let mut app = App::new(timeline, feed)?;
    while !app.quit {
        app.update();
        terminal.draw(|frame| draw(frame, &mut app))?;
        if event::poll(FRAME_TIME)? {
            match event::read()? {
                Event::Key(key) => {
                    let before = app.mouse_capture;
                    app.handle_key(key);
                    if before != app.mouse_capture {
                        if app.mouse_capture {
                            execute!(terminal.backend_mut(), EnableMouseCapture)?;
                        } else {
                            execute!(terminal.backend_mut(), DisableMouseCapture)?;
                        }
                    }
                }
                Event::Mouse(mouse) if app.mouse_capture => app.handle_mouse(mouse),
                Event::Resize(_, _) => {
                    app.camera = CameraMode::Overview;
                    app.flow.request_fit_view();
                }
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
    if frame.area().width < 64 || frame.area().height < 20 {
        frame.render_widget(
            Paragraph::new("terminal too small · need at least 64×20")
                .alignment(Alignment::Center)
                .style(Style::default().fg(GOLD).bg(CANVAS)),
            frame.area(),
        );
        app.regions = UiRegions::default();
        return;
    }
    let [main, timeline, footer] = Layout::vertical([
        Constraint::Fill(1),
        Constraint::Length(6),
        Constraint::Length(1),
    ])
    .areas(frame.area());
    let (canvas, inspector) = if app.selected_id().is_some() && main.width >= 100 {
        let [left, right] =
            Layout::horizontal([Constraint::Percentage(30), Constraint::Percentage(70)])
                .areas(main);
        (left, Some(right))
    } else {
        (main, None)
    };
    app.regions.canvas = canvas;
    app.regions.timeline = timeline;
    app.regions.inspector = inspector;
    draw_canvas(frame, canvas, app);
    if let Some(area) = inspector {
        draw_inspector(frame, area, app);
    }
    draw_timeline(frame, timeline, app);
    draw_footer(frame, footer, app);
    if app.show_help {
        draw_help(frame);
    } else if app.show_info {
        draw_info(frame, app);
    }
    if let Some(error) = app.error.take() {
        draw_message(frame, " error ", &error, RED);
    }
}

fn draw_canvas(frame: &mut ratatui::Frame<'_>, area: Rect, app: &mut App) {
    let zoom = app.flow.viewport.zoom.max(0.08);
    let gap_x = (14.0 / zoom).round().clamp(8.0, 160.0) as u16;
    let gap_y = (7.0 / zoom).round().clamp(4.0, 80.0) as u16;
    frame.render_widget(
        Background::new(&app.flow)
            .variant(BackgroundVariant::Dots)
            .gap(gap_x, gap_y)
            .style(
                BackgroundStyle::default()
                    .with_pattern_color(GRID)
                    .with_bg_color(CANVAS),
            ),
        area,
    );
    frame.render_widget(&mut app.flow, area);
    if area.width >= 74 && area.height >= 18 && app.display_scene.cards.len() > 1 {
        let block = Block::default()
            .borders(Borders::ALL)
            .border_type(BorderType::Plain)
            .border_style(Style::default().fg(BORDER))
            .style(Style::default().bg(SURFACE));
        frame.render_widget(
            MiniMap::new(&app.flow)
                .position(MiniMapPosition::TopRight)
                .size(22, 8)
                .margin(1)
                .style(
                    MiniMapStyle::default()
                        .with_bg_color(SURFACE)
                        .with_node_color(MUTED)
                        .with_selected_node_color(GOLD)
                        .with_viewport_color(BORDER),
                )
                .block(block),
            area,
        );
    }
}

fn draw_inspector(frame: &mut ratatui::Frame<'_>, area: Rect, app: &mut App) {
    let Some(card) = app.selected_card().cloned() else {
        return;
    };
    let block = Block::default()
        .borders(Borders::LEFT)
        .border_style(Style::default().fg(BORDER))
        .style(Style::default().bg(SURFACE))
        .padding(Padding::new(3, 2, 2, 1));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let heading = Style::default()
        .fg(TEXT)
        .bg(SURFACE)
        .add_modifier(Modifier::BOLD);
    let key = Style::default().fg(GOLD).bg(SURFACE);
    let value = Style::default().fg(TEXT).bg(SURFACE);
    let dim = Style::default().fg(SUBTLE).bg(SURFACE);
    let mut lines = vec![
        Line::from(vec![
            Span::styled(
                "● ",
                Style::default().fg(state_color(&card.state)).bg(SURFACE),
            ),
            Span::styled(card.title.clone(), heading),
        ]),
        Line::from(""),
        detail_line("state", &card.state, key, value),
        detail_line("kind", &card.kind, key, value),
        detail_line("role", card.role.as_deref().unwrap_or("—"), key, value),
        detail_line("model", card.model.as_deref().unwrap_or("—"), key, value),
        detail_line("activity", &card.activity, key, value),
        detail_line("tokens", &compact_number(card.output_tokens), key, value),
        Line::from(""),
        Line::from(Span::styled("identity", key)),
        Line::from(Span::styled(card.key.clone(), dim)),
    ];

    let mut runs = app
        .state
        .related_run_ids(&card.key)
        .into_iter()
        .filter_map(|run_id| app.state.nodes.get(&format!("run:{run_id}")))
        .collect::<Vec<_>>();
    runs.sort_by(|left, right| {
        run_order(right)
            .total_cmp(&run_order(left))
            .then_with(|| right.id.cmp(&left.id))
    });
    if !runs.is_empty() {
        lines.extend([Line::from(""), Line::from(Span::styled("runs", key))]);
        for run in runs {
            let run_id = &run.id;
            lines.push(Line::from(vec![
                Span::styled(
                    "◆ ",
                    Style::default().fg(state_color(&run.state)).bg(SURFACE),
                ),
                Span::styled(run_id.get(..8).unwrap_or(run_id).to_owned(), heading),
                Span::styled(format!("  {}", run.state), value),
            ]));
            lines.push(detail_line(
                "effort",
                raw_string(&run.raw, "effort").unwrap_or("—"),
                key,
                value,
            ));
            lines.push(detail_line(
                "actual",
                model_list(&run.raw).as_deref().unwrap_or("—"),
                key,
                value,
            ));
            lines.push(detail_line(
                "timing",
                &timing_line(&run.raw, &run.state),
                key,
                value,
            ));
            lines.push(detail_line("usage", &usage_line(&run.raw), key, value));
            lines.push(detail_line("cost", &cost_line(&run.raw), key, value));
        }
    }

    let operations = app.state.operations_for(&card.key);
    lines.extend([
        Line::from(""),
        Line::from(Span::styled("controller operations", key)),
    ]);
    let [read, write, patch, check] = card.operation_counts;
    lines.push(Line::from(vec![
        Span::styled(format!(" R{read} "), Style::default().fg(CANVAS).bg(GOLD)),
        Span::raw(" "),
        Span::styled(format!(" W{write} "), Style::default().fg(CANVAS).bg(GOLD)),
        Span::raw(" "),
        Span::styled(format!(" P{patch} "), Style::default().fg(CANVAS).bg(GOLD)),
        Span::raw(" "),
        Span::styled(format!(" C{check} "), Style::default().fg(CANVAS).bg(GOLD)),
        Span::styled(format!("  open {}", card.open_operations), dim),
    ]));
    if operations.is_empty() {
        lines.push(Line::from(Span::styled(
            "No recorded controller operations.",
            dim,
        )));
    } else {
        for operation in operations.iter().rev() {
            let duration = operation
                .duration_seconds()
                .map(format_duration)
                .unwrap_or_else(|| {
                    if operation.state == "started" {
                        "open"
                    } else {
                        "—"
                    }
                    .to_owned()
                });
            lines.push(Line::from(vec![
                Span::styled(format!("#{:<4}", operation.seq), dim),
                Span::styled(format!("{:<12}", operation.kind), value),
                Span::styled(
                    format!("{:<9}", operation.state),
                    Style::default()
                        .fg(state_color(&operation.state))
                        .bg(SURFACE),
                ),
                Span::styled(duration, dim),
            ]));
        }
    }
    lines.extend([
        Line::from(""),
        Line::from(Span::styled(
            "Content-free metadata only · paths, commands, prompts,",
            dim,
        )),
        Line::from(Span::styled(
            "reasoning, operation bodies, and results are excluded.",
            dim,
        )),
        Line::from(Span::styled("PgUp/PgDn or wheel to scroll", dim)),
    ]);

    let selection_changed = app.inspector_selected.as_deref() != Some(&card.key);
    if selection_changed {
        app.inspector_selected = Some(card.key.clone());
        app.inspector_scroll = 0;
        app.inspector_follow_latest = true;
    }
    app.inspector_content_height = lines.len().min(u16::MAX as usize) as u16;
    app.inspector_viewport_height = inner.height;
    let max_scroll = app
        .inspector_content_height
        .saturating_sub(app.inspector_viewport_height);
    if app.inspector_follow_latest {
        app.inspector_scroll = 0;
    } else {
        app.inspector_scroll = app.inspector_scroll.min(max_scroll);
    }
    frame.render_widget(
        Paragraph::new(lines)
            .style(Style::default().bg(SURFACE))
            .scroll((app.inspector_scroll, 0)),
        inner,
    );
}

fn raw_string<'a>(value: &'a serde_json::Value, name: &str) -> Option<&'a str> {
    value.get(name).and_then(serde_json::Value::as_str)
}

fn run_order(run: &crate::model::Node) -> f64 {
    run.raw
        .get("started")
        .or_else(|| run.raw.get("created"))
        .and_then(serde_json::Value::as_f64)
        .filter(|value| value.is_finite())
        .unwrap_or(0.0)
}

fn model_list(value: &serde_json::Value) -> Option<String> {
    let models = value.get("models")?.as_array()?;
    let names = models
        .iter()
        .filter_map(serde_json::Value::as_str)
        .collect::<Vec<_>>();
    (!names.is_empty()).then(|| names.join(", "))
}

fn timing_line(value: &serde_json::Value, state: &str) -> String {
    let started = value.get("started").and_then(serde_json::Value::as_f64);
    let finished = value.get("finished").and_then(serde_json::Value::as_f64);
    let duration = finished
        .zip(started)
        .map(|(finished, started)| format_duration((finished - started).max(0.0)));
    match (started, duration) {
        (Some(started), Some(duration)) => format!("{} · {duration}", format_clock(started)),
        (Some(started), None) if active_state(state) => {
            format!("{} · running", format_clock(started))
        }
        (Some(started), None) => format!("{} · end unavailable", format_clock(started)),
        _ => "—".to_owned(),
    }
}

fn usage_line(value: &serde_json::Value) -> String {
    let Some(telemetry) = value.get("telemetry") else {
        return "—".to_owned();
    };
    let usage = telemetry.get("usage").unwrap_or(telemetry);
    if let Some(items) = usage.as_array() {
        let input: u64 = items
            .iter()
            .filter_map(|item| item.get("inputTokens").and_then(serde_json::Value::as_u64))
            .sum();
        let output: u64 = items
            .iter()
            .filter_map(|item| item.get("outputTokens").and_then(serde_json::Value::as_u64))
            .sum();
        let thinking: u64 = items
            .iter()
            .filter_map(|item| {
                item.get("reasoningTokens")
                    .and_then(serde_json::Value::as_u64)
            })
            .sum();
        return format!(
            "in {} · out {} · think {}",
            compact_number(input),
            compact_number(output),
            compact_number(thinking)
        );
    }
    let input = usage
        .get("input_tokens")
        .and_then(serde_json::Value::as_u64)
        .unwrap_or(0);
    let output = usage
        .get("output_tokens")
        .and_then(serde_json::Value::as_u64)
        .unwrap_or(0);
    let cache = usage
        .get("cache_read_input_tokens")
        .and_then(serde_json::Value::as_u64)
        .unwrap_or(0);
    let thinking = usage
        .pointer("/output_tokens_details/thinking_tokens")
        .and_then(serde_json::Value::as_u64)
        .unwrap_or(0);
    if input + output + cache + thinking == 0 {
        "—".to_owned()
    } else {
        format!(
            "in {} · out {} · cache {} · think {}",
            compact_number(input),
            compact_number(output),
            compact_number(cache),
            compact_number(thinking)
        )
    }
}

fn cost_line(value: &serde_json::Value) -> String {
    value
        .pointer("/telemetry/provider_cost_usd")
        .and_then(serde_json::Value::as_f64)
        .map(|cost| format!("${cost:.4}"))
        .unwrap_or_else(|| "—".to_owned())
}

fn format_duration(seconds: f64) -> String {
    if seconds < 1.0 {
        format!("{:.0}ms", seconds * 1_000.0)
    } else {
        format!("{seconds:.1}s")
    }
}

fn detail_line(label: &'static str, content: &str, key: Style, value: Style) -> Line<'static> {
    Line::from(vec![
        Span::styled(format!("{label:<10}"), key),
        Span::styled(content.to_owned(), value),
    ])
}

fn draw_timeline(frame: &mut ratatui::Frame<'_>, area: Rect, app: &mut App) {
    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(MUTED))
        .style(Style::default().bg(SURFACE));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    app.regions.timeline_track = Rect::default();
    if app.timeline.is_empty() || inner.width < 4 || inner.height < 4 {
        frame.render_widget(
            Paragraph::new("waiting for observation events")
                .alignment(Alignment::Center)
                .style(Style::default().fg(MUTED).bg(SURFACE)),
            inner,
        );
        return;
    }
    let [markers, upper, lower, info] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
    ])
    .areas(inner);
    app.regions.timeline_track = Rect::new(markers.x, markers.y, markers.width, 3);
    let width = usize::from(inner.width);
    let mut counts = vec![0_u64; width];
    let mut marks = vec![' '; width];
    for (index, event) in app.timeline.events().iter().enumerate() {
        let col = event_column(index, app.timeline.len(), width);
        counts[col] += event_weight(&event.summary);
        let mark = event_mark(&event.summary);
        if mark != ' ' {
            marks[col] = mark;
        }
    }
    let max_count = counts.iter().copied().max().unwrap_or(1).max(1);
    let buffer = frame.buffer_mut();
    for col in 0..width {
        let x = inner.x + col as u16;
        let event_index = col * app.timeline.len() / width.max(1);
        let color = if event_index <= app.index {
            GOLD
        } else {
            BORDER
        };
        if marks[col] != ' ' {
            buffer[(x, markers.y)].set_char(marks[col]).set_style(
                Style::default()
                    .fg(if marks[col] == '×' { RED } else { GOLD })
                    .bg(SURFACE),
            );
        }
        let level = (counts[col] * 16).div_ceil(max_count).min(16) as usize;
        buffer[(x, upper.y)]
            .set_symbol(bar_glyph(level.saturating_sub(8)))
            .set_style(Style::default().fg(color).bg(SURFACE));
        buffer[(x, lower.y)]
            .set_symbol(bar_glyph(level.min(8)))
            .set_style(Style::default().fg(color).bg(SURFACE));
    }
    let head = inner.x + event_column(app.index, app.timeline.len(), width) as u16;
    for y in markers.y..=lower.y {
        buffer[(head, y)]
            .set_char('│')
            .set_style(Style::default().fg(GOLD).bg(SURFACE));
    }
    let event = app.timeline.event(app.index);
    let clock = event
        .map(|event| format_clock(event.timestamp))
        .unwrap_or_else(|| "--:--:--".to_owned());
    let summary = event.map(|event| event.summary.as_str()).unwrap_or("event");
    let left = format!(" {clock}  {summary}");
    buffer.set_stringn(
        info.x,
        info.y,
        left,
        usize::from(info.width.saturating_sub(18)),
        Style::default().fg(SUBTLE).bg(SURFACE),
    );
    let tag = format!(
        " {}/{} {} ",
        app.index + 1,
        app.timeline.len(),
        app.playback.label()
    );
    buffer.set_string(
        info.right().saturating_sub(tag.len() as u16),
        info.y,
        tag,
        Style::default()
            .fg(if app.playback == Playback::Live {
                GREEN
            } else {
                GOLD
            })
            .bg(SURFACE)
            .add_modifier(Modifier::BOLD),
    );
}

fn draw_footer(frame: &mut ratatui::Frame<'_>, area: Rect, app: &mut App) {
    let buffer = frame.buffer_mut();
    buffer.set_style(area, Style::default().bg(CANVAS));
    app.regions.play = None;
    app.regions.live = None;
    app.regions.scope = None;
    app.regions.speed = None;
    app.regions.gap = None;
    app.regions.previous_era = None;
    app.regions.next_era = None;
    let mut x = area.x;
    let brand = " observer ";
    buffer.set_string(
        x,
        area.y,
        brand,
        Style::default()
            .fg(CANVAS)
            .bg(GOLD)
            .add_modifier(Modifier::BOLD),
    );
    x += brand.len() as u16 + 1;
    let play = if app.playback == Playback::Playing {
        " Ⅱ PAUSE "
    } else {
        " ▶ PLAY "
    };
    app.regions.play = Some(Rect::new(x, area.y, play.len() as u16, 1));
    buffer.set_string(
        x,
        area.y,
        play,
        Style::default()
            .fg(CANVAS)
            .bg(GOLD)
            .add_modifier(Modifier::BOLD),
    );
    x += play.len() as u16 + 1;
    let live = " ● LIVE ";
    app.regions.live = Some(Rect::new(x, area.y, live.len() as u16, 1));
    buffer.set_string(
        x,
        area.y,
        live,
        if app.playback == Playback::Live {
            Style::default()
                .fg(CANVAS)
                .bg(GREEN)
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(GREEN).bg(SURFACE_RAISED)
        },
    );
    x += live.len() as u16 + 1;
    let scope = format!(" ◉ {} ", app.scope.label().to_ascii_uppercase());
    app.regions.scope = Some(Rect::new(x, area.y, scope.len() as u16, 1));
    buffer.set_string(
        x,
        area.y,
        &scope,
        Style::default()
            .fg(CANVAS)
            .bg(GOLD)
            .add_modifier(Modifier::BOLD),
    );
    x += scope.len() as u16 + 1;
    if area.right().saturating_sub(x) >= 46 {
        let previous = " ‹ ERA ";
        app.regions.previous_era = Some(Rect::new(x, area.y, previous.len() as u16, 1));
        buffer.set_string(
            x,
            area.y,
            previous,
            Style::default().fg(GOLD).bg(SURFACE_RAISED),
        );
        x += previous.len() as u16;
        let next = " ERA › ";
        app.regions.next_era = Some(Rect::new(x, area.y, next.len() as u16, 1));
        buffer.set_string(
            x,
            area.y,
            next,
            Style::default().fg(GOLD).bg(SURFACE_RAISED),
        );
        x += next.len() as u16 + 1;
        let speed = format!(" {:.2}× ", PLAYBACK_SPEEDS[app.speed_index]);
        app.regions.speed = Some(Rect::new(x, area.y, speed.len() as u16, 1));
        buffer.set_string(
            x,
            area.y,
            &speed,
            Style::default()
                .fg(CANVAS)
                .bg(GOLD)
                .add_modifier(Modifier::BOLD),
        );
        x += speed.len() as u16 + 1;
        let gap = format!(" {} ", app.gap_mode.label());
        app.regions.gap = Some(Rect::new(x, area.y, gap.len() as u16, 1));
        buffer.set_string(
            x,
            area.y,
            &gap,
            Style::default()
                .fg(if app.gap_mode == GapMode::Compressed {
                    CANVAS
                } else {
                    MUTED
                })
                .bg(if app.gap_mode == GapMode::Compressed {
                    GREEN
                } else {
                    SURFACE_RAISED
                }),
        );
        x += gap.len() as u16 + 1;
    }
    let total_agents = app
        .scene
        .cards
        .iter()
        .filter(|card| card.kind == "session")
        .count();
    let visible_agents = app
        .display_scene
        .cards
        .iter()
        .filter(|card| card.kind == "session")
        .count();
    let tokens: u64 = app.scene.cards.iter().map(|card| card.output_tokens).sum();
    let (era, eras) = app.timeline.era_ordinal(app.index);
    let stats = format!(
        "{visible_agents}/{total_agents} agents · {} tok · era {era}/{eras} · {}",
        compact_number(tokens),
        app.camera.label()
    );
    buffer.set_stringn(
        x,
        area.y,
        stats,
        usize::from(area.right().saturating_sub(x).saturating_sub(25)),
        Style::default().fg(SUBTLE).bg(CANVAS),
    );
    let hints = "? help · q quit ";
    if area.width > hints.len() as u16 {
        buffer.set_string(
            area.right() - hints.len() as u16,
            area.y,
            hints,
            Style::default().fg(MUTED).bg(CANVAS),
        );
    }
}

fn draw_help(frame: &mut ratatui::Frame<'_>) {
    let lines = vec![
        Line::from(vec![
            Span::styled("mouse", Style::default().fg(GOLD)),
            Span::raw("   click node · drag canvas/node · wheel zoom · drag timeline"),
        ]),
        Line::from(vec![
            Span::styled("view", Style::default().fg(GOLD)),
            Span::raw("    o overview · f follow · r relayout · a scope · +/- zoom"),
        ]),
        Line::from(vec![
            Span::styled("graph", Style::default().fg(GOLD)),
            Span::raw("   tab/arrows select · h/j/k/l pan · esc close detail"),
        ]),
        Line::from(vec![
            Span::styled("replay", Style::default().fg(GOLD)),
            Span::raw("  space play/pause · [ ] step · { } era · ,/. speed · z gap"),
        ]),
        Line::from(vec![
            Span::styled("detail", Style::default().fg(GOLD)),
            Span::raw("  PgUp/PgDn or mouse wheel scroll · home start · g/end live"),
        ]),
        Line::from(vec![
            Span::styled("system", Style::default().fg(GOLD)),
            Span::raw("  i info · x mouse capture · q quit"),
        ]),
    ];
    draw_overlay(frame, 74, 11, " controls ", Text::from(lines));
}

fn draw_info(frame: &mut ratatui::Frame<'_>, app: &App) {
    let fidelity = if app.state.fidelity.is_empty() {
        "unknown"
    } else {
        &app.state.fidelity
    };
    let lines = vec![
        Line::from(vec![
            Span::styled("source    ", Style::default().fg(GOLD)),
            Span::raw("Claude Control observation ledger"),
        ]),
        Line::from(vec![
            Span::styled("fidelity  ", Style::default().fg(GOLD)),
            Span::raw(fidelity.to_owned()),
        ]),
        Line::from(vec![
            Span::styled("cursor    ", Style::default().fg(GOLD)),
            Span::raw(app.timeline.last_cursor().to_string()),
        ]),
        Line::from(vec![
            Span::styled("events    ", Style::default().fg(GOLD)),
            Span::raw(app.timeline.len().to_string()),
        ]),
        Line::from(vec![
            Span::styled("status    ", Style::default().fg(GOLD)),
            Span::raw(app.status.clone()),
        ]),
        Line::from(vec![
            Span::styled("scope     ", Style::default().fg(GOLD)),
            Span::raw(match app.scope {
                ScopeMode::Focus => "latest connected work",
                ScopeMode::Recent => "active + recent nodes",
                ScopeMode::All => "all historical nodes",
            }),
        ]),
        Line::from(""),
        Line::from(Span::styled(
            "Read-only · content-free · replayable",
            Style::default().fg(SUBTLE),
        )),
    ];
    draw_overlay(frame, 66, 13, " observation ", Text::from(lines));
}

fn draw_overlay(
    frame: &mut ratatui::Frame<'_>,
    width: u16,
    height: u16,
    title: &str,
    text: Text<'_>,
) {
    let area = centered_rect(
        width.min(frame.area().width),
        height.min(frame.area().height),
        frame.area(),
    );
    frame.render_widget(Clear, area);
    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .title(Line::from(title.to_owned()).centered())
        .title_bottom(Line::from(" esc to close ").centered())
        .border_style(Style::default().fg(GOLD))
        .style(Style::default().bg(SURFACE))
        .padding(Padding::uniform(1));
    frame.render_widget(
        Paragraph::new(text)
            .block(block)
            .style(Style::default().fg(TEXT).bg(SURFACE))
            .wrap(Wrap { trim: true }),
        area,
    );
}

fn draw_message(frame: &mut ratatui::Frame<'_>, title: &str, message: &str, color: Color) {
    let area = centered_rect(
        68.min(frame.area().width),
        7.min(frame.area().height),
        frame.area(),
    );
    frame.render_widget(Clear, area);
    frame.render_widget(
        Paragraph::new(message.to_owned())
            .alignment(Alignment::Center)
            .wrap(Wrap { trim: true })
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .border_type(BorderType::Rounded)
                    .title(title.to_owned())
                    .border_style(Style::default().fg(color))
                    .style(Style::default().bg(SURFACE))
                    .padding(Padding::uniform(1)),
            )
            .style(Style::default().fg(TEXT).bg(SURFACE)),
        area,
    );
}

fn centered_rect(width: u16, height: u16, area: Rect) -> Rect {
    Rect::new(
        area.x + area.width.saturating_sub(width) / 2,
        area.y + area.height.saturating_sub(height) / 2,
        width,
        height,
    )
}

fn event_column(index: usize, len: usize, width: usize) -> usize {
    if len <= 1 || width <= 1 {
        0
    } else {
        index.saturating_mul(width - 1) / (len - 1)
    }
}

fn event_mark(summary: &str) -> char {
    let lower = summary.to_ascii_lowercase();
    if lower.contains("fail") || lower.contains("error") {
        '×'
    } else if lower.contains("node_created") || lower.contains("created") {
        '✦'
    } else if lower.contains("run started") || lower.contains("run_started") {
        '◇'
    } else {
        ' '
    }
}

fn event_weight(summary: &str) -> u64 {
    let lower = summary.to_ascii_lowercase();
    if lower.contains("fail") || lower.contains("error") {
        7
    } else if lower.contains("run started") || lower.contains("run_started") {
        5
    } else if lower.contains("node_created") || lower.contains("created") {
        3
    } else if lower.contains("telemetry") {
        2
    } else {
        1
    }
}

fn scope_scene(state: &GraphState, scene: &Scene, scope: ScopeMode) -> Scene {
    if scope == ScopeMode::All {
        return scene.clone();
    }
    let mut keep = BTreeSet::from(["controller:local".to_owned()]);
    match scope {
        ScopeMode::Focus => {
            let seed = scene
                .cards
                .iter()
                .filter(|card| card.kind != "controller")
                .max_by(|left, right| {
                    created_at(state, &left.key)
                        .total_cmp(&created_at(state, &right.key))
                        .then_with(|| left.key.cmp(&right.key))
                })
                .map(|card| card.key.clone());
            if let Some(seed) = seed {
                keep.insert(seed);
            }
            keep.extend(
                scene
                    .cards
                    .iter()
                    .filter(|card| live_state(&card.state))
                    .map(|card| card.key.clone()),
            );
            expand_connected(scene, &mut keep, 12);
        }
        ScopeMode::Recent => {
            if scene.cards.len() <= RECENT_NODE_LIMIT + 1 {
                return scene.clone();
            }
            for (kind, quota) in [
                ("composition", 2_usize),
                ("workflow", 2),
                ("workspace", 5),
                ("task", 6),
                ("session", 8),
            ] {
                let mut candidates = scene
                    .cards
                    .iter()
                    .filter(|card| card.kind == kind)
                    .collect::<Vec<_>>();
                candidates.sort_by(|left, right| {
                    active_state(&right.state)
                        .cmp(&active_state(&left.state))
                        .then_with(|| {
                            created_at(state, &right.key).total_cmp(&created_at(state, &left.key))
                        })
                        .then_with(|| left.key.cmp(&right.key))
                });
                keep.extend(
                    candidates
                        .into_iter()
                        .take(quota)
                        .map(|card| card.key.clone()),
                );
            }
        }
        ScopeMode::All => unreachable!(),
    }
    scoped_scene(scene, &keep)
}

fn expand_connected(scene: &Scene, keep: &mut BTreeSet<String>, limit: usize) {
    loop {
        let mut additions = Vec::new();
        for link in scene
            .links
            .iter()
            .filter(|link| !matches!(link.kind.as_str(), "root" | "scope"))
        {
            if keep.contains(&link.from) && !keep.contains(&link.to) {
                additions.push(link.to.clone());
            } else if keep.contains(&link.to) && !keep.contains(&link.from) {
                additions.push(link.from.clone());
            }
        }
        additions.sort();
        additions.dedup();
        let remaining = limit.saturating_sub(keep.len().saturating_sub(1));
        if additions.is_empty() || remaining == 0 {
            break;
        }
        keep.extend(additions.into_iter().take(remaining));
    }
}

fn scoped_scene(scene: &Scene, keep: &BTreeSet<String>) -> Scene {
    let cards = scene
        .cards
        .iter()
        .filter(|card| keep.contains(&card.key))
        .cloned()
        .collect::<Vec<_>>();
    let mut links = scene
        .links
        .iter()
        .filter(|link| keep.contains(&link.from) && keep.contains(&link.to))
        .cloned()
        .collect::<Vec<_>>();
    let connected = links
        .iter()
        .flat_map(|link| [link.from.clone(), link.to.clone()])
        .collect::<BTreeSet<_>>();
    for card in cards.iter().filter(|card| card.kind != "controller") {
        if !connected.contains(&card.key) {
            links.push(Link {
                from: "controller:local".to_owned(),
                to: card.key.clone(),
                kind: "scope".to_owned(),
            });
        }
    }
    Scene {
        cards,
        links,
        ..Scene::default()
    }
}

fn created_at(state: &GraphState, key: &str) -> f64 {
    state
        .nodes
        .get(key)
        .and_then(|node| node.raw.get("created"))
        .and_then(serde_json::Value::as_f64)
        .filter(|value| value.is_finite())
        .unwrap_or(0.0)
}

fn bar_glyph(level: usize) -> &'static str {
    [" ", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"][level.min(8)]
}

fn active_state(state: &str) -> bool {
    matches!(
        state,
        "running"
            | "active"
            | "claimed"
            | "launching"
            | "pending"
            | "queued"
            | "awaiting_codex"
            | "awaiting_leader"
    )
}

fn live_state(state: &str) -> bool {
    matches!(state, "running" | "active" | "claimed" | "launching")
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

#[cfg(test)]
mod tests {
    use super::*;
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
            timeline
                .push(observation(
                    index as u64 + 2,
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

    fn buffer_text(terminal: &Terminal<TestBackend>) -> String {
        terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect()
    }

    #[test]
    fn wide_render_has_graph_timeline_and_compact_chrome() {
        let mut app = sample_app(8);
        let backend = TestBackend::new(160, 45);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();
        let screen = buffer_text(&terminal);
        assert!(screen.contains("Claude"), "{screen}");
        assert!(screen.contains("observer"));
        assert!(screen.contains("PLAY"));
        assert!(app.regions.timeline_track.width > 0);
        assert!(app.regions.inspector.is_none());
    }

    #[test]
    fn selection_opens_a_large_detail_panel() {
        let mut app = sample_app(3);
        app.flow.select_node("session:session-2");
        let backend = TestBackend::new(160, 45);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();
        let panel = app.regions.inspector.expect("selection should open detail");
        assert!(panel.width > app.regions.canvas.width);
        assert!(buffer_text(&terminal).contains("Content-free metadata only"));
    }

    #[test]
    fn timeline_drag_scrubs_and_pauses() {
        let mut app = sample_app(10);
        let backend = TestBackend::new(120, 35);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();
        app.playback = Playback::Playing;
        let track = app.regions.timeline_track;
        app.handle_mouse(mouse(
            MouseEventKind::Down(MouseButton::Left),
            track.x + track.width / 2,
            track.y,
        ));
        assert!((4..=7).contains(&app.index));
        assert_eq!(app.playback, Playback::Paused);
    }

    #[test]
    fn scope_chip_cycles_focus_recent_and_all() {
        let mut app = sample_app(24);
        let backend = TestBackend::new(160, 45);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal.draw(|frame| draw(frame, &mut app)).unwrap();

        let scope = app.regions.scope.expect("scope chip should be clickable");
        app.handle_mouse(mouse(
            MouseEventKind::Down(MouseButton::Left),
            scope.x,
            scope.y,
        ));
        assert_eq!(app.scope, ScopeMode::Recent);

        app.handle_mouse(mouse(
            MouseEventKind::Down(MouseButton::Left),
            scope.x,
            scope.y,
        ));
        assert_eq!(app.scope, ScopeMode::All);
    }

    #[test]
    fn playback_speed_and_gap_only_change_presentation_delay() {
        let mut app = sample_app(3);
        app.index = 0;
        assert_eq!(app.playback_delay(), Duration::from_millis(180));
        app.change_speed(1);
        assert_eq!(app.playback_delay(), Duration::from_millis(90));
        app.toggle_gap();
        assert_eq!(app.playback_delay(), Duration::from_millis(500));
        assert_eq!(app.index, 0);
    }

    #[test]
    fn inspector_scroll_clamps_and_rearms_latest_at_top() {
        let mut app = sample_app(1);
        app.inspector_content_height = 40;
        app.inspector_viewport_height = 10;
        app.scroll_inspector(12);
        assert_eq!(app.inspector_scroll, 12);
        assert!(!app.inspector_follow_latest);
        app.scroll_inspector(100);
        assert_eq!(app.inspector_scroll, 30);
        assert!(!app.inspector_follow_latest);
        app.scroll_inspector(-3);
        assert_eq!(app.inspector_scroll, 27);
        assert!(!app.inspector_follow_latest);
        app.scroll_inspector(-100);
        assert_eq!(app.inspector_scroll, 0);
        assert!(app.inspector_follow_latest);
    }

    #[test]
    fn missing_terminal_timestamp_is_not_reported_as_running() {
        let raw = json!({"started": 1_700_000_000.0});
        assert!(timing_line(&raw, "running").ends_with(" · running"));
        assert!(timing_line(&raw, "completed").ends_with(" · end unavailable"));
    }
}
