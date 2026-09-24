"""Additive schemas; existing sessions and runs retain their identity and rowids."""

VERSION = 15
TASK_SCHEMA = """
CREATE TABLE tasks (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, session_id TEXT REFERENCES sessions(id),
 current_revision INTEGER NOT NULL, active_run_id TEXT REFERENCES runs(id), created REAL NOT NULL
);
CREATE TABLE task_revisions (
 task_id TEXT NOT NULL REFERENCES tasks(id), revision INTEGER NOT NULL,
 prompt TEXT NOT NULL, prompt_sha256 TEXT NOT NULL,
 expected_parent_run_id TEXT REFERENCES runs(id), expected_backend_id TEXT,
 acknowledge_context INTEGER NOT NULL, created REAL NOT NULL,
 PRIMARY KEY(task_id,revision)
);
CREATE TABLE task_runs (
 run_id TEXT PRIMARY KEY REFERENCES runs(id), task_id TEXT NOT NULL,
 revision INTEGER NOT NULL, contract TEXT NOT NULL, prompt_sha256 TEXT NOT NULL,
 FOREIGN KEY(task_id,revision) REFERENCES task_revisions(task_id,revision)
);
CREATE TABLE task_operations (
 operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL
);
CREATE TABLE review_decisions (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
 run_id TEXT NOT NULL REFERENCES task_runs(run_id), result_sha256 TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('review','accept')), reviewer TEXT NOT NULL,
 recommendation TEXT NOT NULL, evidence TEXT NOT NULL, created REAL NOT NULL,
 FOREIGN KEY(task_id,revision) REFERENCES task_revisions(task_id,revision)
);
CREATE TRIGGER immutable_revision_update BEFORE UPDATE ON task_revisions
 BEGIN SELECT RAISE(ABORT,'task revisions are immutable'); END;
CREATE TRIGGER immutable_revision_delete BEFORE DELETE ON task_revisions
 BEGIN SELECT RAISE(ABORT,'task revisions are immutable'); END;
CREATE TRIGGER immutable_decision_update BEFORE UPDATE ON review_decisions
 BEGIN SELECT RAISE(ABORT,'review decisions are append-only'); END;
CREATE TRIGGER immutable_decision_delete BEFORE DELETE ON review_decisions
 BEGIN SELECT RAISE(ABORT,'review decisions are append-only'); END;
"""


QUEUE_SCHEMA = """
CREATE TABLE dependency_sets (
 task_id TEXT NOT NULL, revision INTEGER NOT NULL, fingerprint TEXT NOT NULL,
 PRIMARY KEY(task_id,revision),
 FOREIGN KEY(task_id,revision) REFERENCES task_revisions(task_id,revision)
);
CREATE TABLE task_dependencies (
 child_task_id TEXT NOT NULL, child_revision INTEGER NOT NULL,
 parent_task_id TEXT NOT NULL, parent_revision INTEGER NOT NULL,
 PRIMARY KEY(child_task_id,child_revision,parent_task_id,parent_revision),
 FOREIGN KEY(child_task_id,child_revision) REFERENCES dependency_sets(task_id,revision),
 FOREIGN KEY(parent_task_id,parent_revision) REFERENCES task_revisions(task_id,revision)
);
CREATE TABLE queue_entries (
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('waiting','reserved','cancelled','superseded')),
 run_id TEXT REFERENCES task_runs(run_id), blocked_reason TEXT,
 terminal_block INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
 FOREIGN KEY(task_id,revision) REFERENCES dependency_sets(task_id,revision)
);
CREATE UNIQUE INDEX queue_live_revision ON queue_entries(task_id,revision)
 WHERE state IN ('waiting','reserved');
CREATE INDEX queue_waiting ON queue_entries(state,id);
CREATE TABLE execution_inputs (
 run_id TEXT PRIMARY KEY REFERENCES task_runs(run_id),
 prompt TEXT NOT NULL, prompt_sha256 TEXT NOT NULL, base_sha256 TEXT NOT NULL
);
CREATE TABLE task_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
 revision INTEGER NOT NULL, run_id TEXT REFERENCES runs(id),
 kind TEXT NOT NULL, reference TEXT, status TEXT, reason TEXT, created REAL NOT NULL
);
CREATE INDEX task_events_cursor ON task_events(task_id,id);
CREATE TRIGGER task_revision_event AFTER INSERT ON task_revisions
 BEGIN INSERT INTO task_events(task_id,revision,kind,created)
 VALUES(NEW.task_id,NEW.revision,'revision_created',NEW.created); END;
CREATE TRIGGER task_reserved_event AFTER INSERT ON task_runs
 BEGIN INSERT INTO task_events(task_id,revision,run_id,kind,status,created)
 VALUES(NEW.task_id,NEW.revision,NEW.run_id,'run_status','pending',strftime('%s','now')); END;
CREATE TRIGGER task_run_event AFTER UPDATE OF status ON runs WHEN OLD.status != NEW.status
 BEGIN INSERT INTO task_events(task_id,revision,run_id,kind,status,reason,created)
 SELECT task_id,revision,NEW.id,'run_status',NEW.status,NEW.reason,strftime('%s','now')
 FROM task_runs WHERE run_id=NEW.id; END;
CREATE TRIGGER task_decision_event AFTER INSERT ON review_decisions
 BEGIN INSERT INTO task_events(task_id,revision,run_id,kind,reference,status,created)
 VALUES(NEW.task_id,NEW.revision,NEW.run_id,NEW.kind,NEW.id,NEW.recommendation,NEW.created); END;
CREATE TRIGGER queue_insert_event AFTER INSERT ON queue_entries
 BEGIN INSERT INTO task_events(task_id,revision,run_id,kind,reference,status,reason,created)
 VALUES(NEW.task_id,NEW.revision,NEW.run_id,'queue',NEW.id,NEW.state,NEW.blocked_reason,
 strftime('%s','now')); END;
CREATE TRIGGER queue_update_event AFTER UPDATE ON queue_entries
 WHEN OLD.state IS NOT NEW.state OR OLD.run_id IS NOT NEW.run_id
 OR OLD.blocked_reason IS NOT NEW.blocked_reason OR OLD.terminal_block != NEW.terminal_block
 BEGIN INSERT INTO task_events(task_id,revision,run_id,kind,reference,status,reason,created)
 VALUES(NEW.task_id,NEW.revision,NEW.run_id,'queue',NEW.id,NEW.state,NEW.blocked_reason,
 strftime('%s','now')); END;
"""


