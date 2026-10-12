import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.engine.automation import (
    AutomationEngine,
    AutomationRunInput,
    AutomationStepError,
    ConfiguredAutomationWorkflow,
    WorkflowDefinitionError,
    WorkflowStep,
    parse_workflow_definition,
)
from app.engine.business_file import BusinessFileInput
from app.engine.contracts import ExecutionContext, ExecutionStatus
from app.engine.runner import WorkflowRunner
from app.storage.repository import get_imported_rows


class EmitHandler:
    step_type = "emit"

    def validate(self, step, prior_step_types):
        if set(step.config) != {"value"} or step.inputs:
            raise WorkflowDefinitionError("emit expects a value and no inputs")

    def run(self, step, resolved_inputs, prior_outputs, workflow_input, context):
        return {"value": step.config["value"], "items": []}


class MutateHandler:
    step_type = "mutate"

    def validate(self, step, prior_step_types):
        reference = step.inputs.get("source")
        if step.config or set(step.inputs) != {"source"}:
            raise WorkflowDefinitionError("mutate expects a source reference")
        if not isinstance(reference, dict) or prior_step_types.get(reference.get("from_step")) != "emit":
            raise WorkflowDefinitionError("source must reference an emit step")

    def run(self, step, resolved_inputs, prior_outputs, workflow_input, context):
        result = resolved_inputs["source"]
        result["items"].append("updated")
        return result


class FailHandler:
    step_type = "fail"

    def validate(self, step, prior_step_types):
        if step.config or step.inputs:
            raise WorkflowDefinitionError("fail takes no inputs")

    def run(self, step, resolved_inputs, prior_outputs, workflow_input, context):
        raise ValueError("private spreadsheet cell content")


def generic_handlers():
    return {"emit": EmitHandler(), "mutate": MutateHandler(), "fail": FailHandler()}


def generic_definition():
    return json.dumps({
        "workflow_id": "isolated-run",
        "version": "1",
        "steps": [
            {"id": "first", "type": "emit", "config": {"value": "private-value"}},
            {"id": "second", "type": "mutate", "inputs": {"source": {"from_step": "first"}}},
        ],
    })


def run_input():
    return AutomationRunInput(BusinessFileInput("unused.csv", b"unused"))


def test_definition_rejects_unsupported_duplicate_forward_and_extra_fields():
    handlers = generic_handlers()
    documents = [
        {"workflow_id": "x", "version": "1", "steps": [{"id": "a", "type": "missing"}]},
        {"workflow_id": "x", "version": "1", "steps": [
            {"id": "a", "type": "emit", "config": {"value": "x"}},
            {"id": "a", "type": "emit", "config": {"value": "y"}},
        ]},
        {"workflow_id": "x", "version": "1", "steps": [
            {"id": "a", "type": "mutate", "inputs": {"source": {"from_step": "later"}}},
            {"id": "later", "type": "emit", "config": {"value": "x"}},
        ]},
        {"workflow_id": "x", "version": "1", "steps": [], "python": "ignored"},
    ]
    for document in documents:
        with pytest.raises(WorkflowDefinitionError):
            parse_workflow_definition(json.dumps(document), handlers)


def test_sequential_steps_pass_outputs_and_isolate_each_run(caplog):
    runner = WorkflowRunner()
    workflow = ConfiguredAutomationWorkflow(generic_definition(), AutomationEngine(generic_handlers()))
    with caplog.at_level(logging.INFO, logger="app.engine"):
        first = runner.execute(workflow, run_input())
        second = runner.execute(workflow, run_input())

    assert first.status is second.status is ExecutionStatus.SUCCEEDED
    assert [item["step_id"] for item in first.step_results] == ["first", "second"]
    assert first.step_results[0]["status"] == "succeeded"
    assert first.output["outputs"]["second"]["value"] == "private-value"
    # A downstream handler mutates its private copy, not the upstream output or another execution.
    assert first.output["outputs"]["first"]["items"] == []
    assert second.output["outputs"]["first"]["items"] == []
    assert first.execution_id != second.execution_id
    assert any(getattr(item, "execution_status", None) == "queued" for item in caplog.records)
    assert any(getattr(item, "step_status", None) == "succeeded" for item in caplog.records)
    assert "private-value" not in caplog.text


def test_failed_step_stops_pipeline_and_records_failure_without_logging_data(caplog):
    definition = json.dumps({
        "workflow_id": "failure-case",
        "version": "1",
        "steps": [
            {"id": "fail-now", "type": "fail"},
            {"id": "dependent", "type": "emit", "config": {"value": "must-not-run"}},
        ],
    })
    runner = WorkflowRunner()
    workflow = ConfiguredAutomationWorkflow(definition, AutomationEngine(generic_handlers()))

    with caplog.at_level(logging.ERROR, logger="app.engine"):
        with pytest.raises(AutomationStepError):
            runner.execute(workflow, run_input())

    record = runner.records()[0]
    assert record.status is ExecutionStatus.FAILED
    assert record.finished_at is not None
    assert record.step_results[0]["status"] == "failed"
    assert record.step_results[0]["error"] == {
        "code": "step_failed", "message": "Step execution failed.", "type": "ValueError"
    }
    assert record.step_results[1]["status"] == "skipped"
    assert "private spreadsheet cell content" not in caplog.text


