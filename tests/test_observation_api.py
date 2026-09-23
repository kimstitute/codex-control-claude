"""Bounded cursor queries and deterministic replay over the schema-14 observation ledger."""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import observation, schema  # noqa: E402
from claude_control.store import SCHEMA as BASE_SCHEMA  # noqa: E402
from claude_control.store import ControlError  # noqa: E402

# Schema 14 is built here from the committed builders in Store.initialize's order
# rather than through Store.initialize itself: this suite exercises the ledger, not
# host identity, so it must not depend on /etc/machine-id or a private state directory.
BUILDERS = (
    schema.add_task_schema,
    schema.add_queue_schema,
    schema.add_message_schema,
    schema.add_workflow_schema,
    schema.add_workspace_schema,
    schema.add_execution_schema,
    schema.add_composition_schema,
    schema.add_telemetry_schema,
    schema.add_application_schema,
    schema.add_platform_schema,
    schema.add_observation_schema,
)

# The exact ledger a populate() produces, in cursor order, including the baseline.
EXPECTED = (
    ("baseline", "store"),
    ("node_created", "session"),
    ("node_created", "run"),
    ("node_created", "task"),
    ("node_created", "task"),
    ("edge_created", "dependency"),
    ("node_state", "run"),
    ("node_state", "task"),
    ("run_telemetry", "run"),
)
TOTAL = len(EXPECTED)
NODE_KINDS = ["composition", "run", "session", "task", "workflow", "workspace"]
EDGE_KINDS = ["binding", "dependency", "review"]


def ledger_connection():
    """Only the observation ledger, for hand-built histories."""
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript(schema.OBSERVATION_LEDGER)
    return db


def control_connection():
    """A whole schema-14 database, so the committed triggers append the events."""
    # Autocommit matters: the migration ends in the baseline INSERT, and an open
    # implicit transaction would silently defer the foreign key pragma below.
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript(BASE_SCHEMA)
    for build in BUILDERS:
        build(db)
    db.execute("PRAGMA foreign_keys=ON")
    return db


class Ledger:
    """The minimal store surface the module uses, over an isolated database."""

    def __init__(self, connection=None, version=14):
        self.config = {"schema": version}
        self.connection = ledger_connection() if connection is None else connection

    @contextmanager
    def db(self, write=False):
        yield self.connection

    def append(self, kind, entity_kind, entity_id, payload, contract=schema.OBSERVATION_CONTRACT):
        self.connection.execute(
            "INSERT INTO observation_events(contract,kind,entity_kind,entity_id,"
            "recorded_at,effective_at,payload) VALUES(?,?,?,?,1.0,1.0,?)",
            (
                contract,
                kind,
                entity_kind,
                entity_id,
                payload if isinstance(payload, str) else json.dumps(payload),
            ),
        )
        return self

    def seeded(self):
        return self.append("baseline", "store", "baseline", baseline_payload())


def observed_store():
    return Ledger(control_connection())


def populate(store):
    """Perform one mutation per folded event kind, in a fixed and observable order."""
    ids = {key: str(uuid.uuid4()) for key in ("session", "backend", "run", "worker", "upstream")}
    with store.db(write=True) as db:
        db.execute(
            "INSERT INTO sessions(id,name,backend_id,model,role,project,created)"
            " VALUES(?,'observed',?,'sonnet','executor','/observed',1.0)",
            (ids["session"], ids["backend"]),
        )
        db.execute(
            "INSERT INTO runs(id,session_id,request_id,fingerprint,status,created,resume,"
            "timeout,backend_id) VALUES(?,?,'request','fingerprint','pending',1.0,0,10.0,?)",
            (ids["run"], ids["session"], ids["backend"]),
        )
        for key, name in (("worker", "worker-node"), ("upstream", "upstream-node")):
            db.execute(
                "INSERT INTO tasks(id,name,session_id,current_revision,created)"
                " VALUES(?,?,?,1,1.0)",
                (ids[key], name, ids["session"]),
            )
            db.execute(
                "INSERT INTO task_revisions(task_id,revision,prompt,prompt_sha256,"
                "acknowledge_context,created) VALUES(?,1,'prompt','sha',0,1.0)",
                (ids[key],),
            )
        db.execute(
            "INSERT INTO dependency_sets(task_id,revision,fingerprint) VALUES(?,1,'fp')",
            (ids["worker"],),
        )
        db.execute(
            "INSERT INTO task_dependencies(child_task_id,child_revision,parent_task_id,"
            "parent_revision) VALUES(?,1,?,1)",
            (ids["worker"], ids["upstream"]),
        )
        db.execute("UPDATE runs SET status='completed',finished=2.0 WHERE id=?", (ids["run"],))
        db.execute("UPDATE tasks SET active_run_id=? WHERE id=?", (ids["run"], ids["worker"]))
        db.execute(
            "INSERT INTO run_telemetry(run_id,usage,model_usage,provider_cost_usd,"
            "duration_api_ms,duration_ms,created) VALUES(?,?,?,0.25,120.0,300.0,1.0)",
            (
                ids["run"],
                '{"input_tokens":10,"output_tokens":5}',
                '{"sonnet":{"input_tokens":10}}',
            ),
        )
    return ids


