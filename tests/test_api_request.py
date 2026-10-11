import json
import logging
import socket
import time

import pytest

from app.engine.api_request import APIRequestStepHandler
from app.engine.api_request import APIRequestFailure
from app.engine.automation import (
    AutomationEngine,
    AutomationRunInput,
    AutomationStepError,
    ConfiguredAutomationWorkflow,
    WorkflowDefinitionError,
)
from app.engine.business_file import BusinessFileInput
from app.engine.contracts import ExecutionStatus
from app.engine.runner import WorkflowRunner
from app.storage.repository import get_automation_execution
import app.engine.api_request as api_request


@pytest.fixture(autouse=True)
def mock_public_dns(monkeypatch):
    monkeypatch.setattr(
        api_request.socket,
        "getaddrinfo",
        lambda _host, port, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", port))],
    )


def _step(config=None, inputs=None):
    return {
        "workflow_id": "api-workflow",
        "version": "1",
        "steps": [{"id": "fetch", "type": "api_request", "config": config or {
            "endpoint": "https://api.example.test/v1/items",
        }, **({"inputs": inputs} if inputs else {})}],
    }


def _run(handler, definition):
    runner = WorkflowRunner()
    workflow = ConfiguredAutomationWorkflow(
        json.dumps(definition), AutomationEngine({"api_request": handler})
    )
    try:
        record = runner.execute(workflow, AutomationRunInput(BusinessFileInput("unused.csv", b"unused")))
    except AutomationStepError as error:
        record = runner.get(error.execution_id)
    return record


def test_json_get_uses_server_credential_bound_to_exact_origin(monkeypatch):
    calls = []

    def requester(endpoint, params, timeout, max_bytes, token, checked_address):
        calls.append((endpoint, params, timeout, max_bytes, token, checked_address))
        return 200, "application/json; charset=utf-8", b'{"items":[1,2]}'

    monkeypatch.setenv("FLOWDESK_API_CREDENTIALS_JSON", json.dumps({"test-api": {"origin": "HTTPS://API.EXAMPLE.TEST:443/", "token": "secret-api-token"}}))
    handler = APIRequestStepHandler(requester)
    config = {
        "endpoint": "https://api.example.test/v1/items",
        "query_params": {"page": 2, "active": True},
        "timeout_seconds": 5,
        "max_response_bytes": 4096,
        "credential_id": "test-api",
    }
    record = _run(handler, _step(config))

    assert record.status is ExecutionStatus.SUCCEEDED
    output = record.output["outputs"]["fetch"]
    assert output["response"] == {"items": [1, 2]}
    assert (output["status_code"], output["response_bytes"]) == (200, 15)
    assert calls == [(config["endpoint"], config["query_params"], 5.0, 4096, "secret-api-token", "8.8.8.8")]


def test_token_value_echoed_by_external_json_is_redacted(monkeypatch):
    monkeypatch.setenv("FLOWDESK_API_CREDENTIALS_JSON", json.dumps({"test-api": {"origin": "HTTPS://API.EXAMPLE.TEST:443/", "token": "secret-api-token"}}))
    handler = APIRequestStepHandler(lambda *_: (
        200, "application/json", b'{"token":"secret-api-token"}'
    ))
    record = _run(handler, _step({
        "endpoint": "https://api.example.test",
        "credential_id": "test-api",
    }))
    assert record.status is ExecutionStatus.SUCCEEDED
    assert record.output["outputs"]["fetch"]["response"] == {"token": "[REDACTED]"}


@pytest.mark.parametrize("config", [
    {"endpoint": "http://api.example.test"},
    {"endpoint": "https://api.example.test:444"},
    {"endpoint": "https://user:pass@api.example.test"},
    {"endpoint": "https://api.example.test?mode=private"},
    {"endpoint": "https://api.example.test", "unexpected": 1},
    {"endpoint": "https://api.example.test", "timeout_seconds": 30},
    {"endpoint": "https://api.example.test", "max_response_bytes": 10},
    {"endpoint": "https://api.example.test", "credential_id": "bad name"},
    {"endpoint": "https://api.example.test", "query_params": {"x": {"nested": 1}}},
])
def test_invalid_api_configuration_is_rejected(config):
    from app.engine.automation import parse_workflow_definition

    with pytest.raises(WorkflowDefinitionError):
        parse_workflow_definition(json.dumps(_step(config)), {"api_request": APIRequestStepHandler(lambda *_: None)})


