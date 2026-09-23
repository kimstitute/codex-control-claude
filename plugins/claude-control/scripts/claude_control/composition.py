"""Durable composition of the bounded P4 workflow and P5 workspaces."""

import hashlib
import json
import math
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import task_contracts as contract
from . import tasks, workflow, workspace
from . import workspace_files as files
from .platform.locks import file_lock
from .store import ControlError
from .workspace_policy import normalize_policy, permits
from .workspace_policy import path as policy_path

STATE_ERRORS = (ControlError, OSError, ValueError, KeyError, TypeError, sqlite3.IntegrityError)


def _require(store):
    if store.config["schema"] < 10:
        raise ControlError("migration_required", "Compositions require migrate --offline.")


def _get(db, composition_id):
    row = db.execute("SELECT * FROM compositions WHERE id=?", (composition_id,)).fetchone()
    if not row:
        raise ControlError("composition_not_found", "Unknown composition UUID.")
    return row


def _operation_id(composition_id, action):
    return f"composition:{composition_id}:{action}"


def _composition_id(store, operation_id):
    namespace = uuid.UUID(store.config["installation_id"])
    return str(uuid.uuid5(namespace, "composition:" + operation_id))


def _pin_source(store, composition_id, repo, requested_ref):
    operation_id = _operation_id(composition_id, "source")
    intent = {
        "kind": "composition_source",
        "repo": repo,
        "requested_ref": requested_ref,
    }
    with store.db() as db:
        fingerprint, prior = tasks._operation(db, operation_id, intent)
        if prior:
            return prior["commit"]
    resolved = files.resolve_commit(repo, requested_ref)
    with store.db(write=True) as db:
        fingerprint, prior = tasks._operation(db, operation_id, intent)
        if prior:
            return prior["commit"]
        tasks._record(db, operation_id, fingerprint, {"commit": resolved})
    return resolved


def _member(db, composition_id, phase):
    return db.execute(
        "SELECT * FROM composition_members WHERE composition_id=? AND phase=?",
        (composition_id, phase),
    ).fetchone()


def _result(db, composition_id, phase):
    return db.execute(
        "SELECT * FROM composition_results WHERE composition_id=? AND phase=?",
        (composition_id, phase),
    ).fetchone()


def _canonical_assignment(store, value, *, role=None, model=None):
    assignment = tasks._assignment(store, value)
    if role is not None and assignment["role"] not in role:
        raise ControlError("invalid_composition", "Assignment role is incompatible with its phase.")
    if model is not None and assignment["model"] != model:
        raise ControlError(
            "invalid_composition", "Assignment model is incompatible with its phase."
        )
    return assignment


def _text(value, field, maximum=65536):
    try:
        encoded = value.encode("utf-8") if isinstance(value, str) else b""
    except UnicodeEncodeError:
        encoded = b""
    if not isinstance(value, str) or not value.strip() or not encoded or len(encoded) > maximum:
        raise ControlError("invalid_composition", f"Leader specification {field} is invalid.")
    return value


def _string_list(value, field, maximum=64):
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise ControlError("invalid_composition", f"Leader specification {field} is invalid.")
    return [_text(item, field, 4096) for item in value]


def _leader_spec(value):
    required = {
        "version",
        "id",
        "name",
        "objective",
        "context",
        "scope",
        "acceptance_criteria",
        "implementation_plan",
    }
    if not isinstance(value, dict) or set(value) != required or value["version"] != 1:
        raise ControlError(
            "invalid_composition", "Leader specification fields must match version 1."
        )
    spec = {
        "version": 1,
        "id": _text(value["id"], "id", 64),
        "name": _text(value["name"], "name", 120),
        "objective": _text(value["objective"], "objective"),
        "context": _text(value["context"], "context"),
        "scope": _string_list(value["scope"], "scope"),
        "acceptance_criteria": _string_list(value["acceptance_criteria"], "acceptance_criteria"),
        "implementation_plan": _string_list(value["implementation_plan"], "implementation_plan"),
    }
    raw = contract.canonical(spec)
    if len(raw.encode("utf-8")) > 256 * 1024:
        raise ControlError("invalid_composition", "Leader specification exceeds 256 KiB.")
    return spec, hashlib.sha256(raw.encode()).hexdigest()


def _selector_covers(selector, relative):
    return selector == relative or (selector.endswith("/") and relative.startswith(selector))


