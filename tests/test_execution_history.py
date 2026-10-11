import json
import os
import sqlite3
from datetime import datetime, timezone

from app.engine.runner import WorkflowRunner, _persist_sqlite
from app.storage.database import initialize_database
from app.storage.repository import get_automation_execution, save_automation_execution


def _workflow() -> str:
    return json.dumps({
        "workflow_id": "history-check",
        "version": "3",
        "steps": [
            {"id": "process", "type": "business_file"},
            {"id": "summary", "type": "business_file_summary",
             "inputs": {"result": {"from_step": "process"}}},
        ],
    })


def test_execution_is_persisted_and_retrievable_after_runner_recreation(client, monkeypatch):
    response = client.post(
        "/automation/execute",
        files={"file": ("sales.csv", "Customer,Amount\nSensitiveCustomerValue,10\n", "text/csv")},
        data={"workflow": _workflow()},
    )
    assert response.status_code == 201
    result = response.json()
    assert result["status"] == "succeeded"
    assert result["workflow_id"] == "history-check"
    assert result["workflow_version"] == "3"
    assert result["created_at"] and result["started_at"] and result["completed_at"]
    assert [step["step_id"] for step in result["steps"]] == ["process", "summary"]
    assert [step["status"] for step in result["steps"]] == ["succeeded", "succeeded"]
    assert all(step["started_at"] and step["completed_at"] for step in result["steps"])

    # Simulate a new application service with an empty process-local cache.
    monkeypatch.setattr("app.api.routes.default_runner", WorkflowRunner(persistence=_persist_sqlite))
    retrieved = client.get(f"/executions/{result['execution_id']}")
    assert retrieved.status_code == 200
    persisted = retrieved.json()
    assert persisted["execution_id"] == result["execution_id"]
    assert persisted["workflow_id"] == "history-check"
    assert [step["step_id"] for step in persisted["steps"]] == ["process", "summary"]
    assert [step["status"] for step in persisted["steps"]] == ["succeeded", "succeeded"]

    connection = sqlite3.connect(os.environ["AUTOMATION_DB_PATH"])
    try:
        execution_output = connection.execute(
            "SELECT output_json FROM automation_executions WHERE execution_id = ?",
            (result["execution_id"],),
        ).fetchone()[0]
        step_outputs = " ".join(row[0] or "" for row in connection.execute(
            "SELECT result_json FROM automation_step_results WHERE execution_id = ?",
            (result["execution_id"],),
        ))
    finally:
        connection.close()
    assert "SensitiveCustomerValue" not in (execution_output or "") + step_outputs


def test_failed_business_step_and_dependent_step_are_persisted(client, monkeypatch):
    definition = _workflow()
    response = client.post(
        "/automation/execute",
        files={"file": ("sales.csv", "Customer,Amount\nA,10\n", "text/csv")},
        data={"workflow": definition, "rules": "{invalid-json"},
    )
    assert response.status_code == 422
    execution = response.json()
    assert execution["status"] == "failed"
    assert [step["status"] for step in execution["steps"]] == ["failed", "skipped"]
    monkeypatch.setattr("app.api.routes.default_runner", WorkflowRunner(persistence=_persist_sqlite))
    persisted = client.get(f"/executions/{execution['execution_id']}").json()
    assert [step["status"] for step in persisted["steps"]] == ["failed", "skipped"]
    assert persisted["steps"][0]["error"]["code"] == "invalid_ruleset"


def test_unexpected_step_failure_is_finalized_with_safe_persisted_error(client, monkeypatch, caplog):
    class BrokenHandler:
        def validate(self, step, prior_step_types):
            return None

        def run(self, *args, **kwargs):
            raise RuntimeError("private cell value should not be logged")

    monkeypatch.setattr("app.api.routes._automation_handlers", lambda: {"broken": BrokenHandler()})
    definition = json.dumps({
        "workflow_id": "fault-test", "version": "1",
        "steps": [{"id": "explode", "type": "broken"}],
    })
    response = client.post(
        "/automation/execute",
        files={"file": ("safe.csv", "value\n1\n", "text/csv")},
        data={"workflow": definition},
    )
    assert response.status_code == 500
    result = response.json()
    assert result["status"] == "failed"
    assert result["steps"][0]["status"] == "failed"
    assert result["error"]["message"] == "Workflow execution failed."
    assert "private cell value" not in caplog.text
    monkeypatch.setattr("app.api.routes.default_runner", WorkflowRunner(persistence=_persist_sqlite))
    persisted = client.get(f"/executions/{result['execution_id']}").json()
    assert persisted["status"] == "failed"
    assert persisted["steps"][0]["status"] == "failed"
    assert persisted["error"]["message"] == "Workflow execution failed."


