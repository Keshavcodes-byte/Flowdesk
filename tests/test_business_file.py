import json
import logging
import sqlite3
import uuid
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from app.engine.business_file import BusinessFileInput, BusinessFileWorkflow
from app.engine.runner import WorkflowRunner
from app.engine.runner import default_runner
from app.storage.database import initialize_database
from app.storage.repository import create_business_job, get_business_artifact, get_imported_rows


def xlsx_bytes(rows):
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def test_csv_business_workflow_creates_valid_workbook_report_and_job(client: TestClient, tmp_path, caplog):
    source = "Invoice Ref,Total\n001,25.50\n,\nSensitive customer text,0\nInfinity,9\n"
    with caplog.at_level(logging.INFO, logger="app.engine"):
        response = client.post(
            "/workflows/business-file",
            files={"file": ("invoices.csv", source, "text/csv")},
        )

    assert response.status_code == 201
    result = response.json()
    assert (result["total_rows"], result["valid_rows"], result["invalid_rows"]) == (4, 2, 2)
    root = tmp_path / "outputs"
    processed = load_workbook(root / result["artifacts"]["processed_workbook"].split("/")[-1], data_only=False)
    assert list(processed.active.values) == [
        ("Invoice Ref", "Total"), ("001", "25.50"), ("Sensitive customer text", "0")
    ]
    report_name = result["artifacts"]["validation_report"].split("/")[-1]
    report = load_workbook(root / report_name, data_only=True)
    report_rows = list(report.active.values)
    assert report_rows[1][0] == 3
    assert "blank" in report_rows[1][1].lower()
    assert json.loads(report_rows[1][2]) == {"Invoice Ref": "", "Total": ""}
    assert "non-finite" in report_rows[2][1].lower()
    assert json.loads(report_rows[2][2]) == {"Invoice Ref": "Infinity", "Total": "9"}
    assert get_imported_rows(result["id"]) == [
        {"Invoice Ref": "001", "Total": "25.50"},
        {"Invoice Ref": "Sensitive customer text", "Total": "0"},
    ]
    assert result["execution_status"] == "succeeded"
    assert result["execution_duration_seconds"] >= 0
    history = client.get("/jobs").json()
    assert any(job["id"] == result["id"] for job in history["items"])
    detail = client.get(f"/jobs/{result['id']}")
    assert detail.status_code == 200 and detail.json()["row_count"] == 2
    assert "Sensitive customer text" not in caplog.text
    finalized = next(record for record in caplog.records if record.getMessage() == "Workflow finalized")
    assert finalized.counts == {"total_rows": 4, "valid_rows": 2, "invalid_rows": 2}
    connection = sqlite3.connect(tmp_path / "test.sqlite3")
    try:
        saved = connection.execute(
            "SELECT execution_id, status, total_rows, valid_rows, invalid_rows FROM business_jobs WHERE id = ?",
            (result["id"],),
        ).fetchone()
    finally:
        connection.close()
    assert saved == (result["execution_id"], "completed", 4, 2, 2)


