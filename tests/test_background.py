import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.engine.background import (
    BackgroundExecutionService,
    claim_execution,
    create_schedule_record,
    dispatch_due_schedules,
    enqueue_execution,
    get_background_execution,
    next_recurring_run,
    recover_stale_executions,
    store_upload,
    validate_schedule,
)
from app.storage.database import initialize_database
from app.storage.repository import get_automation_execution


WORKFLOW = json.dumps({
    "workflow_id": "business-file-processing",
    "version": "1",
    "steps": [
        {"id": "process", "type": "business_file"},
        {"id": "summary", "type": "business_file_summary", "inputs": {"result": {"from_step": "process"}}},
    ],
})


@pytest.fixture
def background_env(tmp_path, monkeypatch):
    db = tmp_path / "flowdesk.sqlite3"
    queue = tmp_path / "queue"
    output = tmp_path / "outputs"
    monkeypatch.setenv("AUTOMATION_DB_PATH", str(db))
    monkeypatch.setenv("AUTOMATION_QUEUE_DIR", str(queue))
    monkeypatch.setenv("AUTOMATION_OUTPUT_DIR", str(output))
    initialize_database(db)
    return db, queue, output


def _enqueue(db, queue, content=b"name,amount\nsynthetic,2\n"):
    key, filename = store_upload("synthetic.csv", content)
    return enqueue_execution(
        input_key=key, original_filename=filename, workflow_json=WORKFLOW,
        rules_json=None, db_path=str(db),
    )


def test_queued_workflow_runs_once_and_survives_service_recreation(background_env):
    db, queue, output = background_env
    accepted = _enqueue(db, queue)
    first_service = BackgroundExecutionService(str(db))
    assert first_service.process_one() is True
    persisted = get_automation_execution(accepted["execution_id"], str(db))
    assert persisted["status"] == "succeeded"
    assert [step["step_id"] for step in persisted["steps"]] == ["process", "summary"]
    assert get_background_execution(accepted["execution_id"], str(db))["state"] == "succeeded"
    assert BackgroundExecutionService(str(db)).process_one() is False
    assert len(list(queue.glob("*.csv"))) == 1  # original queued upload is retained
    assert list(output.glob("*_processed.xlsx"))


def test_atomic_claim_allows_only_one_worker_to_claim_execution(background_env):
    db, queue, _ = background_env
    accepted = _enqueue(db, queue)
    first = claim_execution("worker-a", str(db))
    second = claim_execution("worker-b", str(db))
    assert first["execution_id"] == accepted["execution_id"]
    assert second is None


def test_background_failure_logging_omits_exception_text_and_source_name(background_env, caplog):
    db, _, _ = background_env
    accepted = enqueue_execution(
        input_key="c" * 32 + ".csv", original_filename="sensitive-source-name.csv",
        workflow_json=WORKFLOW, rules_json=None, db_path=str(db),
    )
    with caplog.at_level(logging.ERROR, logger="app.background"):
        assert BackgroundExecutionService(str(db)).process_one() is True
    assert get_background_execution(accepted["execution_id"], str(db))["state"] == "failed"
    record = next(record for record in caplog.records if record.getMessage() == "Background execution failed")
    assert record.error_type == "FileNotFoundError"
    assert record.traceback_frames
    assert "sensitive-source-name.csv" not in caplog.text
    execution = get_automation_execution(accepted["execution_id"], str(db))
    assert [(step["step_id"], step["status"]) for step in execution["steps"]] == [
        ("process", "failed"), ("summary", "skipped")
    ]


def test_stale_running_execution_is_interrupted_without_rerun_and_steps_are_finalized(background_env):
    db, queue, _ = background_env
    schedule = create_schedule_record(
        name="Interrupted synthetic schedule", workflow_json=WORKFLOW, rules_json=None,
        input_key="b" * 32 + ".csv", original_filename="synthetic.csv",
        schedule_type="daily", time_zone="UTC", run_at=None,
        local_time_value="23:59", weekday=None,
        next_run=datetime.now(timezone.utc) - timedelta(minutes=1), db_path=str(db),
    )
    now = datetime.now(timezone.utc)
    assert dispatch_due_schedules("schedule-owner", now=now, db_path=str(db)) == 1
    with sqlite3.connect(db) as connection:
        execution_id = connection.execute(
            "SELECT last_execution_id FROM automation_schedules WHERE schedule_id=?", (schedule["schedule_id"],)
        ).fetchone()[0]
    claimed = claim_execution("lost-worker", str(db))
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE background_executions SET heartbeat_at=? WHERE execution_id=?",
            (old, execution_id),
        )
        connection.execute(
            "INSERT INTO automation_step_results(execution_id,step_index,step_id,step_type,status,started_at) "
            "VALUES (?,0,'process','business_file','running',?)",
            (execution_id, old),
        )
    assert claimed["state"] == "running"
    assert recover_stale_executions(str(db)) == 1
    assert get_background_execution(execution_id, str(db))["state"] == "interrupted"
    persisted = get_automation_execution(execution_id, str(db))
    assert persisted["status"] == "failed"
    assert [(step["step_id"], step["status"]) for step in persisted["steps"]] == [
        ("process", "failed"), ("summary", "skipped")
    ]
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT last_run_status FROM automation_schedules WHERE schedule_id=?", (schedule["schedule_id"],)).fetchone()[0] == "interrupted"
    assert BackgroundExecutionService(str(db)).process_one() is False


def test_schedule_dispatch_is_persistent_and_does_not_duplicate_occurrence(background_env):
    db, _, _ = background_env
    due = datetime.now(timezone.utc) - timedelta(minutes=1)
    schedule = create_schedule_record(
        name="Synthetic daily run", workflow_json=WORKFLOW, rules_json=None,
        input_key="a" * 32 + ".csv", original_filename="synthetic.csv",
        schedule_type="daily", time_zone="UTC", run_at=None,
        local_time_value="23:59", weekday=None, next_run=due, db_path=str(db),
    )
    now = datetime.now(timezone.utc)
    assert dispatch_due_schedules("scheduler-one", now=now, db_path=str(db)) == 1
    assert dispatch_due_schedules("scheduler-one", now=now, db_path=str(db)) == 0
    assert dispatch_due_schedules("scheduler-two", now=now, db_path=str(db)) == 0
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM background_executions WHERE schedule_id=?", (schedule["schedule_id"],)).fetchone()[0] == 1


def test_schedule_validation_and_timezone_calculation_are_explicit():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="IANA timezone"):
        validate_schedule(schedule_type="once", time_zone="not/a-zone", run_at="2027-01-01T10:00:00Z", local_time_value=None, weekday=None, now=now)
    next_run = next_recurring_run("daily", "America/New_York", "09:30", None, now)
    assert next_run.tzinfo is timezone.utc
    assert next_run.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).hour == 9


def test_stored_upload_count_limit_is_checked_before_writing(background_env, monkeypatch):
    from app.engine import background

    _, queue, _ = background_env
    monkeypatch.setattr(background, "MAX_STORED_INPUT_FILES", 0)
    with pytest.raises(OverflowError, match="stored upload limit"):
        store_upload("batch.csv", b"a\n1\n")
    assert list(queue.iterdir()) == []
