"""FastAPI application entry point."""

from contextlib import asynccontextmanager
import hmac
import ipaddress
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.storage.database import connect, initialize_database

WEB_DIR = Path(__file__).resolve().parent / "web"


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


@app.middleware("http")
async def local_or_authenticated_access(request: Request, call_next):
    """Keep the app local by default; require a TLS bearer token for remote access."""
    peer = request.client.host if request.client else ""
    local = peer == "testclient"
    address = None
    try:
        address = ipaddress.ip_address(peer)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        local = local or address.is_loopback
    except ValueError:
        pass
    trusted_proxy = False
    if address is not None:
        for configured in os.getenv("FLOWDESK_TRUSTED_PROXY_CIDRS", "").split(","):
            try:
                if address in ipaddress.ip_network(configured.strip(), strict=False):
                    trusted_proxy = True
                    break
            except ValueError:
                continue
    scheme = request.url.scheme
    forwarded_context = (
        "x-forwarded-for" in request.headers or "x-forwarded-proto" in request.headers
    )
    if trusted_proxy and forwarded_context:
        forwarded_for = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        try:
            address = ipaddress.ip_address(forwarded_for)
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
                address = address.ipv4_mapped
            local = address.is_loopback
        except ValueError:
            # Forwarded context from a trusted proxy must include a valid client address.
            local = False
            address = None
        forwarded_scheme = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
        scheme = forwarded_scheme if forwarded_scheme in {"http", "https"} else "http"
    if not local and address is not None:
        for configured in os.getenv("FLOWDESK_TRUSTED_CLIENT_CIDRS", "").split(","):
            try:
                if address in ipaddress.ip_network(configured.strip(), strict=False):
                    local = True
                    break
            except ValueError:
                continue
    if not local:
        token = os.getenv("FLOWDESK_REMOTE_ACCESS_TOKEN", "")
        supplied = request.headers.get("authorization", "")
        if (
            len(token) < 32 or scheme != "https"
            or not hmac.compare_digest(supplied, f"Bearer {token}")
        ):
            return JSONResponse(status_code=403, content={"detail": "Remote access is not authorized."})
    return await call_next(request)


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
