"""Persistent, single-attempt workflow queue and schedule repository operations."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import traceback
import uuid
from datetime import datetime, time as local_time, timedelta, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.engine.automation import AutomationEngine, AutomationRunInput, AutomationStepError, ConfiguredAutomationWorkflow
from app.engine.business_file import MAX_BUSINESS_UPLOAD_BYTES, BusinessFileInput
from app.engine.contracts import ExecutionRecord
from app.engine.runner import default_runner
from app.storage.database import connect, database_path

logger = logging.getLogger("app.background")
MAX_PENDING_EXECUTIONS = 100
MAX_SCHEDULES = 100
MAX_STORED_INPUT_FILES = 1000
MAX_WORKER_CONCURRENCY = 4
STALE_WORKER_SECONDS = 120
WORKER_HEARTBEAT_SECONDS = 10
_INPUT_KEY = re.compile(r"^[0-9a-f]{32}\.(?:csv|xlsx)$")


def queue_directory() -> Path:
    configured = os.getenv("AUTOMATION_QUEUE_DIR")
    return Path(configured).expanduser() if configured else Path(__file__).resolve().parents[2] / "data" / "queue"


def _safe_filename(filename: str) -> str:
    name = PurePosixPath(str(filename or "").replace("\\", "/")).name
    if len(name) > 255:
        name = name[-255:]
    return name


def store_upload(filename: str, content: bytes) -> tuple[str, str]:
    safe_name = _safe_filename(filename)
    suffix = Path(safe_name).suffix.lower()
    if suffix not in {".csv", ".xlsx"}:
        raise ValueError("Upload must use a .csv or .xlsx extension.")
    if not content:
        raise ValueError("Upload file is empty.")
    if len(content) > MAX_BUSINESS_UPLOAD_BYTES:
        raise ValueError("File exceeds the 5 MiB upload limit.")
    root = queue_directory().resolve()
    root.mkdir(parents=True, exist_ok=True)
    key = f"{uuid.uuid4().hex}{suffix}"
    target = (root / key).resolve()
    if target.parent != root:
        raise ValueError("Upload destination is invalid.")
    # Serialize cross-process admission against the shared database before counting files.
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        stored_count = sum(1 for item in root.iterdir() if item.is_file() and _INPUT_KEY.fullmatch(item.name))
        if stored_count >= MAX_STORED_INPUT_FILES:
            connection.rollback()
            raise OverflowError("The stored upload limit has been reached. Contact the administrator for retention management.")
        with target.open("xb") as stream:
            stream.write(content)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return key, safe_name


def _execution_identity(workflow_json: str) -> tuple[str, str | None]:
    try:
        workflow = json.loads(workflow_json)
    except (ValueError, TypeError):
        return "automation_workflow", None
    if not isinstance(workflow, dict):
        return "automation_workflow", None
    return (
        workflow.get("workflow_id") if isinstance(workflow.get("workflow_id"), str) else "automation_workflow",
        workflow.get("version") if isinstance(workflow.get("version"), str) else None,
    )


def _insert_queued_execution(
    connection: sqlite3.Connection,
    *,
    execution_id: str,
    workflow_json: str,
    created_at: str,
    input_key: str,
    original_filename: str,
    rules_json: str | None,
    schedule_id: str | None = None,
    occurrence_key: str | None = None,
) -> None:
    workflow_id, version = _execution_identity(workflow_json)
    connection.execute(
        """INSERT INTO automation_executions
        (execution_id, workflow_name, workflow_id, workflow_version, status, created_at)
        VALUES (?, ?, ?, ?, 'queued', ?)""",
        (execution_id, workflow_id, workflow_id, version, created_at),
    )
    connection.execute(
        """INSERT INTO background_executions
        (execution_id, state, input_key, original_filename, workflow_json, rules_json,
         created_at, schedule_id, occurrence_key)
        VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?)""",
        (execution_id, input_key, original_filename, workflow_json, rules_json,
         created_at, schedule_id, occurrence_key),
    )


def enqueue_execution(
    *, input_key: str, original_filename: str, workflow_json: str,
    rules_json: str | None, db_path: str | None = None,
) -> dict[str, object]:
    if not _INPUT_KEY.fullmatch(input_key):
        raise ValueError("Queued input reference is invalid.")
    created_at = datetime.now(timezone.utc).isoformat()
    execution_id = str(uuid.uuid4())
    connection = connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        pending = connection.execute(
            "SELECT COUNT(*) FROM background_executions WHERE state IN ('queued', 'running')"
        ).fetchone()[0]
        if pending >= MAX_PENDING_EXECUTIONS:
            raise OverflowError("The execution queue is full. Try again later.")
        _insert_queued_execution(
            connection, execution_id=execution_id, workflow_json=workflow_json,
            created_at=created_at, input_key=input_key, original_filename=original_filename,
            rules_json=rules_json,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"execution_id": execution_id, "status": "queued", "created_at": created_at}


def get_background_execution(execution_id: str, db_path: str | None = None) -> dict[str, object] | None:
    connection = connect(db_path)
    try:
        row = connection.execute(
            """SELECT execution_id, state, original_filename, created_at, started_at,
                      completed_at, error_code, schedule_id
               FROM background_executions WHERE execution_id = ?""",
            (execution_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def list_background_executions(limit: int, offset: int, db_path: str | None = None) -> dict[str, object]:
    connection = connect(db_path)
    try:
        rows = connection.execute(
            """SELECT execution_id, state, original_filename, created_at, started_at,
                      completed_at, error_code, schedule_id
               FROM background_executions ORDER BY created_at DESC, execution_id DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        total = connection.execute("SELECT COUNT(*) FROM background_executions").fetchone()[0]
        return {"items": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}
    finally:
        connection.close()