def baseline_payload():
    return {
        "fidelity": "baseline_only",
        "nodes": {kind: [] for kind in NODE_KINDS},
        "edges": {kind: [] for kind in EDGE_KINDS},
        "telemetry": [],
    }


class ObservationQueryTests(unittest.TestCase):
    def test_exclusive_cursor_paging_walks_history_exactly_once(self):
        store = observed_store()
        populate(store)

        walked, page = [], {"next_cursor": 0, "has_more": True}
        while page["has_more"]:
            page = observation.events(store, after=page["next_cursor"], limit=2)
            self.assertLessEqual(len(page["events"]), 2)
            walked.extend(page["events"])

        self.assertEqual([event["cursor"] for event in walked], list(range(1, TOTAL + 1)))
        self.assertEqual(
            [(event["kind"], event["entity_kind"]) for event in walked], list(EXPECTED)
        )
        self.assertEqual(page["next_cursor"], TOTAL)
        self.assertFalse(page["has_more"])
        self.assertTrue(all(isinstance(event["payload"], dict) for event in walked))
        self.assertTrue(
            all(event["contract"] == schema.OBSERVATION_CONTRACT for event in walked)
        )
        self.assertTrue(all(isinstance(event["effective_at"], float) for event in walked))

    def test_page_boundaries_report_next_cursor_and_has_more(self):
        store = observed_store()
        populate(store)

        exact = observation.events(store, after=0, limit=TOTAL)
        short = observation.events(store, after=0, limit=TOTAL - 1)
        beyond = observation.events(store, after=TOTAL)
        empty = observation.events(store, after=0, limit=TOTAL, through=0)
        bounded = observation.events(store, after=1, limit=TOTAL, through=3)

        self.assertEqual(len(exact["events"]), TOTAL)
        self.assertFalse(exact["has_more"])
        self.assertEqual(exact["next_cursor"], TOTAL)
        self.assertIsNone(exact["through"])
        self.assertEqual(len(short["events"]), TOTAL - 1)
        self.assertTrue(short["has_more"])
        self.assertEqual(short["next_cursor"], TOTAL - 1)
        self.assertEqual(beyond["events"], [])
        self.assertFalse(beyond["has_more"])
        self.assertEqual(beyond["next_cursor"], TOTAL)
        self.assertEqual(empty["events"], [])
        self.assertEqual(empty["next_cursor"], 0)
        self.assertFalse(empty["has_more"])
        self.assertEqual([event["cursor"] for event in bounded["events"]], [2, 3])
        self.assertFalse(bounded["has_more"])
        self.assertEqual(bounded["through"], 3)
        self.assertEqual(bounded["next_cursor"], 3)
        self.assertEqual(json.loads(json.dumps(exact)), exact)

    def test_replay_reconstructs_the_state_visible_at_each_cursor(self):
        store = observed_store()
        ids = populate(store)

        early = observation.replay(store, through=3)
        late = observation.replay(store)

        self.assertEqual(early["cursor"], 3)
        self.assertEqual(late["cursor"], TOTAL)
        self.assertEqual(early["nodes"]["run"][ids["run"]]["state"], "pending")
        self.assertEqual(late["nodes"]["run"][ids["run"]]["state"], "completed")
        # State replacement, not a second node for the same run.
        self.assertEqual(len(late["nodes"]["run"]), 1)
        self.assertEqual(early["nodes"]["task"], {})
        self.assertEqual(sorted(late["nodes"]["task"]), sorted([ids["worker"], ids["upstream"]]))
        self.assertEqual(late["nodes"]["task"][ids["worker"]]["active_run_id"], ids["run"])
        self.assertIsNone(late["nodes"]["task"][ids["upstream"]]["active_run_id"])
        self.assertEqual(early["nodes"]["session"][ids["session"]]["model"], "sonnet")
        self.assertEqual(early["edges"]["dependency"], {})
        self.assertEqual(
            list(late["edges"]["dependency"]), [f"{ids['worker']}:1>{ids['upstream']}:1"]
        )
        self.assertEqual(early["telemetry"], {})
        self.assertEqual(late["telemetry"][ids["run"]]["usage"]["output_tokens"], 5)
        self.assertEqual(late["telemetry"][ids["run"]]["provider_cost_usd"], 0.25)
        self.assertEqual(late["contract"], schema.OBSERVATION_CONTRACT)
        self.assertEqual(late["version"], observation.VERSION)
        # A fresh store summarised nothing, so its fold loses nothing.
        self.assertEqual(late["fidelity"], "complete")
        self.assertEqual(list(late["nodes"]), NODE_KINDS)
        self.assertEqual(list(late["edges"]), EDGE_KINDS)
        self.assertEqual(json.loads(json.dumps(late)), late)
        self.assertEqual(observation.replay(store, through=TOTAL), late)
        self.assertEqual(observation.replay(store, through=3), early)


