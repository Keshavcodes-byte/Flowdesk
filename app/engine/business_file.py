"""Format-neutral validation and safe artifact generation for business files."""

from __future__ import annotations

import csv
import json
import math
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time as time_value
from io import BytesIO, StringIO
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Protocol

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

from app.engine.contracts import ExecutionContext, WorkflowInputError
from app.engine.rule_engine import BusinessRuleIssue, evaluate_rules, validate_rule_columns
from app.engine.rules import RulesetError, parse_ruleset
from app.engine.transformations import apply_transformations

MAX_BUSINESS_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_WORKSHEET_CELLS = 200_000
MAX_BUSINESS_ROWS = 100_000
MAX_ISSUE_PREVIEW = 100
MAX_XLSX_EXPANDED_BYTES = 64 * 1024 * 1024
SUPPORTED_SUFFIXES = {".csv", ".xlsx"}
NON_FINITE_TOKENS = {"nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}


@dataclass(frozen=True)
class BusinessFileInput:
    filename: str
    content: bytes
    rules_json: str | None = None


@dataclass(frozen=True)
class BusinessFileOutput:
    job_id: str
    execution_id: str
    filename: str
    status: str
    total_rows: int
    valid_rows: int
    invalid_rows: int
    duration_ms: int
    created_at: str
    processed_artifact: str | None
    validation_artifact: str
    issues: list[dict[str, object]]
    issues_truncated: bool
    rule_issues: list[dict[str, object]] | None = None
    rule_issues_truncated: bool = False

    def as_dict(self) -> dict[str, object]:
        artifacts = {"validation_report": f"/artifacts/{self.validation_artifact}"}
        if self.processed_artifact:
            artifacts["processed_workbook"] = f"/artifacts/{self.processed_artifact}"
        payload = {
            "id": self.job_id,
            "execution_id": self.execution_id,
            "filename": self.filename,
            "status": self.status,
            "total_rows": self.total_rows,
            "valid_rows": self.valid_rows,
            "invalid_rows": self.invalid_rows,
            "duration_ms": self.duration_ms,
            "created_at": self.created_at,
            "artifacts": artifacts,
            "issues": self.issues,
            "issues_truncated": self.issues_truncated,
        }
        if self.rule_issues is not None:
            payload["rule_issues"] = self.rule_issues
            payload["rule_issues_truncated"] = self.rule_issues_truncated
        return payload


class BusinessJobPersistence(Protocol):
    def create_business_job(self, **metadata: object) -> Mapping[str, object]: ...


class BusinessFileWorkflow:
    name = "business_file_processing"

    def __init__(self, persistence: BusinessJobPersistence, output_dir: str | Path) -> None:
        self._persistence = persistence
        self._output_dir = Path(output_dir)

    def run(
        self,
        workflow_input: BusinessFileInput,
        context: ExecutionContext,
        transformations: object = None,
    ) -> BusinessFileOutput:
        try:
            ruleset = parse_ruleset(workflow_input.rules_json)
        except RulesetError as error:
            raise WorkflowInputError(str(error), "invalid_ruleset") from error
        filename = _safe_source_filename(workflow_input.filename)
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise WorkflowInputError("Upload must be a .csv or .xlsx file.", "unsupported_file_type")
        if len(workflow_input.content) > MAX_BUSINESS_UPLOAD_BYTES:
            raise WorkflowInputError("File exceeds the 5 MiB upload limit.", "file_too_large")
        if not workflow_input.content:
            raise WorkflowInputError("File is empty.", "empty_file")

        if suffix == ".csv":
            rows, headers, row_numbers = _read_csv(workflow_input.content)
        else:
            rows, headers, row_numbers = _read_xlsx(workflow_input.content)
        if not rows:
            raise WorkflowInputError("File must contain at least one data row.", "missing_data")

        integrity_by_row = {
            row_number: _validate_row(row, headers)
            for row_number, row in zip(row_numbers, rows, strict=True)
        }
        if ruleset is not None:
            try:
                validate_rule_columns(ruleset, headers)
                eligible = [
                    (row, row_number)
                    for row, row_number in zip(rows, row_numbers, strict=True)
                    if not integrity_by_row[row_number]
                ]
                rule_findings = evaluate_rules(
                    ruleset, headers,
                    [row for row, _ in eligible],
                    [row_number for _, row_number in eligible],
                )
            except RulesetError as error:
                raise WorkflowInputError(str(error), "invalid_ruleset") from error
        else:
            rule_findings = []
        findings_by_row: dict[int, list[BusinessRuleIssue]] = {}
        for finding in rule_findings:
            findings_by_row.setdefault(finding.source_row_number, []).append(finding)

        valid: list[list[Any]] = []
        valid_row_numbers: list[int] = []
        invalid: list[tuple[int, list[Any], list[str], list[dict[str, object]]]] = []
        issue_summaries: list[dict[str, object]] = []
        row_rule_issues: list[dict[str, object]] = []
        for row_number, original in zip(row_numbers, rows, strict=True):
            integrity_issues = integrity_by_row[row_number]
            business_issues = findings_by_row.get(row_number, [])
            issues = integrity_issues + [
                f"Rule '{finding.rule_id}' on column '{finding.column_name}': {finding.message}"
                for finding in business_issues
            ]
            if len(row_rule_issues) < MAX_ISSUE_PREVIEW:
                row_rule_issues.extend(finding.as_dict() for finding in business_issues[:MAX_ISSUE_PREVIEW - len(row_rule_issues)])
            if issues:
                invalid.append((row_number, original, issues, [item.as_dict() for item in business_issues]))
                if len(issue_summaries) < MAX_ISSUE_PREVIEW:
                    issue_summaries.append({"row_number": row_number, "issues": issues})
            else:
                valid.append(original)
                valid_row_numbers.append(row_number)

        output_headers, output_rows, _ = apply_transformations(
            headers, valid, valid_row_numbers, transformations
        )
        valid = output_rows
        context.counts.update(total_rows=len(rows), valid_rows=len(valid), invalid_rows=len(invalid))

        self._output_dir.mkdir(parents=True, exist_ok=True)
        job_id = str(uuid.uuid4())
        processed_name = f"{job_id}_processed.xlsx" if valid else None
        report_name = f"{job_id}_validation.xlsx"
        created: list[Path] = []
        started = time.perf_counter()
        try:
            if processed_name:
                path = self._output_dir / processed_name
                _save_workbook_atomically(path, _build_processed_workbook(output_headers, valid))
                created.append(path)
            report_path = self._output_dir / report_name
            _save_workbook_atomically(report_path, _build_validation_workbook(headers, invalid))
            created.append(report_path)
            duration_ms = max(0, round((time.perf_counter() - started) * 1000))
            saved = self._persistence.create_business_job(
                job_id=job_id,
                execution_id=context.execution_id,
                source_filename=filename,
                total_rows=len(rows),
                valid_rows=len(valid),
                invalid_rows=len(invalid),
                processed_artifact=processed_name,
                validation_artifact=report_name,
                duration_ms=duration_ms,
                rows=[
                    {header: _json_value(value) for header, value in zip(output_headers, row, strict=False)}
                    for row in valid
                ],
                ruleset_id=ruleset.ruleset_id if ruleset else None,
                ruleset_version=ruleset.version if ruleset else None,
                ruleset_json=workflow_input.rules_json if ruleset else None,
            )
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            raise

        return BusinessFileOutput(
            job_id=job_id,
            execution_id=context.execution_id,
            filename=filename,
            status="completed",
            total_rows=len(rows),
            valid_rows=len(valid),
            invalid_rows=len(invalid),
            duration_ms=duration_ms,
            created_at=str(saved["created_at"]),
            processed_artifact=processed_name,
            validation_artifact=report_name,
            issues=issue_summaries,
            issues_truncated=len(invalid) > MAX_ISSUE_PREVIEW,
            rule_issues=row_rule_issues if ruleset else None,
            rule_issues_truncated=bool(ruleset and len(rule_findings) > MAX_ISSUE_PREVIEW),
        )


def _safe_source_filename(filename: str) -> str:
    normalized = str(filename or "").replace("\\", "/")
    return PurePosixPath(normalized).name


def _headers(values: list[Any]) -> list[str]:
    if not values or all(value is None or str(value).strip() == "" for value in values):
        raise WorkflowInputError("File must start with a non-empty header row.", "missing_headers")
    headers = ["" if value is None else str(value) for value in values]
    if any(not header.strip() for header in headers):
        raise WorkflowInputError("Every column must have a non-empty header.", "blank_header")
    normalized = [header.strip().casefold() for header in headers]
    if len(set(normalized)) != len(normalized):
        raise WorkflowInputError("Column headers must be unique (ignoring case and surrounding spaces).", "duplicate_headers")
    if any(ILLEGAL_CHARACTERS_RE.search(header) for header in headers):
        raise WorkflowInputError("Headers contain unsupported control characters.", "invalid_header")
    return headers


def _read_csv(content: bytes) -> tuple[list[list[Any]], list[str], list[int]]:
    try:
        text = content.decode("utf-8-sig")
        reader = csv.reader(StringIO(text, newline=""), strict=True)
        header_values = next(reader, None)
        headers = _headers(header_values or [])
        rows: list[list[Any]] = []
        numbers: list[int] = []
        for row in reader:
            rows.append(row)
            numbers.append(reader.line_num)
            if len(rows) > MAX_BUSINESS_ROWS:
                raise WorkflowInputError("File exceeds the 100,000 data-row processing limit.", "too_many_rows")
    except UnicodeDecodeError as error:
        raise WorkflowInputError("CSV must use UTF-8 or UTF-8 with a BOM.", "invalid_encoding") from error
    except csv.Error as error:
        raise WorkflowInputError(f"Could not parse CSV: {error}", "malformed_csv") from error
    return rows, headers, numbers


def _read_xlsx(content: bytes) -> tuple[list[list[Any]], list[str], list[int]]:
    workbook = None
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            if sum(item.file_size for item in archive.infolist()) > MAX_XLSX_EXPANDED_BYTES:
                raise WorkflowInputError("XLSX expands beyond the 64 MiB processing limit.", "xlsx_expanded_too_large")
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=False)
        sheet = workbook.active
        if (sheet.max_row or 0) * (sheet.max_column or 0) > MAX_WORKSHEET_CELLS:
            raise WorkflowInputError("Worksheet exceeds the 200,000-cell processing limit.", "worksheet_too_large")
        iterator = sheet.iter_rows()
        first = next(iterator, None)
        headers = _headers([cell.value for cell in first] if first else [])
        rows: list[list[Any]] = []
        numbers: list[int] = []
        for row_number, cells in enumerate(iterator, start=2):
            if row_number - 1 > MAX_BUSINESS_ROWS:
                raise WorkflowInputError("File exceeds the 100,000 data-row processing limit.", "too_many_rows")
            values: list[Any] = []
            for cell in cells:
                if cell.data_type == "f":
                    values.append(_FormulaValue(str(cell.value)))
                elif cell.data_type == "e":
                    values.append(_CellErrorValue(str(cell.value)))
                else:
                    values.append(cell.value)
            rows.append(values)
            numbers.append(row_number)
        return rows, headers, numbers
    except WorkflowInputError:
        raise
    except Exception as error:
        raise WorkflowInputError("Could not read this XLSX workbook.", "malformed_xlsx") from error
    finally:
        if workbook is not None:
            workbook.close()