MESSAGE_SCHEMA = """
CREATE TABLE messages (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 task_id TEXT NOT NULL, base_revision INTEGER NOT NULL,
 target_session_id TEXT REFERENCES sessions(id), target_backend_id TEXT,
 content TEXT NOT NULL, content_sha256 TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('instruction','handoff')),
 source_run_id TEXT REFERENCES task_runs(run_id), source_result_sha256 TEXT,
 source_report TEXT, created REAL NOT NULL,
 UNIQUE(id,task_id),
 FOREIGN KEY(task_id,base_revision) REFERENCES task_revisions(task_id,revision)
);
CREATE INDEX message_task_order ON messages(task_id,seq);
CREATE TABLE message_cancellations (
 message_id TEXT PRIMARY KEY REFERENCES messages(id), created REAL NOT NULL
);
CREATE TABLE revision_messages (
 task_id TEXT NOT NULL, revision INTEGER NOT NULL, message_id TEXT NOT NULL,
 expected_prior_run_id TEXT,
 PRIMARY KEY(task_id,revision,message_id),
 FOREIGN KEY(task_id,revision) REFERENCES task_revisions(task_id,revision),
 FOREIGN KEY(message_id,task_id) REFERENCES messages(id,task_id),
 FOREIGN KEY(message_id,expected_prior_run_id) REFERENCES message_bindings(message_id,run_id)
);
CREATE UNIQUE INDEX task_run_identity ON task_runs(run_id,task_id,revision);
CREATE TABLE message_bindings (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL, run_id TEXT NOT NULL,
 task_id TEXT NOT NULL, revision INTEGER NOT NULL, created REAL NOT NULL,
 UNIQUE(message_id,run_id),
 FOREIGN KEY(run_id,task_id,revision) REFERENCES task_runs(run_id,task_id,revision),
 FOREIGN KEY(task_id,revision,message_id) REFERENCES revision_messages(task_id,revision,message_id)
);
CREATE INDEX message_delivery_order ON message_bindings(message_id,seq);
CREATE TRIGGER message_cancel_guard BEFORE INSERT ON message_cancellations
 WHEN EXISTS(SELECT 1 FROM message_bindings WHERE message_id=NEW.message_id)
 BEGIN SELECT RAISE(ABORT,'bound messages cannot be cancelled'); END;
CREATE TRIGGER message_bind_guard BEFORE INSERT ON message_bindings
 WHEN EXISTS(SELECT 1 FROM message_cancellations WHERE message_id=NEW.message_id)
 BEGIN SELECT RAISE(ABORT,'cancelled messages cannot be bound'); END;
CREATE TRIGGER message_enqueue_event AFTER INSERT ON messages
 BEGIN INSERT INTO task_events(task_id,revision,kind,reference,status,created)
 VALUES(NEW.task_id,NEW.base_revision,'message',NEW.id,'queued',NEW.created); END;
CREATE TRIGGER message_selection_event AFTER INSERT ON revision_messages
 BEGIN INSERT INTO task_events(task_id,revision,kind,reference,status,created)
 VALUES(NEW.task_id,NEW.revision,'message',NEW.message_id,'selected',strftime('%s','now')); END;
CREATE TRIGGER message_binding_event AFTER INSERT ON message_bindings
 BEGIN INSERT INTO task_events(task_id,revision,run_id,kind,reference,status,created)
 VALUES(NEW.task_id,NEW.revision,NEW.run_id,'message',NEW.message_id,'bound_to_run',NEW.created); END;
CREATE TRIGGER message_cancel_event AFTER INSERT ON message_cancellations
 BEGIN INSERT INTO task_events(task_id,revision,kind,reference,status,created)
 SELECT task_id,base_revision,'message',id,'cancelled',NEW.created
 FROM messages WHERE id=NEW.message_id; END;
"""


