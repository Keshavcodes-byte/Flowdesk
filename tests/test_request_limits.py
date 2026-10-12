import asyncio

from fastapi.testclient import TestClient

from app.request_limits import MAX_REQUEST_BODY_BYTES, RequestBodyLimitMiddleware


def run_asgi_request(content_length=None, chunks=()):
    messages = iter(
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    )
    if not chunks:
        messages = iter([{"type": "http.request", "body": b"", "more_body": False}])
    received = []
    sent = []
    downstream_called = False

    async def receive():
        received.append(True)
        return next(messages)

    async def send(message):
        sent.append(message)

    async def downstream(scope, receive, send):
        nonlocal downstream_called
        downstream_called = True
        while (await receive()).get("more_body"):
            pass
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    headers = [] if content_length is None else [(b"content-length", content_length if isinstance(content_length, bytes) else str(content_length).encode("ascii"))]
    scope = {
        "type": "http", "method": "POST", "path": "/upload", "query_string": b"",
        "headers": headers, "scheme": "http", "server": ("testserver", 80),
        "client": ("127.0.0.1", 1234), "http_version": "1.1",
    }
    asyncio.run(RequestBodyLimitMiddleware(downstream)(scope, receive, send))
    return sent, received, downstream_called


def test_content_length_limit_rejects_before_application_body_parsing():
    sent, received, called = run_asgi_request(content_length=MAX_REQUEST_BODY_BYTES + 1)

    assert sent[0]["status"] == 413
    assert b"6 MiB" in sent[1]["body"]
    assert received == []
    assert called is False


def test_chunked_body_limit_stops_at_first_excess_chunk_without_unbounded_buffering():
    sent, received, called = run_asgi_request(
        chunks=[b"x" * MAX_REQUEST_BODY_BYTES, b"y", b"unread"],
    )

    assert sent[0]["status"] == 413
    assert len(received) == 2
    assert called is False


def test_content_length_limit_is_enforced_by_the_mounted_app(client: TestClient):
    response = client.post(
        "/rulesets",
        content=b"{}",
        headers={"Content-Length": str(MAX_REQUEST_BODY_BYTES + 1)},
    )

    assert response.status_code == 413
    assert "6 MiB" in response.json()["detail"]


def test_extremely_long_content_length_is_rejected_without_integer_conversion():
    sent, received, called = run_asgi_request(content_length=b"9" * 5000)

    assert sent[0]["status"] == 413
    assert received == []
    assert called is False
