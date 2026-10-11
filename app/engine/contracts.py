"""Framework and storage independent workflow contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Callable


class ExecutionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class WorkflowInputError(ValueError):
    """A caller supplied input that a workflow cannot process."""

    def __init__(self, message: str, code: str = "invalid_input") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CsvImportInput:
    filename: str
    rows: list[dict[str, Any]]


@dataclass(frozen=True)
class CsvImportOutput:
    job_id: str
    filename: str
    status: str
    row_count: int
    created_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.job_id,
            "filename": self.filename,
            "status": self.status,
            "row_count": self.row_count,
            "created_at": self.created_at,
        }


@dataclass
class ExecutionRecord:
    execution_id: str
    workflow_name: str
    status: ExecutionStatus
    started_at: datetime
    finished_at: datetime | None = None
    output: object | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_type: str | None = None
    duration_seconds: float | None = None
    counts: dict[str, int] | None = None
    step_results: list[dict[str, object]] = field(default_factory=list)
    workflow_id: str | None = None
    workflow_version: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class ExecutionContext:
    execution_id: str
    counts: dict[str, int] = field(default_factory=dict)
    step_results: list[dict[str, object]] = field(default_factory=list)
    step_result_callback: Callable[[list[dict[str, object]]], None] | None = None
    workflow_id: str | None = None
    workflow_version: str | None = None

