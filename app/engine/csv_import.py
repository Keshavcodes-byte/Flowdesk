"""CSV parsing, validation, and orchestration for an import job."""

from __future__ import annotations

import csv
import math
from io import BytesIO, StringIO
from pathlib import PurePath

import pandas as pd
from fastapi import UploadFile
from pandas.errors import EmptyDataError, ParserError

from app.engine.contracts import CsvImportInput, WorkflowInputError
from app.engine.csv_workflow import CsvImportWorkflow, JobPersistence
from app.engine.runner import WorkflowRunner, default_runner
from app.storage.repository import InvalidRowDataError, create_job

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
NON_FINITE_TOKENS = {
    "nan",
    "+nan",
    "-nan",
    "inf",
    "+inf",
    "-inf",
    "infinity",
    "+infinity",
    "-infinity",
}


class CsvImportError(Exception):
    """Expected input error with an HTTP status suitable for the API."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class _RepositoryPersistence:
    """Adapt the existing repository function to the workflow persistence port."""

    def create_job(self, filename: str, rows: list[dict[str, object]]) -> dict[str, object]:
        try:
            return create_job(filename=filename, rows=rows)
        except InvalidRowDataError as error:
            raise WorkflowInputError(str(error), "invalid_row_data") from error


async def import_csv(
    upload: UploadFile,
    *,
    runner: WorkflowRunner = default_runner,
    persistence: JobPersistence | None = None,
) -> dict[str, object]:
    """Read a bounded upload in memory, validate it, and persist the job."""
    filename = PurePath(upload.filename or "upload.csv").name
    if not filename.lower().endswith(".csv"):
        raise CsvImportError("Upload must have a .csv filename.", status_code=415)

    content = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise CsvImportError("CSV upload exceeds the 5 MiB size limit.", status_code=413)
    if not content.strip():
        raise CsvImportError("CSV file is empty.")

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise CsvImportError("CSV must use UTF-8 or UTF-8 with a BOM.") from error

    try:
        header_reader = csv.reader(StringIO(text, newline=""), strict=True)
        source_header = next((row for row in header_reader if row), None)
    except csv.Error as error:
        raise CsvImportError(f"Could not parse CSV header: {error}") from error

    if not source_header:
        raise CsvImportError("CSV must contain a header row and at least one data row.")
    if any(not name.strip() for name in source_header):
        raise CsvImportError("Every CSV column must have a non-empty header.")
    if len(set(source_header)) != len(source_header):
        raise CsvImportError("CSV column headers must be unique.")

    try:
        # Keep fields as text so identifiers (for example, 001) and literal
        # strings (for example, NA) are not changed by Pandas type inference.
        frame = pd.read_csv(
            BytesIO(content),
            encoding="utf-8-sig",
            on_bad_lines="error",
            dtype=str,
            keep_default_na=False,
            na_filter=False,
        )
    except (EmptyDataError, ParserError, ValueError) as error:
        raise CsvImportError(f"Could not parse CSV: {error}") from error

    if frame.empty or frame.shape[1] == 0:
        raise CsvImportError("CSV must contain a header row and at least one data row.")
    if any(not isinstance(column, str) or not column.strip() for column in frame.columns):
        raise CsvImportError("Every CSV column must have a non-empty header.")
    if frame.columns.duplicated().any():
        raise CsvImportError("CSV column headers must be unique.")

    frame = frame.astype(object).where(pd.notna(frame), None)
    rows = frame.to_dict(orient="records")
    if not any(any(value is not None and str(value).strip() for value in row.values()) for row in rows):
        raise CsvImportError("CSV must contain at least one non-empty data value.")

    for row_number, row in enumerate(rows, start=2):
        for column, value in row.items():
            if isinstance(value, str) and value.strip().casefold() in NON_FINITE_TOKENS:
                raise CsvImportError(
                    f"Column '{column}' contains a non-finite numeric value at data row {row_number}."
                )
            if isinstance(value, (int, float)) and not math.isfinite(value):
                raise CsvImportError(
                    f"Column '{column}' contains a non-finite numeric value at data row {row_number}."
                )

    workflow = CsvImportWorkflow(persistence or _RepositoryPersistence())
    execution = runner.execute(workflow, CsvImportInput(filename=filename, rows=rows))
    if execution.output is None:
        raise CsvImportError(execution.error_message or "CSV workflow rejected the input.")
    return execution.output.as_dict()