def _normalize_test_contract(value, editor_policy):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"version", "frozen_paths", "checks"}:
        raise ControlError("invalid_composition", "Test contract fields must match version 1.")
    if value["version"] != 1:
        raise ControlError("invalid_composition", "Use test contract version 1.")
    selected = value["frozen_paths"]
    if not isinstance(selected, list) or not 1 <= len(selected) <= 64:
        raise ControlError("invalid_composition", "Use 1–64 frozen test paths.")
    frozen_paths = sorted(policy_path(item, allow_directory=True) for item in selected)
    if len(set(frozen_paths)) != len(frozen_paths) or "." in frozen_paths:
        raise ControlError("invalid_composition", "Frozen test paths must be unique and bounded.")
    for frozen in frozen_paths:
        relative = frozen[:-1] if frozen.endswith("/") else frozen
        readable = permits(editor_policy["read_paths"], relative) or any(
            item == frozen or (item.endswith("/") and frozen.startswith(item))
            for item in editor_policy["read_paths"]
        )
        if not readable:
            raise ControlError(
                "invalid_composition", "Every frozen test path must be readable by the editor."
            )
    for writable in editor_policy["write_paths"]:
        if any(_selector_covers(frozen, writable) for frozen in frozen_paths):
            raise ControlError(
                "invalid_composition", "Editor writes cannot overlap frozen test paths."
            )
    checks = value["checks"]
    if not isinstance(checks, dict) or not 1 <= len(checks) <= 8:
        raise ControlError("invalid_composition", "Use 1–8 required test checks.")
    normalized = {}
    for name, expectation in checks.items():
        if name not in editor_policy["checks"]:
            raise ControlError(
                "invalid_composition", "Every test contract check must exist in editor policy."
            )
        if (
            not isinstance(expectation, dict)
            or set(expectation) != {"baseline", "post"}
            or expectation["baseline"] not in ("pass", "fail")
            or expectation["post"] != "pass"
        ):
            raise ControlError(
                "invalid_composition",
                "A test contract check needs baseline pass/fail and post pass.",
            )
        normalized[name] = dict(expectation)
    content = {
        "version": 1,
        "frozen_paths": frozen_paths,
        "checks": dict(sorted(normalized.items())),
    }
    content["sha256"] = hashlib.sha256(contract.canonical(content).encode()).hexdigest()
    return content


def _reviewer_policy(editor_policy):
    return normalize_policy(
        {
            "version": 1,
            "role": "verifier",
            "read_paths": editor_policy["read_paths"],
            "write_paths": [],
            "checks": {},
            "max_actions": editor_policy["max_actions"],
            "max_calls": editor_policy["max_calls"],
        }
    )


