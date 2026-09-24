//! Rataflow-backed projection and rendering for the observation graph.
//!
//! The observation ledger remains the source of truth. This module only turns the
//! content-free `Scene` projection into a stable, interactive graph canvas.

use std::collections::{BTreeMap, BTreeSet};

use rataflow::{
    Edge, EdgeContent, EdgePathContext, EdgeRenderContext, EdgeStyle, Flow, Handle, HandlePosition,
    Node, NodeContent, NodeRenderContext, Path, Reconnectable, SelectionReveal, StepEdge, Sugiyama,
    Theme,
};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Padding, Paragraph, Widget};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

use crate::graph::{Card, Scene};

const PRIMARY_DIMS: (f64, f64) = (30.0, 7.0);
const SECONDARY_DIMS: (f64, f64) = (28.0, 6.0);
const CELL_MIN_WIDTH: u16 = 10;
const CELL_MIN_HEIGHT: u16 = 3;

pub type ObserverFlow = Flow<ObserverNode, ObserverEdge>;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ObserverNode {
    pub card: Card,
}

impl ObserverNode {
    fn lines(&self, palette: rataflow::Palette, area: Rect) -> Vec<Line<'static>> {
        let bg = Style::default().bg(palette.surface);
        let glyph = status_mark(&self.card.state);
        let title_budget = usize::from(area.width).saturating_sub(4);
        let mut lines = vec![Line::from(vec![
            Span::styled(format!("{glyph} "), bg.fg(state_color(&self.card.state))),
            Span::styled(
                truncate(&self.card.title, title_budget),
                bg.fg(palette.text).add_modifier(Modifier::BOLD),
            ),
        ])];

        let identity = match (&self.card.role, &self.card.model) {
            (Some(role), Some(model)) if !model.is_empty() => format!("{role}  {model}"),
            (Some(role), _) => role.clone(),
            (_, Some(model)) => model.clone(),
            _ => self.card.kind.clone(),
        };
        lines.push(Line::from(Span::styled(
            truncate(&identity, usize::from(area.width)),
            bg.fg(palette.subtle),
        )));
        lines.push(Line::from(Span::styled(
            truncate(&self.card.activity, usize::from(area.width)),
            bg.fg(palette.accent),
        )));

        let tokens = compact_number(self.card.output_tokens);
        let footer = if self.card.output_tokens > 0 {
            format!("{}  ·  {tokens} tok", self.card.state)
        } else {
            self.card.state.clone()
        };
        lines.push(Line::from(Span::styled(
            truncate(&footer, usize::from(area.width)),
            bg.fg(state_color(&self.card.state)),
        )));
        lines
    }
}