@pytest.mark.parametrize(("status", "content_type", "body", "expected_code"), [
    (503, "application/json", b'{"ok":false}', "api_http_error"),
    (200, "text/plain", b"hello", "api_content_type"),
    (200, "application/json", b"{broken", "api_invalid_json"),
    (200, "application/json", b'{"x":1,"x":2}', "api_invalid_json"),
    (200, "application/json", b"x" * 2049, "api_response_too_large"),
    (302, "application/json", b"{}", "api_redirect_rejected"),
])
def test_external_api_failures_have_safe_step_errors(status, content_type, body, expected_code):
    handler = APIRequestStepHandler(lambda *_: (status, content_type, body))
    definition = _step({"endpoint": "https://api.example.test", "max_response_bytes": 2048})
    record = _run(handler, definition)
    assert record.status is ExecutionStatus.FAILED
    assert record.step_results[0]["error"]["code"] == expected_code
    assert "x" * 50 not in record.step_results[0]["error"]["message"]


def test_timeout_fails_workflow_and_does_not_log_secret_or_request_details(caplog, monkeypatch):
    def timeout(*_):
        raise TimeoutError("secret-api-token https://private.example/path?customer=secret")

    handler = APIRequestStepHandler(timeout)
    monkeypatch.setenv("FLOWDESK_API_CREDENTIALS_JSON", json.dumps({"test-api": {"origin": "HTTPS://API.EXAMPLE.TEST:443/", "token": "secret-api-token"}}))
    definition = _step({
        "endpoint": "https://api.example.test/private-path",
        "query_params": {"customer": "private-customer"},
        "credential_id": "test-api",
    })
    with caplog.at_level(logging.INFO, logger="app.engine"):
        record = _run(handler, definition)
    assert record.status is ExecutionStatus.FAILED
    assert record.step_results[0]["error"]["code"] == "api_timeout"
    for private_value in ("secret-api-token", "private-customer", "private-path"):
        assert private_value not in caplog.text


@pytest.mark.parametrize("environment_name", ["FLOWDESK_SMTP_PASSWORD", "SOME_UNRELATED_SECRET"])
def test_workflow_cannot_select_arbitrary_environment_variables(environment_name, monkeypatch):
    from app.engine.automation import parse_workflow_definition

    monkeypatch.setenv(environment_name, "must-not-be-read")
    definition = _step({"endpoint": "https://api.example.test", "token_env": environment_name})
    with pytest.raises(WorkflowDefinitionError, match="unsupported fields"):
        parse_workflow_definition(json.dumps(definition), {"api_request": APIRequestStepHandler(lambda *_: None)})
    calls = []
    monkeypatch.setenv("FLOWDESK_API_CREDENTIALS_JSON", "{}")
    record = _run(APIRequestStepHandler(lambda *args: (calls.append(args) or (200, "application/json", b"{}"))),
                  _step({"endpoint": "https://api.example.test", "credential_id": environment_name}))
    assert record.status is ExecutionStatus.FAILED
    assert record.step_results[0]["error"]["code"] == "api_credential_unavailable"
    assert calls == []


def test_credential_is_never_sent_to_unapproved_origin(monkeypatch):
    monkeypatch.setenv("FLOWDESK_API_CREDENTIALS_JSON", json.dumps({"billing": {"origin": "https://api.example.test", "token": "private-placeholder-token"}}))
    calls = []
    record = _run(APIRequestStepHandler(lambda *args: (calls.append(args) or (200, "application/json", b"{}"))),
                  _step({"endpoint": "https://attacker.example.test/v1", "credential_id": "billing"}))
    assert record.status is ExecutionStatus.FAILED
    assert record.step_results[0]["error"]["code"] == "api_credential_origin_mismatch"
    assert calls == []


