"""Reusable CSV import workflow, independent of HTTP and storage technology."""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from app.engine.contracts import CsvImportInput, CsvImportOutput, ExecutionContext, WorkflowInputError


class JobPersistence(Protocol):
    def create_job(self, filename: str, rows: list[dict[str, Any]]) -> Mapping[str, object]: ...


class CsvImportWorkflow:
    name = "csv_import"

    def __init__(self, persistence: JobPersistence) -> None:
        self._persistence = persistence

    def run(self, workflow_input: CsvImportInput, context: ExecutionContext | None = None) -> CsvImportOutput:
        if not isinstance(workflow_input.filename, str) or not workflow_input.filename.strip():
            raise WorkflowInputError("A filename is required.", "filename_required")
        if not isinstance(workflow_input.rows, list) or not workflow_input.rows:
            raise WorkflowInputError("At least one data row is required.", "rows_required")
        if any(not isinstance(row, dict) for row in workflow_input.rows):
            raise WorkflowInputError("Each data row must be an object.", "invalid_row")

        saved = self._persistence.create_job(workflow_input.filename, workflow_input.rows)
        try:
            return CsvImportOutput(
                job_id=str(saved["id"]),
                filename=str(saved["filename"]),
                status=str(saved["status"]),
                row_count=int(saved["row_count"]),
                created_at=str(saved["created_at"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("Persistence returned an invalid job record.") from error