class ObservationBaselineTests(unittest.TestCase):
    def test_migrated_baseline_only_history_keeps_its_declared_fidelity(self):
        run, task, parent = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        envelope, decision = str(uuid.uuid4()), str(uuid.uuid4())
        summary = baseline_payload()
        summary["nodes"]["run"] = [{"node": "run", "id": run, "state": "cancelled"}]
        summary["nodes"]["task"] = [
            {"node": "task", "id": task, "revision": 1},
            {"node": "task", "id": parent, "revision": 1},
        ]
        summary["edges"]["dependency"] = [
            {
                "edge": "dependency",
                "from_kind": "task",
                "from_id": task,
                "from_revision": 1,
                "to_kind": "task",
                "to_id": parent,
                "to_revision": 1,
            }
        ]
        summary["edges"]["binding"] = [
            {"edge": "binding", "from_kind": "envelope", "from_id": envelope, "to_id": run}
        ]
        summary["edges"]["review"] = [{"edge": "review", "id": decision, "from_id": run}]
        summary["telemetry"] = [
            {"node": "run_telemetry", "run_id": run, "usage": {"input_tokens": 10}}
        ]
        ledger = Ledger().append("baseline", "store", "baseline", summary)

        folded = observation.replay(ledger)

        self.assertEqual(folded["fidelity"], "baseline_only")
        self.assertEqual(folded["cursor"], 1)
        self.assertEqual(sorted(folded["nodes"]["task"]), sorted([task, parent]))
        self.assertEqual(list(folded["edges"]["dependency"]), [f"{task}:1>{parent}:1"])
        self.assertEqual(list(folded["edges"]["binding"]), [f"{envelope}>{run}"])
        self.assertEqual(list(folded["edges"]["review"]), [decision])
        self.assertEqual(folded["telemetry"][run]["usage"]["input_tokens"], 10)

        ledger.append("node_state", "run", run, {"node": "run", "id": run, "state": "unknown"})
        later = observation.replay(ledger)

        self.assertEqual(later["fidelity"], "baseline_only")
        self.assertEqual(later["cursor"], 2)
        self.assertEqual(len(later["nodes"]["run"]), 1)
        self.assertEqual(later["nodes"]["run"][run]["state"], "unknown")
        self.assertEqual(observation.replay(ledger, through=1), folded)