@pytest.mark.parametrize("endpoint", ["https://api.example.test.attacker.test/v1", "https://evilapi.example.test/v1"])
def test_credential_origin_match_is_exact_not_suffix_based(endpoint, monkeypatch):
    monkeypatch.setenv("FLOWDESK_API_CREDENTIALS_JSON", json.dumps({"billing": {"origin": "https://api.example.test", "token": "private-placeholder-token"}}))
    calls = []
    record = _run(APIRequestStepHandler(lambda *args: (calls.append(args) or (200, "application/json", b"{}"))),
                  _step({"endpoint": endpoint, "credential_id": "billing"}))
    assert record.status is ExecutionStatus.FAILED
    assert record.step_results[0]["error"]["code"] == "api_credential_origin_mismatch"
    assert calls == []


def test_malformed_api_credential_configuration_fails_safely_before_request(monkeypatch):
    secret = "private-placeholder-token"
    monkeypatch.setenv("FLOWDESK_API_CREDENTIALS_JSON", '{"billing":{"origin":"https://api.example.test","token":"' + secret + '"},}')
    calls = []
    record = _run(APIRequestStepHandler(lambda *args: (calls.append(args) or (200, "application/json", b"{}"))),
                  _step({"endpoint": "https://api.example.test", "credential_id": "billing"}))
    assert record.status is ExecutionStatus.FAILED
    assert record.step_results[0]["error"]["code"] == "api_credentials_invalid"
    assert record.step_results[0]["error"]["message"] == "Server API credential configuration is invalid."
    assert secret not in json.dumps(record.step_results)
    assert calls == []


def test_private_ip_destination_is_blocked_before_request():
    called = False

    def requester(*_):
        nonlocal called
        called = True
        return 200, "application/json", b"{}"

    handler = APIRequestStepHandler(requester)
    record = _run(handler, _step({"endpoint": "https://127.0.0.1"}))
    assert record.status is ExecutionStatus.FAILED
    assert record.step_results[0]["error"]["code"] == "prohibited_api_destination"
    assert called is False


def test_dns_resolution_rejects_a_host_with_any_private_address(monkeypatch):
    monkeypatch.setattr(api_request.socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (2, 1, 6, "", ("8.8.8.8", 443)),
        (2, 1, 6, "", ("10.0.0.5", 443)),
    ])
    called = False

    def requester(*_):
        nonlocal called
        called = True
        return 200, "application/json", b"{}"

    record = _run(APIRequestStepHandler(requester), _step())
    assert record.status is ExecutionStatus.FAILED
    assert record.step_results[0]["error"]["code"] == "prohibited_api_destination"
    assert called is False


def test_hostname_with_only_public_addresses_reaches_mock_with_checked_ip(monkeypatch):
    monkeypatch.setattr(api_request.socket, "getaddrinfo", lambda _host, port, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", port)),
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", port)),
    ])
    calls = []
    handler = APIRequestStepHandler(lambda *args: (calls.append(args) or (200, "application/json", b"{}")))
    record = _run(handler, _step())
    assert record.status is ExecutionStatus.SUCCEEDED
    assert calls and calls[0][-1] == "8.8.8.8"