WORKFLOW_SCHEMA = """
CREATE TABLE workflows (
 id TEXT PRIMARY KEY, name TEXT NOT NULL,
 worker_task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
 reviewer_task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
 policy TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','awaiting_codex','stopping','stopped')),
 reason TEXT, phase TEXT NOT NULL CHECK(phase IN ('worker','review')),
 round INTEGER NOT NULL CHECK(round>=0), first_reserved_at REAL, created REAL NOT NULL,
 CHECK(worker_task_id!=reviewer_task_id)
);
CREATE TABLE workflow_steps (
 workflow_id TEXT NOT NULL REFERENCES workflows(id), phase TEXT NOT NULL, round INTEGER NOT NULL,
 task_id TEXT NOT NULL, revision INTEGER NOT NULL,
 source_task_id TEXT, source_revision INTEGER, source_run_id TEXT, source_result_sha256 TEXT,
 source_report TEXT, source_message_id TEXT REFERENCES messages(id), created REAL NOT NULL,
 PRIMARY KEY(workflow_id,phase,round), UNIQUE(task_id,revision),
 UNIQUE(workflow_id,phase,round,task_id,revision),
 FOREIGN KEY(task_id,revision) REFERENCES task_revisions(task_id,revision),
 FOREIGN KEY(source_run_id,source_task_id,source_revision) REFERENCES task_runs(run_id,task_id,revision),
 CHECK((source_run_id IS NULL AND source_task_id IS NULL AND source_revision IS NULL
        AND source_result_sha256 IS NULL AND source_report IS NULL AND source_message_id IS NULL)
    OR (source_run_id IS NOT NULL AND source_task_id IS NOT NULL AND source_revision IS NOT NULL
        AND source_result_sha256 IS NOT NULL AND source_report IS NOT NULL AND source_message_id IS NOT NULL))
);
CREATE TABLE workflow_runs (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id TEXT NOT NULL, phase TEXT NOT NULL,
 round INTEGER NOT NULL, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
 run_id TEXT NOT NULL UNIQUE, created REAL NOT NULL,
 UNIQUE(workflow_id,phase,round),
 FOREIGN KEY(workflow_id,phase,round,task_id,revision)
 REFERENCES workflow_steps(workflow_id,phase,round,task_id,revision),
 FOREIGN KEY(run_id,task_id,revision) REFERENCES task_runs(run_id,task_id,revision)
);
CREATE TRIGGER workflow_step_owner BEFORE INSERT ON workflow_steps
 WHEN NOT EXISTS(SELECT 1 FROM workflows w WHERE w.id=NEW.workflow_id AND
  ((NEW.phase='worker' AND w.worker_task_id=NEW.task_id) OR
   (NEW.phase='review' AND w.reviewer_task_id=NEW.task_id)))
 BEGIN SELECT RAISE(ABORT,'workflow step owner mismatch'); END;
CREATE TRIGGER workflow_policy_immutable BEFORE UPDATE ON workflows
 WHEN NEW.id IS NOT OLD.id OR NEW.name IS NOT OLD.name OR NEW.policy IS NOT OLD.policy
 OR NEW.worker_task_id IS NOT OLD.worker_task_id OR NEW.reviewer_task_id IS NOT OLD.reviewer_task_id
 OR NEW.created IS NOT OLD.created
 OR (OLD.first_reserved_at IS NOT NULL AND NEW.first_reserved_at IS NOT OLD.first_reserved_at)
 BEGIN SELECT RAISE(ABORT,'workflow policy and start are immutable'); END;
CREATE TRIGGER workflow_no_delete BEFORE DELETE ON workflows
 BEGIN SELECT RAISE(ABORT,'workflow history is retained'); END;
CREATE TRIGGER workflow_create_event AFTER INSERT ON workflows
 BEGIN INSERT INTO task_events(task_id,revision,kind,reference,status,created)
 VALUES(NEW.worker_task_id,1,'workflow',NEW.id,NEW.state,NEW.created); END;
CREATE TRIGGER workflow_change_event AFTER UPDATE ON workflows
 WHEN OLD.state IS NOT NEW.state OR OLD.phase IS NOT NEW.phase OR OLD.round!=NEW.round
 OR OLD.reason IS NOT NEW.reason OR OLD.first_reserved_at IS NOT NEW.first_reserved_at
 BEGIN INSERT INTO task_events(task_id,revision,kind,reference,status,reason,created)
 SELECT NEW.worker_task_id,current_revision,'workflow',NEW.id,NEW.state,NEW.reason,strftime('%s','now')
 FROM tasks WHERE id=NEW.worker_task_id; END;
"""


