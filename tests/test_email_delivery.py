import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from io import BytesIO
from app.engine.automation import WorkflowStep
from app.engine.contracts import ExecutionContext, WorkflowInputError
from app.engine.email_delivery import EmailDeliveryStepHandler, validate_email_config
from app.main import app
from app.storage.database import initialize_database


def _config(**overrides):
    return {
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_security": "starttls",
        "sender": "flowdesk@example.com",
        "recipient": "operator@example.net",
        "subject": "Processing report",
        "message": "The report is attached.",
        "username_env": "FLOWDESK_SMTP_USERNAME",
        "password_env": "FLOWDESK_SMTP_PASSWORD",
        "timeout_seconds": 10,
        "max_attachment_bytes": 5 * 1024 * 1024,
        **overrides,
    }


def _workflow(config=None):
    return json.dumps({
        "workflow_id": "report-delivery",
        "version": "1",
        "steps": [
            {"id": "process", "type": "business_file"},
            {"id": "report", "type": "pdf_report", "config": {"title": "Test Report"},
             "inputs": {"result": {"from_step": "process"}}},
            {"id": "email", "type": "email_delivery", "config": config or _config(),
             "inputs": {"report": {"from_step": "report"}}},
        ],
    })


class FakeSMTP:
    instances = []
    send_error = None

    def __init__(self, host, port, timeout, context=None):
        self.args = (host, port, timeout)
        self.ssl_context = context
        self.messages = []
        self.instances.append(self)

    def ehlo(self):
        return 250, b"ok"

    def has_extn(self, name):
        return name == "starttls"

    def starttls(self, context):
        self.tls_context = context
        return 220, b"ready"

    def login(self, username, password):
        self.login_values = (username, password)

    def send_message(self, message, from_addr, to_addrs):
        if self.send_error:
            raise self.send_error
        self.messages.append((message, from_addr, to_addrs))
        return {}

    def quit(self):
        self.quit_called = True


@pytest.fixture
def mail_client(tmp_path, monkeypatch):
    db_path = tmp_path / "mail.sqlite3"
    output_path = tmp_path / "outputs"
    monkeypatch.setenv("AUTOMATION_DB_PATH", str(db_path))
    monkeypatch.setenv("AUTOMATION_OUTPUT_DIR", str(output_path))
    monkeypatch.setenv("FLOWDESK_SMTP_USERNAME", "private-test-user")
    monkeypatch.setenv("FLOWDESK_SMTP_PASSWORD", "private-test-password")
    monkeypatch.setenv("FLOWDESK_SMTP_ALLOWED_HOSTS", "smtp.example.com")
    FakeSMTP.instances = []
    FakeSMTP.send_error = None
    initialize_database(db_path)
    with TestClient(app) as client:
        yield client, db_path, output_path


def _post(client, workflow=None):
    return client.post(
        "/automation/execute",
        files={"file": ("sales.csv", "Customer,Amount\nA-102,12.50\n", "text/csv")},
        data={"workflow": workflow or _workflow()},
    )


def test_email_step_sends_registered_pdf_and_persists_only_safe_metadata(mail_client, monkeypatch):
    client, db_path, output_path = mail_client
    monkeypatch.setattr("app.engine.email_delivery.smtplib.SMTP", FakeSMTP)

    response = _post(client)

    assert response.status_code == 201
    payload = response.json()
    assert [step["status"] for step in payload["steps"]] == ["succeeded"] * 3
    email_output = payload["steps"][2]["output"]
    assert email_output["accepted_by_smtp"] is True
    assert email_output["recipient_count"] == 1
    assert email_output["smtp_security"] == "starttls"
    message, sender, recipients = FakeSMTP.instances[0].messages[0]
    assert sender == "flowdesk@example.com"
    assert recipients == ["operator@example.net"]
    assert message["Subject"] == "Processing report"
    assert "The report is attached." in message.get_body().get_content()
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_content_type() == "application/pdf"
    pdf_bytes = attachments[0].get_payload(decode=True)
    assert pdf_bytes.startswith(b"%PDF-")
    assert (output_path / email_output["attachment_filename"]).is_file()

    with sqlite3.connect(db_path) as connection:
        persisted = connection.execute(
            "SELECT output_json FROM automation_executions WHERE execution_id = ?",
            (payload["execution_id"],),
        ).fetchone()
        persisted_step = connection.execute(
            "SELECT result_json FROM automation_step_results WHERE execution_id = ? AND step_id = 'email'",
            (payload["execution_id"],),
        ).fetchone()[0]
    persisted_text = persisted[0] + persisted_step
    for secret in (
        "private-test-user", "private-test-password", "operator@example.net",
        "Processing report", "The report is attached.", "smtp.example.com",
    ):
        assert secret not in persisted_text
    assert email_output["attachment_filename"] in persisted_text


