"""Bound total HTTP request bodies before the application parses forms."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.responses import JSONResponse

ASGIMessage = dict[str, object]
ASGIReceive = Callable[[], Awaitable[ASGIMessage]]
ASGISend = Callable[[ASGIMessage], Awaitable[None]]
ASGIApp = Callable[[dict[str, object], ASGIReceive, ASGISend], Awaitable[None]]
MAX_REQUEST_BODY_BYTES = 6 * 1024 * 1024


class RequestBodyLimitMiddleware:
    """Reject oversize requests while buffering no more than the fixed limit."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, object], receive: ASGIReceive, send: ASGISend) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers", [])
        lengths = [value for key, value in headers if key.lower() == b"content-length"]
        if len(lengths) > 1:
            await JSONResponse({"detail": "Invalid Content-Length header."}, status_code=400)(scope, receive, send)
            return
        if lengths:
            raw_length = lengths[0]
            if not raw_length.isdigit():
                await JSONResponse({"detail": "Invalid Content-Length header."}, status_code=400)(scope, receive, send)
                return
            normalized_length = raw_length.lstrip(b"0") or b"0"
            maximum_length = str(MAX_REQUEST_BODY_BYTES).encode("ascii")
            if len(normalized_length) > len(maximum_length) or (
                len(normalized_length) == len(maximum_length) and normalized_length > maximum_length
            ):
                await self._too_large(scope, receive, send)
                return

        body = bytearray()
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if not isinstance(chunk, bytes):
                await JSONResponse({"detail": "Invalid request body."}, status_code=400)(scope, receive, send)
                return
            if len(body) + len(chunk) > MAX_REQUEST_BODY_BYTES:
                await self._too_large(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        offset = 0
        replayed_empty_body = False

        async def replay_receive() -> ASGIMessage:
            nonlocal offset, replayed_empty_body
            if offset < len(body):
                end = min(offset + 64 * 1024, len(body))
                chunk = bytes(body[offset:end])
                offset = end
                return {"type": "http.request", "body": chunk, "more_body": offset < len(body)}
            if not replayed_empty_body:
                replayed_empty_body = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _too_large(scope: dict[str, object], receive: ASGIReceive, send: ASGISend) -> None:
        await JSONResponse(
            {"detail": "Request body exceeds the 6 MiB total upload limit."}, status_code=413
        )(scope, receive, send)