WORKSPACE_SCHEMA = """
CREATE TABLE workspace_creations (
 id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE REFERENCES task_operations(operation_id),
 created REAL NOT NULL
);
CREATE TABLE workspaces (
 id TEXT PRIMARY KEY REFERENCES workspace_creations(id), source_repo TEXT NOT NULL, base_commit TEXT NOT NULL,
 source_workspace TEXT REFERENCES workspaces(id), policy TEXT NOT NULL,
 baseline_sha256 TEXT NOT NULL, state TEXT NOT NULL
 CHECK(state IN ('idle','active','operating','awaiting_codex','finished','stopping','stopped')),
 reason TEXT, created REAL NOT NULL
);
CREATE TABLE workspace_tasks (
 workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id),
 task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id), assignment TEXT NOT NULL
);
CREATE TABLE workspace_calls (
 run_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 task_id TEXT NOT NULL, revision INTEGER NOT NULL, tree_sha256 TEXT NOT NULL,
 receipts_sha256 TEXT NOT NULL, envelope TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(workspace_id,revision),
 FOREIGN KEY(run_id,task_id,revision) REFERENCES task_runs(run_id,task_id,revision)
);
CREATE TABLE workspace_requests (
 run_id TEXT NOT NULL REFERENCES workspace_calls(run_id), seq INTEGER NOT NULL,
 action TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(run_id,seq)
);
CREATE TABLE workspace_receipts (
 run_id TEXT NOT NULL, seq INTEGER NOT NULL, result TEXT NOT NULL, created REAL NOT NULL,
 PRIMARY KEY(run_id,seq), FOREIGN KEY(run_id,seq) REFERENCES workspace_requests(run_id,seq)
);
CREATE TABLE workspace_commands (
 run_id TEXT NOT NULL, seq INTEGER NOT NULL, owner_pid INTEGER NOT NULL,
 owner_start TEXT NOT NULL, boot TEXT NOT NULL, namespace TEXT NOT NULL,
 child_pid INTEGER, child_start TEXT,
 PRIMARY KEY(run_id,seq), FOREIGN KEY(run_id,seq) REFERENCES workspace_requests(run_id,seq)
);
CREATE TABLE workspace_exports (
 workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id), run_id TEXT NOT NULL UNIQUE REFERENCES workspace_calls(run_id),
 manifest_sha256 TEXT NOT NULL, result_sha256 TEXT NOT NULL, created REAL NOT NULL
);
CREATE TRIGGER workspace_policy_immutable BEFORE UPDATE ON workspaces
 WHEN NEW.id IS NOT OLD.id OR NEW.source_repo IS NOT OLD.source_repo
 OR NEW.base_commit IS NOT OLD.base_commit OR NEW.source_workspace IS NOT OLD.source_workspace
 OR NEW.policy IS NOT OLD.policy OR NEW.baseline_sha256 IS NOT OLD.baseline_sha256
 OR NEW.created IS NOT OLD.created
 BEGIN SELECT RAISE(ABORT,'workspace authority is immutable'); END;
CREATE TRIGGER workspace_no_delete BEFORE DELETE ON workspaces
 BEGIN SELECT RAISE(ABORT,'workspace history is retained'); END;
CREATE TRIGGER workspace_command_identity BEFORE UPDATE ON workspace_commands
 WHEN OLD.child_pid IS NOT NULL OR NEW.child_pid IS NULL OR NEW.child_start IS NULL
 OR NEW.run_id IS NOT OLD.run_id OR NEW.seq IS NOT OLD.seq
 OR NEW.owner_pid IS NOT OLD.owner_pid OR NEW.owner_start IS NOT OLD.owner_start
 OR NEW.boot IS NOT OLD.boot OR NEW.namespace IS NOT OLD.namespace
 BEGIN SELECT RAISE(ABORT,'command identity is immutable'); END;
CREATE TRIGGER workspace_command_no_delete BEFORE DELETE ON workspace_commands
 BEGIN SELECT RAISE(ABORT,'command history is retained'); END;
"""


def _apply(db, script, version):
    # executescript implicitly commits; migrations must keep DDL and user_version atomic.
    import sqlite3

    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("Incomplete schema statement")
    db.execute(f"PRAGMA user_version={version}")


def add_task_schema(db):
    _apply(db, TASK_SCHEMA, 4)


def add_queue_schema(db):
    _apply(db, QUEUE_SCHEMA, 5)
    for table in ("dependency_sets", "task_dependencies", "execution_inputs", "task_events"):
        for action in ("UPDATE", "DELETE"):
            db.execute(
                f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                "BEGIN SELECT RAISE(ABORT,'task records are append-only'); END"
            )


def add_message_schema(db):
    _apply(db, MESSAGE_SCHEMA, 6)
    for table in ("messages", "message_cancellations", "revision_messages", "message_bindings"):
        for action in ("UPDATE", "DELETE"):
            db.execute(
                f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                "BEGIN SELECT RAISE(ABORT,'message records are append-only'); END"
            )


def add_workflow_schema(db):
    _apply(db, WORKFLOW_SCHEMA, 7)
    for table in ("workflow_steps", "workflow_runs"):
        for action in ("UPDATE", "DELETE"):
            db.execute(
                f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                "BEGIN SELECT RAISE(ABORT,'workflow records are append-only'); END"
            )


def add_workspace_schema(db):
    _apply(db, WORKSPACE_SCHEMA, 8)
    for table in (
        "workspace_creations",
        "workspace_tasks",
        "workspace_calls",
        "workspace_requests",
        "workspace_receipts",
        "workspace_exports",
    ):
        for action in ("UPDATE", "DELETE"):
            db.execute(
                f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                "BEGIN SELECT RAISE(ABORT,'workspace records are append-only'); END"
            )


def add_execution_schema(db):
    _apply(
        db,
        """
ALTER TABLE sessions ADD COLUMN effort TEXT CHECK(effort IN ('low','medium','high','xhigh','max'));
ALTER TABLE runs ADD COLUMN effort TEXT CHECK(effort IN ('low','medium','high','xhigh','max'));
CREATE TRIGGER session_effort_immutable BEFORE UPDATE OF effort ON sessions
 WHEN NEW.effort IS NOT OLD.effort
 BEGIN SELECT RAISE(ABORT,'session effort is immutable'); END;
CREATE TRIGGER run_effort_immutable BEFORE UPDATE OF effort ON runs
 WHEN NEW.effort IS NOT OLD.effort
 BEGIN SELECT RAISE(ABORT,'run effort is immutable'); END;
CREATE TRIGGER run_effort_matches_session BEFORE INSERT ON runs
 WHEN NEW.effort IS NOT (SELECT effort FROM sessions WHERE id=NEW.session_id)
 BEGIN SELECT RAISE(ABORT,'run effort must match session'); END;
""",
        9,
    )