def test_dns_resolution_has_a_bounded_timeout_and_safe_error(monkeypatch):
    monkeypatch.setattr(api_request, "DNS_RESOLUTION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(api_request.socket, "getaddrinfo", lambda *_args, **_kwargs: (time.sleep(0.1) or []))
    started = time.monotonic()
    with pytest.raises(APIRequestFailure) as error:
        api_request._resolve_public_ip("slow.example.test", 443)
    assert error.value.safe_error_code == "api_dns_timeout"
    assert time.monotonic() - started < 0.08


def test_pinned_transport_connects_to_checked_ip_and_keeps_tls_hostname(monkeypatch):
    connections = []

    class SocketStub:
        def close(self):
            pass

    class TLSContextStub:
        def wrap_socket(self, raw, server_hostname):
            connections.append((raw, server_hostname))
            return raw

    raw = SocketStub()
    monkeypatch.setattr(api_request.socket, "create_connection", lambda address, timeout: (connections.append((address, timeout)) or raw))
    connection = api_request._PinnedHTTPSConnection("api.example.test", "8.8.8.8", 5)
    assert connection._context.check_hostname is True
    connection._context = TLSContextStub()
    connection.connect()
    assert connections == [(("8.8.8.8", 443), 5), (raw, "api.example.test")]
    connection.close()


def test_api_step_output_can_feed_a_later_api_steps_query_parameters():
    seen_params = []

    def requester(endpoint, params, *_):
        seen_params.append((endpoint, params))
        body = b'{"account":"acct-7"}' if len(seen_params) == 1 else b'{"ok":true}'
        return 200, "application/json", body

    definition = {
        "workflow_id": "api-chain", "version": "1", "steps": [
            {"id": "first", "type": "api_request", "config": {"endpoint": "https://api.example.test/first"}},
            {"id": "second", "type": "api_request", "config": {"endpoint": "https://api.example.test/second"},
             "inputs": {"query_params": {"from_step": "first"}}},
        ],
    }
    handlers = {"api_request": APIRequestStepHandler(requester)}
    runner = WorkflowRunner()
    record = runner.execute(
        ConfiguredAutomationWorkflow(json.dumps(definition), AutomationEngine(handlers)),
        AutomationRunInput(BusinessFileInput("unused.csv", b"unused")),
    )
    assert record.status is ExecutionStatus.SUCCEEDED
    assert seen_params[1][1] == {"account": "acct-7"}


def test_api_step_is_registered_and_persisted_history_omits_response_body(client, monkeypatch):
    import app.api.routes as routes

    assert "api_request" in routes._automation_handlers()
    secret_response = {"account": "private-result-value"}
    monkeypatch.setenv("FLOWDESK_API_CREDENTIALS_JSON", json.dumps({"test-api": {"origin": "https://api.example.test", "token": "private-token-value"}}))
    handler = APIRequestStepHandler(lambda *_: (200, "application/json", json.dumps(secret_response).encode()))
    monkeypatch.setattr(routes, "_automation_handlers", lambda: {"api_request": handler})
    configured = _step({
        "endpoint": "https://api.example.test/private-endpoint",
        "query_params": {"customer": "private-query-value"},
        "credential_id": "test-api",
    })
    response = client.post(
        "/automation/execute",
        files={"file": ("unused.csv", "not read by this step", "text/csv")},
        data={"workflow": json.dumps(configured)},
    )
    assert response.status_code == 201
    result = response.json()
    assert result["output"]["outputs"]["fetch"]["response"] == secret_response

    persisted = get_automation_execution(result["execution_id"])
    assert persisted is not None
    serialized = json.dumps({"output": persisted["output"], "steps": persisted["steps"]})
    for private_value in (
        "private-result-value", "private-endpoint", "private-query-value", "private-token-value",
        "FLOWDESK_API_CREDENTIALS_JSON", "api.example.test",
    ):
        assert private_value not in serialized
    assert "response_bytes" in serialized
    history_response = client.get(f"/executions/{result['execution_id']}")
    assert history_response.status_code == 200
    history_json = json.dumps(history_response.json())
    assert "private-result-value" not in history_json
    assert "private-query-value" not in history_json
    assert "private-endpoint" not in history_json
    assert "private-token-value" not in history_json
    assert "response_bytes" in history_json


def test_failed_api_step_returns_safe_failure_details_and_is_persisted(client, monkeypatch):
    import app.api.routes as routes

    monkeypatch.setattr(
        routes, "_automation_handlers",
        lambda: {"api_request": APIRequestStepHandler(lambda *_: (_ for _ in ()).throw(TimeoutError("private detail")))},
    )
    response = client.post(
        "/automation/execute",
        files={"file": ("unused.csv", "unused", "text/csv")},
        data={"workflow": json.dumps(_step())},
    )
    assert response.status_code == 500
    result = response.json()
    assert result["status"] == "failed"
    assert result["steps"][0]["error"]["code"] == "api_timeout"
    assert result["steps"][0]["error"]["message"] == "External API request timed out."
    persisted = get_automation_execution(result["execution_id"])
    assert persisted and persisted["status"] == "failed"