@dataclass(frozen=True)
class _FormulaValue:
    value: str


@dataclass(frozen=True)
class _CellErrorValue:
    value: str


def _validate_row(row: list[Any], headers: list[str]) -> list[str]:
    width = len(headers)
    values = row[:width]
    issues: list[str] = []
    if len(row) > width:
        issues.append(f"Row has {len(row) - width} extra cell(s).")
    elif len(row) < width:
        issues.append(f"Row is missing {width - len(row)} cell(s).")
        values.extend([None] * (width - len(row)))
    if all(value is None or (isinstance(value, str) and value.strip() == "") for value in values):
        issues.append("Row is blank.")
    for index, value in enumerate(values):
        if isinstance(value, _FormulaValue):
            issues.append(f"Column '{headers[index]}' contains a formula; formulas are not evaluated.")
        elif isinstance(value, _CellErrorValue):
            issues.append(f"Column '{headers[index]}' contains an Excel error value.")
        elif isinstance(value, float) and not math.isfinite(value):
            issues.append(f"Column '{headers[index]}' contains a non-finite number.")
        elif isinstance(value, str) and value.strip().casefold() in NON_FINITE_TOKENS:
            issues.append(f"Column '{headers[index]}' contains a non-finite numeric token.")
        elif isinstance(value, str) and ILLEGAL_CHARACTERS_RE.search(value):
            issues.append(f"Column '{headers[index]}' contains an unsupported control character.")
    return issues


