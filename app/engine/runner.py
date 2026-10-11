"""Synchronous workflow execution lifecycle and in-memory execution history."""

from __future__ import annotations

import logging
import json
import math
import time
import uuid
from datetime import datetime, timezone
from threading import Lock
from typing import Protocol, TypeVar

from app.engine.contracts import ExecutionContext, ExecutionRecord, ExecutionStatus, WorkflowInputError

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")
logger = logging.getLogger("app.engine")


class ExecutionPersistence(Protocol):
    def __call__(self, record: ExecutionRecord) -> None: ...


_BUSINESS_OUTPUT_FIELDS = {
    "id", "execution_id", "filename", "status", "total_rows", "valid_rows", "invalid_rows",
    "duration_ms", "created_at", "artifacts", "issues", "issues_truncated", "rule_issues",
    "rule_issues_truncated",
}
_SUMMARY_OUTPUT_FIELDS = {
    "id", "execution_id", "status", "total_rows", "valid_rows", "invalid_rows", "artifacts",
}
_PDF_OUTPUT_FIELDS = {
    "title", "filename", "generated_at", "finding_count", "findings_included",
    "findings_truncated", "artifacts",
}
_API_REQUEST_OUTPUT_FIELDS = {"status_code", "content_type", "response_bytes", "duration_ms"}
_EMAIL_DELIVERY_OUTPUT_FIELDS = {
    "accepted_by_smtp", "recipient_count", "attachment_filename", "attachment_bytes",
    "smtp_security", "duration_ms", "sent_at",
}


def _safe_output(value: object, step_results: list[dict[str, object]]) -> dict[str, object] | None:
    """Persist only known workflow metadata, never arbitrary step payloads or cell data."""
    if hasattr(value, "as_dict"):
        value = value.as_dict()
    if isinstance(value, dict) and "outputs" not in value:
        return {key: value[key] for key in _BUSINESS_OUTPUT_FIELDS if key in value}
    if not isinstance(value, dict) or not isinstance(value.get("outputs"), dict):
        return None
    safe_outputs: dict[str, object] = {}
    types = {str(step.get("step_id")): step.get("step_type") for step in step_results}
    for step_id, result in value["outputs"].items():
        if not isinstance(step_id, str) or not isinstance(result, dict):
            continue
        step_type = types.get(step_id)
        if step_type == "business_file":
            safe_outputs[step_id] = {key: result[key] for key in _BUSINESS_OUTPUT_FIELDS if key in result}
        elif step_type == "business_file_summary":
            safe_outputs[step_id] = {key: result[key] for key in _SUMMARY_OUTPUT_FIELDS if key in result}
        elif step_type == "pdf_report":
            safe_outputs[step_id] = {key: result[key] for key in _PDF_OUTPUT_FIELDS if key in result}
        elif step_type == "api_request":
            safe_outputs[step_id] = {key: result[key] for key in _API_REQUEST_OUTPUT_FIELDS if key in result}
        elif step_type == "email_delivery":
            safe_outputs[step_id] = {
                key: result[key] for key in _EMAIL_DELIVERY_OUTPUT_FIELDS if key in result
            }
        elif step_type in {"clean_data", "map_columns", "calculate_field"}:
            operations = result.get("operations")
            if isinstance(operations, list):
                safe_outputs[step_id] = {
                    "operation_count": len(operations),
                    "operation_types": [item.get("type") for item in operations if isinstance(item, dict)],
                }
    return {
        key: value[key] for key in ("workflow_id", "version") if isinstance(value.get(key), str)
    } | {"outputs": safe_outputs}


def _safe_steps(steps: list[dict[str, object]]) -> list[dict[str, object]]:
    safe: list[dict[str, object]] = []
    for item in steps:
        error = item.get("error")
        if isinstance(error, dict):
            error = {
                "code": str(error.get("code", "execution_failed"))[:100],
                "message": str(error.get("message", "Step failed."))[:500],
                **({"type": str(error["type"])[:100]} if error.get("type") else {}),
            }
        result: dict[str, object] = {
            key: item[key] for key in ("step_id", "step_type", "status", "started_at", "completed_at", "duration_ms")
            if key in item
        }
        if error:
            result["error"] = error
        output = item.get("output")
        if isinstance(output, dict):
            safe_result = _safe_output({"outputs": {str(item.get("step_id")): output}}, [item])
            if safe_result and safe_result.get("outputs"):
                result["output"] = safe_result["outputs"].get(str(item.get("step_id")))
        safe.append(result)
    return safe


def _persist_sqlite(record: ExecutionRecord) -> None:
    from app.storage.repository import save_automation_execution

    safe_steps = _safe_steps(record.step_results)
    error = None
    if record.error_code:
        error = {
            "code": record.error_code[:100],
            "message": (record.error_message or "Workflow failed.")[:500],
            **({"type": record.error_type[:100]} if record.error_type else {}),
        }
    counts = record.counts if isinstance(record.counts, dict) else None
    save_automation_execution(
        execution_id=record.execution_id,
        workflow_name=record.workflow_name,
        workflow_id=record.workflow_id or record.workflow_name,
        workflow_version=record.workflow_version,
        status=record.status.value,
        created_at=(record.created_at or record.started_at).isoformat(),
        started_at=record.started_at.isoformat() if record.status != ExecutionStatus.QUEUED else None,
        completed_at=record.finished_at.isoformat() if record.finished_at else None,
        duration_seconds=record.duration_seconds if record.duration_seconds is None or math.isfinite(record.duration_seconds) else None,
        error=error,
        counts=counts,
        output=_safe_output(record.output, record.step_results),
        steps=safe_steps,
    )