def test_xlsx_business_workflow_preserves_valid_cells_and_reports_formula_row(client: TestClient, tmp_path):
    payload = xlsx_bytes([["Txn Id", "Amount"], [7, 12.5], [8, "=1+1"]])
    response = client.post(
        "/workflows/business-file",
        files={"file": ("transactions.xlsx", payload, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert response.status_code == 201
    result = response.json()
    assert (result["valid_rows"], result["invalid_rows"]) == (1, 1)
    output_path = tmp_path / "outputs" / result["artifacts"]["processed_workbook"].split("/")[-1]
    assert list(load_workbook(output_path, data_only=False).active.values) == [
        ("Txn Id", "Amount"), (7, 12.5)
    ]
    report_path = tmp_path / "outputs" / result["artifacts"]["validation_report"].split("/")[-1]
    report = list(load_workbook(report_path, data_only=True).active.values)
    assert report[1][0] == 3
    assert "formula" in report[1][1].lower()
    assert "=1+1" in report[1][2]


@pytest.mark.parametrize(
    ("filename", "content", "status", "message"),
    [
        ("wrong.xls", b"not excel", 415, ".csv or .xlsx"),
        ("broken.csv", b'name\n"unterminated', 422, "parse CSV"),
        ("broken.xlsx", b"not a workbook", 422, "XLSX workbook"),
        ("empty.csv", b"", 422, "empty"),
        ("headers.csv", b"\nvalue\nyes\n", 422, "header"),
        ("duplicate.csv", b"Name,name\nAda,Ada\n", 422, "unique"),
    ],
)
def test_business_file_fatal_errors(client: TestClient, filename, content, status, message):
    response = client.post("/workflows/business-file", files={"file": (filename, content)})
    assert response.status_code == status
    assert message.lower() in response.json()["detail"].lower()


def test_business_file_upload_limit_is_enforced(client: TestClient):
    content = b"a\n" + b"x" * (5 * 1024 * 1024)
    response = client.post("/workflows/business-file", files={"file": ("large.csv", content)})
    assert response.status_code == 413
    assert "5 MiB" in response.json()["detail"]


def test_extra_cells_are_retained_in_validation_report(client: TestClient, tmp_path):
    response = client.post(
        "/workflows/business-file",
        files={"file": ("extra.csv", "a,b\n1,2,private-original\n", "text/csv")},
    )
    assert response.status_code == 201
    result = response.json()
    assert result["valid_rows"] == 0 and result["invalid_rows"] == 1
    assert "processed_workbook" not in result["artifacts"]
    name = result["artifacts"]["validation_report"].split("/")[-1]
    report = list(load_workbook(tmp_path / "outputs" / name, data_only=True).active.values)
    assert "private-original" in report[1][2]
    assert "extra cell" in report[1][1]


def test_artifact_download_requires_registered_safe_basename(client: TestClient):
    created = client.post(
        "/workflows/business-file",
        files={"file": ("one.csv", "a\n1\n", "text/csv")},
    ).json()
    artifact_name = created["artifacts"]["validation_report"].split("/")[-1]
    allowed = client.get(f"/artifacts/{artifact_name}")
    assert allowed.status_code == 200
    assert allowed.content.startswith(b"PK")
    assert client.get("/artifacts/..%2Fpyproject.toml").status_code == 404
    assert client.get("/artifacts/00000000-0000-0000-0000-000000000000_validation.xlsx").status_code == 404
    assert get_business_artifact(artifact_name) is not None


def test_registered_artifact_that_is_missing_returns_safe_not_found(client: TestClient, tmp_path):
    job_id = str(uuid.uuid4())
    artifact_name = f"{job_id}_validation.xlsx"
    create_business_job(
        job_id=job_id,
        execution_id=str(uuid.uuid4()),
        source_filename="missing-artifact.csv",
        total_rows=1,
        valid_rows=0,
        invalid_rows=1,
        processed_artifact=None,
        validation_artifact=artifact_name,
        duration_ms=1,
        rows=[],
    )
    artifact_path = Path(tmp_path) / "outputs" / artifact_name
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(b"registered artifact")
    assert get_business_artifact(artifact_name) is not None
    artifact_path.unlink()

    response = client.get(f"/artifacts/{artifact_name}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Artifact not found"}


def test_uploaded_source_path_is_reduced_to_metadata_basename(client: TestClient):
    response = client.post(
        "/workflows/business-file",
        files={"file": ("..\\private\\source.csv", "code\n001\n", "text/csv")},
    )
    assert response.status_code == 201
    assert response.json()["filename"] == "source.csv"


def test_rules_are_applied_persisted_and_written_to_both_workbooks(client: TestClient, tmp_path):
    config = {
        "schema_version": 1,
        "ruleset_id": "invoice-checks",
        "version": "2.0",
        "rules": [
            {"id": "amount-range", "type": "numeric_range", "column": "Amount", "min": 0, "max": 100},
            {"id": "status-values", "type": "allowed_values", "column": "Status", "values": ["Open", "Paid"]},
        ],
    }
    config_json = json.dumps(config)
    response = client.post(
        "/workflows/business-file",
        files={"file": ("invoices.csv", "Amount,Status\n12,Open\n-1,Pending\n", "text/csv")},
        data={"rules": config_json},
    )
    assert response.status_code == 201
    result = response.json()
    assert (result["valid_rows"], result["invalid_rows"]) == (1, 1)
    assert result["rule_issues"] == [
        {"source_row_number": 3, "column_name": "Amount", "rule_id": "amount-range", "message": "Value is below the configured minimum."},
        {"source_row_number": 3, "column_name": "Status", "rule_id": "status-values", "message": "Value is not one of the configured allowed values."},
    ]
    processed_name = result["artifacts"]["processed_workbook"].split("/")[-1]
    assert list(load_workbook(tmp_path / "outputs" / processed_name, data_only=True).active.values) == [
        ("Amount", "Status"), ("12", "Open")
    ]
    report_name = result["artifacts"]["validation_report"].split("/")[-1]
    report = list(load_workbook(tmp_path / "outputs" / report_name, data_only=True).active.values)
    assert report[1][0] == 3
    assert json.loads(report[1][2]) == {"Amount": "-1", "Status": "Pending"}
    rule_report = json.loads(report[1][3])
    assert [item["rule_id"] for item in rule_report] == ["amount-range", "status-values"]
    connection = sqlite3.connect(tmp_path / "test.sqlite3")
    try:
        stored = connection.execute(
            "SELECT ruleset_id, ruleset_version, ruleset_json FROM business_jobs WHERE id = ?", (result["id"],)
        ).fetchone()
    finally:
        connection.close()
    assert stored == ("invoice-checks", "2.0", config_json)
    assert client.get(f"/artifacts/{processed_name}").status_code == 200


@pytest.mark.parametrize("rules", ["{bad", "{\"schema_version\":99}"])
def test_invalid_rules_fail_request_and_finalize_execution(client: TestClient, rules):
    before = {record.execution_id for record in default_runner.records()}
    response = client.post(
        "/workflows/business-file",
        files={"file": ("input.csv", "Name\nAda\n", "text/csv")},
        data={"rules": rules},
    )
    assert response.status_code == 422
    records = [record for record in default_runner.records() if record.execution_id not in before]
    assert len(records) == 1
    assert records[0].status.value == "failed"
    assert records[0].finished_at is not None
    assert records[0].error_code == "invalid_ruleset"


def test_rules_missing_column_is_a_client_error_and_blank_rules_preserve_contract(client: TestClient, tmp_path):
    config = json.dumps({"schema_version": 1, "ruleset_id": "x", "version": "1", "rules": [
        {"id": "missing", "type": "required", "column": "Absent"}
    ]})
    bad = client.post("/workflows/business-file", files={"file": ("input.csv", "Name\nAda\n")}, data={"rules": config})
    assert bad.status_code == 422
    assert "columns" in bad.json()["detail"]
    blank = client.post("/workflows/business-file", files={"file": ("input.csv", "Name\nAda\n")}, data={"rules": " "})
    omitted = client.post("/workflows/business-file", files={"file": ("input.csv", "Name\nAda\n")})
    assert blank.status_code == omitted.status_code == 201
    assert set(blank.json()) == set(omitted.json())
    assert "rule_issues" not in blank.json()
    connection = sqlite3.connect(tmp_path / "test.sqlite3")
    try:
        snapshots = connection.execute(
            "SELECT ruleset_id, ruleset_version, ruleset_json FROM business_jobs ORDER BY created_at"
        ).fetchall()
    finally:
        connection.close()
    assert snapshots == [(None, None, None), (None, None, None)]


def test_persistence_failure_finalizes_execution_and_removes_created_artifacts(tmp_path):
    db_path = tmp_path / "business.sqlite3"
    initialize_database(db_path)
    output_dir = tmp_path / "outputs"

    class BrokenPersistence:
        def create_business_job(self, **metadata):
            raise OSError("persistence unavailable")

    workflow = BusinessFileWorkflow(BrokenPersistence(), output_dir)
    runner = WorkflowRunner()
    with pytest.raises(OSError, match="persistence unavailable"):
        runner.execute(workflow, BusinessFileInput("source.csv", b"a\n1\n"))
    assert runner.records()[0].status.value == "failed"
    assert runner.records()[0].counts == {"total_rows": 1, "valid_rows": 1, "invalid_rows": 0}
    assert list(output_dir.iterdir()) == []


def test_rules_persistence_failure_is_logged_without_cell_or_configuration_values(tmp_path, caplog):
    output_dir = tmp_path / "outputs"
    config = json.dumps({"schema_version": 1, "ruleset_id": "private-rules", "version": "1",
                         "rules": [{"id": "private-rule", "type": "allowed_values", "column": "Name",
                                    "values": ["secret-allowed-value"]}]})

    class BrokenPersistence:
        def create_business_job(self, **metadata):
            raise OSError("database unavailable")

    runner = WorkflowRunner()
    workflow = BusinessFileWorkflow(BrokenPersistence(), output_dir)
    with caplog.at_level(logging.ERROR, logger="app.engine"):
        with pytest.raises(OSError, match="database unavailable"):
            runner.execute(workflow, BusinessFileInput(
                "source.csv", b"Name\nsecret-cell-value\n", rules_json=config
            ))
    record = runner.records()[0]
    assert record.status.value == "failed" and record.finished_at is not None
    assert list(output_dir.iterdir()) == []
    assert "secret-cell-value" not in caplog.text
    assert "secret-allowed-value" not in caplog.text
    assert "private-rules" not in caplog.text
    failure = next(item for item in caplog.records if item.getMessage() == "Workflow execution failed")
    assert failure.exc_info is not None