impl NodeContent for ObserverNode {
    fn render(&self, ctx: &NodeRenderContext, buf: &mut Buffer) {
        if ctx.area.width == 0 || ctx.area.height == 0 {
            return;
        }
        let palette = ctx.theme.palette();
        if ctx.area.width < CELL_MIN_WIDTH || ctx.area.height < CELL_MIN_HEIGHT {
            let color = if ctx.selected {
                palette.accent
            } else {
                state_color(&self.card.state)
            };
            buf.set_style(ctx.area, Style::default().bg(color));
            return;
        }

        let surface = Style::default().bg(palette.surface);
        let border = if ctx.selected {
            palette.accent
        } else {
            palette.muted
        };
        let block = Block::default()
            .borders(Borders::ALL)
            .border_type(BorderType::Plain)
            .border_style(surface.fg(border))
            .style(surface)
            .padding(Padding::horizontal(1));
        let inner = block.inner(ctx.area);
        block.render(ctx.area, buf);
        if inner.width == 0 || inner.height == 0 {
            return;
        }
        for (row, line) in self.lines(palette, inner).into_iter().enumerate() {
            if row >= usize::from(inner.height) {
                break;
            }
            Paragraph::new(line).style(surface).render(
                Rect::new(inner.x, inner.y + row as u16, inner.width, 1),
                buf,
            );
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct ObserverEdge {
    route: StepEdge,
    active: bool,
}

impl ObserverEdge {
    fn new(active: bool) -> Self {
        Self {
            route: StepEdge::default(),
            active,
        }
    }
}

impl EdgeContent for ObserverEdge {
    fn compute_path(&self, ctx: &EdgePathContext) -> Path {
        self.route.compute_path(ctx)
    }

    fn render(&self, ctx: &EdgeRenderContext, buf: &mut Buffer) {
        let color = if self.active {
            ctx.theme.palette().success
        } else {
            ctx.theme.palette().muted
        };
        let style = EdgeStyle::default().with_stroke_style(Style::default().fg(color));
        ctx.render_path(&style, None, buf);
    }
}

pub fn new_flow() -> ObserverFlow {
    let mut palette = Theme::Dark.palette();
    palette.accent = Color::Indexed(178);
    palette.canvas_bg = Color::Rgb(13, 14, 13);
    palette.surface = Color::Rgb(27, 28, 27);
    palette.muted = Color::Rgb(62, 64, 61);
    palette.subtle = Color::Rgb(116, 117, 111);
    palette.text = Color::Rgb(226, 227, 221);
    palette.success = Color::Rgb(102, 181, 91);
    palette.error = Color::Rgb(205, 92, 92);
    Flow::new()
        .with_theme(Theme::Custom(palette))
        .with_min_zoom(0.08)
        .with_max_zoom(1.5)
        .with_deselect_on_pane_click(false)
        .with_deselect_on_drag(false)
        .with_selection_reveal(SelectionReveal::None)
        .with_animation_speed(105)
}

/// Incrementally mirrors a scene into the canvas. Existing node positions survive
/// state-only updates and manual dragging. A structural change relayouts only when
/// the caller explicitly allows it.
pub fn sync(flow: &mut ObserverFlow, scene: &Scene, allow_layout: bool) -> bool {
    let wanted_nodes = scene
        .cards
        .iter()
        .map(|card| card.key.as_str())
        .collect::<BTreeSet<_>>();
    let before_nodes = flow.nodes().count();
    flow.retain_nodes(|node| wanted_nodes.contains(node.id.as_str()));
    let mut structural = flow.nodes().count() != before_nodes;

    for card in &scene.cards {
        if let Some(content) = flow.node_content_mut(&card.key) {
            if content.card != *card {
                content.card = card.clone();
            }
            continue;
        }
        let dims = if card.kind == "controller" {
            PRIMARY_DIMS
        } else {
            SECONDARY_DIMS
        };
        let node = Node::new(
            card.key.clone(),
            (card.position.x as f64, card.position.y as f64),
            dims,
            ObserverNode { card: card.clone() },
        )
        .with_deletable(false)
        .with_resizable(false)
        .with_connectable(false)
        .with_handles(vec![
            Handle::source(HandlePosition::Bottom).with_hidden(true),
            Handle::target(HandlePosition::Top).with_hidden(true),
        ]);
        if flow.add_node(node).is_ok() {
            structural = true;
        }
    }

    let by_key = scene
        .cards
        .iter()
        .map(|card| (card.key.as_str(), card))
        .collect::<BTreeMap<_, _>>();
    let mut wanted_edges = BTreeMap::<String, (String, String, bool)>::new();
    for link in &scene.links {
        let (Some(from), Some(to)) = (by_key.get(link.from.as_str()), by_key.get(link.to.as_str()))
        else {
            continue;
        };
        let (source, target, target_card) = if kind_rank(&from.kind) <= kind_rank(&to.kind) {
            (from.key.clone(), to.key.clone(), *to)
        } else {
            (to.key.clone(), from.key.clone(), *from)
        };
        let id = format!("{source}\u{2192}{target}");
        wanted_edges.insert(id, (source, target, active_state(&target_card.state)));
    }

    let before_edges = flow.edges().len();
    flow.retain_edges(|edge| wanted_edges.contains_key(&edge.id));
    structural |= flow.edges().len() != before_edges;
    for (id, (source, target, active)) in wanted_edges {
        if let Some(content) = flow.edge_content_mut(&id) {
            content.active = active;
            flow.set_edge_animated(&id, active);
            continue;
        }
        let edge = Edge::new(id, source, target)
            .with_content(ObserverEdge::new(active))
            .with_animated(active)
            .with_selectable(false)
            .with_deletable(false)
            .with_reconnectable(Reconnectable::None);
        if flow.add_edge(edge).is_ok() {
            structural = true;
        }
    }

    if structural && allow_layout {
        relayout(flow);
    }
    structural
}

pub fn relayout(flow: &mut ObserverFlow) {
    flow.apply_layout(
        Sugiyama::vertical()
            .with_node_spacing(8.0)
            .with_rank_spacing(5.0)
            .with_margin(5.0),
    );
}

pub fn state_color(state: &str) -> Color {
    match state {
        "running" | "active" | "claimed" | "launching" => Color::Rgb(102, 181, 91),
        "completed" | "accepted" | "idle" => Color::Rgb(132, 154, 124),
        "failed" | "unknown" | "cancelled" | "blocked" => Color::Rgb(205, 92, 92),
        "pending" | "queued" | "awaiting_codex" | "awaiting_leader" => Color::Rgb(206, 170, 58),
        _ => Color::Rgb(138, 140, 134),
    }
}

fn status_mark(state: &str) -> &'static str {
    match state {
        "running" | "active" | "claimed" | "launching" => "●",
        "completed" | "accepted" => "✓",
        "failed" | "unknown" | "cancelled" | "blocked" => "×",
        "pending" | "queued" | "awaiting_codex" | "awaiting_leader" => "◆",
        _ => "○",
    }
}

fn active_state(state: &str) -> bool {
    matches!(state, "running" | "active" | "claimed" | "launching")
}

fn kind_rank(kind: &str) -> u8 {
    match kind {
        "controller" => 0,
        "composition" => 1,
        "workflow" => 2,
        "workspace" => 3,
        "session" => 4,
        "task" => 5,
        _ => 6,
    }
}

pub fn compact_number(value: u64) -> String {
    if value < 1_000 {
        value.to_string()
    } else if value < 1_000_000 {
        format!("{:.1}k", value as f64 / 1_000.0)
    } else {
        format!("{:.1}m", value as f64 / 1_000_000.0)
    }
}

fn truncate(text: &str, width: usize) -> String {
    if text.width() <= width {
        return text.to_owned();
    }
    if width == 0 {
        return String::new();
    }
    let mut result = String::new();
    let budget = width.saturating_sub(1);
    let mut used = 0;
    for ch in text.chars() {
        let char_width = ch.width().unwrap_or(0);
        if used + char_width > budget {
            break;
        }
        result.push(ch);
        used += char_width;
    }
    result.push('…');
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph::{Link, Point};

    fn card(key: &str, kind: &str) -> Card {
        Card {
            key: key.into(),
            kind: kind.into(),
            title: key.into(),
            state: "running".into(),
            role: Some("editor".into()),
            model: Some("claude-opus-5".into()),
            activity: "working".into(),
            output_tokens: 1_200,
            position: Point::default(),
            width: 30,
            height: 6,
        }
    }

    #[test]
    fn scene_sync_builds_a_vertical_read_only_flow() {
        let scene = Scene {
            cards: vec![
                card("controller:local", "controller"),
                card("session:a", "session"),
            ],
            links: vec![Link {
                from: "controller:local".into(),
                to: "session:a".into(),
                kind: "root".into(),
            }],
            ..Scene::default()
        };
        let mut flow = new_flow();
        assert!(sync(&mut flow, &scene, true));
        assert_eq!(flow.nodes().count(), 2);
        assert_eq!(flow.edges().len(), 1);
        assert!(!flow.edges()[0].selectable);
        assert!(flow.node("session:a").is_some_and(|node| !node.deletable));
    }

    #[test]
    fn compact_labels_respect_wide_character_columns() {
        assert!(truncate("구현자모델", 7).width() <= 7);
        assert_eq!(compact_number(1_200), "1.2k");
    }
}