COMPOSITION_SCHEMA = """
CREATE TABLE compositions (
 id TEXT PRIMARY KEY, name TEXT NOT NULL,
 workflow_id TEXT NOT NULL UNIQUE REFERENCES workflows(id),
 policy TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('active','awaiting_codex','stopping','stopped')),
 reason TEXT, phase TEXT NOT NULL CHECK(phase IN ('plan','editor','reviewer')),
 created REAL NOT NULL
);
CREATE TABLE composition_members (
 composition_id TEXT NOT NULL REFERENCES compositions(id),
 phase TEXT NOT NULL CHECK(phase IN ('editor','reviewer')),
 workspace_id TEXT NOT NULL UNIQUE REFERENCES workspace_tasks(workspace_id),
 task_id TEXT NOT NULL UNIQUE REFERENCES workspace_tasks(task_id), created REAL NOT NULL,
 PRIMARY KEY(composition_id,phase)
);
CREATE TABLE composition_results (
 composition_id TEXT NOT NULL REFERENCES compositions(id),
 phase TEXT NOT NULL CHECK(phase IN ('plan','editor','reviewer')),
 task_id TEXT NOT NULL, revision INTEGER NOT NULL,
 run_id TEXT NOT NULL UNIQUE, result_sha256 TEXT NOT NULL,
 workspace_id TEXT, manifest_sha256 TEXT, tree_sha256 TEXT, created REAL NOT NULL,
 PRIMARY KEY(composition_id,phase),
 FOREIGN KEY(composition_id) REFERENCES compositions(id),
 FOREIGN KEY(run_id,task_id,revision) REFERENCES task_runs(run_id,task_id,revision),
 FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
 CHECK((phase='plan' AND workspace_id IS NULL AND manifest_sha256 IS NULL AND tree_sha256 IS NULL)
    OR (phase IN ('editor','reviewer') AND workspace_id IS NOT NULL
        AND manifest_sha256 IS NOT NULL AND tree_sha256 IS NOT NULL))
);
CREATE TRIGGER composition_policy_immutable BEFORE UPDATE ON compositions
 WHEN NEW.id IS NOT OLD.id OR NEW.name IS NOT OLD.name OR NEW.workflow_id IS NOT OLD.workflow_id
 OR NEW.policy IS NOT OLD.policy OR NEW.created IS NOT OLD.created
 BEGIN SELECT RAISE(ABORT,'composition policy is immutable'); END;
CREATE TRIGGER composition_no_delete BEFORE DELETE ON compositions
 BEGIN SELECT RAISE(ABORT,'composition history is retained'); END;
CREATE TRIGGER composition_member_binding BEFORE INSERT ON composition_members
 WHEN NOT EXISTS(SELECT 1 FROM workspace_tasks
                 WHERE workspace_id=NEW.workspace_id AND task_id=NEW.task_id)
 BEGIN SELECT RAISE(ABORT,'composition member binding mismatch'); END;
CREATE TRIGGER composition_workspace_result BEFORE INSERT ON composition_results
 WHEN NEW.phase IN ('editor','reviewer') AND NOT EXISTS(
  SELECT 1 FROM composition_members m JOIN workspace_exports e
   ON e.workspace_id=m.workspace_id
  WHERE m.composition_id=NEW.composition_id AND m.phase=NEW.phase
   AND m.workspace_id=NEW.workspace_id AND m.task_id=NEW.task_id
   AND e.run_id=NEW.run_id AND e.result_sha256=NEW.result_sha256
   AND e.manifest_sha256=NEW.manifest_sha256)
 BEGIN SELECT RAISE(ABORT,'composition result does not match frozen workspace'); END;
"""


def add_composition_schema(db):
    _apply(db, COMPOSITION_SCHEMA, 10)
    for table in ("composition_members", "composition_results"):
        for action in ("UPDATE", "DELETE"):
            db.execute(
                f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                "BEGIN SELECT RAISE(ABORT,'composition records are append-only'); END"
            )


TELEMETRY_SCHEMA = """
CREATE TABLE run_telemetry (
 run_id TEXT PRIMARY KEY REFERENCES runs(id),
 usage TEXT NOT NULL, model_usage TEXT NOT NULL,
 provider_cost_usd REAL, duration_api_ms REAL, duration_ms REAL NOT NULL,
 created REAL NOT NULL,
 CHECK(provider_cost_usd IS NULL OR provider_cost_usd>=0),
 CHECK(duration_api_ms IS NULL OR duration_api_ms>=0),
 CHECK(duration_ms>=0)
);
CREATE TRIGGER immutable_run_telemetry_update BEFORE UPDATE ON run_telemetry
 BEGIN SELECT RAISE(ABORT,'run telemetry is append-only'); END;
CREATE TRIGGER immutable_run_telemetry_delete BEFORE DELETE ON run_telemetry
 BEGIN SELECT RAISE(ABORT,'run telemetry is append-only'); END;
"""


def add_telemetry_schema(db):
    _apply(db, TELEMETRY_SCHEMA, 11)


APPLICATION_SCHEMA = """
CREATE TABLE workspace_applications (
 workspace_id TEXT PRIMARY KEY REFERENCES workspace_exports(workspace_id),
 operation_id TEXT NOT NULL UNIQUE REFERENCES task_operations(operation_id),
 source_repo TEXT NOT NULL, base_commit TEXT NOT NULL,
 patch_sha256 TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('applying','applied','unknown')),
 applied_tree_sha256 TEXT, created REAL NOT NULL, finished REAL,
 CHECK((state='applied' AND applied_tree_sha256 IS NOT NULL AND finished IS NOT NULL)
    OR state!='applied')
);
CREATE TRIGGER workspace_application_identity BEFORE UPDATE ON workspace_applications
 WHEN NEW.workspace_id IS NOT OLD.workspace_id OR NEW.operation_id IS NOT OLD.operation_id
 OR NEW.source_repo IS NOT OLD.source_repo OR NEW.base_commit IS NOT OLD.base_commit
 OR NEW.patch_sha256 IS NOT OLD.patch_sha256
 OR NEW.manifest_sha256 IS NOT OLD.manifest_sha256 OR NEW.created IS NOT OLD.created
 OR OLD.state!='applying' OR NEW.state NOT IN ('applied','unknown')
 BEGIN SELECT RAISE(ABORT,'workspace application identity is immutable'); END;
CREATE TRIGGER workspace_application_no_delete BEFORE DELETE ON workspace_applications
 BEGIN SELECT RAISE(ABORT,'workspace application history is retained'); END;
"""