def test_execution_list_limit_offset_and_unknown_id(client):
    created = []
    for index in range(3):
        response = client.post(
            "/automation/execute",
            files={"file": (f"sales-{index}.csv", "Customer,Amount\nA,10\n", "text/csv")},
            data={"workflow": _workflow()},
        )
        assert response.status_code == 201
        created.append(response.json()["execution_id"])
    first = client.get("/executions?limit=2&offset=0")
    second = client.get("/executions?limit=2&offset=2")
    assert first.status_code == second.status_code == 200
    assert first.json()["total"] == 3
    assert first.json()["limit"] == 2 and first.json()["offset"] == 0
    assert len(first.json()["items"]) == 2
    assert len(second.json()["items"]) == 1
    assert [item["execution_id"] for item in first.json()["items"]] == created[-1:-3:-1]
    assert {item["execution_id"] for item in first.json()["items"] + second.json()["items"]} == set(created)
    assert client.get("/executions?limit=0").status_code == 422
    assert client.get("/executions/not-a-real-execution").status_code == 404


def test_execution_schema_migration_preserves_legacy_records_and_is_idempotent(tmp_path):
    database = tmp_path / "legacy-executions.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE jobs (id TEXT PRIMARY KEY, filename TEXT NOT NULL, status TEXT NOT NULL,
            row_count INTEGER NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE business_jobs (id TEXT PRIMARY KEY, execution_id TEXT NOT NULL UNIQUE,
            source_filename TEXT NOT NULL, status TEXT NOT NULL, total_rows INTEGER NOT NULL,
            valid_rows INTEGER NOT NULL, invalid_rows INTEGER NOT NULL, processed_artifact TEXT,
            validation_artifact TEXT NOT NULL, duration_ms INTEGER NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE business_rulesets (ruleset_id TEXT PRIMARY KEY, name TEXT NOT NULL,
            version TEXT NOT NULL, configuration_json TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL);
        INSERT INTO jobs VALUES ('legacy-job', 'legacy.csv', 'completed', 2, '2025-01-01');
        INSERT INTO business_jobs VALUES ('legacy-job', 'legacy-execution', 'legacy.csv', 'completed',
            2, 2, 0, 'legacy_processed.xlsx', 'legacy_validation.xlsx', 12, '2025-01-01');
        INSERT INTO business_rulesets VALUES ('legacy-rules', 'Legacy', '1', '{"rules":[]}', 't1', 't2');
        """
    )
    connection.commit()
    connection.close()

    initialize_database(database)
    initialize_database(database)
    connection = sqlite3.connect(database)
    try:
        job = connection.execute("SELECT id, filename, row_count FROM jobs").fetchone()
        business = connection.execute(
            "SELECT id, processed_artifact, validation_artifact, ruleset_json FROM business_jobs"
        ).fetchone()
        ruleset = connection.execute(
            "SELECT ruleset_id, configuration_json FROM business_rulesets"
        ).fetchone()
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {row[1] for row in connection.execute("PRAGMA table_info(business_jobs)")}
    finally:
        connection.close()
    assert job == ("legacy-job", "legacy.csv", 2)
    assert business == ("legacy-job", "legacy_processed.xlsx", "legacy_validation.xlsx", None)
    assert ruleset == ("legacy-rules", '{"rules":[]}')
    assert {
        "automation_executions", "automation_step_results", "business_artifacts",
        "background_executions", "automation_schedules", "background_worker_slots",
        "scheduler_leases",
    } <= tables
    assert {"ruleset_id", "ruleset_version", "ruleset_json"} <= columns


def test_execution_status_transitions_and_step_snapshots_are_persisted(tmp_path):
    database = tmp_path / "execution-lifecycle.sqlite3"
    initialize_database(database)
    now = datetime.now(timezone.utc).isoformat()
    base = {
        "execution_id": "exec-lifecycle", "workflow_name": "test", "workflow_id": "test",
        "workflow_version": "1", "created_at": now, "started_at": None,
        "completed_at": None, "duration_seconds": None, "error": None, "counts": None,
        "output": None, "steps": [], "db_path": str(database),
    }
    for state in ("queued", "running", "succeeded"):
        save_automation_execution(**base, status=state)
        assert get_automation_execution("exec-lifecycle", str(database))["status"] == state
    step = {
        "step_id": "process", "step_type": "business_file", "status": "succeeded",
        "started_at": now, "completed_at": now, "duration_ms": 2,
        "output": {"valid_rows": 2},
    }
    save_automation_execution(**{**base, "started_at": now, "completed_at": now, "steps": [step]}, status="succeeded")
    persisted = get_automation_execution("exec-lifecycle", str(database))
    assert persisted["steps"][0]["status"] == "succeeded"
    assert persisted["steps"][0]["duration_ms"] == 2


def test_execution_history_dashboard_is_served_with_persistent_api_controls(client):
    page = client.get("/")
    script = client.get("/static/app.js")
    assert page.status_code == script.status_code == 200
    assert 'id="execution-history-panel"' in page.text
    assert 'id="execution-detail"' in page.text
    assert 'fetch(`/executions?limit=${RECENT_LIMIT}&offset=${executionOffset}`' in script.text
    assert 'fetch(`/executions/${encodeURIComponent(executionButton.dataset.executionId)}`' in script.text
    assert 'function renderExecutionDetail(execution)' in script.text
    assert 'setExecutionHistoryState' in script.text