def _excel_value(value: Any) -> Any:
    if isinstance(value, (_FormulaValue, _CellErrorValue)):
        return value.value
    return value


def _json_value(value: Any) -> Any:
    if isinstance(value, (_FormulaValue, _CellErrorValue)):
        return value.value
    if isinstance(value, (datetime, date, time_value)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _write_safe_cell(sheet, row: int, column: int, value: Any) -> None:
    safe_value = _excel_value(value)
    cell = sheet.cell(row=row, column=column, value=safe_value)
    if isinstance(safe_value, str):
        cell.data_type = "s"


def _build_processed_workbook(headers: list[str], rows: list[list[Any]]) -> Workbook:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Processed records"
    for column, value in enumerate(headers, start=1):
        _write_safe_cell(sheet, 1, column, value)
    for row_number, row in enumerate(rows, start=2):
        for column, value in enumerate(row[:len(headers)], start=1):
            _write_safe_cell(sheet, row_number, column, value)
    return workbook


def _build_validation_workbook(
    headers: list[str], invalid: list[tuple[int, list[Any], list[str], list[dict[str, object]]]]
) -> Workbook:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Validation report"
    for column, heading in enumerate(["Source row", "Issues", "Original source data (JSON)", "Business rule findings (JSON)"], start=1):
        _write_safe_cell(sheet, 1, column, heading)
    for output_row, (row_number, row, issues, rule_issues) in enumerate(invalid, start=2):
        original = {
            header: _json_value(row[index]) if index < len(row) else None
            for index, header in enumerate(headers)
        }
        if len(row) > len(headers):
            original["_extra_cells"] = [_json_value(value) for value in row[len(headers):]]
        _write_safe_cell(sheet, output_row, 1, row_number)
        _write_safe_cell(sheet, output_row, 2, "; ".join(issues))
        _write_safe_cell(sheet, output_row, 3, json.dumps(original, ensure_ascii=False, default=str))
        _write_safe_cell(sheet, output_row, 4, json.dumps(rule_issues, ensure_ascii=False))
    return workbook


def _save_workbook_atomically(path: Path, workbook: Workbook) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".xlsx", delete=False) as handle:
            temp_path = Path(handle.name)
        workbook.save(temp_path)
        temp_path.replace(path)
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)
