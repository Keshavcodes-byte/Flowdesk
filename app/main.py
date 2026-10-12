"""FastAPI application entry point."""

from contextlib import asynccontextmanager
import hmac
import ipaddress
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.request_limits import RequestBodyLimitMiddleware
from app.storage.database import connect, initialize_database

WEB_DIR = Path(__file__).resolve().parent / "web"
_DEFAULT_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1", "testserver"}
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(
    title="Automation AI Platform",
    description="CSV-to-SQLite import workflow",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(router)


def _headers(scope: dict[str, object], name: bytes) -> list[str]:
    return [value.decode("latin-1") for key, value in scope.get("headers", []) if key.lower() == name]


def _normalized_host(value: str) -> tuple[str, int | None] | None:
    if not value or value != value.strip() or any(char in value for char in "\\/;?#@,\r\n\t"):
        return None
    try:
        parsed = urlsplit("//" + value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not hostname or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        return None
    hostname = hostname.rstrip(".").lower()
    try:
        hostname = ipaddress.ip_address(hostname).compressed.lower()
    except ValueError:
        try:
            hostname = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return None
        if len(hostname) > 253 or any(not _HOST_LABEL.fullmatch(label) for label in hostname.split(".")):
            return None
    if port is not None and not 1 <= port <= 65535:
        return None
    return hostname, port


def _allowed_hosts() -> set[str] | None:
    allowed = set(_DEFAULT_ALLOWED_HOSTS)
    configured = os.getenv("FLOWDESK_ALLOWED_HOSTS", "")
    if not configured.strip():
        return allowed
    for item in configured.split(","):
        parsed = _normalized_host(item.strip())
        if parsed is None or parsed[1] is not None:
            return None
        allowed.add(parsed[0])
    return allowed


def _origin(value: str) -> tuple[str, str, int] | None:
    if not value or value != value.strip() or value.lower() == "null":
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        return None
    if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return None
    normalized = _normalized_host(parsed.netloc)
    if normalized is None:
        return None
    host, normalized_port = normalized
    scheme = parsed.scheme.lower()
    return scheme, host, normalized_port or (443 if scheme == "https" else 80)


def _peer_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            return address.ipv4_mapped
        return address
    except ValueError:
        return None


def _in_configured_networks(address: ipaddress.IPv4Address | ipaddress.IPv6Address | None, variable: str) -> bool:
    if address is None:
        return False
    for configured in os.getenv(variable, "").split(","):
        try:
            if address in ipaddress.ip_network(configured.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


@app.middleware("http")
async def local_or_authenticated_access(request: Request, call_next):
    """Validate Host/origin and preserve local or authenticated remote access."""
    host_values = _headers(request.scope, b"host")
    parsed_host = _normalized_host(host_values[0]) if len(host_values) == 1 else None
    allowed_hosts = _allowed_hosts()
    if parsed_host is None or allowed_hosts is None or parsed_host[0] not in allowed_hosts:
        return JSONResponse(status_code=400, content={"detail": "Invalid or unapproved Host header."})

    peer = request.client.host if request.client else ""
    address = _peer_address(peer)
    local = peer == "testclient" or bool(address and address.is_loopback)
    trusted_proxy = _in_configured_networks(address, "FLOWDESK_TRUSTED_PROXY_CIDRS")
    scheme = request.url.scheme
    forwarded_context = "x-forwarded-for" in request.headers or "x-forwarded-proto" in request.headers
    if trusted_proxy and forwarded_context:
        forwarded_for = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        address = _peer_address(forwarded_for)
        local = bool(address and address.is_loopback)
        forwarded_scheme = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
        scheme = forwarded_scheme if forwarded_scheme in {"http", "https"} else "http"
    if not local and _in_configured_networks(address, "FLOWDESK_TRUSTED_CLIENT_CIDRS"):
        local = True

    if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        origins = _headers(request.scope, b"origin")
        fetch_sites = _headers(request.scope, b"sec-fetch-site")
        if len(origins) > 1 or len(fetch_sites) > 1:
            return JSONResponse(status_code=403, content={"detail": "Cross-origin request rejected."})
        if fetch_sites and fetch_sites[0].lower() != "same-origin":
            return JSONResponse(status_code=403, content={"detail": "Cross-origin request rejected."})
        if origins:
            supplied_origin = _origin(origins[0])
            expected_port = parsed_host[1] or (443 if scheme == "https" else 80)
            expected_origin = (scheme, parsed_host[0], expected_port)
            if supplied_origin is None or supplied_origin != expected_origin:
                return JSONResponse(status_code=403, content={"detail": "Cross-origin request rejected."})

    if not local:
        token = os.getenv("FLOWDESK_REMOTE_ACCESS_TOKEN", "")
        supplied = request.headers.get("authorization", "")
        if len(token) < 32 or scheme != "https" or not hmac.compare_digest(supplied, f"Bearer {token}"):
            return JSONResponse(status_code=403, content={"detail": "Remote access is not authorized."})
    return await call_next(request)


# This wraps the HTTP middleware and router, so multipart parsing happens only
# after the bounded body has been received and checked.
app.add_middleware(RequestBodyLimitMiddleware)


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    """Serve the browser dashboard without catching API or documentation routes."""
    return FileResponse(WEB_DIR / "index.html")


@app.get("/healthz", include_in_schema=False)
def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
def readiness() -> JSONResponse:
    try:
        connection = connect()
        try:
            connection.execute("SELECT 1").fetchone()
        finally:
            connection.close()
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return JSONResponse(status_code=200, content={"status": "ready"})


app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
