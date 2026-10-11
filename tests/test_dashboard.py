import asyncio
import sqlite3

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.storage.repository import create_job


def test_jobs_list_is_empty_with_zero_total(client: TestClient) -> None:
    response = client.get("/jobs")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 10}


def test_health_endpoints_and_remote_access_boundary(client: TestClient, monkeypatch):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}

    async def remote_get(url, headers=None):
        transport = httpx.ASGITransport(app=app, client=("198.51.100.20", 12345))
        async with httpx.AsyncClient(transport=transport, base_url=url) as remote:
            return await remote.get("/healthz", headers=headers)

    monkeypatch.delenv("FLOWDESK_REMOTE_ACCESS_TOKEN", raising=False)
    denied = asyncio.run(remote_get("https://flowdesk.example"))
    assert denied.status_code == 403

    token = "a" * 48
    monkeypatch.setenv("FLOWDESK_REMOTE_ACCESS_TOKEN", token)
    denied_without_token = asyncio.run(remote_get("https://flowdesk.example"))
    denied_over_http = asyncio.run(remote_get("http://flowdesk.example", {"Authorization": f"Bearer {token}"}))
    allowed = asyncio.run(remote_get("https://flowdesk.example", {"Authorization": f"Bearer {token}"}))
    assert denied_without_token.status_code == 403
    assert denied_over_http.status_code == 403
    assert allowed.status_code == 200

    async def proxied_get(url, headers=None):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.2", 12345))
        async with httpx.AsyncClient(transport=transport, base_url=url) as remote:
            return await remote.get("/healthz", headers=headers)

    monkeypatch.setenv("FLOWDESK_TRUSTED_PROXY_CIDRS", "127.0.0.0/8")
    monkeypatch.setenv("FLOWDESK_REMOTE_ACCESS_TOKEN", token)
    spoofed_forwarding_from_remote = asyncio.run(remote_get("https://flowdesk.example", {
        "X-Forwarded-For": "127.0.0.1", "X-Forwarded-Proto": "https",
    }))
    untrusted_forwarded_client = asyncio.run(proxied_get("https://flowdesk.example", {
        "X-Forwarded-For": "198.51.100.20", "X-Forwarded-Proto": "https",
    }))
    forwarded_without_tls = asyncio.run(proxied_get("http://flowdesk.example", {
        "X-Forwarded-For": "198.51.100.20", "X-Forwarded-Proto": "http",
        "Authorization": f"Bearer {token}",
    }))
    forwarded_with_tls = asyncio.run(proxied_get("http://flowdesk.example", {
        "X-Forwarded-For": "198.51.100.20", "X-Forwarded-Proto": "https",
        "Authorization": f"Bearer {token}",
    }))
    assert spoofed_forwarding_from_remote.status_code == 403
    assert untrusted_forwarded_client.status_code == 403
    assert forwarded_without_tls.status_code == 403
    assert forwarded_with_tls.status_code == 200

    # Both address families are accepted by the peer parser; IPv4 must not
    # attempt the IPv6-only ipv4_mapped property.
    ipv4_loopback_transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async def ipv4_loopback_get():
        async with httpx.AsyncClient(transport=ipv4_loopback_transport, base_url="http://flowdesk.local") as local_client:
            return await local_client.get("/healthz")
    assert asyncio.run(ipv4_loopback_get()).status_code == 200

    ipv6_loopback_transport = httpx.ASGITransport(app=app, client=("::1", 12345))
    async def ipv6_loopback_get():
        async with httpx.AsyncClient(transport=ipv6_loopback_transport, base_url="http://flowdesk.local") as local_client:
            return await local_client.get("/healthz")
    assert asyncio.run(ipv6_loopback_get()).status_code == 200