def _scout_provenance(store, workspace_id, source_repo, pinned_commit, read_paths):
    with store.db() as db:
        row = workspace._get(db, workspace_id)
        policy = json.loads(row["policy"])
        binding = db.execute(
            "SELECT * FROM workspace_tasks WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
        saved = db.execute(
            "SELECT * FROM workspace_exports WHERE workspace_id=?", (workspace_id,)
        ).fetchone()
        if (
            row["state"] != "finished"
            or row["source_workspace"] is not None
            or row["source_repo"] != source_repo
            or row["base_commit"] != pinned_commit
            or policy["role"] != "scout"
            or policy["read_paths"] != read_paths
            or policy["write_paths"]
            or policy["checks"]
            or not binding
            or not saved
        ):
            raise ControlError(
                "invalid_composition",
                "Scout must be a finished read-only Sonnet workspace at the exact source commit "
                "with the editor's readable paths.",
            )
        run = db.execute("SELECT * FROM runs WHERE id=?", (saved["run_id"],)).fetchone()
        checked = contract.inspect_run(store, db, run)
        if checked["format_status"] != "valid" or checked["agent_status"] != "complete":
            raise ControlError("invalid_composition", "Scout final report is not complete.")
        manifest = files.verify_frozen(
            workspace.directory(store, workspace_id) / "frozen",
            policy,
            saved["manifest_sha256"],
        )
        if manifest["changes"]:
            raise ControlError(
                "invalid_composition", "Scout snapshot unexpectedly contains changes."
            )
        assignment = json.loads(binding["assignment"])
        if assignment["role"] != "researcher" or assignment["model"] != "sonnet":
            raise ControlError("invalid_composition", "Scout must use the Sonnet researcher role.")
        return {
            "protocol": "claude-control.scout.v1",
            "workspace_id": workspace_id,
            "task_id": binding["task_id"],
            "run_id": saved["run_id"],
            "result_sha256": saved["result_sha256"],
            "manifest_sha256": saved["manifest_sha256"],
            "tree_sha256": manifest["tree_sha256"],
            "report": checked["report"],
        }


def create(
    store,
    planning_assignment,
    editor_assignment,
    editor_policy,
    reviewer_assignment,
    operation_id,
    *,
    repo,
    ref="HEAD",
    max_revisions=2,
    max_calls=6,
    dispatch_window_seconds=900,
    reviewer_effort=None,
    scout_workspace=None,
    leader_spec=None,
    critic_assignment=None,
    test_contract=None,
):
    """Create a composition, recovering a previously created P4 child by operation ID."""
    _require(store)
    source_repo = store.project(repo)
    composition_id = _composition_id(store, operation_id)
    pinned_commit = _pin_source(store, composition_id, source_repo, ref)
    editor = _canonical_assignment(store, editor_assignment, role=("executor",))
    reviewer = _canonical_assignment(
        store, reviewer_assignment, role=("critic", "verifier"), model="fable"
    )
    if (planning_assignment is None) == (leader_spec is None):
        raise ControlError(
            "invalid_composition", "Choose one generated plan or one leader specification."
        )
    planning_mode = "leader_spec" if leader_spec is not None else "generated"
    spec = None
    spec_sha256 = None
    if planning_mode == "leader_spec":
        if critic_assignment is None:
            raise ControlError(
                "invalid_composition", "Leader specifications require a Fable critic assignment."
            )
        spec, spec_sha256 = _leader_spec(leader_spec)
        planning = _canonical_assignment(store, critic_assignment, role=("critic",), model="fable")
        if editor["acceptance_criteria"] != spec["acceptance_criteria"]:
            raise ControlError(
                "invalid_composition",
                "The editor must use the leader specification's exact acceptance criteria.",
            )
        planning = dict(planning)
        planning["context"] = (
            planning["context"]
            + "\n\nCritique the following immutable leader-authored specification. "
            "Do not rewrite it. Identify criterion-level blockers and return a recommendation.\n"
            + contract.canonical(
                {
                    "protocol": "claude-control.leader-spec.v1",
                    "sha256": spec_sha256,
                    "specification": spec,
                }
            )
        )
        planning = _canonical_assignment(store, planning, role=("critic",), model="fable")
    else:
        if critic_assignment is not None:
            raise ControlError(
                "invalid_composition", "Generated planning cannot use a leader-spec critic."
            )
        planning = _canonical_assignment(
            store, planning_assignment, role=("planner", "architect", "executor")
        )
    if any(a["project"] != source_repo for a in (planning, editor, reviewer)):
        raise ControlError(
            "invalid_composition", "Every composition assignment must use the source repository."
        )
    if reviewer["acceptance_criteria"] != editor["acceptance_criteria"]:
        raise ControlError(
            "invalid_composition",
            "The frozen reviewer must assess the editor's exact acceptance criteria.",
        )
    editor_policy = normalize_policy(editor_policy)
    if editor_policy["role"] != "executor":
        raise ControlError("invalid_composition", "The editor policy must use the executor role.")
    test_contract = _normalize_test_contract(test_contract, editor_policy)
    scout = None
    if scout_workspace is not None:
        scout = _scout_provenance(
            store,
            scout_workspace,
            source_repo,
            pinned_commit,
            editor_policy["read_paths"],
        )
        planning = dict(planning)
        planning["context"] = (
            planning["context"]
            + "\n\nTrusted read-only scout evidence follows. Cite its file:line evidence and "
            "identify any missing source context before planning.\n" + contract.canonical(scout)
        )
        planning = _canonical_assignment(store, planning, role=("planner", "architect", "executor"))
    policy = {
        "version": 1,
        "planning_mode": planning_mode,
        "repo": source_repo,
        "requested_ref": ref,
        "ref": pinned_commit,
        "planning_assignment": planning,
        "leader_spec": spec,
        "leader_spec_sha256": spec_sha256,
        "editor_assignment": editor,
        "editor_policy": editor_policy,
        "reviewer_assignment": reviewer,
        "reviewer_policy": _reviewer_policy(editor_policy),
        "test_contract": test_contract,
        "scout": scout,
        "workflow": {
            "max_revisions": 0 if planning_mode == "leader_spec" else max_revisions,
            "max_calls": max_calls,
            "dispatch_window_seconds": dispatch_window_seconds,
            "reviewer_effort": reviewer_effort,
        },
    }
    intent = {"kind": "composition_create", "policy": policy}
    with store.db() as db:
        fingerprint, prior = tasks._operation(db, operation_id, intent)
        if prior:
            return {**prior, "deduplicated": True}

    child = workflow.create(
        store,
        planning,
        _operation_id(composition_id, "plan:create"),
        max_revisions=0 if planning_mode == "leader_spec" else max_revisions,
        max_calls=max_calls,
        dispatch_window_seconds=dispatch_window_seconds,
        reviewer_effort=reviewer_effort,
    )
    with store.db(write=True) as db:
        fingerprint, prior = tasks._operation(db, operation_id, intent)
        if prior:
            return {**prior, "deduplicated": True}
        existing = db.execute("SELECT * FROM compositions WHERE id=?", (composition_id,)).fetchone()
        if existing and existing["workflow_id"] != child["id"]:
            raise ControlError(
                "request_conflict", "Composition identity belongs to another workflow."
            )
        if not existing:
            db.execute(
                "INSERT INTO compositions"
                "(id,name,workflow_id,policy,state,reason,phase,created) "
                "VALUES(?,?,?,?, 'active',NULL,'plan',?)",
                (
                    composition_id,
                    spec["name"] if spec else planning["name"],
                    child["id"],
                    contract.canonical(policy),
                    time.time(),
                ),
            )
        response = {
            "id": composition_id,
            "workflow_id": child["id"],
            "state": "active",
            "phase": "plan",
        }
        tasks._record(db, operation_id, fingerprint, response)
    return {**response, "deduplicated": False}


def _completed_result(store, db, task_id, *, accepted=False):
    task = tasks._task(db, task_id)
    if not task["active_run_id"]:
        return None
    run = db.execute("SELECT * FROM runs WHERE id=?", (task["active_run_id"],)).fetchone()
    if not run or run["status"] != "completed" or not run["result_sha256"]:
        return None
    checked = contract.inspect_run(store, db, run)
    if checked["format_status"] != "valid" or checked["agent_status"] != "complete":
        return None
    decision = None
    if accepted:
        decision = db.execute(
            "SELECT * FROM review_decisions WHERE task_id=? AND revision=? AND run_id=? "
            "AND result_sha256=? AND kind='accept' ORDER BY rowid DESC LIMIT 1",
            (task_id, task["current_revision"], run["id"], run["result_sha256"]),
        ).fetchone()
        if not decision:
            return None
    return {
        "task_id": task_id,
        "revision": task["current_revision"],
        "run_id": run["id"],
        "result_sha256": run["result_sha256"],
        "report": checked["report"],
        "decision_id": decision["id"] if decision else None,
    }


def _record_result(db, composition_id, phase, source, *, workspace_id=None, exported=None):
    values = (
        composition_id,
        phase,
        source["task_id"],
        source["revision"],
        source["run_id"],
        source["result_sha256"],
        workspace_id,
        exported["manifest_sha256"] if exported else None,
        exported["manifest"]["tree_sha256"] if exported else None,
        time.time(),
    )
    existing = _result(db, composition_id, phase)
    if existing:
        pinned = tuple(
            existing[key]
            for key in (
                "task_id",
                "revision",
                "run_id",
                "result_sha256",
                "workspace_id",
                "manifest_sha256",
                "tree_sha256",
            )
        )
        if pinned != values[2:9]:
            raise ControlError("dependency_changed", "A composed phase result changed.")
        return
    db.execute(
        "INSERT INTO composition_results"
        "(composition_id,phase,task_id,revision,run_id,result_sha256,workspace_id,"
        "manifest_sha256,tree_sha256,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
        values,
    )


def _append_context(assignment, envelope):
    result = dict(assignment)
    separator = "\n\n" if result["context"] else ""
    result["context"] += (
        separator + "claude-control composition provenance:\n" + contract.canonical(envelope)
    )
    return result


def _frozen_manifest(manifest, frozen_paths):
    selected = {
        relative: receipt
        for relative, receipt in manifest["files"].items()
        if any(_selector_covers(selector, relative) for selector in frozen_paths)
    }
    for selector in frozen_paths:
        if not any(_selector_covers(selector, relative) for relative in selected):
            raise ControlError(
                "invalid_composition", "A frozen test path selects no file at the pinned commit."
            )
    return {
        "files": selected,
        "sha256": hashlib.sha256(contract.canonical(selected).encode()).hexdigest(),
    }


def _baseline_gate(store, row, policy, workspace_id):
    test_contract = policy.get("test_contract")
    if not test_contract:
        return None
    root = workspace.directory(store, workspace_id)
    editor_policy = policy["editor_policy"]
    baseline = files.inspect_tree(root / "baseline", editor_policy)
    frozen = _frozen_manifest(baseline, test_contract["frozen_paths"])
    operation_id = _operation_id(row["id"], "test:baseline")
    intent = {
        "kind": "composition_test_baseline",
        "composition_id": row["id"],
        "workspace_id": workspace_id,
        "base_commit": policy["ref"],
        "baseline_sha256": baseline["sha256"],
        "test_contract_sha256": test_contract["sha256"],
        "frozen_sha256": frozen["sha256"],
    }
    with store.db() as db:
        fingerprint, prior = tasks._operation(db, operation_id, intent)
    if not prior:
        workspace.sandbox.require()
        receipts = []
        for name, expectation in test_contract["checks"].items():
            check = editor_policy["checks"][name]
            with tempfile.TemporaryDirectory(prefix="baseline-", dir=root) as temporary:
                scratch = Path(temporary) / "tree"
                files.copy_tree(root / "baseline", scratch, editor_policy)
                receipt = workspace.sandbox.execute(scratch, check["argv"], check["timeout"])
            observed = "pass" if receipt["outcome"] == "ok" else "fail"
            receipts.append(
                {
                    "name": name,
                    "expected": expectation["baseline"],
                    "observed": observed,
                    "tree_sha256": baseline["sha256"],
                    "receipt": receipt,
                }
            )
        passed = all(
            item["observed"] == item["expected"] and item["receipt"]["outcome"] in ("ok", "failed")
            for item in receipts
        )
        response = {
            "status": "passed" if passed else "failed",
            "workspace_id": workspace_id,
            "baseline_sha256": baseline["sha256"],
            "frozen": frozen,
            "checks": receipts,
        }
        with store.db(write=True) as db:
            fingerprint, prior = tasks._operation(db, operation_id, intent)
            if not prior:
                tasks._record(db, operation_id, fingerprint, response)
                prior = response
    if prior["status"] != "passed":
        raise ControlError(
            "baseline_check_failed", "A required baseline check did not match its expectation."
        )
    return prior


def _post_gate(store, row, policy, member, exported):
    test_contract = policy.get("test_contract")
    if not test_contract:
        return None
    workspace_id = member["workspace_id"]
    root = workspace.directory(store, workspace_id)
    baseline = files.inspect_tree(root / "baseline", policy["editor_policy"])
    baseline_frozen = _frozen_manifest(baseline, test_contract["frozen_paths"])
    final_frozen = _frozen_manifest(exported["manifest"], test_contract["frozen_paths"])
    operation_id = _operation_id(row["id"], "test:post")
    intent = {
        "kind": "composition_test_post",
        "composition_id": row["id"],
        "workspace_id": workspace_id,
        "manifest_sha256": exported["manifest_sha256"],
        "tree_sha256": exported["manifest"]["tree_sha256"],
        "test_contract_sha256": test_contract["sha256"],
    }
    with store.db() as db:
        fingerprint, prior = tasks._operation(db, operation_id, intent)
        if not prior:
            records = db.execute(
                "SELECT q.run_id,q.seq,q.action,r.result FROM workspace_requests q "
                "JOIN workspace_calls c USING(run_id) "
                "JOIN workspace_receipts r USING(run_id,seq) "
                "WHERE c.workspace_id=? ORDER BY c.rowid,q.seq",
                (workspace_id,),
            ).fetchall()
    if not prior:
        evidence = {}
        for record in records:
            action = json.loads(record["action"])
            receipt = json.loads(record["result"])
            if action.get("op") == "run_check" and action.get("name") in test_contract["checks"]:
                evidence[action["name"]] = {
                    "run_id": record["run_id"],
                    "seq": record["seq"],
                    "outcome": receipt.get("outcome"),
                    "tree_sha256": receipt.get("tree_sha256"),
                }
        expected_tree = exported["manifest"]["tree_sha256"]
        passed = baseline_frozen == final_frozen and all(
            name in evidence
            and evidence[name]["outcome"] == "ok"
            and evidence[name]["tree_sha256"] == expected_tree
            for name in test_contract["checks"]
        )
        response = {
            "status": "passed" if passed else "failed",
            "workspace_id": workspace_id,
            "manifest_sha256": exported["manifest_sha256"],
            "tree_sha256": expected_tree,
            "frozen_sha256": final_frozen["sha256"],
            "checks": evidence,
        }
        with store.db(write=True) as db:
            fingerprint, prior = tasks._operation(db, operation_id, intent)
            if not prior:
                tasks._record(db, operation_id, fingerprint, response)
                prior = response
    if prior["status"] != "passed":
        raise ControlError(
            "post_check_failed",
            "Frozen tests changed or a required check did not pass on the final tree.",
        )
    return prior


def _materialize_editor(store, row, policy, plan):
    composition_id = row["id"]
    planning_evidence = {
        "protocol": "claude-control.composition.v2",
        "phase": "plan",
        "planning_mode": policy.get("planning_mode", "generated"),
        "workflow_id": row["workflow_id"],
        "task_id": plan["task_id"],
        "revision": plan["revision"],
        "run_id": plan["run_id"],
        "result_sha256": plan["result_sha256"],
        "acceptance_decision_id": plan["decision_id"],
        "report": plan["report"],
    }
    if policy.get("planning_mode") == "leader_spec":
        planning_evidence.update(
            leader_spec_protocol="claude-control.leader-spec.v1",
            leader_spec=policy["leader_spec"],
            leader_spec_sha256=policy["leader_spec_sha256"],
            report_kind="fable_critique",
        )
    assignment = _append_context(
        policy["editor_assignment"],
        planning_evidence,
    )
    made = workspace.create(
        store,
        policy["editor_policy"],
        _operation_id(composition_id, "editor:create"),
        repo=policy["repo"],
        ref=policy["ref"],
    )
    baseline = _baseline_gate(store, row, policy, made["id"])
    if baseline:
        assignment = _append_context(
            assignment,
            {
                "protocol": "claude-control.test-contract.v1",
                "test_contract": policy["test_contract"],
                "baseline": baseline,
                "requirement": (
                    "Keep frozen paths unchanged and run every required named check after the "
                    "last edit. A passing receipt must match the final tree hash."
                ),
            },
        )
    bound = workspace.bind(
        store,
        made["id"],
        assignment,
        _operation_id(composition_id, "editor:bind"),
    )
    with store.db(write=True) as db:
        current = _get(db, composition_id)
        if current["state"] != "active" or current["phase"] != "plan":
            return
        _record_result(db, composition_id, "plan", plan)
        db.execute(
            "INSERT INTO composition_members"
            "(composition_id,phase,workspace_id,task_id,created) VALUES(?,?,?,?,?)",
            (composition_id, "editor", made["id"], bound["task_id"], time.time()),
        )
        db.execute(
            "UPDATE compositions SET phase='editor',state='active',reason=NULL "
            "WHERE id=? AND state='active' AND phase='plan'",
            (composition_id,),
        )


def _workspace_result(store, db, member):
    row = workspace._get(db, member["workspace_id"])
    if row["state"] != "finished":
        return None
    exported = workspace.export(store, row["id"])
    saved = db.execute(
        "SELECT * FROM workspace_exports WHERE workspace_id=?", (row["id"],)
    ).fetchone()
    source = _completed_result(store, db, member["task_id"])
    if not source or source["run_id"] != saved["run_id"]:
        raise ControlError(
            "dependency_changed", "Frozen export does not match the final task result."
        )
    return source, exported


def _materialize_reviewer(store, row, policy, editor, exported):
    composition_id = row["id"]
    assignment = _append_context(
        policy["reviewer_assignment"],
        {
            "protocol": "claude-control.composition.v1",
            "phase": "editor",
            "workspace_id": editor["workspace_id"],
            "task_id": editor["task_id"],
            "revision": editor["revision"],
            "run_id": editor["run_id"],
            "result_sha256": editor["result_sha256"],
            "manifest_sha256": exported["manifest_sha256"],
            "tree_sha256": exported["manifest"]["tree_sha256"],
        },
    )
    made = workspace.create(
        store,
        policy["reviewer_policy"],
        _operation_id(composition_id, "reviewer:create"),
        from_snapshot=editor["workspace_id"],
    )
    bound = workspace.bind(
        store,
        made["id"],
        assignment,
        _operation_id(composition_id, "reviewer:bind"),
    )
    with store.db(write=True) as db:
        current = _get(db, composition_id)
        if current["state"] != "active" or current["phase"] != "editor":
            return
        _record_result(
            db,
            composition_id,
            "editor",
            editor,
            workspace_id=editor["workspace_id"],
            exported=exported,
        )
        db.execute(
            "INSERT INTO composition_members"
            "(composition_id,phase,workspace_id,task_id,created) VALUES(?,?,?,?,?)",
            (composition_id, "reviewer", made["id"], bound["task_id"], time.time()),
        )
        db.execute(
            "UPDATE compositions SET phase='reviewer',state='active',reason=NULL "
            "WHERE id=? AND state='active' AND phase='editor'",
            (composition_id,),
        )


def _halt(store, composition_id, reason):
    with store.db(write=True) as db:
        db.execute(
            "UPDATE compositions SET state='awaiting_codex',reason=? WHERE id=? AND state='active'",
            (reason, composition_id),
        )


def _advance(store, composition_id):
    with store.db() as db:
        row = _get(db, composition_id)
        policy = json.loads(row["policy"])
    if row["state"] == "stopping":
        _finish_stop(store, composition_id)
        return []
    if row["state"] == "stopped":
        return []

    if row["phase"] == "plan":
        with store.db() as db:
            flow = workflow._get(db, row["workflow_id"])
            accepted = _completed_result(store, db, flow["worker_task_id"], accepted=True)
        reviewed = flow["state"] == "awaiting_codex" and flow["reason"] == "approve_recommended"
        if accepted and reviewed:
            with store.db(write=True) as db:
                db.execute(
                    "UPDATE compositions SET state='active',reason=NULL WHERE id=? "
                    "AND state='awaiting_codex' AND reason='plan_acceptance_required' "
                    "AND phase='plan'",
                    (composition_id,),
                )
            _materialize_editor(store, row, policy, accepted)
            return []
        if accepted and not reviewed:
            _halt(store, composition_id, "plan_review_required")
            return []
        if row["state"] == "awaiting_codex":
            return []
        child = workflow.run(store, row["workflow_id"], once=True)
        if child["state"] == "awaiting_codex":
            reason = child["reason"]
            _halt(
                store,
                composition_id,
                "plan_acceptance_required" if reason == "approve_recommended" else reason,
            )
        return child.get("started", [])

    with store.db() as db:
        member = _member(db, composition_id, row["phase"])
        if not member:
            raise ControlError("invalid_state", "Composition phase has no bound workspace.")
        completed = _workspace_result(store, db, member)
    if completed:
        source, exported = completed
        if row["phase"] == "editor":
            _post_gate(store, row, policy, member, exported)
            _materialize_reviewer(
                store,
                row,
                policy,
                {**source, "workspace_id": member["workspace_id"]},
                exported,
            )
        else:
            recommendation = source["report"]["review"]["recommendation"]
            reason = {
                "approve": "final_review_ready",
                "revise": "final_review_revise",
                "blocked": "final_review_blocked",
            }[recommendation]
            with store.db(write=True) as db:
                _record_result(
                    db,
                    composition_id,
                    "reviewer",
                    source,
                    workspace_id=member["workspace_id"],
                    exported=exported,
                )
                db.execute(
                    "UPDATE compositions SET state='awaiting_codex',"
                    "reason=? WHERE id=? "
                    "AND state='active' AND phase='reviewer'",
                    (reason, composition_id),
                )
        return []
    if row["state"] == "awaiting_codex":
        return []
    child = workspace.run(store, member["workspace_id"], once=True, max_seconds=0)
    if child["state"] in ("awaiting_codex", "preparing"):
        _halt(store, composition_id, child.get("reason") or "workspace_incomplete")
    return child.get("started", [])


def _accepted(db, composition_id):
    reviewer = _result(db, composition_id, "reviewer")
    editor = _result(db, composition_id, "editor")
    if not reviewer or not editor:
        return None
    return db.execute(
        "SELECT * FROM review_decisions WHERE task_id=? AND revision=? AND run_id=? "
        "AND result_sha256=? AND kind='accept' ORDER BY rowid DESC LIMIT 1",
        (editor["task_id"], editor["revision"], editor["run_id"], editor["result_sha256"]),
    ).fetchone()


def accept_guard(store, db, task_id, run_id):
    """Enforce both explicit composition acceptance boundaries."""
    plan = db.execute(
        "SELECT c.id,w.state,w.reason,w.worker_task_id FROM compositions c "
        "JOIN workflows w ON w.id=c.workflow_id WHERE w.worker_task_id=?",
        (task_id,),
    ).fetchone()
    if plan:
        if plan["state"] != "awaiting_codex" or plan["reason"] != "approve_recommended":
            raise ControlError(
                "composition_plan_review_required",
                "Accept the plan only after its independent workflow review recommends approval.",
            )
        return
    member = db.execute(
        "SELECT m.*,c.id AS owner_id,c.state AS owner_state,c.reason AS owner_reason "
        "FROM composition_members m "
        "JOIN compositions c ON c.id=m.composition_id "
        "WHERE m.task_id=? AND m.phase='editor'",
        (task_id,),
    ).fetchone()
    if not member:
        return
    if member["owner_reason"] in ("final_review_revise", "final_review_blocked"):
        raise ControlError(
            "composition_review_veto",
            "The independent reviewer vetoed acceptance; inspect its criterion verdicts.",
        )
    if member["owner_state"] != "awaiting_codex" or member["owner_reason"] != "final_review_ready":
        raise ControlError(
            "composition_final_review_not_ready",
            "Accept the editor only while its completed independent review awaits Codex.",
        )
    editor = _result(db, member["composition_id"], "editor")
    reviewer = _result(db, member["composition_id"], "reviewer")
    if not editor or editor["run_id"] != run_id or not reviewer:
        raise ControlError(
            "composition_review_required",
            "Accept the exact editor result only after its independent frozen-snapshot review.",
        )
    reviewer_run = db.execute("SELECT * FROM runs WHERE id=?", (reviewer["run_id"],)).fetchone()
    checked = contract.inspect_run(store, db, reviewer_run)
    if checked["report"]["review"]["recommendation"] != "approve":
        raise ControlError(
            "composition_review_veto",
            "The independent reviewer did not recommend approval.",
        )
    saved = db.execute(
        "SELECT * FROM workspace_exports WHERE workspace_id=? AND run_id=?",
        (reviewer["workspace_id"], reviewer["run_id"]),
    ).fetchone()
    if (
        not saved
        or saved["manifest_sha256"] != reviewer["manifest_sha256"]
        or saved["result_sha256"] != reviewer["result_sha256"]
    ):
        raise ControlError(
            "composition_review_required", "The independent review export is missing or changed."
        )
    reviewed = workspace.export(store, reviewer["workspace_id"])
    if reviewed["manifest"]["tree_sha256"] != reviewer["tree_sha256"]:
        raise ControlError(
            "composition_review_required", "The independent review snapshot identity changed."
        )


def status(store, composition_id):
    _require(store)
    store.refresh()
    with store.db() as db:
        row = _get(db, composition_id)
        output = dict(row)
        output["policy"] = json.loads(row["policy"])
        members = {
            r["phase"]: dict(r)
            for r in db.execute(
                "SELECT * FROM composition_members WHERE composition_id=? ORDER BY rowid",
                (composition_id,),
            )
        }
        output["results"] = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM composition_results WHERE composition_id=? ORDER BY rowid",
                (composition_id,),
            )
        ]
        accepted = _accepted(db, composition_id)
        gates = {}
        for name in ("baseline", "post"):
            operation = db.execute(
                "SELECT response FROM task_operations WHERE operation_id=?",
                (_operation_id(composition_id, "test:" + name),),
            ).fetchone()
            gates[name] = json.loads(operation["response"]) if operation else None
    output["workflow"] = workflow.status(store, row["workflow_id"])
    output["members"] = {}
    for phase, member in members.items():
        output["members"][phase] = {
            **member,
            "workspace": workspace.status(store, member["workspace_id"]),
        }
    output["accepted"] = bool(accepted)
    output["test_gates"] = gates
    output["acceptance_decision_id"] = accepted["id"] if accepted else None
    if accepted and row["state"] not in ("stopping", "stopped"):
        output.update(state="accepted", reason="codex_accepted")
    return output


