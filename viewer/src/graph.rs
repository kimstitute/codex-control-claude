use std::collections::{BTreeMap, BTreeSet};

use serde_json::Value;

use crate::model::{GraphState, Node};

pub const CARD_WIDTH: i32 = 30;
pub const CARD_HEIGHT: i32 = 6;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Point {
    pub x: i32,
    pub y: i32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Card {
    pub key: String,
    pub kind: String,
    pub title: String,
    pub state: String,
    pub role: Option<String>,
    pub model: Option<String>,
    pub activity: String,
    pub output_tokens: u64,
    pub position: Point,
    pub width: i32,
    pub height: i32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Link {
    pub from: String,
    pub to: String,
    pub kind: String,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Bounds {
    pub min_x: i32,
    pub min_y: i32,
    pub max_x: i32,
    pub max_y: i32,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Scene {
    pub cards: Vec<Card>,
    pub links: Vec<Link>,
    pub bounds: Bounds,
}

impl Scene {
    pub fn index_of(&self, key: &str) -> Option<usize> {
        self.cards.iter().position(|card| card.key == key)
    }
}

pub fn project(state: &GraphState) -> Scene {
    let mut nodes = BTreeMap::<String, Node>::new();
    for (key, node) in &state.nodes {
        if visible_kind(&node.kind) {
            nodes.insert(key.clone(), node.clone());
        }
    }

    let root_key = "controller:local".to_owned();
    let mut cards = vec![Card {
        key: root_key.clone(),
        kind: "controller".to_owned(),
        title: "Claude Control".to_owned(),
        state: controller_state(state).to_owned(),
        role: Some("orchestrator".to_owned()),
        model: None,
        activity: format!(
            "{} agents · {} runs",
            count_kind(&nodes, "session"),
            count_kind(&state.nodes, "run")
        ),
        output_tokens: state
            .nodes
            .values()
            .filter(|node| node.kind == "run")
            .map(|node| node.output_tokens)
            .sum(),
        position: Point::default(),
        width: CARD_WIDTH,
        height: CARD_HEIGHT,
    }];

    for (key, node) in &nodes {
        let (output_tokens, active_runs, completed_runs) = session_activity(state, node);
        let activity = if node.kind == "session" {
            if active_runs > 0 {
                format!("{active_runs} active · {completed_runs} done")
            } else {
                format!("{completed_runs} runs")
            }
        } else {
            compact_activity(node)
        };
        cards.push(Card {
            key: key.clone(),
            kind: node.kind.clone(),
            title: node.label.clone(),
            state: node.state.clone(),
            role: node.role.clone(),
            model: node.model.clone(),
            activity,
            output_tokens,
            position: Point::default(),
            width: CARD_WIDTH,
            height: CARD_HEIGHT,
        });
    }

    cards.sort_by(|left, right| {
        rank(&left.kind)
            .cmp(&rank(&right.kind))
            .then_with(|| left.title.to_lowercase().cmp(&right.title.to_lowercase()))
            .then_with(|| left.key.cmp(&right.key))
    });
    layout(&mut cards);

    let known = cards
        .iter()
        .map(|card| card.key.clone())
        .collect::<BTreeSet<_>>();
    let mut links = BTreeSet::<(String, String, String)>::new();
    for edge in state.edges.values() {
        let from = resolve_endpoint(state, &edge.from_kind, &edge.from_id);
        let to = resolve_endpoint(state, &edge.to_kind, &edge.to_id);
        if from != to && known.contains(&from) && known.contains(&to) {
            links.insert((from, to, edge.kind.clone()));
        }
    }
    for node in nodes.values() {
        if node.kind == "task"
            && let Some(session_id) = &node.session_id
        {
            let session = format!("session:{session_id}");
            let task = format!("task:{}", node.id);
            if known.contains(&session) {
                links.insert((task, session, "assignment".to_owned()));
            }
        }
    }

    let incoming = links
        .iter()
        .map(|(_, to, _)| to.clone())
        .collect::<BTreeSet<_>>();
    for card in cards.iter().skip(1) {
        if !incoming.contains(&card.key) {
            links.insert((root_key.clone(), card.key.clone(), "root".to_owned()));
        }
    }

    let bounds = cards.iter().fold(Bounds::default(), |mut result, card| {
        result.max_x = result.max_x.max(card.position.x + card.width);
        result.max_y = result.max_y.max(card.position.y + card.height);
        result.min_x = result.min_x.min(card.position.x);
        result.min_y = result.min_y.min(card.position.y);
        result
    });
    Scene {
        cards,
        links: links
            .into_iter()
            .map(|(from, to, kind)| Link { from, to, kind })
            .collect(),
        bounds,
    }
}

fn visible_kind(kind: &str) -> bool {
    matches!(
        kind,
        "composition" | "workflow" | "workspace" | "task" | "session"
    )
}

fn rank(kind: &str) -> u8 {
    match kind {
        "controller" => 0,
        "composition" => 1,
        "workflow" => 2,
        "workspace" => 3,
        "task" => 4,
        "session" => 5,
        _ => 6,
    }
}

fn layout(cards: &mut [Card]) {
    let mut groups = BTreeMap::<u8, Vec<usize>>::new();
    for (index, card) in cards.iter().enumerate() {
        groups.entry(rank(&card.kind)).or_default().push(index);
    }
    let mut base_x = 0;
    for indices in groups.values() {
        let lanes = match indices.len() {
            0..=5 => 1,
            6..=12 => 2,
            13..=24 => 3,
            _ => 4,
        };
        for (offset, index) in indices.iter().enumerate() {
            cards[*index].position = Point {
                x: base_x + (offset % lanes) as i32 * (CARD_WIDTH + 8),
                y: (offset / lanes) as i32 * (CARD_HEIGHT + 3),
            };
        }
        base_x += lanes as i32 * (CARD_WIDTH + 8);
    }
}

fn count_kind(nodes: &BTreeMap<String, Node>, kind: &str) -> usize {
    nodes.values().filter(|node| node.kind == kind).count()
}

fn controller_state(state: &GraphState) -> &'static str {
    if state.nodes.values().any(|node| active(&node.state)) {
        "active"
    } else {
        "idle"
    }
}

fn active(state: &str) -> bool {
    matches!(
        state,
        "pending" | "claimed" | "launching" | "running" | "stopping"
    )
}

fn session_activity(state: &GraphState, node: &Node) -> (u64, usize, usize) {
    if node.kind != "session" {
        return (node.output_tokens, 0, 0);
    }
    let runs = state.nodes.values().filter(|candidate| {
        candidate.kind == "run" && candidate.session_id.as_deref() == Some(&node.id)
    });
    let mut tokens = 0;
    let mut running = 0;
    let mut completed = 0;
    for run in runs {
        tokens += run.output_tokens;
        if active(&run.state) {
            running += 1;
        } else {
            completed += 1;
        }
    }
    (tokens, running, completed)
}

fn compact_activity(node: &Node) -> String {
    if let Some(run) = &node.active_run_id {
        format!("run {}", short(run))
    } else {
        value_string(&node.raw, &["current_stage", "stage", "recommendation"])
            .unwrap_or_else(|| node.state.clone())
    }
}

fn resolve_endpoint(state: &GraphState, kind: &str, id: &str) -> String {
    if kind == "run"
        && let Some(session_id) = state
            .nodes
            .get(&format!("run:{id}"))
            .and_then(|node| node.session_id.as_deref())
    {
        return format!("session:{session_id}");
    }
    format!("{kind}:{id}")
}

fn value_string(value: &Value, names: &[&str]) -> Option<String> {
    names
        .iter()
        .find_map(|name| value.get(*name).and_then(Value::as_str))
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn short(value: &str) -> &str {
    value.get(..8).unwrap_or(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn projects_agents_and_collapses_runs_into_session_activity() {
        let mut state = GraphState::default();
        state.nodes.insert(
            "session:s1".to_owned(),
            Node {
                id: "s1".to_owned(),
                kind: "session".to_owned(),
                label: "critic".to_owned(),
                state: "idle".to_owned(),
                role: Some("critic".to_owned()),
                model: Some("claude-fable-5-1".to_owned()),
                ..Node::default()
            },
        );
        state.nodes.insert(
            "run:r1".to_owned(),
            Node {
                id: "r1".to_owned(),
                kind: "run".to_owned(),
                state: "completed".to_owned(),
                session_id: Some("s1".to_owned()),
                output_tokens: 42,
                ..Node::default()
            },
        );
        let scene = project(&state);
        assert_eq!(scene.cards.len(), 2);
        let agent = scene
            .cards
            .iter()
            .find(|card| card.key == "session:s1")
            .unwrap();
        assert_eq!(agent.output_tokens, 42);
        assert_eq!(agent.activity, "1 runs");
        assert_eq!(scene.links[0].from, "controller:local");
    }
}
