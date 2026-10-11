"""SMTP delivery of registered PDF artifacts from prior workflow steps."""

from __future__ import annotations

import re
import logging
import os
import smtplib
import ssl
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Mapping

from app.engine.automation import WorkflowDefinitionError, WorkflowStep
from app.engine.contracts import WorkflowInputError

logger = logging.getLogger("app.engine")

_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_EMAIL = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)
_PDF_FILENAME = re.compile(r"^[0-9a-f]{32}_report\.pdf$")
MAX_ATTACHMENT_LIMIT = 10 * 1024 * 1024
MAX_SMTP_TIMEOUT = 30.0
SMTP_USERNAME_ENV = "FLOWDESK_SMTP_USERNAME"
SMTP_PASSWORD_ENV = "FLOWDESK_SMTP_PASSWORD"
SMTP_ALLOWED_HOSTS_ENV = "FLOWDESK_SMTP_ALLOWED_HOSTS"


class EmailDeliveryFailure(RuntimeError):
    """An SMTP failure with a safe, non-sensitive public description."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.safe_error_code = code
        self.safe_error_message = message


def _valid_email(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) > 254 or not _EMAIL.fullmatch(value):
        raise ValueError(f"{field} must be a valid email address.")
    return value


def validate_email_config(config: Mapping[str, object]) -> dict[str, object]:
    allowed = {
        "smtp_host", "smtp_port", "smtp_security", "sender", "recipient", "subject", "message",
        "username_env", "password_env", "timeout_seconds", "max_attachment_bytes",
    }
    if set(config) != allowed:
        raise ValueError("email_delivery config must contain exactly the supported SMTP and message fields.")
    host = config["smtp_host"]
    if not isinstance(host, str) or len(host) > 253 or not _HOST.fullmatch(host) or ".." in host:
        raise ValueError("smtp_host must be a valid SMTP hostname or IPv4 address.")
    port = config["smtp_port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("smtp_port must be an integer between 1 and 65535.")
    security = config["smtp_security"]
    if not isinstance(security, str) or security not in {"starttls", "implicit_tls"}:
        raise ValueError("smtp_security must be starttls or implicit_tls.")
    sender = _valid_email(config["sender"], "sender")
    recipient = _valid_email(config["recipient"], "recipient")
    subject = config["subject"]
    if not isinstance(subject, str) or not 1 <= len(subject.strip()) <= 200 or any(ord(ch) < 32 or ord(ch) == 127 for ch in subject):
        raise ValueError("subject must contain 1 to 200 printable characters without line breaks.")
    message = config["message"]
    if not isinstance(message, str) or not 1 <= len(message) <= 10000 or "\x00" in message:
        raise ValueError("message must contain 1 to 10000 plain-text characters.")
    username_env = config["username_env"]
    password_env = config["password_env"]
    if username_env != SMTP_USERNAME_ENV:
        raise ValueError(f"username_env must use the approved name {SMTP_USERNAME_ENV}.")
    if password_env != SMTP_PASSWORD_ENV:
        raise ValueError(f"password_env must use the approved name {SMTP_PASSWORD_ENV}.")
    timeout = config["timeout_seconds"]
    if type(timeout) not in {int, float} or not 0.1 <= timeout <= MAX_SMTP_TIMEOUT:
        raise ValueError("timeout_seconds must be between 0.1 and 30 seconds.")
    max_attachment = config["max_attachment_bytes"]
    if type(max_attachment) is not int or not 1024 <= max_attachment <= MAX_ATTACHMENT_LIMIT:
        raise ValueError("max_attachment_bytes must be between 1024 and 10485760.")
    return {
        "smtp_host": host, "smtp_port": port, "smtp_security": security, "sender": sender,
        "recipient": recipient, "subject": subject.strip(), "message": message,
        "username_env": username_env, "password_env": password_env,
        "timeout_seconds": float(timeout), "max_attachment_bytes": max_attachment,
    }


class EmailDeliveryStepHandler:
    """Send one message with one registered PDF attachment, without retrying."""

    step_type = "email_delivery"

    def __init__(
        self,
        output_dir: str | Path,
        get_artifact: Callable[[str], dict[str, object] | None],
        *,
        smtp_factory: Callable | None = None,
        smtp_ssl_factory: Callable | None = None,
        ssl_context_factory: Callable | None = None,
    ) -> None:
        self._output_dir = Path(output_dir)
        self._get_artifact = get_artifact
        self._smtp_factory = smtp_factory or smtplib.SMTP
        self._smtp_ssl_factory = smtp_ssl_factory or smtplib.SMTP_SSL
        self._ssl_context_factory = ssl_context_factory or ssl.create_default_context

    def validate(self, step: WorkflowStep, prior_step_types: Mapping[str, str]) -> None:
        try:
            validate_email_config(step.config)
        except ValueError as error:
            raise WorkflowDefinitionError(str(error)) from error
        if set(step.inputs) != {"report"}:
            raise WorkflowDefinitionError("email_delivery requires only a PDF report reference.")
        reference = step.inputs["report"]
        source = reference.get("from_step") if isinstance(reference, dict) and set(reference) == {"from_step"} else None
        if not isinstance(source, str) or prior_step_types.get(source) != "pdf_report":
            raise WorkflowDefinitionError("report must reference an earlier pdf_report step.")

    def _load_registered_pdf(self, report: object, size_limit: int) -> tuple[str, bytes]:
        if not isinstance(report, Mapping):
            raise WorkflowInputError("PDF attachment reference is invalid.", "invalid_email_attachment")
        filename = report.get("filename")
        artifacts = report.get("artifacts")
        if (
            not isinstance(filename, str) or not _PDF_FILENAME.fullmatch(filename)
            or not isinstance(artifacts, Mapping)
            or artifacts.get("pdf_report") != f"/artifacts/{filename}"
        ):
            raise WorkflowInputError("PDF attachment reference is invalid.", "invalid_email_attachment")
        registration = self._get_artifact(filename)
        if (
            not isinstance(registration, Mapping)
            or registration.get("artifact_name") != filename
            or registration.get("artifact_type") != "pdf_report"
        ):
            raise WorkflowInputError("Registered PDF attachment was not found.", "email_attachment_not_found")
        root = self._output_dir.expanduser().resolve()
        try:
            path = (root / filename).resolve(strict=True)
            if path.parent != root or not path.is_file():
                raise WorkflowInputError("PDF attachment path is not allowed.", "invalid_email_attachment")
            if path.stat().st_size > size_limit:
                raise WorkflowInputError("PDF attachment exceeds the configured size limit.", "email_attachment_too_large")
            with path.open("rb") as stream:
                content = stream.read(size_limit + 1)
        except FileNotFoundError as error:
            raise WorkflowInputError("Registered PDF attachment is missing.", "email_attachment_not_found") from error
        except OSError as error:
            raise WorkflowInputError("Registered PDF attachment could not be read.", "email_attachment_unavailable") from error
        if len(content) > size_limit:
            raise WorkflowInputError("PDF attachment exceeds the configured size limit.", "email_attachment_too_large")
        if not content.startswith(b"%PDF-"):
            raise WorkflowInputError("Registered artifact is not a supported PDF.", "invalid_email_attachment")
        return filename, content

    def run(self, step, resolved_inputs, prior_outputs, workflow_input, context):
        config = validate_email_config(step.config)
        filename, pdf_bytes = self._load_registered_pdf(
            resolved_inputs.get("report"), config["max_attachment_bytes"]
        )
        allowed_hosts = {
            item.strip().lower()
            for item in os.environ.get(SMTP_ALLOWED_HOSTS_ENV, "").split(",")
            if item.strip()
        }
        if str(config["smtp_host"]).lower() not in allowed_hosts:
            raise EmailDeliveryFailure(
                "email_smtp_host_not_approved", "SMTP host is not approved by the server configuration."
            )
        username = os.environ.get(config["username_env"])
        password = os.environ.get(config["password_env"])
        if not username or not password:
            raise EmailDeliveryFailure(
                "email_credentials_unavailable", "Configured SMTP credentials are unavailable on the server."
            )

        message = EmailMessage()
        message["From"] = config["sender"]
        message["To"] = config["recipient"]
        message["Subject"] = config["subject"]
        message.set_content(config["message"])
        message.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=filename)

        server = None
        started = time.perf_counter()
        security = config["smtp_security"]
        try:
            try:
                if security == "implicit_tls":
                    server = self._smtp_ssl_factory(
                        config["smtp_host"], config["smtp_port"], timeout=config["timeout_seconds"],
                        context=self._ssl_context_factory(),
                    )
                else:
                    server = self._smtp_factory(
                        config["smtp_host"], config["smtp_port"], timeout=config["timeout_seconds"]
                    )
            except Exception as error:
                code = "email_tls_failed" if security == "implicit_tls" else "email_connection_failed"
                summary = "Secure SMTP connection failed." if security == "implicit_tls" else "SMTP connection failed."
                raise EmailDeliveryFailure(code, summary) from error

            if security == "starttls":
                try:
                    server.ehlo()
                    if not server.has_extn("starttls"):
                        raise RuntimeError("SMTP server does not offer STARTTLS.")
                    server.starttls(context=self._ssl_context_factory())
                    server.ehlo()
                except Exception as error:
                    raise EmailDeliveryFailure("email_tls_failed", "SMTP STARTTLS negotiation failed.") from error
            try:
                server.login(username, password)
            except Exception as error:
                raise EmailDeliveryFailure("email_authentication_failed", "SMTP authentication failed.") from error
            try:
                refused = server.send_message(
                    message, from_addr=config["sender"], to_addrs=[config["recipient"]]
                )
                if refused:
                    raise RuntimeError("SMTP server refused the configured recipient.")
            except Exception as error:
                raise EmailDeliveryFailure("email_send_failed", "SMTP message submission failed.") from error
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    try:
                        server.close()
                    except Exception:
                        pass

        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        logger.info("Email delivery step completed", extra={
            "execution_id": context.execution_id,
            "step_id": step.step_id,
            "step_type": self.step_type,
            "delivery_status": "accepted_by_smtp",
            "attachment_bytes": len(pdf_bytes),
            "duration_ms": duration_ms,
            "smtp_security": security,
        })
        return {
            "accepted_by_smtp": True,
            "recipient_count": 1,
            "attachment_filename": filename,
            "attachment_bytes": len(pdf_bytes),
            "smtp_security": security,
            "duration_ms": duration_ms,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