@contextmanager
def _coordinator(store, composition_id):
    lock_path = store.path / ("composition-" + composition_id + ".lock")
    lock_context = file_lock(lock_path, exclusive=True, blocking=False)
    try:
        lock_context.__enter__()
    except BlockingIOError:
        raise ControlError(
            "composition_busy", "Another coordinator owns this composition."
        ) from None
    try:
        yield
    finally:
        lock_context.__exit__(None, None, None)


def run(store, composition_id, *, once=False, max_seconds=30):
    _require(store)
    if (
        isinstance(max_seconds, bool)
        or not isinstance(max_seconds, (int, float))
        or not math.isfinite(max_seconds)
        or not 0 <= max_seconds <= 3600
    ):
        raise ControlError("invalid_composition", "max-seconds must be finite, 0..3600.")
    deadline = None if once else time.monotonic() + max_seconds
    started = []
    with _coordinator(store, composition_id):
        while True:
            try:
                started.extend(_advance(store, composition_id))
            except STATE_ERRORS as exc:
                _halt(store, composition_id, getattr(exc, "code", "composition_failed"))
            output = status(store, composition_id)
            if once:
                reason = "once"
            elif output["state"] not in ("active", "stopping"):
                reason = "needs_attention" if output["state"] == "awaiting_codex" else "idle"
            elif time.monotonic() >= deadline:
                reason = "deadline"
            else:
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
                continue
            return {**output, "started": started, "loop_reason": reason}