def add_application_schema(db):
    _apply(db, APPLICATION_SCHEMA, 12)


def add_platform_schema(db):
    _apply(
        db,
        """
ALTER TABLE runs ADD COLUMN platform TEXT;
ALTER TABLE runs ADD COLUMN worker_identity_json TEXT;
ALTER TABLE runs ADD COLUMN child_identity_json TEXT;
ALTER TABLE runs ADD COLUMN execution_scope TEXT;
ALTER TABLE runs ADD COLUMN process_backend TEXT;
ALTER TABLE runs ADD COLUMN sandbox_backend TEXT;
""",
        13,
    )


# The observation wire contract is pinned independently of the DB schema version so
# the folded view can evolve without an offline migration.
OBSERVATION_CONTRACT = "claude-control.observation.v1"
_NOW = "(julianday('now')-2440587.5)*86400.0"
_WIRE = f"'{OBSERVATION_CONTRACT}'"
_EVENT = (
    "INSERT INTO observation_events"
    "(contract,kind,entity_kind,entity_id,recorded_at,effective_at,payload)"
)
_JSON = "json(CASE WHEN json_valid({0}) THEN {0} ELSE 'null' END)"

# Projections are shared by the triggers (p='NEW.') and the baseline snapshot (p='').
# Each one is a self-contained node or edge state carrying only stable identifiers,
# names, roles, models, states, reasons, timestamps, token counts and costs. Project
# paths, source repositories, prompts, results, bodies, policies, operation actions,
# receipts, account identity and credentials are deliberately never projected.
_SESSION = (
    "json_object('node','session','id',{p}id,'name',{p}name,'backend_id',{p}backend_id,"
    "'model',{p}model,'role',{p}role,'effort',{p}effort,'blocked',{p}blocked,"
    "'created',{p}created)"
)
_RUN = (
    "json_object('node','run','id',{p}id,'session_id',{p}session_id,"
    "'backend_id',{p}backend_id,'state',{p}status,'effort',{p}effort,"
    "'resume',{p}resume,'timeout',{p}timeout,'created',{p}created,'started',{p}started,"
    "'finished',{p}finished,'exit_code',{p}exit_code,"
    "'models'," + _JSON.format("{p}actual_models") + ")"
)
_TASK = (
    "json_object('node','task','id',{p}id,'name',{p}name,'session_id',{p}session_id,"
    "'revision',{p}current_revision,'active_run_id',{p}active_run_id,'created',{p}created)"
)
_WORKFLOW = (
    "json_object('node','workflow','id',{p}id,'name',{p}name,"
    "'worker_task_id',{p}worker_task_id,'reviewer_task_id',{p}reviewer_task_id,"
    "'state',{p}state,'phase',{p}phase,'round',{p}round,"
    "'first_reserved_at',{p}first_reserved_at,'created',{p}created)"
)
_WORKSPACE = (
    "json_object('node','workspace','id',{p}id,'base_commit',{p}base_commit,"
    "'source_workspace',{p}source_workspace,'baseline_sha256',{p}baseline_sha256,"
    "'state',{p}state,'created',{p}created)"
)
_COMPOSITION = (
    "json_object('node','composition','id',{p}id,'name',{p}name,"
    "'workflow_id',{p}workflow_id,'state',{p}state,"
    "'phase',{p}phase,'created',{p}created)"
)
_TELEMETRY = (
    "json_object('node','run_telemetry','run_id',{p}run_id,"
    "'usage'," + _JSON.format("{p}usage") + ","
    "'model_usage'," + _JSON.format("{p}model_usage") + ","
    "'provider_cost_usd',{p}provider_cost_usd,'duration_api_ms',{p}duration_api_ms,"
    "'duration_ms',{p}duration_ms,'created',{p}created)"
)
# The delivery edge names its origin 'envelope' so no payload ever mentions bodies.
_DEPENDENCY = (
    "json_object('edge','dependency','from_kind','task','from_id',{p}child_task_id,"
    "'from_revision',{p}child_revision,'to_kind','task','to_id',{p}parent_task_id,"
    "'to_revision',{p}parent_revision)"
)
_BINDING = (
    "json_object('edge','binding','from_kind','envelope','from_id',{p}message_id,"
    "'to_kind','run','to_id',{p}run_id,'task_id',{p}task_id,'revision',{p}revision,"
    "'created',{p}created)"
)
_REVIEW = (
    "json_object('edge','review','id',{p}id,'from_kind','run','from_id',{p}run_id,"
    "'to_kind','task','to_id',{p}task_id,'revision',{p}revision,'decision',{p}kind,"
    "'reviewer',{p}reviewer,'recommendation',{p}recommendation,'created',{p}created)"
)
_TASK_RUN = (
    "json_object('edge','task_run','from_kind','task','from_id',{p}task_id,"
    "'from_revision',{p}revision,'to_kind','run','to_id',{p}run_id)"
)
_WORKFLOW_RUN = (
    "json_object('edge','workflow_run','from_kind','workflow','from_id',{p}workflow_id,"
    "'phase',{p}phase,'round',{p}round,'task_id',{p}task_id,'revision',{p}revision,"
    "'to_kind','run','to_id',{p}run_id,'created',{p}created)"
)
_WORKSPACE_TASK = (
    "json_object('edge','workspace_task','from_kind','workspace','from_id',{p}workspace_id,"
    "'to_kind','task','to_id',{p}task_id)"
)
_WORKSPACE_RUN = (
    "json_object('edge','workspace_run','from_kind','workspace','from_id',{p}workspace_id,"
    "'task_id',{p}task_id,'revision',{p}revision,'to_kind','run','to_id',{p}run_id,"
    "'created',{p}created)"
)
_COMPOSITION_MEMBER = (
    "json_object('edge','composition_member','from_kind','composition',"
    "'from_id',{p}composition_id,'phase',{p}phase,'task_id',{p}task_id,"
    "'to_kind','workspace','to_id',{p}workspace_id,'created',{p}created)"
)
_COMPOSITION_RUN = (
    "json_object('edge','composition_run','from_kind','composition',"
    "'from_id',{p}composition_id,'phase',{p}phase,'task_id',{p}task_id,"
    "'revision',{p}revision,'workspace_id',{p}workspace_id,'to_kind','run',"
    "'to_id',{p}run_id,'created',{p}created)"
)

