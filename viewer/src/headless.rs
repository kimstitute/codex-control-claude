use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Value, json};

use crate::graph::{Card, Scene};
use crate::model::{GraphState, Timeline};

const INSPECT_NODE_KINDS: [&str; 6] = [
    "session",
    "run",
    "task",
    "workflow",
    "workspace",
    "composition",
];

pub fn inspect_v1(timeline: &Timeline, state: &GraphState, scene: &Scene) -> Value {
    let output_tokens: u64 = state
        .nodes
        .values()
        .filter(|node| node.kind == "run")
        .map(|node| node.output_tokens)
        .sum();
    let nodes = state
        .nodes
        .values()
        .filter(|node| INSPECT_NODE_KINDS.contains(&node.kind.as_str()))
        .count();
    json!({
        "contract": "claude-control.viewer.inspect.v1",
        "version": env!("CARGO_PKG_VERSION"),
        "events": timeline.len(),
        "cursor": timeline.last_cursor(),
        "fidelity": state.fidelity,
        "nodes": nodes,
        "edges": state.edges.len(),
        "cards": scene.cards.len(),
        "agents": scene.cards.iter().filter(|card| card.kind == "session").count(),
        "output_tokens": output_tokens,
    })
}

pub fn render_tree(timeline: &Timeline, state: &GraphState, scene: &Scene) -> String {
    let cards = scene
        .cards
        .iter()
        .map(|card| (card.key.clone(), card))
        .collect::<BTreeMap<_, _>>();
    let mut parents = BTreeMap::<String, (u8, String)>::new();
    for link in &scene.links {
        let Some(priority) = link_priority(&link.kind) else {
            continue;
        };
        if link.to == "controller:local" || !cards.contains_key(&link.to) {
            continue;
        }
        let candidate = (priority, link.from.clone());
        if parents
            .get(&link.to)
            .is_none_or(|current| candidate < *current)
        {
            parents.insert(link.to.clone(), candidate);
        }
    }
    for key in cards
        .keys()
        .filter(|key| key.as_str() != "controller:local")
    {
        parents
            .entry(key.clone())
            .or_insert((u8::MAX, "controller:local".to_owned()));
    }
    let mut children = BTreeMap::<String, Vec<String>>::new();
    for (child, (_, parent)) in parents {
        children.entry(parent).or_default().push(child);
    }
    for listed in children.values_mut() {
        listed.sort_by(|left, right| card_sort(cards[left], cards[right]));
    }

    let mut lines = vec![format!(
        "# claude-control.viewer.tree.v1 cursor={} events={} fidelity={}",
        timeline.last_cursor(),
        timeline.len(),
        state.fidelity
    )];
    let mut seen_cards = BTreeSet::new();
    let mut seen_runs = BTreeSet::new();
    let mut seen_operations = BTreeSet::new();
    render_card(
        "controller:local",
        "",
        true,
        &cards,
        &children,
        state,
        &mut seen_cards,
        &mut seen_runs,
        &mut seen_operations,
        &mut lines,
    );

    for node in state.nodes.values().filter(|node| node.kind == "run") {
        if seen_runs.insert(node.id.clone()) {
            lines.push(format!("+- {}", run_line(node)));
            render_operations(state, &node.id, "   ", &mut seen_operations, &mut lines);
        }
    }
    for operation in state.operations_for("controller:local") {
        if seen_operations.insert(operation.id.clone()) {
            lines.push(format!(
                "+- [operation] #{} {} {}",
                operation.seq, operation.kind, operation.state
            ));
        }
    }
    lines.join("\n") + "\n"
}

