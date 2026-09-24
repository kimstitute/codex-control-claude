use std::collections::BTreeMap;

use anyhow::{Result, anyhow, bail};
use serde_json::Value;

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Node {
    pub id: String,
    pub kind: String,
    pub label: String,
    pub state: String,
    pub role: Option<String>,
    pub model: Option<String>,
    pub session_id: Option<String>,
    pub active_run_id: Option<String>,
    pub output_tokens: u64,
    pub raw: Value,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Edge {
    pub id: String,
    pub kind: String,
    pub from_kind: String,
    pub from_id: String,
    pub to_kind: String,
    pub to_id: String,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct GraphState {
    pub fidelity: String,
    pub nodes: BTreeMap<String, Node>,
    pub edges: BTreeMap<String, Edge>,
    pub skipped: u64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct OperationRecord {
    pub id: String,
    pub run_id: String,
    pub seq: u64,
    pub kind: String,
    pub state: String,
    pub started: Option<f64>,
    pub finished: Option<f64>,
}

impl OperationRecord {
    pub fn duration_seconds(&self) -> Option<f64> {
        let duration = self.finished? - self.started?;
        duration.is_finite().then_some(duration.max(0.0))
    }
}

impl GraphState {
    pub fn related_run_ids(&self, key: &str) -> std::collections::BTreeSet<String> {
        let mut runs = std::collections::BTreeSet::new();
        if key == "controller:local" {
            runs.extend(
                self.nodes
                    .values()
                    .filter(|node| node.kind == "run")
                    .map(|node| node.id.clone()),
            );
            return runs;
        }
        let Some((kind, id)) = key.split_once(':') else {
            return runs;
        };
        if kind == "session" {
            runs.extend(
                self.nodes
                    .values()
                    .filter(|node| node.kind == "run" && node.session_id.as_deref() == Some(id))
                    .map(|node| node.id.clone()),
            );
        }
        if kind == "run" {
            runs.insert(id.to_owned());
        }
        if let Some(active) = self
            .nodes
            .get(key)
            .and_then(|node| node.active_run_id.as_ref())
        {
            runs.insert(active.clone());
        }
        for edge in self.edges.values() {
            if edge.from_kind == kind && edge.from_id == id && edge.to_kind == "run" {
                runs.insert(edge.to_id.clone());
            }
        }
        runs
    }

    pub fn operations_for(&self, key: &str) -> Vec<OperationRecord> {
        let runs = self.related_run_ids(key);
        let mut operations = self
            .nodes
            .values()
            .filter(|node| node.kind == "operation")
            .filter_map(operation_record)
            .filter(|operation| runs.contains(&operation.run_id))
            .collect::<Vec<_>>();
        operations.sort_by(|left, right| {
            left.started
                .partial_cmp(&right.started)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| left.run_id.cmp(&right.run_id))
                .then_with(|| left.seq.cmp(&right.seq))
        });
        operations
    }
}

#[derive(Clone, Debug)]
pub struct EventRecord {
    pub cursor: u64,
    pub timestamp: f64,
    pub summary: String,
    pub raw: Value,
}

#[derive(Clone, Debug, Default)]
pub struct Timeline {
    events: Vec<EventRecord>,
    checkpoints: BTreeMap<usize, GraphState>,
    live: GraphState,
    last_cursor: u64,
    duplicates: u64,
}

impl Timeline {
    pub fn push(&mut self, raw: Value) -> Result<bool> {
        let cursor = cursor(&raw)?;
        if cursor <= self.last_cursor {
            match self
                .events
                .binary_search_by_key(&cursor, |event| event.cursor)
            {
                Ok(index) if self.events[index].raw == raw => {
                    self.duplicates += 1;
                    return Ok(false);
                }
                Ok(_) => bail!("conflicting observation event for cursor {cursor}"),
                Err(_) => bail!("observation cursor {cursor} predates the loaded history"),
            }
        }
        if self.events.is_empty() && cursor != 1 {
            bail!("observation replay must start at baseline cursor 1, received {cursor}");
        }
        if self.last_cursor > 0 && cursor != self.last_cursor + 1 {
            bail!(
                "observation cursor gap: expected {}, received {cursor}",
                self.last_cursor + 1
            );
        }
        let timestamp = raw.get("timestamp").and_then(Value::as_f64).unwrap_or(0.0);
        let summary = event_summary(&raw);
        apply(&mut self.live, &raw)?;
        self.last_cursor = cursor;
        self.events.push(EventRecord {
            cursor,
            timestamp,
            summary,
            raw,
        });
        let index = self.events.len() - 1;
        if index == 0 || index.is_multiple_of(64) {
            self.checkpoints.insert(index, self.live.clone());
        }
        Ok(true)
    }

    pub fn len(&self) -> usize {
        self.events.len()
    }

    pub fn is_empty(&self) -> bool {
        self.events.is_empty()
    }

    pub fn latest_index(&self) -> usize {
        self.events.len().saturating_sub(1)
    }

    pub fn event(&self, index: usize) -> Option<&EventRecord> {
        self.events.get(index)
    }

    pub fn events(&self) -> &[EventRecord] {
        &self.events
    }

    pub fn last_cursor(&self) -> u64 {
        self.last_cursor
    }

    pub fn live(&self) -> &GraphState {
        &self.live
    }

    pub fn duplicates(&self) -> u64 {
        self.duplicates
    }

    pub fn state_at(&self, index: usize) -> Result<GraphState> {
        if self.events.is_empty() {
            return Ok(GraphState::default());
        }
        let target = index.min(self.latest_index());
        let (start, mut state) = self
            .checkpoints
            .range(..=target)
            .next_back()
            .map(|(index, state)| (*index + 1, state.clone()))
            .unwrap_or((0, GraphState::default()));
        if start <= target {
            for event in &self.events[start..=target] {
                apply(&mut state, &event.raw)?;
            }
        }
        Ok(state)
    }

    pub fn era_starts(&self) -> Vec<usize> {
        let mut starts = vec![0];
        starts.extend(
            self.events
                .iter()
                .enumerate()
                .filter(|(_, event)| is_run_start(&event.raw))
                .map(|(index, _)| index)
                .filter(|index| *index > 0),
        );
        starts.sort_unstable();
        starts.dedup();
        starts
    }

    pub fn previous_era(&self, index: usize) -> usize {
        self.era_starts()
            .into_iter()
            .rfind(|start| *start < index)
            .unwrap_or(0)
    }

    pub fn next_era(&self, index: usize) -> usize {
        self.era_starts()
            .into_iter()
            .find(|start| *start > index)
            .unwrap_or_else(|| self.latest_index())
    }

    pub fn era_ordinal(&self, index: usize) -> (usize, usize) {
        let starts = self.era_starts();
        let ordinal = starts.partition_point(|start| *start <= index).max(1);
        (ordinal, starts.len())
    }
}

fn is_run_start(event: &Value) -> bool {
    event.get("type").and_then(Value::as_str) == Some("RUN_STARTED")
        || (event.get("type").and_then(Value::as_str) == Some("CUSTOM")
            && event.pointer("/value/kind").and_then(Value::as_str) == Some("node_created")
            && event.pointer("/value/entity_kind").and_then(Value::as_str) == Some("run"))
}

fn operation_record(node: &Node) -> Option<OperationRecord> {
    let raw = node.raw.as_object()?;
    Some(OperationRecord {
        id: node.id.clone(),
        run_id: raw.get("run_id")?.as_str()?.to_owned(),
        seq: raw.get("seq")?.as_u64()?,
        kind: raw.get("operation")?.as_str()?.to_owned(),
        state: raw.get("state")?.as_str()?.to_owned(),
        started: raw.get("started").and_then(Value::as_f64),
        finished: raw.get("finished").and_then(Value::as_f64),
    })
}

fn cursor(event: &Value) -> Result<u64> {
    event
        .pointer("/metadata/claude-control/cursor")
        .and_then(Value::as_u64)
        .ok_or_else(|| anyhow!("AG-UI event has no positive Claude Control cursor"))
}

fn event_summary(event: &Value) -> String {
    match event
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or("UNKNOWN")
    {
        "CUSTOM" => {
            let kind = event
                .pointer("/value/kind")
                .and_then(Value::as_str)
                .unwrap_or("custom");
            let entity = event
                .pointer("/value/entity_kind")
                .and_then(Value::as_str)
                .unwrap_or("entity");
            format!("{kind} · {entity}")
        }
        other => other.replace('_', " ").to_lowercase(),
    }
}

fn apply(state: &mut GraphState, event: &Value) -> Result<()> {
    let kind = event.get("type").and_then(Value::as_str).unwrap_or("");
    match kind {
        "CUSTOM" => apply_custom(state, event.get("value").unwrap_or(&Value::Null)),
        "RUN_STARTED" => apply_run_lifecycle(state, event, "running"),
        "RUN_FINISHED" => apply_run_lifecycle(state, event, "completed"),
        "RUN_ERROR" => {
            let terminal = event
                .get("code")
                .and_then(Value::as_str)
                .unwrap_or("failed");
            apply_run_lifecycle(state, event, terminal)
        }
        "STATE_SNAPSHOT" => apply_snapshot(state, event.get("snapshot").unwrap_or(&Value::Null)),
        _ => {
            state.skipped += 1;
            Ok(())
        }
    }
}

fn apply_custom(state: &mut GraphState, value: &Value) -> Result<()> {
    let kind = value.get("kind").and_then(Value::as_str).unwrap_or("");
    let entity_kind = value
        .get("entity_kind")
        .and_then(Value::as_str)
        .unwrap_or("");
    let entity_id = value.get("entity_id").and_then(Value::as_str).unwrap_or("");
    let payload = value.get("payload").unwrap_or(&Value::Null);
    match kind {
        "baseline" => apply_snapshot(state, payload),
        "node_created" | "node_state" => {
            upsert_node(state, entity_kind, entity_id, payload);
            Ok(())
        }
        "edge_created" => {
            upsert_edge(state, entity_kind, entity_id, payload);
            Ok(())
        }
        "run_telemetry" => {
            apply_telemetry(state, entity_id, payload);
            Ok(())
        }
        _ => {
            state.skipped += 1;
            Ok(())
        }
    }
}

fn apply_snapshot(state: &mut GraphState, snapshot: &Value) -> Result<()> {
    let nodes = snapshot.get("nodes").and_then(Value::as_object);
    let edges = snapshot.get("edges").and_then(Value::as_object);
    let Some(nodes) = nodes else {
        bail!("observation snapshot has no node groups")
    };
    state.nodes.clear();
    state.edges.clear();
    state.fidelity = snapshot
        .get("fidelity")
        .and_then(Value::as_str)
        .unwrap_or("exact")
        .to_owned();
    for (kind, group) in nodes {
        if let Some(items) = group.as_array() {
            for item in items {
                let id = item.get("id").and_then(Value::as_str).unwrap_or("");
                upsert_node(state, kind, id, item);
            }
        } else if let Some(items) = group.as_object() {
            for (id, item) in items {
                upsert_node(state, kind, id, item);
            }
        }
    }
    if let Some(edges) = edges {
        for (kind, group) in edges {
            if let Some(items) = group.as_array() {
                for item in items {
                    let id = item
                        .get("id")
                        .and_then(Value::as_str)
                        .filter(|id| !id.is_empty())
                        .map(ToOwned::to_owned)
                        .unwrap_or_else(|| edge_identity(kind, item));
                    upsert_edge(state, kind, &id, item);
                }
            } else if let Some(items) = group.as_object() {
                for (id, item) in items {
                    upsert_edge(state, kind, id, item);
                }
            }
        }
    }
    if let Some(items) = snapshot.get("telemetry").and_then(Value::as_array) {
        for item in items {
            if let Some(run_id) = item.get("run_id").and_then(Value::as_str) {
                apply_telemetry(state, run_id, item);
            }
        }
    } else if let Some(items) = snapshot.get("telemetry").and_then(Value::as_object) {
        for (run_id, item) in items {
            apply_telemetry(state, run_id, item);
        }
    }
    Ok(())
}

fn upsert_node(state: &mut GraphState, kind: &str, entity_id: &str, payload: &Value) {
    if kind.is_empty() || entity_id.is_empty() || !payload.is_object() {
        state.skipped += 1;
        return;
    }
    let key = format!("{kind}:{entity_id}");
    let label =
        string(payload, &["name", "label"]).unwrap_or_else(|| short_id(entity_id).to_owned());
    let model = string(payload, &["model"]).or_else(|| {
        payload
            .get("models")
            .and_then(Value::as_array)
            .and_then(|models| models.iter().filter_map(Value::as_str).next_back())
            .map(ToOwned::to_owned)
    });
    let existing_tokens = state
        .nodes
        .get(&key)
        .map(|node| node.output_tokens)
        .unwrap_or(0);
    state.nodes.insert(
        key,
        Node {
            id: entity_id.to_owned(),
            kind: kind.to_owned(),
            label,
            state: string(payload, &["state", "status"]).unwrap_or_else(|| "idle".to_owned()),
            role: string(payload, &["role"]),
            model,
            session_id: string(payload, &["session_id"]),
            active_run_id: string(payload, &["active_run_id"]),
            output_tokens: existing_tokens,
            raw: payload.clone(),
        },
    );
}

fn upsert_edge(state: &mut GraphState, kind: &str, entity_id: &str, payload: &Value) {
    let from_kind = string(payload, &["from_kind"]);
    let from_id = string(payload, &["from_id"]);
    let to_kind = string(payload, &["to_kind"]);
    let to_id = string(payload, &["to_id"]);
    let (Some(from_kind), Some(from_id), Some(to_kind), Some(to_id)) =
        (from_kind, from_id, to_kind, to_id)
    else {
        state.skipped += 1;
        return;
    };
    let id = if entity_id.is_empty() {
        edge_identity(kind, payload)
    } else {
        entity_id.to_owned()
    };
    state.edges.insert(
        format!("{kind}:{id}"),
        Edge {
            id,
            kind: kind.to_owned(),
            from_kind,
            from_id,
            to_kind,
            to_id,
        },
    );
}

fn apply_run_lifecycle(state: &mut GraphState, event: &Value, run_state: &str) -> Result<()> {
    let run_id = event
        .get("runId")
        .and_then(Value::as_str)
        .or_else(|| {
            event
                .pointer("/metadata/claude-control/runId")
                .and_then(Value::as_str)
        })
        .ok_or_else(|| anyhow!("run lifecycle event has no run id"))?;
    let session_id = event
        .get("threadId")
        .and_then(Value::as_str)
        .or_else(|| {
            event
                .pointer("/metadata/claude-control/threadId")
                .and_then(Value::as_str)
        })
        .unwrap_or("");
    let key = format!("run:{run_id}");
    let mut raw = state
        .nodes
        .get(&key)
        .map(|node| node.raw.clone())
        .unwrap_or_else(|| serde_json::json!({"id": run_id}));
    if let Some(object) = raw.as_object_mut() {
        object.insert("state".to_owned(), Value::String(run_state.to_owned()));
        if !session_id.is_empty() {
            object.insert(
                "session_id".to_owned(),
                Value::String(session_id.to_owned()),
            );
        }
    }
    upsert_node(state, "run", run_id, &raw);
    if let Some(usage) = event.get("usage") {
        apply_telemetry(state, run_id, &serde_json::json!({"usage": usage}));
    }
    Ok(())
}

fn apply_telemetry(state: &mut GraphState, run_id: &str, payload: &Value) {
    let key = format!("run:{run_id}");
    if let Some(run) = state.nodes.get_mut(&key) {
        run.output_tokens = token_count(payload);
        if let Some(object) = run.raw.as_object_mut() {
            object.insert("telemetry".to_owned(), payload.clone());
        }
    }
}

fn token_count(value: &Value) -> u64 {
    let direct = value
        .pointer("/usage/output_tokens")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    if direct > 0 {
        return direct;
    }
    value
        .get("usage")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.get("outputTokens").and_then(Value::as_u64))
                .sum()
        })
        .unwrap_or(0)
}