def _heartbeat(execution_id: str, worker_id: str, db_path: str | None = None) -> None:
    connection = connect(db_path)
    try:
        connection.execute(
            "UPDATE background_executions SET heartbeat_at = ? WHERE execution_id = ? AND worker_id = ? AND state = 'running'",
            (datetime.now(timezone.utc).isoformat(), execution_id, worker_id),
        )
        connection.commit()
    finally:
        connection.close()


def claim_execution(worker_id: str, db_path: str | None = None) -> dict[str, object] | None:
    connection = connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        slots = {row[0] for row in connection.execute("SELECT slot_id FROM background_worker_slots")}
        free_slots = set(range(MAX_WORKER_CONCURRENCY)) - slots
        if not free_slots:
            connection.rollback()
            return None
        row = connection.execute(
            "SELECT * FROM background_executions WHERE state = 'queued' ORDER BY created_at, execution_id LIMIT 1"
        ).fetchone()
        if row is None:
            connection.rollback()
            return None
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            "UPDATE background_executions SET state='running', worker_id=?, started_at=?, heartbeat_at=? WHERE execution_id=? AND state='queued'",
            (worker_id, now, now, row["execution_id"]),
        )
        connection.execute(
            "INSERT INTO background_worker_slots(slot_id, execution_id) VALUES (?, ?)",
            (min(free_slots), row["execution_id"]),
        )
        claimed = connection.execute(
            "SELECT * FROM background_executions WHERE execution_id = ?", (row["execution_id"],)
        ).fetchone()
        connection.commit()
        return dict(claimed)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def finish_execution(execution_id: str, state: str, error_code: str | None, db_path: str | None = None) -> None:
    if state not in {"succeeded", "failed"}:
        raise ValueError("Background execution can only be finalized as succeeded or failed.")
    now = datetime.now(timezone.utc).isoformat()
    connection = connect(db_path)
    try:
        with connection:
            row = connection.execute(
                "SELECT schedule_id FROM background_executions WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            connection.execute(
                "UPDATE background_executions SET state=?, error_code=?, completed_at=? WHERE execution_id=? AND state='running'",
                (state, error_code, now, execution_id),
            )
            connection.execute("DELETE FROM background_worker_slots WHERE execution_id = ?", (execution_id,))
            if row and row["schedule_id"]:
                connection.execute(
                    "UPDATE automation_schedules SET last_execution_id=?, last_run_status=?, updated_at=? WHERE schedule_id=?",
                    (execution_id, state, now, row["schedule_id"]),
                )
    finally:
        connection.close()


