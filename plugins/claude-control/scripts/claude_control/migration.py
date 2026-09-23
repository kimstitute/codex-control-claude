"""Explicit offline incremental upgrade, with a verified backup and resumable marker."""

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from .platform.locks import file_lock
from .schema import (
    VERSION,
    add_application_schema,
    add_composition_schema,
    add_execution_schema,
    add_message_schema,
    add_observation_schema,
    add_platform_schema,
    add_queue_schema,
    add_task_schema,
    add_telemetry_schema,
    add_workflow_schema,
    add_workspace_schema,
)
from .store import (
    ACTIVE,
    ControlError,
    check_host_and_principal,
    private_dir,
    secure_new_file,
    verify_private_entry,
    write_json,
)


def _open(path):
    if not Path(path).is_dir():
        raise ControlError("not_initialized", "Run init with this state directory first.")
    path = private_dir(path)
    for name in ("config.json", "state.sqlite3", "runs"):
        verify_private_entry(path / name)
    config = json.loads((path / "config.json").read_text())
    check_host_and_principal(config)
    return path, config


def migration_status(path):
    path, config = _open(path)
    with closing(sqlite3.connect(f"{(path / 'state.sqlite3').as_uri()}?mode=ro", uri=True)) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        active = db.execute(
            "SELECT id,status FROM runs WHERE status IN (?,?,?,?,?,?)", ACTIVE
        ).fetchall()
    journals = [path / f"migration-{v}-{v + 1}.json" for v in range(3, VERSION)]
    journal = next((p for p in reversed(journals) if p.exists()), journals[0])
    return dict(
        schema=config.get("schema"),
        db_version=version,
        target=VERSION,
        active_runs=[dict(id=row[0], status=row[1]) for row in active],
        journal=json.loads(journal.read_text()) if journal.exists() else None,
    )


def _checkpoint(phase):
    """Fault-injection boundary for offline migration regression tests."""


def _no_active(db):
    if db.execute("SELECT 1 FROM runs WHERE status IN (?,?,?,?,?,?) LIMIT 1", ACTIVE).fetchone():
        raise ControlError(
            "migration_busy", "Stop/reconcile all active and unknown runs before migration."
        )


def _check_backup(path, source):
    if not path.is_file():
        raise ControlError("migration_backup", "Private migration backup is missing or unsafe.")
    try:
        verify_private_entry(path)
    except ControlError as exc:
        raise ControlError("migration_backup", str(exc)) from exc
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute(
            "PRAGMA user_version"
        ).fetchone()[0] != (0 if source == 3 else source):
            raise ControlError("migration_backup", "Migration backup did not pass validation.")


def migrate(path, *, offline=False, target=VERSION):
    if not offline:
        raise ControlError(
            "offline_required",
            "Stop all old/new CLI clients and workers, then run migrate --offline. "
            "Use migrate --status to inspect; unresolved executions must not be forced terminal.",
        )
    if target not in tuple(range(4, VERSION + 1)):
        raise ControlError("schema_mismatch", "Unsupported migration target.")
    os.umask(0o077)
    path, _ = _open(path)
    changed = False
    backups = []
    lock_context = file_lock(path / "lifecycle.lock", exclusive=True, blocking=False)
    try:
        lock_context.__enter__()
    except BlockingIOError:
        raise ControlError(
            "migration_busy", "Another controller operation still holds the store lock."
        ) from None
    try:
        while True:
            path, config = _open(path)
            with closing(sqlite3.connect(path / "state.sqlite3", isolation_level=None)) as db:
                db.execute("PRAGMA foreign_keys=ON")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                schema = config.get("schema")
                # Complete a journal even when the previous process died after config commit.
                if schema in tuple(range(4, VERSION + 1)) and version == schema:
                    previous = path / f"migration-{schema - 1}-{schema}.json"
                    if previous.exists():
                        journal = json.loads(previous.read_text())
                        if journal.get("phase") != "complete":
                            write_json(previous, {**journal, "phase": "complete"})
                if schema == target and version == target:
                    return dict(schema=target, migrated=changed, backups=backups)
                if schema == "migrating":
                    candidates = []
                    for source in range(3, VERSION):
                        journal_path = path / f"migration-{source}-{source + 1}.json"
                        if not journal_path.is_file() or journal_path.is_symlink():
                            continue
                        journal = json.loads(journal_path.read_text())
                        original = journal.get("original_config", {})
                        if (
                            journal.get("source") == source
                            and journal.get("target") == source + 1
                            and journal.get("phase") != "complete"
                            and original.get("schema") == source
                            and {**original, "schema": "migrating"} == config
                        ):
                            candidates.append(source)
                    if len(candidates) != 1:
                        raise ControlError(
                            "migration_journal", "Journal does not match this store."
                        )
                    source = candidates[0]
                elif schema in tuple(range(3, VERSION)):
                    source = schema
                else:
                    raise ControlError("schema_mismatch", "Unsupported migration source.")
                if source >= target:
                    raise ControlError("schema_mismatch", "Migration cannot downgrade this store.")
                backup = _step(path, config, db, version, source)
                backups.append(str(backup))
                changed = True
    finally:
        lock_context.__exit__(None, None, None)


def _step(path, config, db, version, source):
    target = source + 1
    source_version = 0 if source == 3 else source
    journal_path = path / f"migration-{source}-{target}.json"
    backup = path / f"schema-{source}-backup.sqlite3"
    if version not in (source_version, target):
        raise ControlError("schema_mismatch", "Unsupported migration DB version.")
    if config["schema"] == source:
        _no_active(db)
        if version != source_version:
            raise ControlError("schema_mismatch", "Configuration and DB version disagree.")
        if backup.is_symlink():
            raise ControlError("unsafe_state", "Backup cannot be a symlink.")
        with closing(sqlite3.connect(backup)) as copy:
            db.backup(copy)
        secure_new_file(backup)
        with backup.open("rb") as handle:
            os.fsync(handle.fileno())
        _check_backup(backup, source)
        journal = dict(source=source, target=target, phase="backed_up", original_config=config)
        write_json(journal_path, journal)
        _checkpoint("backup")
        write_json(path / "config.json", {**config, "schema": "migrating"})
        _checkpoint("marker")
    else:
        journal = json.loads(journal_path.read_text())
        _check_backup(backup, source)
    if version == target:
        _no_active(db)
    if version == source_version:
        db.execute("BEGIN IMMEDIATE")
        try:
            _no_active(db)
            {
                3: add_task_schema,
                4: add_queue_schema,
                5: add_message_schema,
                6: add_workflow_schema,
                7: add_workspace_schema,
                8: add_execution_schema,
                9: add_composition_schema,
                10: add_telemetry_schema,
                11: add_application_schema,
                12: add_platform_schema,
                13: add_observation_schema,
            }[source](db)
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise ControlError("migration_integrity", "Foreign key validation failed.")
            _checkpoint("ddl")
            db.commit()
        except BaseException as exc:
            db.rollback()
            if isinstance(exc, ControlError) and exc.code == "migration_busy":
                write_json(path / "config.json", journal["original_config"])
                write_json(journal_path, {**journal, "phase": "blocked_active"})
            raise
    _checkpoint("db_committed")
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ControlError("migration_integrity", "Database integrity validation failed.")
    new_config = {**journal["original_config"], "schema": target}
    if target == 5:
        new_config["max_queued"] = 100
    write_json(path / "config.json", new_config)
    _checkpoint("config_committed")
    write_json(journal_path, {**journal, "phase": "complete"})
    return backup