fn edge_identity(kind: &str, payload: &Value) -> String {
    format!(
        "{}>{}:{}>{}",
        payload
            .get("from_kind")
            .and_then(Value::as_str)
            .unwrap_or(""),
        payload.get("from_id").and_then(Value::as_str).unwrap_or(""),
        payload
            .get("to_kind")
            .and_then(Value::as_str)
            .unwrap_or(kind),
        payload.get("to_id").and_then(Value::as_str).unwrap_or("")
    )
}

fn string(value: &Value, names: &[&str]) -> Option<String> {
    names
        .iter()
        .find_map(|name| value.get(*name).and_then(Value::as_str))
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn short_id(value: &str) -> &str {
    value.get(..8).unwrap_or(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn custom(cursor: u64, kind: &str, entity_kind: &str, id: &str, payload: Value) -> Value {
        serde_json::json!({
            "type":"CUSTOM",
            "name":"claude_control_observation",
            "value":{
                "cursor":cursor,"kind":kind,"entity_kind":entity_kind,
                "entity_id":id,"effective_at":cursor as f64,"payload":payload
            },
            "timestamp":cursor as f64,
            "metadata":{"claude-control":{"cursor":cursor}}
        })
    }

    #[test]
    fn timeline_replays_each_cursor_and_ignores_duplicates() {
        let mut timeline = Timeline::default();
        timeline
            .push(custom(
                1,
                "baseline",
                "store",
                "baseline",
                serde_json::json!({"fidelity":"exact","nodes":{},"edges":{},"telemetry":[]}),
            ))
            .unwrap();
        timeline
            .push(custom(
                2,
                "node_created",
                "session",
                "s1",
                serde_json::json!({"id":"s1","name":"worker","state":"running"}),
            ))
            .unwrap();
        timeline
            .push(custom(
                3,
                "node_state",
                "session",
                "s1",
                serde_json::json!({"id":"s1","name":"worker","state":"completed"}),
            ))
            .unwrap();

        assert!(!timeline.push(timeline.events[2].raw.clone()).unwrap());
        assert_eq!(
            timeline.state_at(1).unwrap().nodes["session:s1"].state,
            "running"
        );
        assert_eq!(
            timeline.state_at(2).unwrap().nodes["session:s1"].state,
            "completed"
        );
        assert_eq!(timeline.duplicates(), 1);
    }

    #[test]
    fn cursor_gaps_fail_closed() {
        let mut timeline = Timeline::default();
        timeline
            .push(custom(
                1,
                "baseline",
                "store",
                "baseline",
                serde_json::json!({"nodes":{},"edges":{}}),
            ))
            .unwrap();
        assert!(
            timeline
                .push(custom(3, "unknown", "x", "x", serde_json::json!({})))
                .is_err()
        );
    }

    #[test]
    fn conflicting_duplicate_and_midstream_start_fail_closed() {
        let mut timeline = Timeline::default();
        assert!(
            timeline
                .push(custom(
                    2,
                    "node_created",
                    "session",
                    "s1",
                    serde_json::json!({})
                ))
                .is_err()
        );
        let baseline = custom(
            1,
            "baseline",
            "store",
            "baseline",
            serde_json::json!({"fidelity":"exact","nodes":{},"edges":{},"telemetry":[]}),
        );
        timeline.push(baseline).unwrap();
        assert!(
            timeline
                .push(custom(
                    1,
                    "baseline",
                    "store",
                    "baseline",
                    serde_json::json!({"fidelity":"baseline_only","nodes":{},"edges":{},"telemetry":[]}),
                ))
                .is_err()
        );
    }

    #[test]
    fn replay_at_checkpoint_does_not_apply_it_twice_or_slice_past_target() {
        let mut timeline = Timeline::default();
        for cursor in 1..=65 {
            let event = if cursor == 1 {
                custom(
                    cursor,
                    "baseline",
                    "store",
                    "baseline",
                    serde_json::json!({"fidelity":"exact","nodes":{},"edges":{},"telemetry":[]}),
                )
            } else {
                custom(
                    cursor,
                    "node_state",
                    "session",
                    "s1",
                    serde_json::json!({"id":"s1","name":"worker","state":format!("s{cursor}")}),
                )
            };
            timeline.push(event).unwrap();
        }
        assert_eq!(
            timeline.state_at(64).unwrap().nodes["session:s1"].state,
            "s65"
        );
    }

    #[test]
    fn run_starts_define_deterministic_era_navigation() {
        let mut timeline = Timeline::default();
        timeline
            .push(custom(
                1,
                "baseline",
                "store",
                "baseline",
                serde_json::json!({"fidelity":"exact","nodes":{},"edges":{},"telemetry":[]}),
            ))
            .unwrap();
        timeline
            .push(custom(
                2,
                "node_created",
                "run",
                "r1",
                serde_json::json!({"id":"r1","session_id":"s1","state":"pending"}),
            ))
            .unwrap();
        timeline
            .push(custom(
                3,
                "node_state",
                "run",
                "r1",
                serde_json::json!({"id":"r1","session_id":"s1","state":"completed"}),
            ))
            .unwrap();
        timeline
            .push(custom(
                4,
                "node_created",
                "run",
                "r2",
                serde_json::json!({"id":"r2","session_id":"s1","state":"pending"}),
            ))
            .unwrap();

        assert_eq!(timeline.era_starts(), vec![0, 1, 3]);
        assert_eq!(timeline.next_era(1), 3);
        assert_eq!(timeline.previous_era(3), 1);
        assert_eq!(timeline.era_ordinal(2), (2, 3));
    }

    #[test]
    fn operation_history_is_related_through_runs_without_exposing_bodies() {
        let mut state = GraphState::default();
        state.nodes.insert(
            "run:r1".into(),
            Node {
                id: "r1".into(),
                kind: "run".into(),
                session_id: Some("s1".into()),
                ..Node::default()
            },
        );
        state.nodes.insert(
            "operation:op:r1:2".into(),
            Node {
                id: "op:r1:2".into(),
                kind: "operation".into(),
                state: "settled".into(),
                raw: serde_json::json!({
                    "node":"operation","id":"op:r1:2","run_id":"r1","seq":2,
                    "operation":"patch","state":"settled","started":1.0,"finished":1.25
                }),
                ..Node::default()
            },
        );

        let operations = state.operations_for("session:s1");
        assert_eq!(operations.len(), 1);
        assert_eq!(operations[0].kind, "patch");
        assert_eq!(operations[0].duration_seconds(), Some(0.25));
    }
}