def test_jobs_list_returns_newest_metadata_and_unlimited_total(
    client: TestClient, tmp_path
) -> None:
    db_path = tmp_path / "test.sqlite3"
    created = [
        create_job(f"batch-{index}.csv", [{"value": str(index)}], str(db_path))
        for index in range(1, 4)
    ]

    connection = sqlite3.connect(db_path)
    try:
        for index, job in enumerate(created, start=1):
            connection.execute(
                "UPDATE jobs SET created_at = ? WHERE id = ?",
                (f"2026-01-0{index}T00:00:00+00:00", job["id"]),
            )
        connection.commit()
    finally:
        connection.close()

    response = client.get("/jobs?limit=2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert payload["limit"] == 2
    assert [job["id"] for job in payload["items"]] == [created[2]["id"], created[1]["id"]]
    assert [job["filename"] for job in payload["items"]] == ["batch-3.csv", "batch-2.csv"]
    assert payload["items"][0]["row_count"] == 1


def test_jobs_list_rejects_out_of_bounds_limit(client: TestClient) -> None:
    assert client.get("/jobs?limit=0").status_code == 422
    assert client.get("/jobs?limit=101").status_code == 422


def test_background_queue_and_schedule_api_validation_and_lifecycle(client: TestClient):
    workflow = '{"workflow_id":"business-file-processing","version":"1","steps":[{"id":"process","type":"business_file"},{"id":"summary","type":"business_file_summary","inputs":{"result":{"from_step":"process"}}}]}'
    queued = client.post("/automation/queue", files={"file": ("batch.csv", b"name\nsynthetic\n", "text/csv")}, data={"workflow": workflow})
    assert queued.status_code == 202
    invalid_queue = client.post(
        "/automation/queue",
        files={"file": ("batch.csv", b"name\nsynthetic\n", "text/csv")},
        data={"workflow": '{"workflow_id":"bad","version":"1","steps":[{"id":"unknown","type":"not_registered"}]}'},
    )
    assert invalid_queue.status_code == 422
    assert "unsupported type" in invalid_queue.json()["detail"]
    execution_id = queued.json()["execution_id"]
    assert client.get("/automation/queue?limit=1").json()["items"][0]["execution_id"] == execution_id
    assert client.get(f"/automation/queue/{execution_id}").json()["state"] == "queued"
    assert client.get("/automation/queue/not-found").status_code == 404
    invalid = client.post("/schedules", files={"file": ("batch.csv", b"name\nsynthetic\n", "text/csv")}, data={"workflow": workflow, "name": "bad timezone", "schedule_type": "once", "time_zone": "Unknown/Zone", "run_at": "2030-01-01T10:00"})
    assert invalid.status_code == 422
    scheduled = client.post("/schedules", files={"file": ("batch.csv", b"name\nsynthetic\n", "text/csv")}, data={"workflow": workflow, "name": "weekly run", "schedule_type": "weekly", "time_zone": "UTC", "local_time": "09:30", "weekday": "1"})
    assert scheduled.status_code == 201
    schedule_id = scheduled.json()["schedule_id"]
    assert client.get("/schedules").json()["items"][0]["schedule_id"] == schedule_id
    assert client.post(f"/schedules/{schedule_id}/disable").json()["enabled"] == 0
    assert client.post(f"/schedules/{schedule_id}/enable").json()["enabled"] == 1
    assert client.delete(f"/schedules/{schedule_id}").status_code == 204


def test_dashboard_and_static_assets_are_served_without_shadowing_api_docs(
    client: TestClient,
) -> None:
    dashboard = client.get("/")
    stylesheet = client.get("/static/styles.css")
    script = client.get("/static/app.js")

    assert dashboard.status_code == 200
    assert "text/html" in dashboard.headers["content-type"]
    assert "<title>Overview · Flowdesk</title>" in dashboard.text
    assert 'aria-label="Flowdesk home"' in dashboard.text
    assert "<strong>Flowdesk</strong>" in dashboard.text
    assert '<footer class="page-footer"><span>Flowdesk</span>' in dashboard.text
    assert "/static/styles.css" in dashboard.text
    assert "/static/app.js" in dashboard.text
    assert 'data-scroll="history-panel" aria-label="CSV imports"' in dashboard.text
    workspace_navigation = dashboard.text.split('<nav class="navigation" aria-label="Workspace navigation">', 1)[1].split("</nav>", 1)[0]
    planned_navigation = dashboard.text.split('<nav class="navigation" aria-label="Planned features">', 1)[1].split("</nav>", 1)[0]
    assert 'data-scroll="schedule-panel"' in workspace_navigation
    assert 'data-scroll="schedule-panel"' not in planned_navigation
    assert 'id="history-panel"' in dashboard.text
    assert '<nav class="breadcrumb" aria-label="Breadcrumb"><a href="/"' in dashboard.text
    assert 'id="breadcrumb-current" aria-current="page"' in dashboard.text
    assert 'Scheduled workflows</strong><small>Available now</small>' in dashboard.text
    assert 'aria-label="Choose a CSV or XLSX file to process"' in dashboard.text
    assert 'id="rules-json" name="rules"' in dashboard.text
    assert "Optional JSON ruleset" in dashboard.text
    assert 'id="saved-ruleset-select"' in dashboard.text
    assert 'id="ruleset-name"' in dashboard.text
    assert 'id="save-ruleset-button"' in dashboard.text
    assert stylesheet.status_code == 200
    assert "@media" in stylesheet.text
    assert "color-scheme: dark" in stylesheet.text
    assert "prefers-reduced-motion: reduce" in stylesheet.text
    assert "@media (max-width: 820px)" in stylesheet.text
    assert script.status_code == 200
    assert 'function pageScrollBehavior()' in script.text
    assert 'function setActiveNavigation(sectionId)' in script.text
    assert 'new IntersectionObserver' in script.text
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in script.text
    assert 'const breadcrumbCurrent = document.querySelector("#breadcrumb-current")' in script.text
    assert 'if (label && breadcrumbCurrent) breadcrumbCurrent.textContent = label' in script.text
    assert 'fetch(`/jobs?limit=${RECENT_LIMIT}`' in script.text
    assert 'request.open("POST", "/workflows/business-file")' in script.text
    assert 'formData.append("rules", rulesText)' in script.text
    assert 'JSON.parse(rulesText)' in script.text
    assert 'fetch("/rulesets"' in script.text
    assert 'method: "POST"' in script.text
    assert 'fetch(`/rulesets/${encodeURIComponent(rulesetId)}`' in script.text
    assert 'rulesInput.value = ruleset.configuration' in script.text
    assert 'rulesInput.readOnly = true' in script.text
    assert 'id="automation-form"' in dashboard.text
    assert 'id="automation-workflow"' in dashboard.text
    assert 'id="automation-file"' in dashboard.text
    assert 'id="automation-ruleset"' in dashboard.text
    assert 'id="automation-result"' in dashboard.text
    assert 'id="transform-trim"' in dashboard.text
    assert 'id="transform-remove-blank"' in dashboard.text
    assert 'id="add-column-map"' in dashboard.text
    assert 'id="add-calculation"' in dashboard.text
    assert 'id="api-request-enabled"' in dashboard.text
    assert 'id="api-request-endpoint"' in dashboard.text
    assert 'id="api-request-credential-id"' in dashboard.text
    assert 'id="api-request-timeout"' in dashboard.text
    assert 'id="api-request-limit"' in dashboard.text
    assert 'id="add-api-query"' in dashboard.text
    assert 'workflow_id: "business-file-processing"' in script.text
    assert 'type: "business_file_summary"' in script.text
    assert 'formData.append("workflow", JSON.stringify(buildAutomationWorkflow()))' in script.text
    assert 'function buildAutomationWorkflow()' in script.text
    assert 'appendTransform("clean-data", "clean_data", { trim_strings: trimStrings, remove_blank_rows: removeBlankRows })' in script.text
    assert 'appendTransform("map-columns", "map_columns", { mapping })' in script.text
    assert 'appendTransform(`calculate-${calculationNumber}`, "calculate_field", {' in script.text
    assert 'operation: row.querySelector(".calculation-operation").value' in script.text
    assert 'step.inputs = { previous: { from_step: previousTransform } }' in script.text
    assert 'businessFile.inputs = { transformations: { from_step: previousTransform } }' in script.text
    assert 'if (apiRequestEnabled.checked)' in script.text
    assert 'type: "api_request"' in script.text
    assert 'query_params: queryParams' in script.text
    assert 'config.credential_id = credentialId' in script.text
    assert 'apiRequestCredentialId.value.trim()' in script.text
    assert 'token_env' not in script.text
    assert 'new URL(endpoint)' in script.text
    assert 'function addApiQueryRow()' in script.text
    ruleset_submission = script.text.split("async function buildAutomationFormData(file)", 1)[1].split(
        "function labeledInput", 1
    )[0]
    assert 'fetch(`/rulesets/${encodeURIComponent(automationRuleset.value)}`' in ruleset_submission
    assert 'formData.append("rules", saved.configuration)' in ruleset_submission
    assert 'function submitAutomation(formData, endpoint = "/automation/execute")' in script.text
    assert 'request.open("POST", endpoint)' in script.text
    assert 'submitAutomation(formData, background ? "/automation/queue" : "/automation/execute")' in script.text
    assert 'request.status === 201 || request.status === 202' in script.text
    assert 'fetch(`/automation/queue/${encodeURIComponent(executionId)}`' in script.text
    assert 'fetch("/schedules"' in script.text
    assert 'fetch(endpoint, { method, headers:' in script.text
    assert 'id="automation-background"' in dashboard.text
    assert 'id="schedule-form"' in dashboard.text
    assert 'data-scroll="schedule-panel"' in dashboard.text
    assert "renderAutomationExecution(payload)" in script.text
    assert "step.status" in script.text
    assert "function validateSelectedFile(file)" in script.text
    assert r'/\.(csv|xlsx)$/i.test(file.name)' in script.text
    assert "file.size > MAX_FILE_BYTES" in script.text
    assert "function formatFileSize(bytes)" in script.text
    assert 'dropZone.addEventListener("drop"' in script.text
    assert "if (isUploading) return;" in script.text
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
