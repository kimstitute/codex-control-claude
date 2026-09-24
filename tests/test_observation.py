"""Schemas 14–15 provide sanitized durable observation and operation history."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/claude-control/scripts"))

from claude_control import migration  # noqa: E402
from claude_control.schema import OBSERVATION_CONTRACT, VERSION  # noqa: E402
from claude_control.store import Store  # noqa: E402
from test_controller import FAKE  # noqa: E402
from test_migration import legacy_store  # noqa: E402

# Field names that must never be projected, plus the private content planted by
# populate() so that its absence is evidence rather than coincidence.
FORBIDDEN = (
    "project",
    "source_repo",
    "prompt",
    "result",
    "message",
    "policy",
    "action",
    "receipt",
    "account",
    "credential",
)
PLANTED = ("SECRET-PROMPT", "SECRET-BODY", "secret-repo-path", "secret-policy")


def fresh_store(root):
    root = Path(root)
    workarea = root / "workarea"
    workarea.mkdir()
    state = root / "state"
    Store.initialize(state, FAKE, [workarea])
    return Store(state), workarea


def populate(store, workarea):
    """Perform one representative mutation of every observed node and edge."""
    run_id, _ = store.reserve(
        prompt="observe this turn",
        request_id="observe",
        timeout=10,
        name="ledger",
        model="sonnet",
        role="executor",
        project=workarea,
    )
    store.stop(run_id)
    session_id = store.get_run(run_id)["session_id"]
    ids = {
        key: str(uuid.uuid4())
        for key in (
            "worker",
            "reviewer",
            "upstream",
            "decision",
            "envelope",
            "flow",
            "operation",
            "space",
            "composed",
        )
    }
    with store.db(write=True) as db:
        # Reasons can contain raw OS/provider failures, including paths or echoed input.
        # Keep them in the private operational store but never copy them to observation.
        db.execute("UPDATE runs SET reason='SECRET-BODY' WHERE id=?", (run_id,))
        for key, name in (
            ("worker", "worker-node"),
            ("reviewer", "reviewer-node"),
            ("upstream", "upstream-node"),
        ):
            db.execute(
                "INSERT INTO tasks(id,name,session_id,current_revision,created)"
                " VALUES(?,?,?,1,1.0)",
                (ids[key], name, session_id),
            )
            db.execute(
                "INSERT INTO task_revisions(task_id,revision,prompt,prompt_sha256,"
                "acknowledge_context,created) VALUES(?,1,'SECRET-PROMPT','sha',0,1.0)",
                (ids[key],),
            )
        db.execute("UPDATE tasks SET active_run_id=? WHERE id=?", (run_id, ids["worker"]))
        db.execute(
            "INSERT INTO dependency_sets(task_id,revision,fingerprint) VALUES(?,1,'fp')",
            (ids["worker"],),
        )
        db.execute(
            "INSERT INTO task_dependencies(child_task_id,child_revision,parent_task_id,"
            "parent_revision) VALUES(?,1,?,1)",
            (ids["worker"], ids["upstream"]),
        )
        db.execute(
            "INSERT INTO task_runs(run_id,task_id,revision,contract,prompt_sha256)"
            " VALUES(?,?,1,'wire','sha')",
            (run_id, ids["worker"]),
        )
        db.execute(
            "INSERT INTO review_decisions(id,task_id,revision,run_id,result_sha256,kind,"
            "reviewer,recommendation,evidence,created)"
            " VALUES(?,?,1,?,'sha','review','auditor','approve','[]',1.0)",
            (ids["decision"], ids["worker"], run_id),
        )
        db.execute(
            "INSERT INTO messages(id,task_id,base_revision,content,content_sha256,kind,created)"
            " VALUES(?,?,1,'SECRET-BODY','sha','instruction',1.0)",
            (ids["envelope"], ids["worker"]),
        )
        db.execute(
            "INSERT INTO revision_messages(task_id,revision,message_id) VALUES(?,1,?)",
            (ids["worker"], ids["envelope"]),
        )
        db.execute(
            "INSERT INTO message_bindings(message_id,run_id,task_id,revision,created)"
            " VALUES(?,?,?,1,1.0)",
            (ids["envelope"], run_id, ids["worker"]),
        )
        db.execute(
            "INSERT INTO workflows(id,name,worker_task_id,reviewer_task_id,policy,state,"
            "phase,round,created)"
            " VALUES(?,'flow-node',?,?,'secret-policy','active','worker',0,1.0)",
            (ids["flow"], ids["worker"], ids["reviewer"]),
        )
        db.execute(
            "UPDATE workflows SET state='stopped',reason='SECRET-PROMPT' WHERE id=?",
            (ids["flow"],),
        )
        db.execute(
            "INSERT INTO workflow_steps(workflow_id,phase,round,task_id,revision,created)"
            " VALUES(?,'worker',0,?,1,1.0)",
            (ids["flow"], ids["worker"]),
        )
        db.execute(
            "INSERT INTO workflow_runs(workflow_id,phase,round,task_id,revision,run_id,created)"
            " VALUES(?,'worker',0,?,1,?,1.0)",
            (ids["flow"], ids["worker"], run_id),
        )
        db.execute(
            "INSERT INTO task_operations(operation_id,fingerprint,response) VALUES(?,'fp','{}')",
            (ids["operation"],),
        )
        db.execute(
            "INSERT INTO workspace_creations(id,operation_id,created) VALUES(?,?,1.0)",
            (ids["space"], ids["operation"]),
        )
        db.execute(
            "INSERT INTO workspaces(id,source_repo,base_commit,policy,baseline_sha256,"
            "state,created)"
            " VALUES(?,'/secret-repo-path','abc123','secret-policy','def456','idle',1.0)",
            (ids["space"],),
        )
        db.execute(
            "UPDATE workspaces SET state='active',reason='secret-repo-path' WHERE id=?",
            (ids["space"],),
        )
        db.execute(
            "INSERT INTO workspace_tasks(workspace_id,task_id,assignment) VALUES(?,?,'{}')",
            (ids["space"], ids["worker"]),
        )
        db.execute(
            "INSERT INTO workspace_calls(run_id,workspace_id,task_id,revision,tree_sha256,"
            "receipts_sha256,envelope,created) VALUES(?,?,?,1,'tree','receipts','{}',1.0)",
            (run_id, ids["space"], ids["worker"]),
        )
        db.execute(
            "INSERT INTO workspace_requests(run_id,seq,action,created) VALUES(?,0,?,1.25)",
            (
                run_id,
                '{"op":"patch","path":"secret-repo-path","content":"SECRET-BODY"}',
            ),
        )
        db.execute(
            "INSERT INTO workspace_receipts(run_id,seq,result,created) VALUES(?,0,?,1.5)",
            (run_id, '{"outcome":"ok","message":"SECRET-PROMPT"}'),
        )
        db.execute(
            "INSERT INTO workspace_exports(workspace_id,run_id,manifest_sha256,"
            "result_sha256,created) VALUES(?,?,'manifest','result-sha',1.0)",
            (ids["space"], run_id),
        )
        db.execute(
            "INSERT INTO compositions(id,name,workflow_id,policy,state,phase,created)"
            " VALUES(?,'compose-node',?,'secret-policy','active','plan',1.0)",
            (ids["composed"], ids["flow"]),
        )
        db.execute(
            "UPDATE compositions SET state='stopped',reason='secret-policy' WHERE id=?",
            (ids["composed"],),
        )
        db.execute(
            "INSERT INTO composition_members(composition_id,phase,workspace_id,task_id,created)"
            " VALUES(?,'editor',?,?,1.0)",
            (ids["composed"], ids["space"], ids["worker"]),
        )
        db.execute(
            "INSERT INTO composition_results(composition_id,phase,task_id,revision,run_id,"
            "result_sha256,workspace_id,manifest_sha256,tree_sha256,created)"
            " VALUES(?,'editor',?,1,?,'result-sha',?,'manifest','tree',1.0)",
            (ids["composed"], ids["worker"], run_id, ids["space"]),
        )
        db.execute(
            "INSERT INTO run_telemetry(run_id,usage,model_usage,provider_cost_usd,"
            "duration_api_ms,duration_ms,created) VALUES(?,?,?,0.25,120.0,300.0,1.0)",
            (
                run_id,
                '{"input_tokens":10,"output_tokens":5}',
                '{"sonnet":{"input_tokens":10}}',
            ),
        )
    return run_id


def events(store):
    with store.db() as db:
        return [dict(row) for row in db.execute("SELECT * FROM observation_events ORDER BY cursor")]


class ObservationLedgerTests(unittest.TestCase):
    def test_fresh_store_holds_exactly_one_baseline_only_event(self):
        with tempfile.TemporaryDirectory() as root:
            store, _ = fresh_store(root)

            rows = events(store)

            self.assertEqual(VERSION, 15)
            self.assertEqual(store.config["schema"], 15)
            self.assertEqual(OBSERVATION_CONTRACT, "claude-control.observation.v1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["kind"], "baseline")
            self.assertEqual(rows[0]["contract"], OBSERVATION_CONTRACT)
            self.assertEqual(rows[0]["entity_kind"], "store")
            self.assertEqual(rows[0]["cursor"], 1)
            self.assertIsInstance(rows[0]["recorded_at"], float)
            self.assertIsInstance(rows[0]["effective_at"], float)
            payload = json.loads(rows[0]["payload"])
            self.assertEqual(payload["fidelity"], "baseline_only")
            self.assertEqual(
                sorted(payload["nodes"]),
                [
                    "composition",
                    "operation",
                    "run",
                    "session",
                    "task",
                    "workflow",
                    "workspace",
                ],
            )
            self.assertEqual(
                sorted(payload["edges"]),
                [
                    "binding",
                    "composition_member",
                    "composition_run",
                    "dependency",
                    "review",
                    "task_run",
                    "workflow_run",
                    "workspace_run",
                    "workspace_task",
                ],
            )
            self.assertEqual(payload["telemetry"], [])
            self.assertTrue(all(nodes == [] for nodes in payload["nodes"].values()))
            self.assertTrue(all(edges == [] for edges in payload["edges"].values()))

    def test_migrated_store_summarises_history_without_inventing_transitions(self):
        with tempfile.TemporaryDirectory() as root:
            state, workarea = legacy_store(root)
            migration.migrate(state, offline=True, target=13)
            store = Store(state)
            run_id = populate(store, workarea)
            with store.db() as db:
                before = [tuple(row) for row in db.execute("SELECT rowid,* FROM runs")]

            result = migration.migrate(state, offline=True, target=15)

            current = Store(state)
            with current.db() as db:
                after = [tuple(row) for row in db.execute("SELECT rowid,* FROM runs")]
                violations = db.execute("PRAGMA foreign_key_check").fetchall()
            rows = events(current)
            self.assertEqual(result["schema"], 15)
            self.assertEqual(after, before)
            self.assertEqual(violations, [])
            # Pre-observation state is summarised once. Schema 15 then appends one
            # final operation summary without inventing its start/settle transition.
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["kind"], "baseline")
            payload = json.loads(rows[0]["payload"])
            self.assertEqual(payload["fidelity"], "baseline_only")
            self.assertEqual([node["id"] for node in payload["nodes"]["run"]], [run_id])
            self.assertEqual(payload["nodes"]["run"][0]["state"], "cancelled")
            self.assertEqual(len(payload["nodes"]["task"]), 3)
            self.assertEqual(len(payload["nodes"]["workflow"]), 1)
            self.assertEqual(len(payload["nodes"]["workspace"]), 1)
            self.assertEqual(len(payload["nodes"]["composition"]), 1)
            self.assertEqual(len(payload["edges"]["dependency"]), 1)
            self.assertEqual(len(payload["edges"]["binding"]), 1)
            self.assertEqual(len(payload["edges"]["review"]), 1)
            for kind in (
                "task_run",
                "workflow_run",
                "workspace_task",
                "workspace_run",
                "composition_member",
                "composition_run",
            ):
                self.assertEqual(len(payload["edges"][kind]), 1)
            self.assertEqual(payload["telemetry"][0]["usage"]["input_tokens"], 10)
            operation = json.loads(rows[1]["payload"])
            self.assertEqual(
                operation,
                {
                    "node": "operation",
                    "id": f"op:{run_id}:0",
                    "run_id": run_id,
                    "seq": 0,
                    "operation": "patch",
                    "state": "settled",
                    "started": 1.25,
                    "finished": 1.5,
                },
            )
            for needle in FORBIDDEN + PLANTED:
                self.assertNotIn(needle, rows[0]["payload"])
            with closing(sqlite3.connect(state / "schema-13-backup.sqlite3")) as backup:
                self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 13)

    def test_ledger_refuses_update_and_delete(self):
        with tempfile.TemporaryDirectory() as root:
            store, _ = fresh_store(root)

            for statement in (
                "UPDATE observation_events SET kind='tampered'",
                "DELETE FROM observation_events",
            ):
                with self.subTest(statement=statement):
                    with self.assertRaises(sqlite3.IntegrityError) as raised:
                        with store.db(write=True) as db:
                            db.execute(statement)
                    self.assertIn("append-only", str(raised.exception))
            self.assertEqual(len(events(store)), 1)

    def test_rollback_discards_the_event_its_mutation_appended(self):
        with tempfile.TemporaryDirectory() as root:
            store, workarea = fresh_store(root)
            session_id = str(uuid.uuid4())

            with self.assertRaises(RuntimeError):
                with store.db(write=True) as db:
                    db.execute(
                        "INSERT INTO sessions(id,name,backend_id,model,role,project,created)"
                        " VALUES(?,'rolled-back',?,'sonnet','executor',?,1.0)",
                        (session_id, str(uuid.uuid4()), str(workarea)),
                    )
                    # The append is visible inside the very transaction that caused it.
                    self.assertEqual(
                        db.execute(
                            "SELECT count(*) FROM observation_events WHERE entity_id=?",
                            (session_id,),
                        ).fetchone()[0],
                        1,
                    )
                    raise RuntimeError("abandon the turn")

            with store.db() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
            rows = events(store)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["kind"], "baseline")

    def test_operation_projection_is_closed_and_transactional(self):
        with tempfile.TemporaryDirectory() as root:
            store, workarea = fresh_store(root)
            run_id = populate(store, workarea)
            before = len(events(store))

            with self.assertRaises(RuntimeError):
                with store.db(write=True) as db:
                    db.execute(
                        "INSERT INTO workspace_requests(run_id,seq,action,created) "
                        "VALUES(?,1,?,2.0)",
                        (run_id, '{"op":"SECRET-UNKNOWN","path":"SECRET-PATH"}'),
                    )
                    self.assertEqual(
                        db.execute("SELECT count(*) FROM observation_events").fetchone()[0],
                        before + 1,
                    )
                    raise RuntimeError("rollback operation")

            self.assertEqual(len(events(store)), before)
            with store.db(write=True) as db:
                db.execute(
                    "INSERT INTO workspace_requests(run_id,seq,action,created) "
                    "VALUES(?,1,?,2.0)",
                    (run_id, '{"op":"SECRET-UNKNOWN","path":"SECRET-PATH"}'),
                )
            payload = json.loads(events(store)[-1]["payload"])
            self.assertEqual(payload["operation"], "unknown")
            self.assertNotIn("SECRET", json.dumps(payload))

    def test_representative_entity_events_append_in_cursor_order(self):
        with tempfile.TemporaryDirectory() as root:
            store, workarea = fresh_store(root)
            run_id = populate(store, workarea)

            rows = events(store)

            cursors = [row["cursor"] for row in rows]
            self.assertEqual(cursors, list(range(1, len(rows) + 1)))
            self.assertEqual(rows[0]["kind"], "baseline")
            self.assertTrue(all(row["contract"] == OBSERVATION_CONTRACT for row in rows))
            self.assertTrue(
                all(
                    isinstance(row["recorded_at"], float)
                    and isinstance(row["effective_at"], float)
                    for row in rows
                )
            )
            seen = {(row["kind"], row["entity_kind"]) for row in rows}
            for expected in (
                ("node_created", "session"),
                ("node_created", "run"),
                ("node_state", "run"),
                ("node_created", "task"),
                ("node_state", "task"),
                ("node_created", "workflow"),
                ("node_state", "workflow"),
                ("node_created", "workspace"),
                ("node_state", "workspace"),
                ("node_created", "composition"),
                ("node_state", "composition"),
                ("node_created", "operation"),
                ("node_state", "operation"),
                ("edge_created", "dependency"),
                ("edge_created", "binding"),
                ("edge_created", "review"),
                ("edge_created", "task_run"),
                ("edge_created", "workflow_run"),
                ("edge_created", "workspace_task"),
                ("edge_created", "workspace_run"),
                ("edge_created", "composition_member"),
                ("edge_created", "composition_run"),
                ("run_telemetry", "run"),
            ):
                self.assertIn(expected, seen)
            states = [
                json.loads(row["payload"])["state"]
                for row in rows
                if row["entity_kind"] == "run" and row["kind"] in ("node_created", "node_state")
            ]
            self.assertEqual(states, ["pending", "cancelled"])
            session = next(
                json.loads(row["payload"]) for row in rows if row["entity_kind"] == "session"
            )
            self.assertEqual((session["model"], session["role"]), ("sonnet", "executor"))
            telemetry = next(
                json.loads(row["payload"]) for row in rows if row["kind"] == "run_telemetry"
            )
            self.assertEqual(telemetry["run_id"], run_id)
            self.assertEqual(telemetry["usage"]["output_tokens"], 5)
            self.assertEqual(telemetry["provider_cost_usd"], 0.25)
            edge = next(
                json.loads(row["payload"]) for row in rows if row["entity_kind"] == "review"
            )
            self.assertEqual((edge["decision"], edge["recommendation"]), ("review", "approve"))
            operations = [
                json.loads(row["payload"])
                for row in rows
                if row["entity_kind"] == "operation"
            ]
            self.assertEqual([item["state"] for item in operations], ["started", "settled"])
            self.assertEqual(operations[-1]["operation"], "patch")

    def test_no_serialized_payload_carries_private_content(self):
        with tempfile.TemporaryDirectory() as root:
            store, workarea = fresh_store(root)
            populate(store, workarea)

            dump = "\n".join(
                f"{row['kind']} {row['entity_kind']} {row['entity_id']} {row['payload']}"
                for row in events(store)
            )

            for needle in FORBIDDEN:
                with self.subTest(needle=needle):
                    self.assertNotIn(needle, dump)
            for planted in PLANTED:
                with self.subTest(planted=planted):
                    self.assertNotIn(planted, dump)
            self.assertNotIn(str(workarea), dump)


if __name__ == "__main__":
    unittest.main()