class ObservationFailureTests(unittest.TestCase):
    def assertControl(self, code, call, *args, **kwargs):
        with self.assertRaises(ControlError) as raised:
            call(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_numeric_bounds_are_refused(self):
        ledger = Ledger().seeded()

        for after in (-1, True, "1", 1.0, 2**63):
            with self.subTest(after=after):
                self.assertControl("invalid_cursor", observation.events, ledger, after=after)
        for limit in (0, -1, True, "10", 1.5, observation.MAX_LIMIT + 1):
            with self.subTest(limit=limit):
                self.assertControl("invalid_limit", observation.events, ledger, limit=limit)
        self.assertControl("invalid_cursor", observation.events, ledger, through=-1)
        self.assertControl("invalid_cursor", observation.replay, ledger, through=-1)
        self.assertControl("invalid_cursor", observation.replay, ledger, through="2")

    def test_stale_schema_is_refused_before_any_read(self):
        ledger = Ledger(version=13).seeded()

        self.assertControl("migration_required", observation.events, ledger)
        self.assertControl("migration_required", observation.replay, ledger)

    def test_wrong_contract_fails_both_query_and_replay(self):
        ledger = Ledger().seeded()
        ledger.append(
            "node_created",
            "session",
            "s1",
            {"node": "session", "id": "s1"},
            contract="other.observation.v9",
        )

        self.assertControl("invalid_contract", observation.events, ledger)
        self.assertControl("invalid_contract", observation.replay, ledger)

    def test_unknown_event_kind_fails(self):
        ledger = Ledger().seeded()
        ledger.append("tampered", "session", "s1", {"node": "session", "id": "s1"})

        self.assertControl("unknown_event", observation.events, ledger)
        self.assertControl("unknown_event", observation.replay, ledger)

    def test_malformed_payloads_fail_deterministically(self):
        listed = Ledger().seeded()
        listed.append("node_created", "session", "s1", "[]")
        unlabelled = Ledger().seeded()
        unlabelled.append("node_created", "session", "s1", {"id": "s1"})
        mismatched = Ledger().seeded()
        mismatched.append(
            "run_telemetry", "run", "r1", {"node": "run_telemetry", "run_id": "other"}
        )
        stranger = Ledger().seeded()
        stranger.append("node_created", "galaxy", "g1", {"node": "galaxy", "id": "g1"})
        wrong_node_id = Ledger().seeded()
        wrong_node_id.append(
            "node_created", "session", "s1", {"node": "session", "id": "other"}
        )
        state_before_create = Ledger().seeded()
        state_before_create.append(
            "node_state", "session", "s1", {"node": "session", "id": "s1"}
        )
        duplicate_node = Ledger().seeded()
        duplicate_node.append(
            "node_created", "session", "s1", {"node": "session", "id": "s1"}
        ).append("node_created", "session", "s1", {"node": "session", "id": "s1"})
        summary = baseline_payload()
        summary["edges"]["dependency"] = [
            {"edge": "dependency", "from_id": "a", "to_id": "b", "to_revision": 1}
        ]
        partial = Ledger().append("baseline", "store", "baseline", summary)

        self.assertControl("invalid_payload", observation.events, listed)
        self.assertControl("invalid_payload", observation.replay, listed)
        self.assertControl("invalid_payload", observation.replay, unlabelled)
        self.assertControl("invalid_payload", observation.replay, mismatched)
        self.assertControl("unknown_entity", observation.replay, stranger)
        self.assertControl("invalid_payload", observation.replay, wrong_node_id)
        self.assertControl("missing_entity", observation.replay, state_before_create)
        self.assertControl("duplicate_entity", observation.replay, duplicate_node)
        first = self.assertControl("invalid_payload", observation.replay, partial)
        again = self.assertControl("invalid_payload", observation.replay, partial)
        self.assertEqual(str(first), str(again))

    def test_baseline_placement_is_enforced(self):
        early = Ledger().append("node_created", "session", "s1", {"node": "session", "id": "s1"})
        twice = Ledger().seeded().seeded()
        empty = Ledger()
        seeded = Ledger().seeded()
        misidentified = Ledger().append(
            "baseline", "store", "not-baseline", baseline_payload()
        )

        self.assertControl("event_before_baseline", observation.replay, early)
        self.assertControl("duplicate_baseline", observation.replay, twice)
        self.assertControl("missing_baseline", observation.replay, empty)
        self.assertControl("missing_baseline", observation.replay, seeded, through=0)
        self.assertControl("invalid_payload", observation.replay, misidentified)
        # Paging never asserts placement; it returns the rows as they were appended.
        self.assertEqual(len(observation.events(early)["events"]), 1)


if __name__ == "__main__":
    unittest.main()
