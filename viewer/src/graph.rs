use std::collections::{BTreeMap, BTreeSet};

use serde_json::Value;

use crate::model::{GraphState, Node};

pub const CARD_WIDTH: i32 = 34;
pub const CARD_HEIGHT: i32 = 7;
const CARD_X_GAP: i32 = 12;
const CARD_Y_GAP: i32 = 3;
const ROWS_PER_COLUMN: i32 = 8;

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
    pub operation_counts: [u32; 4],
    pub open_operations: u32,
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
    project_with_positions(state, &mut BTreeMap::new())
}

/// Projects graph state while preserving every position already recorded in `positions`.
///
/// The caller can retain this cache for the lifetime of a viewer. Removed nodes remain in the
/// cache, so a node that later returns occupies its original slot and newly discovered nodes never
/// take a previously assigned position.
pub fn project_with_positions(
    state: &GraphState,
    positions: &mut BTreeMap<String, Point>,
) -> Scene {
    let mut nodes = BTreeMap::<String, Node>::new();
    for (key, node) in &state.nodes {
        if visible_kind(&node.kind) {
            nodes.insert(key.clone(), node.clone());
        }
    }

    let root_key = "controller:local".to_owned();
    let root_operations = operation_summary(state, &root_key);
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
        operation_counts: root_operations.0,
        open_operations: root_operations.1,
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
        let operations = operation_summary(state, key);
        cards.push(Card {
            key: key.clone(),
            kind: node.kind.clone(),
            title: node.label.clone(),
            state: node.state.clone(),
            role: node.role.clone(),
            model: node.model.clone(),
            activity,
            output_tokens,
            operation_counts: operations.0,
            open_operations: operations.1,
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
    layout_stable(&mut cards, positions);

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

fn operation_summary(state: &GraphState, key: &str) -> ([u32; 4], u32) {
    let mut counts = [0; 4];
    let mut open = 0;
    for operation in state.operations_for(key) {
        if let Some(index) = match operation.kind.as_str() {
            "read" => Some(0),
            "write" => Some(1),
            "patch" => Some(2),
            "named_check" => Some(3),
            _ => None,
        } {
            counts[index] += 1;
        }
        if operation.state == "started" {
            open += 1;
        }
    }
    (counts, open)
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

fn layout_stable(cards: &mut [Card], positions: &mut BTreeMap<String, Point>) {
    let mut reserved = positions.values().copied().collect::<Vec<_>>();
    for card in cards {
        if let Some(position) = positions.get(&card.key) {
            card.position = *position;
            continue;
        }

        let mut slot = 0;
        loop {
            let candidate = grid_slot(&card.kind, slot);
            if reserved
                .iter()
                .all(|occupied| !card_rects_overlap(candidate, *occupied))
            {
                card.position = candidate;
                positions.insert(card.key.clone(), candidate);
                reserved.push(candidate);
                break;
            }
            slot += 1;
        }
    }
}

fn grid_slot(kind: &str, slot: i32) -> Point {
    let lane_x = match kind {
        "controller" => 0,
        "composition" => 54,
        "workflow" => 180,
        "workspace" => 306,
        "task" => 432,
        "session" => 684,
        _ => 1_000,
    };
    Point {
        x: lane_x + (slot / ROWS_PER_COLUMN) * (CARD_WIDTH + CARD_X_GAP),
        y: (slot % ROWS_PER_COLUMN) * (CARD_HEIGHT + CARD_Y_GAP),
    }
}

fn card_rects_overlap(left: Point, right: Point) -> bool {
    left.x < right.x + CARD_WIDTH
        && left.x + CARD_WIDTH > right.x
        && left.y < right.y + CARD_HEIGHT
        && left.y + CARD_HEIGHT > right.y
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

    fn node(id: &str, kind: &str, label: &str) -> Node {
        Node {
            id: id.to_owned(),
            kind: kind.to_owned(),
            label: label.to_owned(),
            state: "idle".to_owned(),
            ..Node::default()
        }
    }

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
        state.nodes.insert(
            "operation:op:r1:0".to_owned(),
            Node {
                id: "op:r1:0".to_owned(),
                kind: "operation".to_owned(),
                state: "started".to_owned(),
                raw: serde_json::json!({
                    "run_id":"r1","seq":0,"operation":"read","state":"started"
                }),
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
        assert_eq!(agent.operation_counts, [1, 0, 0, 0]);
        assert_eq!(agent.open_operations, 1);
        assert_eq!(scene.links[0].from, "controller:local");
    }

    #[test]
    fn cached_positions_survive_addition_removal_and_return() {
        let mut positions = BTreeMap::new();
        let mut state = GraphState::default();
        state
            .nodes
            .insert("task:t1".to_owned(), node("t1", "task", "first task"));
        state.nodes.insert(
            "session:s1".to_owned(),
            node("s1", "session", "first session"),
        );

        let first = project_with_positions(&state, &mut positions);
        let controller = first.cards[first.index_of("controller:local").unwrap()].position;
        let task = first.cards[first.index_of("task:t1").unwrap()].position;
        let session = first.cards[first.index_of("session:s1").unwrap()].position;

        state.nodes.remove("task:t1");
        state.nodes.insert(
            "session:s2".to_owned(),
            node("s2", "session", "second session"),
        );
        let second = project_with_positions(&state, &mut positions);
        assert_eq!(
            second.cards[second.index_of("controller:local").unwrap()].position,
            controller
        );
        assert_eq!(
            second.cards[second.index_of("session:s1").unwrap()].position,
            session
        );

        state
            .nodes
            .insert("task:t1".to_owned(), node("t1", "task", "first task"));
        let third = project_with_positions(&state, &mut positions);
        assert_eq!(
            third.cards[third.index_of("task:t1").unwrap()].position,
            task
        );
        assert_eq!(
            third.cards[third.index_of("session:s1").unwrap()].position,
            session
        );
    }

    #[test]
    fn new_cards_use_deterministic_non_overlapping_hierarchical_slots() {
        let mut positions = BTreeMap::new();
        let mut state = GraphState::default();
        for index in 0..12 {
            let id = format!("s{index:02}");
            state.nodes.insert(
                format!("session:{id}"),
                node(&id, "session", &format!("session {index:02}")),
            );
        }
        state
            .nodes
            .insert("task:t1".to_owned(), node("t1", "task", "task"));
        state
            .nodes
            .insert("workflow:w1".to_owned(), node("w1", "workflow", "workflow"));

        let scene = project_with_positions(&state, &mut positions);
        for (index, left) in scene.cards.iter().enumerate() {
            for right in scene.cards.iter().skip(index + 1) {
                assert!(
                    !card_rects_overlap(left.position, right.position),
                    "{} overlaps {}",
                    left.key,
                    right.key
                );
            }
        }

        let workflow_x = scene.cards[scene.index_of("workflow:w1").unwrap()]
            .position
            .x;
        let task_x = scene.cards[scene.index_of("task:t1").unwrap()].position.x;
        let session_x = scene.cards[scene.index_of("session:s00").unwrap()]
            .position
            .x;
        assert!(workflow_x < task_x && task_x < session_x);

        let mut second_positions = BTreeMap::new();
        let second = project_with_positions(&state, &mut second_positions);
        assert_eq!(positions, second_positions);
        assert_eq!(scene.cards, second.cards);
    }
}