def recover_stale_executions(db_path: str | None = None, now: datetime | None = None) -> int:
    current = now or datetime.now(timezone.utc)
    cutoff = (current - timedelta(seconds=STALE_WORKER_SECONDS)).isoformat()
    completed = current.isoformat()
    connection = connect(db_path)
    count = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT * FROM background_executions WHERE state='running' AND heartbeat_at < ?",
            (cutoff,),
        ).fetchall()
        for row in rows:
            execution_id = row["execution_id"]
            safe_error = json.dumps({"code": "worker_interrupted", "message": "Execution was interrupted and will not be restarted."})
            connection.execute(
                "UPDATE background_executions SET state='interrupted', error_code='worker_interrupted', completed_at=? WHERE execution_id=?",
                (completed, execution_id),
            )
            connection.execute(
                "UPDATE automation_executions SET status='failed', completed_at=?, error_json=? WHERE execution_id=?",
                (completed, safe_error, execution_id),
            )
            if row["schedule_id"]:
                connection.execute(
                    "UPDATE automation_schedules SET last_execution_id=?, last_run_status='interrupted', updated_at=? WHERE schedule_id=?",
                    (execution_id, completed, row["schedule_id"]),
                )
            known = connection.execute(
                "SELECT step_id, step_index, status FROM automation_step_results WHERE execution_id=? ORDER BY step_index",
                (execution_id,),
            ).fetchall()
            try:
                steps = json.loads(row["workflow_json"]).get("steps", [])
            except (ValueError, AttributeError):
                steps = []
            failing = next((step["step_index"] for step in known if step["status"] == "running"), None)
            if failing is None:
                existing_indexes = {step["step_index"] for step in known}
                failing = next((index for index in range(len(steps)) if index not in existing_indexes), len(steps))
            for index, definition in enumerate(steps):
                if not isinstance(definition, dict) or not isinstance(definition.get("id"), str) or not isinstance(definition.get("type"), str):
                    continue
                existing = next((item for item in known if item["step_id"] == definition["id"]), None)
                if existing and existing["status"] in {"succeeded", "failed"}:
                    continue
                status = "failed" if index == failing else "skipped"
                message = "Execution was interrupted; this step was not restarted." if status == "failed" else "An earlier step was interrupted."
                error = json.dumps({"code": "worker_interrupted" if status == "failed" else "dependency_failed", "message": message})
                if existing:
                    connection.execute(
                        "UPDATE automation_step_results SET status=?, completed_at=?, error_json=?, result_json=NULL WHERE execution_id=? AND step_id=?",
                        (status, completed, error, execution_id, definition["id"]),
                    )
                else:
                    connection.execute(
                        """INSERT INTO automation_step_results
                        (execution_id, step_index, step_id, step_type, status, completed_at, duration_ms, error_json)
                        VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                        (execution_id, index, definition["id"], definition["type"], status, completed, error),
                    )
            connection.execute("DELETE FROM background_worker_slots WHERE execution_id=?", (execution_id,))
            count += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return count


class BackgroundExecutionService:
    """One-claim-at-a-time poller; SQLite limits all processes to four active runs."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path
        self.worker_id = str(uuid.uuid4())

    def process_one(self) -> bool:
        recover_stale_executions(self.db_path)
        row = claim_execution(self.worker_id, self.db_path)
        if row is None:
            return False
        execution_id = row["execution_id"]
        stop = threading.Event()

        def heartbeat_loop() -> None:
            while not stop.wait(WORKER_HEARTBEAT_SECONDS):
                try:
                    _heartbeat(execution_id, self.worker_id, self.db_path)
                except Exception:
                    logger.error("Background execution heartbeat failed", extra={"execution_id": execution_id})

        heartbeat = threading.Thread(target=heartbeat_loop, name="flowdesk-queue-heartbeat", daemon=True)
        heartbeat.start()
        try:
            key = row["input_key"]
            if not _INPUT_KEY.fullmatch(key):
                raise ValueError("Queued source reference is invalid.")
            root = queue_directory().resolve()
            source = (root / key).resolve(strict=True)
            if source.parent != root:
                raise ValueError("Queued source reference is invalid.")
            with source.open("rb") as stream:
                content = stream.read(MAX_BUSINESS_UPLOAD_BYTES + 1)
            from app.api.routes import _automation_handlers

            workflow = ConfiguredAutomationWorkflow(
                row["workflow_json"], AutomationEngine(_automation_handlers())
            )
            created_at = datetime.fromisoformat(row["created_at"])
            try:
                record = default_runner.execute(
                    workflow,
                    AutomationRunInput(BusinessFileInput(row["original_filename"], content, row["rules_json"])),
                    execution_id=execution_id,
                    created_at=created_at,
                )
            except AutomationStepError:
                record = default_runner.get(execution_id)
                if record is None:
                    raise
            state = record.status.value
            finish_execution(execution_id, "succeeded" if state == "succeeded" else "failed", record.error_code, self.db_path)
        except Exception as error:
            logger.error(
                "Background execution failed",
                extra={
                    "execution_id": execution_id,
                    "error_type": type(error).__name__,
                    # Traceback frames only: omit exception text and locals, which may contain input data.
                    "traceback_frames": traceback.format_tb(error.__traceback__),
                },
            )
            _mark_unexpected_failure(execution_id, self.db_path)
            finish_execution(execution_id, "failed", "execution_failed", self.db_path)
        finally:
            stop.set()
            heartbeat.join(timeout=1)
        return True


def _mark_unexpected_failure(execution_id: str, db_path: str | None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    error_json = json.dumps({"code": "execution_failed", "message": "Workflow execution failed."})
    connection = connect(db_path)
    try:
        with connection:
            connection.execute(
                "UPDATE automation_executions SET status='failed', completed_at=?, error_json=? WHERE execution_id=? AND status IN ('queued','running')",
                (now, error_json, execution_id),
            )
            queued = connection.execute(
                "SELECT workflow_json FROM background_executions WHERE execution_id=?", (execution_id,)
            ).fetchone()
            known = connection.execute(
                "SELECT step_id, step_index, step_type, status FROM automation_step_results WHERE execution_id=? ORDER BY step_index",
                (execution_id,),
            ).fetchall()
            if queued is not None:
                try:
                    definitions = json.loads(queued["workflow_json"]).get("steps", [])
                except (ValueError, TypeError, AttributeError):
                    definitions = []
                failing = next((step["step_index"] for step in known if step["status"] == "running"), None)
                if failing is None:
                    known_indexes = {step["step_index"] for step in known}
                    failing = next((index for index in range(len(definitions)) if index not in known_indexes), None)
                for index, definition in enumerate(definitions):
                    if not isinstance(definition, dict):
                        continue
                    step_id, step_type = definition.get("id"), definition.get("type")
                    if not isinstance(step_id, str) or not isinstance(step_type, str):
                        continue
                    existing = next((step for step in known if step["step_id"] == step_id), None)
                    if existing and existing["status"] in {"succeeded", "failed", "skipped"}:
                        continue
                    failed = index == failing
                    status_value = "failed" if failed else "skipped"
                    safe_step_error = json.dumps({
                        "code": "execution_failed" if failed else "dependency_failed",
                        "message": "Workflow execution failed." if failed else "An earlier step failed.",
                    })
                    if existing:
                        connection.execute(
                            "UPDATE automation_step_results SET status=?, completed_at=?, duration_ms=COALESCE(duration_ms,0), error_json=?, result_json=NULL WHERE execution_id=? AND step_id=?",
                            (status_value, now, safe_step_error, execution_id, step_id),
                        )
                    else:
                        connection.execute(
                            """INSERT INTO automation_step_results
                            (execution_id,step_index,step_id,step_type,status,completed_at,duration_ms,error_json)
                            VALUES (?,?,?,?,?,?,0,?)""",
                            (execution_id, index, step_id, step_type, status_value, now, safe_step_error),
                        )
    finally:
        connection.close()


def _parse_local_time(value: str) -> local_time:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("local_time must use 24-hour HH:MM format.")
    hour, minute = (int(part) for part in value.split(":"))
    return local_time(hour, minute)


def next_recurring_run(schedule_type: str, zone_name: str, run_time: str, weekday: int | None, after: datetime) -> datetime:
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("time_zone must be a valid IANA timezone.") from error
    target_time = _parse_local_time(run_time)
    local_after = after.astimezone(zone)
    for offset in range(0, 15):
        date = local_after.date() + timedelta(days=offset)
        if schedule_type == "weekly" and date.weekday() != weekday:
            continue
        naive = datetime.combine(date, target_time)
        candidate = naive.replace(tzinfo=zone, fold=0)
        roundtrip = candidate.astimezone(timezone.utc).astimezone(zone)
        if roundtrip.replace(tzinfo=None) != naive:
            candidate = roundtrip
        if candidate.astimezone(timezone.utc) > after.astimezone(timezone.utc):
            return candidate.astimezone(timezone.utc)
    raise ValueError("Could not calculate the next scheduled time.")


def validate_schedule(
    *, schedule_type: str, time_zone: str, run_at: str | None,
    local_time_value: str | None, weekday: int | None, now: datetime | None = None,
) -> tuple[str | None, str | None, int | None, datetime]:
    current = now or datetime.now(timezone.utc)
    try:
        ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("time_zone must be a valid IANA timezone.") from error
    if schedule_type == "once":
        if not run_at or local_time_value is not None or weekday is not None:
            raise ValueError("One-time schedules require run_at only.")
        try:
            parsed = datetime.fromisoformat(run_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("run_at must be an ISO 8601 timestamp.") from error
        if parsed.tzinfo is None:
            zone = ZoneInfo(time_zone)
            local_candidate = parsed.replace(tzinfo=zone, fold=0)
            roundtrip = local_candidate.astimezone(timezone.utc).astimezone(zone)
            parsed = roundtrip if roundtrip.replace(tzinfo=None) != parsed else local_candidate
        if parsed <= current:
            raise ValueError("run_at must be a future timestamp.")
        return parsed.astimezone(timezone.utc).isoformat(), None, None, parsed.astimezone(timezone.utc)
    if schedule_type not in {"daily", "weekly"} or run_at is not None:
        raise ValueError("schedule_type must be once, daily, or weekly with matching time fields.")
    if schedule_type == "daily" and weekday is not None:
        raise ValueError("Daily schedules must not specify a weekday.")
    if schedule_type == "weekly" and (type(weekday) is not int or not 0 <= weekday <= 6):
        raise ValueError("Weekly schedules require weekday from 0 (Monday) through 6 (Sunday).")
    if schedule_type == "daily":
        try:
            ZoneInfo(time_zone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("time_zone must be a valid IANA timezone.") from error
    next_run = next_recurring_run(schedule_type, time_zone, local_time_value or "", weekday, current)
    return None, local_time_value, weekday, next_run


def create_schedule_record(
    *, name: str, workflow_json: str, rules_json: str | None, input_key: str,
    original_filename: str, schedule_type: str, time_zone: str, run_at: str | None,
    local_time_value: str | None, weekday: int | None, next_run: datetime,
    db_path: str | None = None,
) -> dict[str, object]:
    schedule_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    connection = connect(db_path)
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            total = connection.execute("SELECT COUNT(*) FROM automation_schedules").fetchone()[0]
            if total >= MAX_SCHEDULES:
                raise OverflowError("A maximum of 100 schedules can be stored.")
            connection.execute(
                """INSERT INTO automation_schedules
                (schedule_id, name, workflow_json, rules_json, input_key, original_filename,
                 schedule_type, time_zone, run_at, local_time, weekday, next_run_at, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (schedule_id, name, workflow_json, rules_json, input_key, original_filename,
                 schedule_type, time_zone, run_at, local_time_value, weekday,
                 next_run.isoformat(), now, now),
            )
    finally:
        connection.close()
    result = get_schedule(schedule_id, db_path)
    assert result is not None
    return result


def list_schedules(db_path: str | None = None) -> list[dict[str, object]]:
    connection = connect(db_path)
    try:
        rows = connection.execute(
            """SELECT schedule_id, name, schedule_type, time_zone, run_at, local_time,
                      weekday, next_run_at, enabled, created_at, updated_at,
                      last_execution_id, last_run_status
               FROM automation_schedules ORDER BY created_at DESC, schedule_id DESC"""
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def get_schedule(schedule_id: str, db_path: str | None = None) -> dict[str, object] | None:
    connection = connect(db_path)
    try:
        row = connection.execute(
            """SELECT schedule_id, name, schedule_type, time_zone, run_at, local_time,
                      weekday, next_run_at, enabled, created_at, updated_at,
                      last_execution_id, last_run_status
               FROM automation_schedules WHERE schedule_id = ?""",
            (schedule_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def change_schedule_enabled(schedule_id: str, enabled: bool, db_path: str | None = None) -> dict[str, object] | None:
    connection = connect(db_path)
    try:
        with connection:
            row = connection.execute("SELECT * FROM automation_schedules WHERE schedule_id = ?", (schedule_id,)).fetchone()
            if row is None:
                return None
            next_run = row["next_run_at"]
            if enabled:
                current = datetime.now(timezone.utc)
                if row["schedule_type"] == "once":
                    candidate = datetime.fromisoformat(row["run_at"])
                    if candidate <= current:
                        raise ValueError("This one-time schedule has already passed and cannot be re-enabled.")
                    next_run = candidate.isoformat()
                else:
                    next_run = next_recurring_run(
                        row["schedule_type"], row["time_zone"], row["local_time"], row["weekday"], current
                    ).isoformat()
            connection.execute(
                "UPDATE automation_schedules SET enabled=?, next_run_at=?, updated_at=? WHERE schedule_id=?",
                (int(enabled), next_run if enabled else None, datetime.now(timezone.utc).isoformat(), schedule_id),
            )
    finally:
        connection.close()
    return get_schedule(schedule_id, db_path)


def delete_schedule(schedule_id: str, db_path: str | None = None) -> bool:
    connection = connect(db_path)
    try:
        with connection:
            cursor = connection.execute("DELETE FROM automation_schedules WHERE schedule_id = ?", (schedule_id,))
            return cursor.rowcount > 0
    finally:
        connection.close()


def dispatch_due_schedules(
    owner_id: str, *, now: datetime | None = None, db_path: str | None = None,
) -> int:
    current = now or datetime.now(timezone.utc)
    current_iso = current.isoformat()
    lease_until = (current + timedelta(seconds=30)).isoformat()
    connection = connect(db_path)
    dispatched = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        lease = connection.execute(
            "SELECT owner_id, lease_until FROM scheduler_leases WHERE lease_name='schedule-dispatcher'"
        ).fetchone()
        if lease and lease["owner_id"] != owner_id and lease["lease_until"] > current_iso:
            connection.rollback()
            return 0
        connection.execute(
            """INSERT INTO scheduler_leases(lease_name, owner_id, lease_until)
               VALUES ('schedule-dispatcher', ?, ?)
               ON CONFLICT(lease_name) DO UPDATE SET owner_id=excluded.owner_id, lease_until=excluded.lease_until""",
            (owner_id, lease_until),
        )
        pending = connection.execute(
            "SELECT COUNT(*) FROM background_executions WHERE state IN ('queued','running')"
        ).fetchone()[0]
        available = MAX_PENDING_EXECUTIONS - pending
        if available <= 0:
            connection.commit()
            return 0
        due_rows = connection.execute(
            "SELECT * FROM automation_schedules WHERE enabled=1 AND next_run_at <= ? ORDER BY next_run_at LIMIT ?",
            (current_iso, available),
        ).fetchall()
        for row in due_rows:
            occurrence = row["next_run_at"]
            execution_id = str(uuid.uuid4())
            _insert_queued_execution(
                connection, execution_id=execution_id, workflow_json=row["workflow_json"],
                created_at=current_iso, input_key=row["input_key"],
                original_filename=row["original_filename"], rules_json=row["rules_json"],
                schedule_id=row["schedule_id"], occurrence_key=occurrence,
            )
            if row["schedule_type"] == "once":
                connection.execute(
                    "UPDATE automation_schedules SET enabled=0, next_run_at=NULL, last_execution_id=?, last_run_status='queued', updated_at=? WHERE schedule_id=?",
                    (execution_id, current_iso, row["schedule_id"]),
                )
            else:
                next_run = next_recurring_run(
                    row["schedule_type"], row["time_zone"], row["local_time"], row["weekday"], current
                )
                connection.execute(
                    "UPDATE automation_schedules SET next_run_at=?, last_execution_id=?, last_run_status='queued', updated_at=? WHERE schedule_id=?",
                    (next_run.isoformat(), execution_id, current_iso, row["schedule_id"]),
                )
            dispatched += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return dispatched


def release_scheduler_lease(owner_id: str, db_path: str | None = None) -> None:
    connection = connect(db_path)
    try:
        connection.execute(
            "DELETE FROM scheduler_leases WHERE lease_name='schedule-dispatcher' AND owner_id=?",
            (owner_id,),
        )
        connection.commit()
    finally:
        connection.close()