def test_invalid_definition_is_finalized_as_client_input_failure():
    workflow = ConfiguredAutomationWorkflow(
        '{"workflow_id":"bad","version":"1","steps":[{"id":"x","type":"unknown"}]}',
        AutomationEngine(generic_handlers()),
    )
    record = WorkflowRunner().execute(workflow, run_input())
    assert record.status is ExecutionStatus.FAILED
    assert record.error_code == "invalid_workflow"
    assert record.finished_at is not None


def test_business_file_workflow_executes_as_sequential_api_steps(client: TestClient):
    definition = json.dumps({
        "workflow_id": "business-file-with-summary",
        "version": "1",
        "steps": [
            {"id": "process", "type": "business_file"},
            {"id": "summary", "type": "business_file_summary",
             "inputs": {"result": {"from_step": "process"}}},
        ],
    })
    rules = json.dumps({
        "schema_version": 1,
        "ruleset_id": "account-required",
        "version": "1",
        "rules": [{"id": "account", "type": "required", "column": "Account"}],
    })
    response = client.post(
        "/automation/execute",
        files={"file": ("transactions.csv", "Account,Amount\n001,12\n,15\n", "text/csv")},
        data={"workflow": definition, "rules": rules},
    )

    assert response.status_code == 201
    result = response.json()
    assert result["status"] == "succeeded"
    assert [step["status"] for step in result["steps"]] == ["succeeded", "succeeded"]
    summary = result["output"]["outputs"]["summary"]
    assert (summary["total_rows"], summary["valid_rows"], summary["invalid_rows"]) == (2, 1, 1)
    assert client.get(f"/executions/{result['execution_id']}").json() == result
    assert client.get(summary["artifacts"]["validation_report"]).status_code == 200


def test_transformation_steps_chain_into_business_file_artifact_and_persistence(client: TestClient):
    definition = json.dumps({
        "workflow_id": "sales-transform",
        "version": "1",
        "steps": [
            {"id": "clean", "type": "clean_data",
             "config": {"trim_strings": True, "remove_blank_rows": True}},
            {"id": "map", "type": "map_columns",
             "config": {"mapping": {"Item": "Product", "Units": "Quantity"}},
             "inputs": {"previous": {"from_step": "clean"}}},
            {"id": "total", "type": "calculate_field",
             "config": {"name": "Line_Total", "operation": "multiply",
                        "left_column": "Quantity", "right_column": "Price"},
             "inputs": {"previous": {"from_step": "map"}}},
            {"id": "process", "type": "business_file",
             "inputs": {"transformations": {"from_step": "total"}}},
            {"id": "summary", "type": "business_file_summary",
             "inputs": {"result": {"from_step": "process"}}},
        ],
    })
    response = client.post(
        "/automation/execute",
        files={"file": ("sales.csv", "Item,Units,Price\n Pen ,2,3.5\n,broken\n,1,2\n", "text/csv")},
        data={
            "workflow": definition,
            "rules": json.dumps({
                "schema_version": 1,
                "ruleset_id": "sales-transform-check",
                "version": "1",
                "rules": [{"id": "item-required", "type": "required", "column": "Item"}],
            }),
        },
    )

    assert response.status_code == 201
    result = response.json()
    assert [step["status"] for step in result["steps"]] == ["succeeded"] * 5
    summary = result["output"]["outputs"]["summary"]
    assert (summary["total_rows"], summary["valid_rows"], summary["invalid_rows"]) == (3, 1, 2)
    workbook_response = client.get(summary["artifacts"]["processed_workbook"])
    assert workbook_response.status_code == 200
    from io import BytesIO
    from openpyxl import load_workbook
    workbook = load_workbook(BytesIO(workbook_response.content), data_only=True)
    assert list(workbook.active.values) == [("Product", "Quantity", "Price", "Line_Total"), ("Pen", "2", "3.5", 7.0)]
    workbook.close()
    report_response = client.get(summary["artifacts"]["validation_report"])
    assert report_response.status_code == 200
    report = load_workbook(BytesIO(report_response.content), data_only=True)
    report_rows = list(report.active.values)[1:]
    assert "broken" in report_rows[0][2]
    assert "Item" in report_rows[1][2]
    assert "Product" not in report_rows[1][2]
    report.close()
    assert get_imported_rows(summary["id"])[0] == {
        "Product": "Pen", "Quantity": "2", "Price": "3.5", "Line_Total": 7.0,
    }