_NODES = (
    (
        "session",
        "sessions",
        _SESSION,
        "NEW.created",
        _NOW,
        "OLD.blocked IS NOT NEW.blocked OR OLD.backend_id IS NOT NEW.backend_id",
    ),
    (
        "run",
        "runs",
        _RUN,
        "NEW.created",
        f"coalesce(NEW.finished,NEW.started,{_NOW})",
        "OLD.status IS NOT NEW.status",
    ),
    (
        "task",
        "tasks",
        _TASK,
        "NEW.created",
        _NOW,
        "OLD.current_revision IS NOT NEW.current_revision"
        " OR OLD.active_run_id IS NOT NEW.active_run_id",
    ),
    (
        "workflow",
        "workflows",
        _WORKFLOW,
        "NEW.created",
        _NOW,
        "OLD.state IS NOT NEW.state OR OLD.phase IS NOT NEW.phase"
        " OR OLD.round IS NOT NEW.round OR OLD.reason IS NOT NEW.reason",
    ),
    (
        "workspace",
        "workspaces",
        _WORKSPACE,
        "NEW.created",
        _NOW,
        "OLD.state IS NOT NEW.state OR OLD.reason IS NOT NEW.reason",
    ),
    (
        "composition",
        "compositions",
        _COMPOSITION,
        "NEW.created",
        _NOW,
        "OLD.state IS NOT NEW.state OR OLD.phase IS NOT NEW.phase"
        " OR OLD.reason IS NOT NEW.reason",
    ),
)
_EDGES = (
    (
        "dependency",
        "task_dependencies",
        _DEPENDENCY,
        "NEW.child_task_id||':'||NEW.child_revision||'>'"
        "||NEW.parent_task_id||':'||NEW.parent_revision",
        _NOW,
    ),
    ("binding", "message_bindings", _BINDING, "NEW.message_id||'>'||NEW.run_id", "NEW.created"),
    ("review", "review_decisions", _REVIEW, "NEW.id", "NEW.created"),
    (
        "task_run",
        "task_runs",
        _TASK_RUN,
        "NEW.task_id||':'||NEW.revision||'>'||NEW.run_id",
        _NOW,
    ),
    (
        "workflow_run",
        "workflow_runs",
        _WORKFLOW_RUN,
        "NEW.workflow_id||':'||NEW.phase||':'||NEW.round||'>'||NEW.run_id",
        "NEW.created",
    ),
    (
        "workspace_task",
        "workspace_tasks",
        _WORKSPACE_TASK,
        "NEW.workspace_id||'>'||NEW.task_id",
        _NOW,
    ),
    (
        "workspace_run",
        "workspace_calls",
        _WORKSPACE_RUN,
        "NEW.workspace_id||'>'||NEW.run_id",
        "NEW.created",
    ),
    (
        "composition_member",
        "composition_members",
        _COMPOSITION_MEMBER,
        "NEW.composition_id||':'||NEW.phase||'>'||NEW.workspace_id",
        "NEW.created",
    ),
    (
        "composition_run",
        "composition_results",
        _COMPOSITION_RUN,
        "NEW.composition_id||':'||NEW.phase||'>'||NEW.run_id",
        "NEW.created",
    ),
)

OBSERVATION_LEDGER = """
CREATE TABLE observation_events (
 cursor INTEGER PRIMARY KEY AUTOINCREMENT, contract TEXT NOT NULL, kind TEXT NOT NULL,
 entity_kind TEXT NOT NULL, entity_id TEXT NOT NULL,
 recorded_at REAL NOT NULL, effective_at REAL NOT NULL,
 payload TEXT NOT NULL CHECK(json_valid(payload))
);
CREATE INDEX observation_entity_cursor ON observation_events(entity_kind,entity_id,cursor);
CREATE INDEX observation_kind_cursor ON observation_events(kind,cursor);
CREATE TRIGGER immutable_observation_events_update BEFORE UPDATE ON observation_events
 BEGIN SELECT RAISE(ABORT,'observation events are append-only'); END;
CREATE TRIGGER immutable_observation_events_delete BEFORE DELETE ON observation_events
 BEGIN SELECT RAISE(ABORT,'observation events are append-only'); END;
"""