#[allow(clippy::too_many_arguments)]
fn render_card(
    key: &str,
    prefix: &str,
    last: bool,
    cards: &BTreeMap<String, &Card>,
    children: &BTreeMap<String, Vec<String>>,
    state: &GraphState,
    seen_cards: &mut BTreeSet<String>,
    seen_runs: &mut BTreeSet<String>,
    seen_operations: &mut BTreeSet<String>,
    lines: &mut Vec<String>,
) {
    let Some(card) = cards.get(key) else {
        return;
    };
    if !seen_cards.insert(key.to_owned()) {
        return;
    }
    let root = key == "controller:local";
    let branch = if root {
        ""
    } else if last {
        "\\- "
    } else {
        "+- "
    };
    lines.push(format!("{prefix}{branch}{}", card_line(card)));
    let child_prefix = if root {
        String::new()
    } else if last {
        format!("{prefix}   ")
    } else {
        format!("{prefix}|  ")
    };
    let listed = children.get(key).cloned().unwrap_or_default();
    for (index, child) in listed.iter().enumerate() {
        render_card(
            child,
            &child_prefix,
            index + 1 == listed.len(),
            cards,
            children,
            state,
            seen_cards,
            seen_runs,
            seen_operations,
            lines,
        );
    }
    if card.kind == "session" {
        let mut runs = state
            .nodes
            .values()
            .filter(|node| {
                node.kind == "run"
                    && node.session_id.as_deref() == Some(card.key.trim_start_matches("session:"))
            })
            .collect::<Vec<_>>();
        runs.sort_by(|left, right| left.id.cmp(&right.id));
        for run in runs {
            if seen_runs.insert(run.id.clone()) {
                lines.push(format!("{child_prefix}+- {}", run_line(run)));
                render_operations(
                    state,
                    &run.id,
                    &format!("{child_prefix}   "),
                    seen_operations,
                    lines,
                );
            }
        }
    }
}

fn render_operations(
    state: &GraphState,
    run_id: &str,
    prefix: &str,
    seen: &mut BTreeSet<String>,
    lines: &mut Vec<String>,
) {
    for operation in state.operations_for(&format!("run:{run_id}")) {
        if seen.insert(operation.id.clone()) {
            let duration = operation
                .duration_seconds()
                .map(|seconds| format!(" {:.0}ms", seconds * 1_000.0))
                .unwrap_or_default();
            lines.push(format!(
                "{prefix}+- [operation] #{} {} {}{}",
                operation.seq, operation.kind, operation.state, duration
            ));
        }
    }
}

fn card_line(card: &Card) -> String {
    let mut parts = vec![format!(
        "[{}] {} state={}",
        card.kind, card.title, card.state
    )];
    if let Some(role) = &card.role {
        parts.push(format!("role={role}"));
    }
    if let Some(model) = &card.model {
        parts.push(format!("model={model}"));
    }
    if card.output_tokens > 0 {
        parts.push(format!("output_tokens={}", card.output_tokens));
    }
    parts.join(" ")
}

fn run_line(node: &crate::model::Node) -> String {
    let mut parts = vec![format!("[run] {} state={}", node.id, node.state)];
    if let Some(effort) = node.raw.get("effort").and_then(Value::as_str) {
        parts.push(format!("effort={effort}"));
    }
    if let Some(models) = node.raw.get("models").and_then(Value::as_array) {
        let names = models.iter().filter_map(Value::as_str).collect::<Vec<_>>();
        if !names.is_empty() {
            parts.push(format!("models={}", names.join(",")));
        }
    }
    if node.output_tokens > 0 {
        parts.push(format!("output_tokens={}", node.output_tokens));
    }
    parts.join(" ")
}

fn link_priority(kind: &str) -> Option<u8> {
    match kind {
        "composition_member" => Some(1),
        "workspace_task" => Some(2),
        "task_run" | "assignment" => Some(3),
        "workflow_run" => Some(4),
        "workspace_run" | "composition_run" => Some(5),
        "root" => Some(100),
        _ => None,
    }
}

fn card_sort(left: &Card, right: &Card) -> std::cmp::Ordering {
    card_rank(&left.kind)
        .cmp(&card_rank(&right.kind))
        .then_with(|| left.title.cmp(&right.title))
        .then_with(|| left.key.cmp(&right.key))
}

fn card_rank(kind: &str) -> u8 {
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph::project;
    use crate::model::Node;

    #[test]
    fn inspect_v1_excludes_additive_operation_nodes() {
        let timeline = Timeline::default();
        let mut state = GraphState {
            fidelity: "complete".into(),
            ..GraphState::default()
        };
        state.nodes.insert(
            "operation:op:r:0".into(),
            Node {
                id: "op:r:0".into(),
                kind: "operation".into(),
                raw: json!({"run_id":"r","seq":0,"operation":"read","state":"started"}),
                ..Node::default()
            },
        );
        let scene = project(&state);
        let value = inspect_v1(&timeline, &state, &scene);
        assert_eq!(value["nodes"], 0);
        assert_eq!(value["contract"], "claude-control.viewer.inspect.v1");
        assert_eq!(timeline.len(), 0);
    }
}