def test_transformation_runtime_failure_finalizes_step_and_skips_downstream(client: TestClient):
    definition = json.dumps({
        "workflow_id": "invalid-calculation",
        "version": "1",
        "steps": [
            {"id": "calculate", "type": "calculate_field",
             "config": {"name": "Ratio", "operation": "divide",
                        "left_column": "A", "right_column": "B"}},
            {"id": "process", "type": "business_file",
             "inputs": {"transformations": {"from_step": "calculate"}}},
            {"id": "summary", "type": "business_file_summary",
             "inputs": {"result": {"from_step": "process"}}},
        ],
    })
    response = client.post(
        "/automation/execute",
        files={"file": ("values.csv", "A,B\n10,0\n", "text/csv")},
        data={"workflow": definition},
    )
    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] == "invalid_transformation"
    assert payload["steps"][0]["status"] == "succeeded"
    assert payload["steps"][1]["status"] == "failed"
    assert payload["steps"][2]["status"] == "skipped"


def test_invalid_transformation_definition_is_rejected(client: TestClient):
    definition = json.dumps({
        "workflow_id": "bad-transform",
        "version": "1",
        "steps": [{"id": "clean", "type": "clean_data", "config": {"trim_strings": "yes"}}],
    })
    response = client.post(
        "/automation/execute",
        files={"file": ("values.csv", "A\ntext\n", "text/csv")},
        data={"workflow": definition},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_workflow"


def test_api_invalid_definition_returns_finalized_execution_record(client: TestClient):
    response = client.post(
        "/automation/execute",
        files={"file": ("input.csv", "a\n1\n", "text/csv")},
        data={"workflow": '{"workflow_id":"bad","version":"1","steps":[{"id":"x","type":"eval"}]}'},
    )
    assert response.status_code == 422
    result = response.json()
    assert result["status"] == "failed"
    assert result["error"]["code"] == "invalid_workflow"
    assert client.get(f"/executions/{result['execution_id']}").status_code == 200


@pytest.mark.parametrize("include_rules", [False, True], ids=["omitted-rules", "blank-rules"])
def test_automation_api_without_rules_preserves_file_integrity_only_behavior(
    client: TestClient, include_rules: bool
):
    definition = json.dumps({
        "workflow_id": "business-file-processing",
        "version": "1",
        "steps": [
            {"id": "process", "type": "business_file"},
            {"id": "summary", "type": "business_file_summary",
             "inputs": {"result": {"from_step": "process"}}},
        ],
    })
    data = {"workflow": definition}
    if include_rules:
        data["rules"] = "  \n"
    response = client.post(
        "/automation/execute",
        files={"file": ("input.csv", "Name\nAda\n", "text/csv")},
        data=data,
    )

    assert response.status_code == 201
    result = response.json()
    assert result["status"] == "succeeded"
    assert result["error"] is None
    assert [step["status"] for step in result["steps"]] == ["succeeded", "succeeded"]
    summary = result["output"]["outputs"]["summary"]
    assert (summary["valid_rows"], summary["invalid_rows"]) == (1, 0)


def test_automation_api_malformed_rules_finalizes_failed_execution(client: TestClient):
    definition = json.dumps({
        "workflow_id": "business-file-processing",
        "version": "1",
        "steps": [{"id": "process", "type": "business_file"}],
    })
    response = client.post(
        "/automation/execute",
        files={"file": ("input.csv", "Name\nAda\n", "text/csv")},
        data={"workflow": definition, "rules": "{"},
    )

    assert response.status_code == 422
    result = response.json()
    assert result["status"] == "failed"
    assert result["error"]["code"] == "invalid_ruleset"
    assert result["error"]["message"] == "Ruleset must be valid JSON."
    assert result["steps"][0]["status"] == "failed"
    assert result["finished_at"] is not None
    assert result["counts"] == {}
    history_result = client.get(f"/executions/{result['execution_id']}").json()
    assert history_result["counts"] == {}
    assert history_result == result


def test_missing_execution_record_returns_404(client: TestClient):
    assert client.get("/executions/missing-execution").status_code == 404


def test_legacy_token_env_api_workflow_fails_safely_with_migration_guidance(client: TestClient):
    definition = json.dumps({
        "workflow_id": "legacy-token-env",
        "version": "1",
        "steps": [{
            "id": "lookup",
            "type": "api_request",
            "config": {
                "endpoint": "https://api.example.test/v1/status",
                "token_env": "FLOWDESK_SMTP_PASSWORD",
            },
        }],
    })
    response = client.post(
        "/automation/execute",
        files={"file": ("staging.csv", "Customer\nQA\n", "text/csv")},
        data={"workflow": definition},
    )

    assert response.status_code == 422
    result = response.json()
    message = result["error"]["message"]
    assert result["status"] == "failed"
    assert result["error"]["code"] == "invalid_workflow"
    assert result["finished_at"] is not None
    history = client.get(f"/executions/{result['execution_id']}").json()
    assert history["error"]["message"] == message
    assert "token_env is no longer supported" in message
    assert "credential_id" in message
    assert "FLOWDESK_API_CREDENTIALS_JSON" in message
    assert "FLOWDESK_SMTP_PASSWORD" not in response.text