class Workflow(Protocol[InputT, OutputT]):
    name: str

    def run(self, workflow_input: InputT, context: ExecutionContext) -> OutputT: ...


class WorkflowRunner:
    """Run workflows synchronously and retain their latest process-local records."""

    def __init__(self, persistence: ExecutionPersistence | None = None) -> None:
        self._records: dict[str, ExecutionRecord] = {}
        self._lock = Lock()
        self._persistence = persistence

    def get(self, execution_id: str) -> ExecutionRecord | None:
        with self._lock:
            return self._records.get(execution_id)

    def records(self) -> tuple[ExecutionRecord, ...]:
        with self._lock:
            return tuple(self._records.values())

    def execute(
        self,
        workflow: Workflow[InputT, OutputT],
        workflow_input: InputT,
        *,
        execution_id: str | None = None,
        created_at: datetime | None = None,
    ) -> ExecutionRecord:
        started = datetime.now(timezone.utc)
        workflow_id = getattr(workflow, "name", "workflow")
        workflow_version = None
        metadata_provider = getattr(workflow, "execution_metadata", None)
        if callable(metadata_provider):
            try:
                metadata = metadata_provider()
                if isinstance(metadata, tuple) and len(metadata) == 2:
                    workflow_id, workflow_version = metadata
            except Exception:
                # Identity lookup is advisory; the workflow itself owns validation errors.
                pass
        record = ExecutionRecord(
            execution_id=execution_id or str(uuid.uuid4()),
            workflow_name=workflow.name,
            status=ExecutionStatus.QUEUED,
            started_at=started,
            created_at=created_at or started,
            workflow_id=workflow_id if isinstance(workflow_id, str) else workflow.name,
            workflow_version=workflow_version if isinstance(workflow_version, str) else None,
        )
        with self._lock:
            self._records[record.execution_id] = record

        logger.info("Workflow queued", extra={
            "execution_id": record.execution_id,
            "workflow_name": record.workflow_name,
            "execution_status": record.status.value,
        })
        tick = time.perf_counter()
        try:
            self._persist(record)
            record.status = ExecutionStatus.RUNNING
            record.started_at = datetime.now(timezone.utc)
            self._store(record)
            self._persist(record)
            logger.info("Workflow started", extra={
                "execution_id": record.execution_id,
                "workflow_name": record.workflow_name,
                "execution_status": record.status.value,
            })
            context = ExecutionContext(
                record.execution_id,
                step_result_callback=lambda steps: self._persist_steps(record, steps),
                workflow_id=record.workflow_id,
                workflow_version=record.workflow_version,
            )
            record.output = workflow.run(workflow_input, context)
        except WorkflowInputError as error:
            record.error_code = error.code
            record.error_message = str(error)
            record.error_type = type(error).__name__
            record.counts = dict(locals().get("context", ExecutionContext(record.execution_id)).counts)
            record.step_results = list(locals().get("context", ExecutionContext(record.execution_id)).step_results)
            self._finish(record, ExecutionStatus.FAILED, time.perf_counter() - tick)
            logger.warning("Workflow rejected input", extra={
                "execution_id": record.execution_id,
                "workflow_name": record.workflow_name,
                "execution_status": record.status.value,
                "error_code": record.error_code,
            })
        except Exception as error:
            context = locals().get("context", ExecutionContext(record.execution_id))
            record.error_code = "execution_failed"
            record.error_message = "Workflow execution failed."
            record.error_type = type(error).__name__
            record.counts = dict(context.counts)
            record.step_results = list(context.step_results)
            self._finish(record, ExecutionStatus.FAILED, time.perf_counter() - tick)
            logger.exception("Workflow execution failed", extra={
                "execution_id": record.execution_id,
                "workflow_name": record.workflow_name,
                "execution_status": record.status.value,
                "error_type": record.error_type,
                "step_id": getattr(error, "step_id", None),
            })
            raise
        else:
            record.counts = dict(context.counts)
            record.step_results = list(context.step_results)
            self._finish(record, ExecutionStatus.SUCCEEDED, time.perf_counter() - tick)
        return record

    def _persist(self, record: ExecutionRecord) -> None:
        if self._persistence:
            self._persistence(record)

    def _persist_steps(self, record: ExecutionRecord, steps: list[dict[str, object]]) -> None:
        record.step_results = list(steps)
        self._store(record)
        self._persist(record)

    def _finish(self, record: ExecutionRecord, status: ExecutionStatus, elapsed: float) -> None:
        record.status = status
        record.finished_at = datetime.now(timezone.utc)
        record.duration_seconds = elapsed
        output_counts = {
            key: int(value)
            for key in ("total_rows", "valid_rows", "invalid_rows")
            if isinstance((value := getattr(record.output, key, None)), int)
        }
        record.counts = {**(record.counts or {}), **output_counts}
        self._store(record)
        self._persist(record)
        output = record.output
        row_count = getattr(output, "row_count", None)
        logger.info("Workflow finalized", extra={
            "execution_id": record.execution_id,
            "workflow_name": record.workflow_name,
            "execution_status": status.value,
            "duration_seconds": elapsed,
            "row_count": row_count,
            "counts": record.counts,
            "error_code": record.error_code,
        })

    def _store(self, record: ExecutionRecord) -> None:
        with self._lock:
            self._records[record.execution_id] = record


default_runner = WorkflowRunner(persistence=_persist_sqlite)