def _finish_stop(store, composition_id):
    with store.db() as db:
        row = _get(db, composition_id)
        workspace_ids = {
            member["workspace_id"]
            for member in db.execute(
                "SELECT workspace_id FROM composition_members WHERE composition_id=?",
                (composition_id,),
            )
        }
        for phase in ("editor", "reviewer"):
            operation = db.execute(
                "SELECT response FROM task_operations WHERE operation_id=?",
                (_operation_id(composition_id, phase + ":create"),),
            ).fetchone()
            if operation:
                workspace_id = json.loads(operation["response"]).get("id")
                if (
                    workspace_id
                    and db.execute(
                        "SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)
                    ).fetchone()
                ):
                    workspace_ids.add(workspace_id)
    children_stopped = True
    for workspace_id in workspace_ids:
        child = workspace.status(store, workspace_id)
        if child["state"] != "finished":
            child = workspace.stop(
                store,
                workspace_id,
                _operation_id(composition_id, "workspace:" + workspace_id + ":stop"),
            )
            children_stopped = children_stopped and child["state"] == "stopped"
    plan = workflow.stop(
        store,
        row["workflow_id"],
        _operation_id(composition_id, "plan:stop"),
    )
    if children_stopped and plan["state"] == "stopped":
        with store.db(write=True) as db:
            db.execute(
                "UPDATE compositions SET state='stopped',reason='stopped' "
                "WHERE id=? AND state='stopping'",
                (composition_id,),
            )


def stop(store, composition_id, operation_id):
    _require(store)
    with _coordinator(store, composition_id):
        with store.db(write=True) as db:
            fingerprint, prior = tasks._operation(
                db, operation_id, {"kind": "composition_stop", "composition": composition_id}
            )
            _get(db, composition_id)
            if not prior:
                db.execute(
                    "UPDATE compositions SET state='stopping',reason='stop_requested' "
                    "WHERE id=? AND state!='stopped'",
                    (composition_id,),
                )
                tasks._record(db, operation_id, fingerprint, {"id": composition_id})
        _finish_stop(store, composition_id)
        return {**status(store, composition_id), "deduplicated": bool(prior)}
