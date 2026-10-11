"""Safe, bounded HTTPS GET workflow step."""

from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import math
import os
import re
import socket
import ssl
import time
from queue import Queue
from threading import BoundedSemaphore, Thread
from collections.abc import Mapping
from typing import Callable
from urllib.parse import urlencode, urlsplit

from app.engine.automation import WorkflowDefinitionError, WorkflowStep
from app.engine.contracts import WorkflowInputError

logger = logging.getLogger("app.engine")
MAX_API_RESPONSE_BYTES = 1024 * 1024
DEFAULT_API_RESPONSE_BYTES = 256 * 1024
MAX_API_TIMEOUT_SECONDS = 20.0
DNS_RESOLUTION_TIMEOUT_SECONDS = 3.0
MAX_PENDING_DNS_LOOKUPS = 4
_DNS_LOOKUP_SLOTS = BoundedSemaphore(MAX_PENDING_DNS_LOOKUPS)
_CREDENTIAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_HOST = re.compile(r"^[a-zA-Z0-9.-]+$")
API_CREDENTIALS_ENV = "FLOWDESK_API_CREDENTIALS_JSON"
MAX_API_CREDENTIALS_JSON_BYTES = 64 * 1024
MAX_API_CREDENTIALS = 32
MAX_API_CREDENTIAL_BYTES = 4096


