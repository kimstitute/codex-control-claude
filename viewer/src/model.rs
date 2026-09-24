use std::collections::{BTreeMap, BTreeSet};

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

#[derive(Clone, Debug, Default, PartialEq)]
pub struct DetailSource {
    pub selector: String,
    pub provider: String,
    pub session_id: Option<String>,
    pub title: Option<String>,
    pub project: Option<String>,
    pub mode: Option<String>,
    pub permission_mode: Option<String>,
    pub last_prompt: Option<String>,
    pub queued_ops: u64,
    pub file_edits: u64,
    pub partial: bool,
    pub truncated: bool,
    pub read_error: Option<String>,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct AgentDetail {
    pub key: String,
    pub agent_id: String,
    pub parent_key: Option<String>,
    pub run_id: Option<String>,
    pub session_id: Option<String>,
    pub role: Option<String>,
    pub model: Option<String>,
    pub description: Option<String>,
    pub prompt: Option<String>,
    pub reasoning: Option<String>,
    pub started: Option<f64>,
    pub finished: Option<f64>,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct ToolDetail {
    pub id: String,
    pub owner_key: String,
    pub name: String,
    pub summary: Option<String>,
    pub state: String,
    pub started: Option<f64>,
    pub finished: Option<f64>,
}

impl ToolDetail {
    pub fn duration_seconds(&self, cutoff: Option<f64>) -> Option<f64> {
        let start = self.started?;
        let end = match (self.finished, cutoff) {
            (Some(finished), Some(limit)) => finished.min(limit),
            (Some(finished), None) => finished,
            (None, Some(limit)) => limit,
            (None, None) => return None,
        };
        let duration = end - start;
        duration.is_finite().then_some(duration.max(0.0))
    }

    pub fn state_at(&self, cutoff: Option<f64>) -> &str {
        if cutoff.is_some_and(|limit| self.finished.is_none_or(|finished| finished > limit)) {
            "pending"
        } else {
            &self.state
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct SemanticDetail {
    pub kind: String,
    pub owner_key: Option<String>,
    pub timestamp: f64,
    pub summary: String,
    pub text: Option<String>,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct DetailStore {
    pub source: DetailSource,
    pub agents: BTreeMap<String, AgentDetail>,
    pub tools: Vec<ToolDetail>,
    pub events: Vec<SemanticDetail>,
}

impl DetailStore {
    pub fn apply_snapshot(&mut self, value: &Value) -> Result<()> {
        if value.get("type").and_then(Value::as_str) != Some("CLAUDE_CONTROL_DETAIL_SNAPSHOT") {
            bail!("unsupported local detail snapshot")
        }
        if value.get("version").and_then(Value::as_u64) != Some(1) {
            bail!("unsupported local detail snapshot version")
        }
        let source = value
            .get("source")
            .and_then(Value::as_object)
            .ok_or_else(|| anyhow!("local detail snapshot has no source"))?;
        self.source = DetailSource {
            selector: object_string(source, "selector").unwrap_or_default(),
            provider: object_string(source, "provider").unwrap_or_default(),
            session_id: object_string(source, "session_id"),
            title: object_string(source, "title"),
            project: object_string(source, "project"),
            mode: object_string(source, "mode"),
            permission_mode: object_string(source, "permission_mode"),
            last_prompt: object_string(source, "last_prompt"),
            queued_ops: object_u64(source, "queued_ops"),
            file_edits: object_u64(source, "file_edits"),
            partial: source
                .get("partial")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            truncated: source
                .get("truncated")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            read_error: object_string(source, "read_error"),
        };
        self.agents.clear();
        for item in value
            .get("agents")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let Some(object) = item.as_object() else {
                continue;
            };
            let Some(key) = object_string(object, "key").filter(|value| !value.is_empty()) else {
                continue;
            };
            self.agents.insert(
                key.clone(),
                AgentDetail {
                    key,
                    agent_id: object_string(object, "agent_id").unwrap_or_default(),
                    parent_key: object_string(object, "parent_key"),
                    run_id: object_string(object, "run_id"),
                    session_id: object_string(object, "session_id"),
                    role: object_string(object, "role"),
                    model: object_string(object, "model"),
                    description: object_string(object, "description"),
                    prompt: object_string(object, "prompt"),
                    reasoning: object_string(object, "reasoning"),
                    started: object_f64(object, "started"),
                    finished: object_f64(object, "finished"),
                },
            );
        }
        self.tools = value
            .get("tools")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|item| {
                let object = item.as_object()?;
                let id = object_string(object, "id")?;
                let owner_key = object_string(object, "owner_key")?;
                let name = object_string(object, "name")?;
                Some(ToolDetail {
                    id,
                    owner_key,
                    name,
                    summary: object_string(object, "summary"),
                    state: object_string(object, "state").unwrap_or_else(|| "pending".into()),
                    started: object_f64(object, "started"),
                    finished: object_f64(object, "finished"),
                })
            })
            .collect();
        self.tools.sort_by(|left, right| {
            left.started
                .unwrap_or(0.0)
                .total_cmp(&right.started.unwrap_or(0.0))
                .then_with(|| left.id.cmp(&right.id))
        });
        self.events = value
            .get("events")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|item| {
                let object = item.as_object()?;
                let timestamp = object_f64(object, "timestamp")?;
                Some(SemanticDetail {
                    kind: object_string(object, "kind")?,
                    owner_key: object_string(object, "owner_key"),
                    timestamp,
                    summary: object_string(object, "summary").unwrap_or_default(),
                    text: object_string(object, "text"),
                })
            })
            .collect();
        self.events.sort_by(|left, right| {
            left.timestamp
                .total_cmp(&right.timestamp)
                .then_with(|| left.kind.cmp(&right.kind))
        });
        Ok(())
    }

    pub fn owner_keys(&self, state: &GraphState, key: &str) -> BTreeSet<String> {
        let mut keys = BTreeSet::from([key.to_owned()]);
        for run_id in state.related_run_ids(key) {
            keys.insert(format!("run:{run_id}"));
        }
        if let Some((kind, id)) = key.split_once(':')
            && kind == "session"
        {
            keys.insert(id.to_owned());
        }
        keys
    }

    pub fn agents_for<'a>(&'a self, keys: &BTreeSet<String>) -> Vec<&'a AgentDetail> {
        self.agents
            .values()
            .filter(|agent| {
                keys.contains(&agent.key)
                    || agent
                        .run_id
                        .as_ref()
                        .is_some_and(|id| keys.contains(&format!("run:{id}")))
                    || agent
                        .session_id
                        .as_ref()
                        .is_some_and(|id| keys.contains(&format!("session:{id}")))
            })
            .collect()
    }

    pub fn tools_for<'a>(
        &'a self,
        keys: &BTreeSet<String>,
        cutoff: Option<f64>,
    ) -> Vec<&'a ToolDetail> {
        self.tools
            .iter()
            .filter(|tool| keys.contains(&tool.owner_key))
            .filter(|tool| cutoff.is_none_or(|limit| tool.started.is_none_or(|ts| ts <= limit)))
            .collect()
    }

    pub fn events_for<'a>(
        &'a self,
        keys: &BTreeSet<String>,
        cutoff: Option<f64>,
    ) -> Vec<&'a SemanticDetail> {
        self.events
            .iter()
            .filter(|event| {
                event
                    .owner_key
                    .as_ref()
                    .is_none_or(|owner| keys.contains(owner))
            })
            .filter(|event| cutoff.is_none_or(|limit| event.timestamp <= limit))
            .collect()
    }

    pub fn latest_text<'a>(
        &'a self,
        keys: &BTreeSet<String>,
        kinds: &[&str],
        cutoff: Option<f64>,
    ) -> Option<&'a str> {
        self.events
            .iter()
            .rev()
            .find(|event| {
                kinds.contains(&event.kind.as_str())
                    && event.text.is_some()
                    && event
                        .owner_key
                        .as_ref()
                        .is_none_or(|owner| keys.contains(owner))
                    && cutoff.is_none_or(|limit| event.timestamp <= limit)
            })
            .and_then(|event| event.text.as_deref())
    }
}

