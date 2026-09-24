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
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Padding, Paragraph, Widget};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

use crate::graph::{Card, Scene};
use crate::theme::{
    AMBER, BORDER, BRIGHT_TEXT, CLAUDE_ORANGE, CODEX_TEAL, GOLD, GREEN, RED, StateTone,
    flow_palette, state_color, state_tone,
};

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
                bg.fg(BRIGHT_TEXT).add_modifier(Modifier::BOLD),
            ),
        ])];

        let role = self.card.role.as_deref().unwrap_or(&self.card.kind);
        let mut identity = vec![
            Span::styled("◆ ", bg.fg(provider_color(&self.card))),
            Span::styled(
                truncate(role, usize::from(area.width).saturating_sub(2)),
                bg.fg(palette.accent).add_modifier(Modifier::BOLD),
            ),
        ];
        if let Some(model) = self.card.model.as_deref().filter(|model| !model.is_empty()) {
            identity.extend([
                Span::styled("  ", bg),
                Span::styled(
                    truncate(
                        model,
                        usize::from(area.width).saturating_sub(role.width() + 4),
                    ),
                    bg.fg(palette.subtle),
                ),
            ]);
        }
        lines.push(Line::from(identity));
        lines.push(Line::from(vec![
            Span::styled(
                if state_tone(&self.card.state) == StateTone::Live {
                    "▶ "
                } else {
                    "↳ "
                },
                bg.fg(if state_tone(&self.card.state) == StateTone::Live {
                    GREEN
                } else {
                    GOLD
                }),
            ),
            Span::styled(
                truncate(
                    &self.card.activity,
                    usize::from(area.width).saturating_sub(2),
                ),
                bg.fg(palette.text),
            ),
        ]));

        let tokens = compact_number(self.card.output_tokens);
        let mut footer = vec![Span::styled(
            self.card.state.to_ascii_uppercase(),
            bg.fg(state_color(&self.card.state))
                .add_modifier(Modifier::BOLD),
        )];
        if self.card.output_tokens > 0 {
            footer.push(Span::styled(
                format!("  ·  {tokens} tok"),
                bg.fg(palette.muted),
            ));
        }
        lines.push(Line::from(footer));
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
        let border = if ctx.selected || ctx.dragging {
            palette.accent
        } else {
            match state_tone(&self.card.state) {
                StateTone::Live => GREEN,
                StateTone::Waiting => AMBER,
                StateTone::Failed => RED,
                StateTone::Done | StateTone::Idle => BORDER,
            }
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
    tone: StateTone,
}

impl ObserverEdge {
    fn new(tone: StateTone) -> Self {
        Self {
            route: StepEdge::default(),
            tone,
        }
    }
}

impl EdgeContent for ObserverEdge {
    fn compute_path(&self, ctx: &EdgePathContext) -> Path {
        self.route.compute_path(ctx)
    }

    fn render(&self, ctx: &EdgeRenderContext, buf: &mut Buffer) {
        let color = match self.tone {
            StateTone::Live => GREEN,
            StateTone::Waiting => AMBER,
            StateTone::Failed => RED,
            StateTone::Done | StateTone::Idle => BORDER,
        };
        let style = EdgeStyle::default().with_stroke_style(Style::default().fg(color));
        ctx.render_path(&style, None, buf);
    }
}

pub fn new_flow() -> ObserverFlow {
    Flow::new()
        .with_theme(Theme::Custom(flow_palette()))
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
    let mut wanted_edges = BTreeMap::<String, (String, String, StateTone)>::new();
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
        wanted_edges.insert(id, (source, target, state_tone(&target_card.state)));
    }

    let before_edges = flow.edges().len();
    flow.retain_edges(|edge| wanted_edges.contains_key(&edge.id));
    structural |= flow.edges().len() != before_edges;
    for (id, (source, target, tone)) in wanted_edges {
        if let Some(content) = flow.edge_content_mut(&id) {
            content.tone = tone;
            flow.set_edge_animated(&id, tone == StateTone::Live);
            continue;
        }
        let edge = Edge::new(id, source, target)
            .with_content(ObserverEdge::new(tone))
            .with_animated(tone == StateTone::Live)
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

fn status_mark(state: &str) -> &'static str {
    match state {
        "running" | "active" | "claimed" | "launching" => "●",
        "completed" | "accepted" => "✓",
        "failed" | "unknown" | "cancelled" | "blocked" => "×",
        "pending" | "queued" | "awaiting_codex" | "awaiting_leader" => "◆",
        _ => "○",
    }
}

fn provider_color(card: &Card) -> ratatui::style::Color {
    if card.kind == "controller" {
        CODEX_TEAL
    } else if card
        .model
        .as_deref()
        .is_some_and(|model| model.to_ascii_lowercase().contains("claude"))
    {
        CLAUDE_ORANGE
    } else {
        GOLD
    }
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
