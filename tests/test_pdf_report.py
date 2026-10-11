import json
import os
import sqlite3
from types import SimpleNamespace
import uuid

import pytest

from app.engine.runner import WorkflowRunner, _persist_sqlite


_DEFAULT_INPUTS = object()


def _workflow(config=None, final_inputs=_DEFAULT_INPUTS):
    report = {"id": "report", "type": "pdf_report", "config": config or {"title": "Sales Validation Report"}}
    if final_inputs is _DEFAULT_INPUTS:
        report["inputs"] = {"result": {"from_step": "process"}}
    elif final_inputs is not None:
        report["inputs"] = final_inputs
    return json.dumps({
        "workflow_id": "sales-report",
        "version": "1",
        "steps": [
            {"id": "process", "type": "business_file"},
            {"id": "summary", "type": "business_file_summary",
             "inputs": {"result": {"from_step": "process"}}},
            report,
        ],
    })


def _run(client, csv_text, workflow=None, rules=None):
    data = {"workflow": workflow or _workflow()}
    if rules is not None:
        data["rules"] = rules
    return client.post(
        "/automation/execute",
        files={"file": ("sales.csv", csv_text, "text/csv")},
        data=data,
    )


def _pdf_url(response):
    result = response.json()
    output = next(step["output"] for step in result["steps"] if step["step_id"] == "report")
    return result, output, output["artifacts"]["pdf_report"]


def test_pdf_report_generates_and_downloads_registered_pdf(client, tmp_path):
    response = _run(client, "Customer,Amount\nA-102,18.50\n")
    assert response.status_code == 201
    result, report, url = _pdf_url(response)
    assert result["steps"][-1]["status"] == "succeeded"
    assert report["title"] == "Sales Validation Report"
    assert report["finding_count"] == 0
    assert report["findings_included"] is True
    assert report["filename"].endswith("_report.pdf")
    output_path = tmp_path / "outputs" / report["filename"]
    assert output_path.read_bytes().startswith(b"%PDF-")
    download = client.get(url)
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/pdf")
    assert download.content.startswith(b"%PDF-")

    connection = sqlite3.connect(tmp_path / "test.sqlite3")
    try:
        registered = connection.execute(
            "SELECT execution_id, artifact_type FROM business_artifacts WHERE artifact_name = ?",
            (report["filename"],),
        ).fetchone()
        stored_output = connection.execute(
            "SELECT output_json FROM automation_executions WHERE execution_id = ?",
            (result["execution_id"],),
        ).fetchone()[0]
    finally:
        connection.close()
    assert registered == (result["execution_id"], "pdf_report")
    assert report["filename"] in stored_output


def test_pdf_report_with_many_findings_spans_pages_and_marks_truncation(client, tmp_path):
    rules = json.dumps({
        "schema_version": 1,
        "ruleset_id": "customer-required",
        "version": "1",
        "rules": [{"id": "customer", "type": "required", "column": "Customer"}],
    })
    csv_text = "Customer,Amount\n" + "\n".join(f",{index}" for index in range(160)) + "\n"
    response = _run(client, csv_text, rules=rules)
    assert response.status_code == 201
    _, report, _ = _pdf_url(response)
    assert report["finding_count"] >= 100
    assert report["findings_truncated"] is True
    data = (tmp_path / "outputs" / report["filename"]).read_bytes()
    assert data.startswith(b"%PDF-")
    page_objects = data.count(b"/Type /Page") - data.count(b"/Type /Pages")
    assert page_objects >= 2


@pytest.mark.parametrize(("config", "expected_message"), [
    ({"title": "   "}, "title"),
    ({"title": "Report", "include_findings": "yes"}, "include_findings"),
    ({"title": "Report", "unexpected": True}, "pdf_report"),
])
def test_pdf_report_rejects_invalid_configuration(client, config, expected_message):
    response = _run(client, "Customer\nA\n", workflow=_workflow(config))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_workflow"
    assert expected_message in response.json()["error"]["message"]


@pytest.mark.parametrize("inputs", [None, {"result": {"from_step": "summary"}}])
def test_pdf_step_requires_direct_business_file_result(client, inputs):
    response = _run(client, "Customer\nA\n", workflow=_workflow(final_inputs=inputs))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_workflow"


def test_failed_processing_skips_pdf_and_does_not_register_report(client):
    workflow = _workflow()
    response = _run(client, "Customer\nA\n", workflow=workflow, rules="{bad-json")
    assert response.status_code == 422
    payload = response.json()
    assert [step["status"] for step in payload["steps"]] == ["failed", "skipped", "skipped"]
    connection = sqlite3.connect(os.environ["AUTOMATION_DB_PATH"])
    try:
        assert connection.execute("SELECT COUNT(*) FROM business_artifacts").fetchone()[0] == 0
    finally:
        connection.close()


def test_pdf_reference_survives_runner_recreation_and_download_path_is_confined(client, monkeypatch):
    response = _run(client, "Customer\nA\n")
    assert response.status_code == 201
    result, report, url = _pdf_url(response)
    monkeypatch.setattr("app.api.routes.default_runner", WorkflowRunner(persistence=_persist_sqlite))
    persisted = client.get(f"/executions/{result['execution_id']}")
    assert persisted.status_code == 200
    saved_report = next(step["output"] for step in persisted.json()["steps"] if step["step_id"] == "report")
    assert saved_report["artifacts"]["pdf_report"] == url
    assert client.get(url).status_code == 200
    assert client.get("/artifacts/..%2Fpyproject.toml").status_code == 404
    assert client.get(f"/artifacts/{uuid.uuid4().hex}_report.pdf").status_code == 404


def test_pdf_registration_failure_fails_step_and_removes_unregistered_pdf(client, monkeypatch, tmp_path):
    def fail_registration(**kwargs):
        raise RuntimeError("database registration unavailable")

    monkeypatch.setattr("app.api.routes.register_business_artifact", fail_registration)
    response = _run(client, "Customer\nA\n")
    assert response.status_code == 500
    assert response.json()["status"] == "failed"
    assert response.json()["steps"][-1]["status"] == "failed"
    assert list((tmp_path / "outputs").glob("*_report.pdf")) == []


def test_pdf_creation_does_not_overwrite_preexisting_file(client, monkeypatch, tmp_path):
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    reserved = output_dir / ("a" * 32 + "_report.pdf")
    reserved.write_bytes(b"unrelated existing file")
    monkeypatch.setattr(
        "app.engine.automation.uuid",
        SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="a" * 32)),
    )
    response = _run(client, "Customer\nA\n")
    assert response.status_code == 500
    assert reserved.read_bytes() == b"unrelated existing file"


def test_dashboard_exposes_accessible_pdf_options_and_builds_chained_step(client):
    page = client.get("/")
    script = client.get("/static/app.js")
    assert page.status_code == script.status_code == 200
    assert 'id="pdf-report-enabled"' in page.text
    assert 'id="pdf-report-title"' in page.text
    assert 'id="pdf-report-include-findings"' in page.text
    assert 'type: "pdf_report"' in script.text
    assert 'inputs: { result: { from_step: "process" } }' in script.text
    assert 'config: { title, include_findings: pdfReportIncludeFindings.checked }' in script.text
    assert 'key === "pdf_report" ? "Download PDF report"' in script.text
    assert 'steps.length > 20' in script.text