fn object_string(object: &serde_json::Map<String, Value>, name: &str) -> Option<String> {
    object
        .get(name)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn object_u64(object: &serde_json::Map<String, Value>, name: &str) -> u64 {
    object.get(name).and_then(Value::as_u64).unwrap_or(0)
}

fn object_f64(object: &serde_json::Map<String, Value>, name: &str) -> Option<f64> {
    object
        .get(name)
        .and_then(Value::as_f64)
        .filter(|value| value.is_finite())
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

    pub fn run_era_starts(&self) -> Vec<usize> {
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

    pub fn prompt_era_starts(&self) -> Vec<usize> {
        let starts = self
            .events
            .iter()
            .enumerate()
            .filter(|(_, event)| semantic_kind(&event.raw) == Some("prompt"))
            .map(|(index, _)| index)
            .collect::<Vec<_>>();
        if starts.is_empty() { vec![0] } else { starts }
    }

    pub fn previous_prompt(&self, index: usize) -> usize {
        self.prompt_era_starts()
            .into_iter()
            .rfind(|start| *start < index)
            .unwrap_or(0)
    }

    pub fn next_prompt(&self, index: usize) -> usize {
        self.prompt_era_starts()
            .into_iter()
            .find(|start| *start > index)
            .unwrap_or_else(|| self.latest_index())
    }

    pub fn previous_run(&self, index: usize) -> usize {
        self.run_era_starts()
            .into_iter()
            .rfind(|start| *start < index)
            .unwrap_or(0)
    }

    pub fn next_run(&self, index: usize) -> usize {
        self.run_era_starts()
            .into_iter()
            .find(|start| *start > index)
            .unwrap_or_else(|| self.latest_index())
    }

    pub fn run_era_ordinal(&self, index: usize) -> (usize, usize) {
        let starts = self.run_era_starts();
        let ordinal = starts.partition_point(|start| *start <= index).max(1);
        (ordinal, starts.len())
    }

    pub fn find(&self, query: &str, from: usize, forward: bool) -> Option<usize> {
        let matches = self.matching_indices(query);
        if matches.is_empty() {
            return None;
        }
        if forward {
            matches
                .iter()
                .copied()
                .find(|index| *index > from)
                .or_else(|| matches.first().copied())
        } else {
            matches
                .iter()
                .rev()
                .copied()
                .find(|index| *index < from)
                .or_else(|| matches.last().copied())
        }
    }

    pub fn matching_indices(&self, query: &str) -> Vec<usize> {
        let query = query.trim().to_lowercase();
        if query.is_empty() {
            return Vec::new();
        }
        self.events
            .iter()
            .enumerate()
            .filter(|(_, event)| {
                event.summary.to_lowercase().contains(&query)
                    || event
                        .raw
                        .pointer("/value/payload/summary")
                        .and_then(Value::as_str)
                        .is_some_and(|value| value.to_lowercase().contains(&query))
            })
            .map(|(index, _)| index)
            .collect()
    }

    pub fn nearest_index(&self, timestamp: f64) -> Option<usize> {
        self.events
            .iter()
            .enumerate()
            .min_by(|(_, left), (_, right)| {
                (left.timestamp - timestamp)
                    .abs()
                    .total_cmp(&(right.timestamp - timestamp).abs())
            })
            .map(|(index, _)| index)
    }

    pub fn has_local_markers(&self) -> bool {
        self.events.iter().any(|event| {
            event.raw.pointer("/value/kind").and_then(Value::as_str) == Some("local_marker")
        })
    }
}

fn is_run_start(event: &Value) -> bool {
    event.get("type").and_then(Value::as_str) == Some("RUN_STARTED")
        || (event.get("type").and_then(Value::as_str) == Some("CUSTOM")
            && event.pointer("/value/kind").and_then(Value::as_str) == Some("node_created")
            && event.pointer("/value/entity_kind").and_then(Value::as_str) == Some("run"))
}

fn semantic_kind(event: &Value) -> Option<&str> {
    (event.get("type").and_then(Value::as_str) == Some("CUSTOM")
        && event.pointer("/value/kind").and_then(Value::as_str) == Some("local_marker"))
    .then(|| event.pointer("/value/entity_kind").and_then(Value::as_str))
    .flatten()
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
            if let Some(kind) = semantic_kind(event) {
                return kind.replace('_', " ");
            }
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
        "local_marker" => Ok(()),
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

        assert_eq!(timeline.run_era_starts(), vec![0, 1, 3]);
        assert_eq!(timeline.next_run(1), 3);
        assert_eq!(timeline.previous_run(3), 1);
        assert_eq!(timeline.run_era_ordinal(2), (2, 3));
    }

    #[test]
    fn local_markers_drive_prompt_navigation_and_search_without_mutating_graph() {
        let mut timeline = Timeline::default();
        timeline
            .push(custom(
                1,
                "baseline",
                "store",
                "baseline",
                serde_json::json!({"fidelity":"local_detail","nodes":{},"edges":{},"telemetry":[]}),
            ))
            .unwrap();
        for (cursor, kind, summary) in [
            (2, "prompt", "first prompt"),
            (3, "tool_start", "Read"),
            (4, "prompt", "second prompt"),
        ] {
            timeline
                .push(serde_json::json!({
                    "type":"CUSTOM",
                    "name":"claude-control.local-detail",
                    "value":{
                        "kind":"local_marker","entity_kind":kind,
                        "entity_id":format!("m-{cursor}"),
                        "payload":{"kind":kind,"summary":summary}
                    },
                    "timestamp":cursor as f64,
                    "metadata":{"claude-control":{"cursor":cursor}}
                }))
                .unwrap();
        }
        assert_eq!(timeline.prompt_era_starts(), vec![1, 3]);
        assert_eq!(timeline.next_prompt(1), 3);
        assert_eq!(timeline.previous_prompt(3), 1);
        assert_eq!(timeline.find("read", 0, true), Some(2));
        assert_eq!(timeline.state_at(3).unwrap().skipped, 0);
    }

    #[test]
    fn detail_snapshot_indexes_agents_tools_and_semantic_events() {
        let mut detail = DetailStore::default();
        detail
            .apply_snapshot(&serde_json::json!({
                "type":"CLAUDE_CONTROL_DETAIL_SNAPSHOT","version":1,
                "source":{"selector":"codex:s1","provider":"codex","session_id":"s1","project":"/work","queued_ops":1,"file_edits":2,"partial":false,"truncated":false},
                "agents":[{"key":"session:s1","agent_id":"s1","session_id":"s1","role":"reviewer","model":"gpt-test","prompt":"p","reasoning":"r"}],
                "tools":[{"id":"t1","owner_key":"session:s1","name":"read","state":"ok","started":1.0,"finished":2.0}],
                "events":[
                    {"kind":"prompt","owner_key":"session:s1","timestamp":1.0,"summary":"prompt","text":"p"},
                    {"kind":"tool_end","owner_key":"session:s1","timestamp":2.0,"summary":"read"}
                ]
            }))
            .unwrap();
        let keys = BTreeSet::from(["session:s1".to_owned()]);
        assert_eq!(detail.source.project.as_deref(), Some("/work"));
        assert_eq!(detail.agents_for(&keys).len(), 1);
        assert_eq!(
            detail.tools_for(&keys, Some(2.0))[0].duration_seconds(None),
            Some(1.0)
        );
        let tool = detail.tools_for(&keys, Some(1.5))[0];
        assert_eq!(tool.state_at(Some(1.5)), "pending");
        assert_eq!(tool.duration_seconds(Some(1.5)), Some(0.5));
        assert_eq!(tool.state_at(Some(2.0)), "ok");
        assert_eq!(detail.events_for(&keys, Some(2.0))[1].kind, "tool_end");
        assert_eq!(detail.latest_text(&keys, &["prompt"], Some(1.0)), Some("p"));
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