def _observation_ddl():
    """Append every event in the same transaction as the mutation that caused it."""
    parts = [OBSERVATION_LEDGER]
    for kind, table, projection, born, moved, condition in _NODES:
        body = projection.format(p="NEW.")
        parts.append(
            f"CREATE TRIGGER observation_{kind}_created AFTER INSERT ON {table}\n"
            f" BEGIN {_EVENT}\n"
            f" VALUES({_WIRE},'node_created','{kind}',NEW.id,{_NOW},{born},{body}); END;"
        )
        parts.append(
            f"CREATE TRIGGER observation_{kind}_state AFTER UPDATE ON {table}\n"
            f" WHEN {condition}\n"
            f" BEGIN {_EVENT}\n"
            f" VALUES({_WIRE},'node_state','{kind}',NEW.id,{_NOW},{moved},{body}); END;"
        )
    for kind, table, projection, identity, effective in _EDGES:
        body = projection.format(p="NEW.")
        parts.append(
            f"CREATE TRIGGER observation_{kind}_created AFTER INSERT ON {table}\n"
            f" BEGIN {_EVENT}\n"
            f" VALUES({_WIRE},'edge_created','{kind}',{identity},{_NOW},{effective},{body});"
            " END;"
        )
    parts.append(
        f"CREATE TRIGGER observation_run_telemetry AFTER INSERT ON run_telemetry\n"
        f" BEGIN {_EVENT}\n"
        f" VALUES({_WIRE},'run_telemetry','run',NEW.run_id,{_NOW},NEW.created,"
        f"{_TELEMETRY.format(p='NEW.')}); END;"
    )
    return "\n".join(parts) + "\n"


def _snapshot(entries):
    return ",".join(
        f"'{kind}',json((SELECT json_group_array({projection.format(p='')}) FROM {table}))"
        for kind, table, projection, *_ in entries
    )


def add_observation_schema(db):
    _apply(db, _observation_ddl(), 14)
    now = db.execute(f"SELECT {_NOW}").fetchone()[0]
    # History before this migration is summarised, never replayed: the single baseline
    # event states its own fidelity so no folded transition is ever invented.
    db.execute(
        f"{_EVENT} SELECT ?,'baseline','store','baseline',?,?,json_object("
        "'fidelity','baseline_only',"
        f"'nodes',json_object({_snapshot(_NODES)},'operation',json('[]')) ,"
        f"'edges',json_object({_snapshot(_EDGES)}),"
        "'telemetry',json((SELECT json_group_array("
        f"{_TELEMETRY.format(p='')}) FROM run_telemetry)))",
        (OBSERVATION_CONTRACT, now, now),
    )


def add_observer_v2_schema(db):
    """Project controller operations without copying their content-bearing fields."""

    operation = (
        "CASE json_extract({action},'$.op') "
        "WHEN 'read' THEN 'read' WHEN 'write' THEN 'write' "
        "WHEN 'patch' THEN 'patch' WHEN 'named_check' THEN 'named_check' "
        "ELSE 'unknown' END"
    )
    request_payload = (
        "json_object('node','operation','id','op:'||NEW.run_id||':'||NEW.seq,"
        "'run_id',NEW.run_id,'seq',NEW.seq,'operation',"
        + operation.format(action="NEW.action")
        + ",'state','started','started',NEW.created)"
    )
    receipt_payload = (
        "json_object('node','operation','id','op:'||NEW.run_id||':'||NEW.seq,"
        "'run_id',NEW.run_id,'seq',NEW.seq,'operation',"
        + operation.format(action="q.action")
        + ",'state','settled','started',q.created,'finished',NEW.created)"
    )
    _apply(
        db,
        f"""
CREATE TRIGGER observation_operation_created AFTER INSERT ON workspace_requests
 BEGIN {_EVENT}
 VALUES({_WIRE},'node_created','operation','op:'||NEW.run_id||':'||NEW.seq,
        {_NOW},NEW.created,{request_payload}); END;
CREATE TRIGGER observation_operation_settled AFTER INSERT ON workspace_receipts
 BEGIN {_EVENT}
 SELECT {_WIRE},'node_state','operation','op:'||NEW.run_id||':'||NEW.seq,
        {_NOW},NEW.created,{receipt_payload}
 FROM workspace_requests q WHERE q.run_id=NEW.run_id AND q.seq=NEW.seq; END;
""",
        15,
    )
    # Schema-14 stores have no operation history. Add one final summary per request,
    # ordered deterministically, without inventing a start/settle transition.
    db.execute(
        f"""
INSERT INTO observation_events
 (contract,kind,entity_kind,entity_id,recorded_at,effective_at,payload)
SELECT {_WIRE},'node_created','operation','op:'||q.run_id||':'||q.seq,
       {_NOW},coalesce(r.created,q.created),
       json_object('node','operation','id','op:'||q.run_id||':'||q.seq,
                   'run_id',q.run_id,'seq',q.seq,'operation',
                   {operation.format(action='q.action')},
                   'state',CASE WHEN r.run_id IS NULL THEN 'started' ELSE 'settled' END,
                   'started',q.created,
                   'finished',CASE WHEN r.run_id IS NULL THEN NULL ELSE r.created END)
FROM workspace_requests q
LEFT JOIN workspace_receipts r ON r.run_id=q.run_id AND r.seq=q.seq
ORDER BY q.created,q.run_id,q.seq
"""
    )