def test_end_to_end_rules_transform_pdf_email_artifacts_and_persistent_history(mail_client, monkeypatch):
    client, db_path, _ = mail_client
    monkeypatch.setattr("app.engine.email_delivery.smtplib.SMTP", FakeSMTP)
    rules = json.dumps({
        "schema_version": 1, "ruleset_id": "e2e-checks", "version": "1",
        "rules": [{"id": "customer-required", "type": "required", "column": "Customer"}],
    })
    workflow = json.dumps({
        "workflow_id": "e2e-business-report", "version": "1",
        "steps": [
            {"id": "clean", "type": "clean_data", "config": {"trim_strings": True, "remove_blank_rows": False}},
            {"id": "mapping", "type": "map_columns", "config": {"mapping": {"Amount": "Unit_Price"}}, "inputs": {"previous": {"from_step": "clean"}}},
            {"id": "calculation", "type": "calculate_field", "config": {"name": "Line_Total", "operation": "multiply", "left_column": "Unit_Price", "right_column": "Quantity"}, "inputs": {"previous": {"from_step": "mapping"}}},
            {"id": "process", "type": "business_file", "inputs": {"transformations": {"from_step": "calculation"}}},
            {"id": "summary", "type": "business_file_summary", "inputs": {"result": {"from_step": "process"}}},
            {"id": "report", "type": "pdf_report", "config": {"title": "Synthetic E2E"}, "inputs": {"result": {"from_step": "process"}}},
            {"id": "email", "type": "email_delivery", "config": _config(), "inputs": {"report": {"from_step": "report"}}},
        ],
    })
    response = client.post(
        "/automation/execute",
        files={"file": ("synthetic.csv", "Customer,Amount,Quantity\n  Alpha  ,2,3\n,4,2\n", "text/csv")},
        data={"workflow": workflow, "rules": rules},
    )
    assert response.status_code == 201
    result = response.json()
    assert result["status"] == "succeeded"
    assert [step["step_id"] for step in result["steps"]] == ["clean", "mapping", "calculation", "process", "summary", "report", "email"]
    assert result["steps"][3]["output"]["execution_id"] == result["execution_id"]
    assert result["steps"][3]["output"]["valid_rows"] == 1
    assert result["steps"][3]["output"]["invalid_rows"] == 1
    assert result["steps"][-1]["output"]["accepted_by_smtp"] is True
    artifacts = result["steps"][3]["output"]["artifacts"]
    processed = load_workbook(BytesIO(client.get(artifacts["processed_workbook"]).content), data_only=True).active
    assert list(processed.values) == [("Customer", "Unit_Price", "Quantity", "Line_Total"), ("Alpha", "2", "3", 6.0)]
    validation = load_workbook(BytesIO(client.get(artifacts["validation_report"]).content), data_only=True).active
    assert validation.cell(2, 1).value == 3
    assert '"Customer": ""' in validation.cell(2, 3).value
    pdf_url = result["steps"][5]["output"]["artifacts"]["pdf_report"]
    assert client.get(pdf_url).content.startswith(b"%PDF-")
    detail = client.get(f"/executions/{result['execution_id']}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "succeeded"
    assert [step["status"] for step in detail.json()["steps"]] == ["succeeded"] * 7
    with sqlite3.connect(db_path) as connection:
        metadata = connection.execute("SELECT status, counts_json FROM automation_executions WHERE execution_id=?", (result["execution_id"],)).fetchone()
    assert metadata[0] == "succeeded"
    assert "Alpha" not in (metadata[1] or "")


def test_unapproved_smtp_host_is_rejected_before_network_connection(mail_client, monkeypatch):
    client, _, _ = mail_client
    monkeypatch.setattr("app.engine.email_delivery.smtplib.SMTP", FakeSMTP)

    response = _post(client, _workflow(_config(smtp_host="attacker.example.org")))

    assert response.status_code == 500
    payload = response.json()
    assert payload["steps"][-1]["error"]["code"] == "email_smtp_host_not_approved"
    assert FakeSMTP.instances == []


def test_invalid_email_configuration_fails_before_smtp_connection(mail_client, monkeypatch):
    client, _, _ = mail_client
    monkeypatch.setattr("app.engine.email_delivery.smtplib.SMTP", FakeSMTP)

    response = _post(client, _workflow(_config(recipient="not-an-email")))

    assert response.status_code == 422
    assert response.json()["status"] == "failed"
    assert response.json()["error"]["code"] == "invalid_workflow"
    assert FakeSMTP.instances == []


def test_missing_credentials_fail_without_connecting_or_disclosing_values(mail_client, monkeypatch):
    client, db_path, _ = mail_client
    monkeypatch.delenv("FLOWDESK_SMTP_USERNAME")
    monkeypatch.delenv("FLOWDESK_SMTP_PASSWORD")
    monkeypatch.setattr("app.engine.email_delivery.smtplib.SMTP", FakeSMTP)

    response = _post(client)

    assert response.status_code == 500
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["steps"][-1]["error"]["code"] == "email_credentials_unavailable"
    assert "private-test-password" not in response.text
    assert FakeSMTP.instances == []
    with sqlite3.connect(db_path) as connection:
        execution_fields = connection.execute(
            "SELECT workflow_name, workflow_id, workflow_version, error_json, counts_json, output_json "
            "FROM automation_executions WHERE execution_id = ?",
            (payload["execution_id"],),
        ).fetchone()
        step_fields = connection.execute(
            "SELECT step_id, step_type, status, error_json, result_json "
            "FROM automation_step_results WHERE execution_id = ? AND step_id = 'email'",
            (payload["execution_id"],),
        ).fetchone()
    persisted_text = json.dumps({"execution": execution_fields, "step": step_fields})
    for sensitive_value in (
        "FLOWDESK_SMTP_USERNAME", "FLOWDESK_SMTP_PASSWORD",
        "private-test-user", "private-test-password", "operator@example.net",
    ):
        assert sensitive_value not in persisted_text


def test_implicit_tls_uses_verified_ssl_context(mail_client, monkeypatch):
    client, _, _ = mail_client
    monkeypatch.setattr("app.engine.email_delivery.smtplib.SMTP_SSL", FakeSMTP)

    response = _post(client, _workflow(_config(smtp_security="implicit_tls", smtp_port=465)))

    assert response.status_code == 201
    assert response.json()["steps"][-1]["status"] == "succeeded"
    instance = FakeSMTP.instances[0]
    context = instance.ssl_context
    assert context is not None and context.check_hostname and context.verify_mode


@pytest.mark.parametrize("failure_stage", ["connection", "tls", "send"])
def test_smtp_failures_are_safe_and_finalize_failed_step(mail_client, monkeypatch, failure_stage, caplog):
    client, db_path, _ = mail_client

    class FailingSMTP(FakeSMTP):
        def __init__(self, *args, **kwargs):
            if failure_stage == "connection":
                raise RuntimeError("private-test-password recipient@example.net")
            super().__init__(*args, **kwargs)

        def starttls(self, context):
            if failure_stage == "tls":
                raise RuntimeError("private-test-password")
            return super().starttls(context)

        def send_message(self, *args, **kwargs):
            if failure_stage == "send":
                raise RuntimeError("private-test-password recipient@example.net")
            return super().send_message(*args, **kwargs)

    monkeypatch.setattr("app.engine.email_delivery.smtplib.SMTP", FailingSMTP)
    response = _post(client)
    assert response.status_code == 500
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["steps"][-1]["status"] == "failed"
    assert "private-test-password" not in response.text
    assert "recipient@example.net" not in response.text
    assert "private-test-password" not in caplog.text
    assert "recipient@example.net" not in caplog.text
    with sqlite3.connect(db_path) as connection:
        history = connection.execute(
            "SELECT error_json FROM automation_executions WHERE execution_id = ?",
            (payload["execution_id"],),
        ).fetchone()[0]
        step_history = connection.execute(
            "SELECT error_json, result_json FROM automation_step_results WHERE execution_id = ? AND step_id = 'email'",
            (payload["execution_id"],),
        ).fetchone()
    history_text = str(history) + str(step_history)
    assert "private-test-password" not in history_text
    assert "recipient@example.net" not in history_text
    expected = {"connection": "email_connection_failed", "tls": "email_tls_failed", "send": "email_send_failed"}
    assert payload["steps"][-1]["error"]["code"] == expected[failure_stage]


def test_email_config_rejects_secret_values_as_environment_names_and_bad_limits():
    with pytest.raises(ValueError, match="username_env"):
        validate_email_config(_config(username_env="actual-password"))
    with pytest.raises(ValueError, match="max_attachment_bytes"):
        validate_email_config(_config(max_attachment_bytes=11 * 1024 * 1024))


@pytest.mark.parametrize("reference,registration,file_bytes,code", [
    ({"filename": "../outside_report.pdf", "artifacts": {}}, None, None, "invalid_email_attachment"),
    ({"filename": "a" * 32 + "_report.pdf", "artifacts": {"pdf_report": "/artifacts/" + "a" * 32 + "_report.pdf"}}, None, None, "email_attachment_not_found"),
    ({"filename": "a" * 32 + "_report.pdf", "artifacts": {"pdf_report": "/artifacts/" + "a" * 32 + "_report.pdf"}}, {"artifact_name": "a" * 32 + "_report.pdf", "artifact_type": "pdf_report"}, b"%PDF-" + b"x" * 1200, "email_attachment_too_large"),
])
def test_attachment_resolution_requires_registered_confined_bounded_pdf(
    tmp_path, reference, registration, file_bytes, code
):
    output = tmp_path / "outputs"
    output.mkdir()
    if file_bytes is not None:
        (output / reference["filename"]).write_bytes(file_bytes)
    handler = EmailDeliveryStepHandler(output, lambda _: registration)
    step = WorkflowStep("email", "email_delivery", _config(max_attachment_bytes=1024), {
        "report": {"from_step": "report"},
    })
    with pytest.raises(WorkflowInputError) as error:
        handler.run(step, {"report": reference}, {}, None, ExecutionContext("execution"))
    assert error.value.code == code


def test_dashboard_exposes_optional_email_form_and_safe_workflow_chaining(mail_client):
    client, _, _ = mail_client
    page = client.get("/").text
    script = client.get("/static/app.js").text
    assert 'id="email-delivery-enabled"' in page
    assert 'id="email-recipient"' in page
    assert 'id="email-password-env"' in page
    assert 'id="email-pdf-source"' in page
    assert 'never enter passwords or secret values' in page
    assert 'type: "email_delivery"' in script
    assert 'inputs: { report: { from_step: "pdf-report" } }' in script
    assert 'pdfReportEnabled.checked || emailDeliveryEnabled.checked' in script
    assert 'formData.append("workflow", JSON.stringify(buildAutomationWorkflow()))' in script