class APIRequestFailure(RuntimeError):
    """An external request failed with a deliberately safe public summary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.safe_error_code = code
        self.safe_error_message = message


def _validate_endpoint(endpoint: object) -> tuple[str, str, int, str]:
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 2048:
        raise ValueError("endpoint must be an HTTPS URL no longer than 2048 characters.")
    try:
        parts = urlsplit(endpoint)
        port = parts.port if parts.port is not None else 443
    except ValueError as error:
        raise ValueError("endpoint must be a valid HTTPS URL.") from error
    hostname = parts.hostname
    if (
        parts.scheme.lower() != "https" or not hostname or parts.username is not None
        or parts.password is not None or parts.fragment or parts.query
        or "?" in endpoint or "#" in endpoint
        or port != 443 or not _SAFE_HOST.fullmatch(hostname)
        or hostname.lower().endswith((".localhost", ".local", ".internal"))
    ):
        raise ValueError("endpoint must be an external HTTPS URL on port 443 without credentials, query, or fragment.")
    return endpoint, hostname, port, parts.path or "/"


def _validate_query_params(value: object, label: str = "query_params") -> dict[str, object]:
    if not isinstance(value, dict) or len(value) > 50:
        raise ValueError(f"{label} must be an object with at most 50 parameters.")
    result: dict[str, object] = {}
    pair_count = 0
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 128:
            raise ValueError(f"{label} keys must contain 1 to 128 characters.")
        items = item if isinstance(item, list) else [item]
        if not items or len(items) > 20:
            raise ValueError(f"{label} values must be scalars or arrays of up to 20 scalars.")
        for scalar in items:
            if not (isinstance(scalar, str) and len(scalar) <= 2048) and type(scalar) not in {int, float, bool}:
                raise ValueError(f"{label} values must be strings, finite numbers, booleans, or arrays of them.")
            if isinstance(scalar, float) and not math.isfinite(scalar):
                raise ValueError(f"{label} numbers must be finite.")
        pair_count += len(items)
        result[key] = list(items) if isinstance(item, list) else item
    if pair_count > 100:
        raise ValueError(f"{label} cannot expand to more than 100 query parameters.")
    return result


def _strict_credential_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate credential configuration property")
        result[key] = value
    return result


def _normalized_origin(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("origin must be an HTTPS origin.")
    _, hostname, _, _ = _validate_endpoint(value)
    parts = urlsplit(value)
    if parts.path not in {"", "/"}:
        raise ValueError("origin must not include a path.")
    normalized_host = hostname.lower().rstrip(".")
    if not normalized_host:
        raise ValueError("origin hostname is invalid.")
    return "https://" + normalized_host


def _endpoint_origin(endpoint: str) -> str:
    _, hostname, _, _ = _validate_endpoint(endpoint)
    return "https://" + hostname.lower().rstrip(".")


def _load_api_credentials() -> dict[str, dict[str, str]]:
    raw = os.environ.get(API_CREDENTIALS_ENV, "")
    if not raw.strip():
        return {}
    try:
        if len(raw.encode("utf-8")) > MAX_API_CREDENTIALS_JSON_BYTES:
            raise ValueError("configuration exceeds size limit")
        document = json.loads(raw, object_pairs_hook=_strict_credential_object, parse_constant=_reject_json_constant)
        if not isinstance(document, dict) or len(document) > MAX_API_CREDENTIALS:
            raise ValueError("configuration must be an object with at most 32 credentials")
        credentials: dict[str, dict[str, str]] = {}
        for credential_id, item in document.items():
            if not isinstance(credential_id, str) or not _CREDENTIAL_ID.fullmatch(credential_id):
                raise ValueError("invalid credential id")
            if not isinstance(item, dict) or set(item) != {"origin", "token"}:
                raise ValueError("credential entries must contain only origin and token")
            origin = _normalized_origin(item["origin"])
            token = item["token"]
            if (not isinstance(token, str) or not 1 <= len(token) <= MAX_API_CREDENTIAL_BYTES
                    or not token.isascii() or any(ord(character) <= 32 or ord(character) == 127 for character in token)):
                raise ValueError("credential token is invalid")
            credentials[credential_id] = {"origin": origin, "token": token}
        return credentials
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise APIRequestFailure("api_credentials_invalid", "Server API credential configuration is invalid.") from None


def _validate_config(config: Mapping[str, object]) -> dict[str, object]:
    allowed = {"endpoint", "query_params", "timeout_seconds", "max_response_bytes", "credential_id"}
    if set(config) - allowed:
        raise ValueError("api_request config contains unsupported fields.")
    endpoint = config.get("endpoint")
    _validate_endpoint(endpoint)
    params = _validate_query_params(config.get("query_params", {}))
    timeout = config.get("timeout_seconds", 10)
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0.1 <= timeout <= MAX_API_TIMEOUT_SECONDS:
        raise ValueError("timeout_seconds must be between 0.1 and 20 seconds.")
    limit = config.get("max_response_bytes", DEFAULT_API_RESPONSE_BYTES)
    if type(limit) is not int or not 1024 <= limit <= MAX_API_RESPONSE_BYTES:
        raise ValueError("max_response_bytes must be between 1024 and 1048576.")
    credential_id = config.get("credential_id")
    if credential_id is not None and (not isinstance(credential_id, str) or not _CREDENTIAL_ID.fullmatch(credential_id)):
        raise ValueError("credential_id must be a valid 1 to 64 character identifier.")
    return {"endpoint": endpoint, "query_params": params, "timeout_seconds": float(timeout),
            "max_response_bytes": limit, "credential_id": credential_id}


def _resolve_public_ip(hostname: str, port: int) -> str:
    try:
        literal = ipaddress.ip_address(hostname)
        candidates = [literal]
    except ValueError:
        if not _DNS_LOOKUP_SLOTS.acquire(blocking=False):
            raise APIRequestFailure("api_dns_capacity", "External API host resolution is temporarily unavailable.")
        result_queue: Queue[tuple[list[tuple], Exception | None]] = Queue(maxsize=1)

        def resolve() -> None:
            try:
                result_queue.put((socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM), None))
            except Exception as error:
                result_queue.put(([], error))
            finally:
                _DNS_LOOKUP_SLOTS.release()

        try:
            Thread(target=resolve, name="flowdesk-dns-lookup", daemon=True).start()
        except Exception as error:
            _DNS_LOOKUP_SLOTS.release()
            raise APIRequestFailure("api_dns_failed", "External API host could not be resolved.") from error
        try:
            records, error = result_queue.get(timeout=DNS_RESOLUTION_TIMEOUT_SECONDS)
        except Exception as error:
            raise APIRequestFailure("api_dns_timeout", "External API host resolution timed out.") from error
        if error is not None:
            raise APIRequestFailure("api_dns_failed", "External API host could not be resolved.") from error
        try:
            candidates = [ipaddress.ip_address(record[4][0]) for record in records]
        except (OSError, ValueError) as error:
            raise APIRequestFailure("api_dns_failed", "External API host could not be resolved.") from error
    if not candidates or any(not address.is_global for address in candidates):
        raise WorkflowInputError("API endpoint resolves to a prohibited network address.", "prohibited_api_destination")
    return str(candidates[0])


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to the address already checked while retaining hostname TLS verification."""

    def __init__(self, hostname: str, pinned_ip: str, timeout: float) -> None:
        super().__init__(hostname, port=443, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def _https_get(
    endpoint: str, params: Mapping[str, object], timeout: float, max_bytes: int,
    token: str | None, checked_address: str,
) -> tuple[int, str, bytes]:
    _, hostname, port, path = _validate_endpoint(endpoint)
    try:
        address = ipaddress.ip_address(checked_address)
    except ValueError as error:
        raise WorkflowInputError("API destination validation failed.", "prohibited_api_destination") from error
    if not address.is_global:
        raise WorkflowInputError("API destination validation failed.", "prohibited_api_destination")
    query = urlencode(list(params.items()), doseq=True)
    target = path + ("?" + query if query else "")
    connection = _PinnedHTTPSConnection(hostname, str(address), timeout)
    headers = {"Accept": "application/json", "User-Agent": "Flowdesk/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        connection.request("GET", target, headers=headers)
        response = connection.getresponse()
        body = response.read(max_bytes + 1)
        return response.status, response.getheader("Content-Type", ""), body
    finally:
        connection.close()


def _reject_json_constant(_: str) -> None:
    raise ValueError("non-finite JSON number")


def _strict_response_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("response JSON contains a duplicate object property")
        result[key] = value
    return result


def _check_json_complexity(value: object, depth: int = 0, count: list[int] | None = None) -> None:
    count = count if count is not None else [0]
    count[0] += 1
    if depth > 32 or count[0] > 50000:
        raise ValueError("response JSON exceeds complexity limits")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("invalid object key")
            _check_json_complexity(item, depth + 1, count)
    elif isinstance(value, list):
        for item in value:
            _check_json_complexity(item, depth + 1, count)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON number")


def _redact_token(value: object, token: str | None) -> object:
    if not token:
        return value
    if isinstance(value, str):
        return value.replace(token, "[REDACTED]")
    if isinstance(value, list):
        return [_redact_token(item, token) for item in value]
    if isinstance(value, dict):
        return {
            key.replace(token, "[REDACTED]") if token in key else key: _redact_token(item, token)
            for key, item in value.items()
        }
    return value


class APIRequestStepHandler:
    """Perform a bounded HTTPS GET and return JSON plus non-sensitive metadata."""

    step_type = "api_request"

    def __init__(self, requester: Callable | None = None) -> None:
        self._requester = requester or _https_get

    def validate(self, step: WorkflowStep, prior_step_types: Mapping[str, str]) -> None:
        try:
            _validate_config(step.config)
            if set(step.inputs) - {"query_params"}:
                raise ValueError("api_request accepts only optional query_params input.")
            if "query_params" in step.inputs:
                value = step.inputs["query_params"]
                if isinstance(value, dict) and set(value) == {"from_step"}:
                    source = value.get("from_step")
                    if not isinstance(source, str) or prior_step_types.get(source) != "api_request":
                        raise ValueError("query_params references must point to an earlier api_request step.")
                else:
                    _validate_query_params(value, "query_params input")
        except ValueError as error:
            raise WorkflowDefinitionError(str(error)) from error

    def run(self, step, resolved_inputs, prior_outputs, workflow_input, context):
        config = _validate_config(step.config)
        params = dict(config["query_params"])
        if "query_params" in resolved_inputs:
            dynamic = resolved_inputs["query_params"]
            if isinstance(dynamic, Mapping) and "response" in dynamic:
                dynamic = dynamic["response"]
            try:
                params.update(_validate_query_params(dynamic, "query_params input"))
            except ValueError as error:
                raise WorkflowInputError(str(error), "invalid_api_request") from error
        params = _validate_query_params(params)
        credentials = _load_api_credentials()
        token = None
        credential_id = config["credential_id"]
        if credential_id is not None:
            credential = credentials.get(credential_id)
            if credential is None:
                raise WorkflowInputError("The configured API credential is unavailable.", "api_credential_unavailable")
            if credential["origin"] != _endpoint_origin(config["endpoint"]):
                raise WorkflowInputError("The configured API credential is not approved for this HTTPS origin.", "api_credential_origin_mismatch")
            token = credential["token"]
        _, hostname, port, _ = _validate_endpoint(config["endpoint"])
        checked_address = _resolve_public_ip(hostname, port)
        started = time.perf_counter()
        try:
            status_code, content_type, body = self._requester(
                config["endpoint"], params, config["timeout_seconds"], config["max_response_bytes"], token,
                checked_address,
            )
        except WorkflowInputError:
            raise
        except (TimeoutError, socket.timeout) as error:
            raise APIRequestFailure("api_timeout", "External API request timed out.") from error
        except APIRequestFailure:
            raise
        except Exception as error:
            raise APIRequestFailure("api_connection_failed", "External API request failed.") from error
        elapsed_ms = max(0, round((time.perf_counter() - started) * 1000))
        if 300 <= status_code < 400:
            raise APIRequestFailure("api_redirect_rejected", "External API redirects are not supported.")
        if not 200 <= status_code < 300:
            raise APIRequestFailure("api_http_error", "External API returned a non-success status.")
        if len(body) > config["max_response_bytes"]:
            raise APIRequestFailure("api_response_too_large", "External API response exceeded the configured size limit.")
        media_type = (content_type.split(";", 1)[0].strip().lower() if isinstance(content_type, str) else "")
        if len(media_type) > 200 or not (
            media_type == "application/json"
            or (media_type.startswith("application/") and media_type.endswith("+json"))
        ):
            raise APIRequestFailure("api_content_type", "External API returned an unsupported content type.")
        try:
            response_data = json.loads(
                body.decode("utf-8"),
                parse_constant=_reject_json_constant,
                object_pairs_hook=_strict_response_object,
            )
            _check_json_complexity(response_data)
        except (UnicodeDecodeError, ValueError, RecursionError) as error:
            raise APIRequestFailure("api_invalid_json", "External API returned malformed or overly complex JSON.") from error
        response_data = _redact_token(response_data, token)
        logger.info("API request step completed", extra={
            "execution_id": context.execution_id,
            "step_id": step.step_id,
            "step_type": self.step_type,
            "status_code": status_code,
            "response_bytes": len(body),
            "duration_ms": elapsed_ms,
        })
        return {
            "status_code": status_code,
            "content_type": media_type,
            "response_bytes": len(body),
            "duration_ms": elapsed_ms,
            "response": response_data,
        }
