"""Validated, deterministic transformations for business-file rows."""

from __future__ import annotations

import math
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
from typing import Any, Sequence

from app.engine.contracts import WorkflowInputError

MAX_TRANSFORM_STEPS = 20
MAX_MAPPINGS = 100
MAX_COLUMN_NAME = 256
_ARITHMETIC = {"add", "subtract", "multiply", "divide"}


def validate_transform_config(step_type: str, config: object) -> dict[str, object]:
    """Reject unknown fields and invalid values for one supported operation."""
    if not isinstance(config, dict):
        raise WorkflowInputError("Transformation config must be an object.", "invalid_transformation")
    if step_type == "clean_data":
        if set(config) != {"trim_strings", "remove_blank_rows"}:
            raise WorkflowInputError(
                "clean_data requires trim_strings and remove_blank_rows boolean options.",
                "invalid_transformation",
            )
        if any(type(config[key]) is not bool for key in config):
            raise WorkflowInputError("Cleaning options must be booleans.", "invalid_transformation")
        return dict(config)
    if step_type == "map_columns":
        if set(config) != {"mapping"} or not isinstance(config.get("mapping"), dict):
            raise WorkflowInputError("map_columns requires only a mapping object.", "invalid_transformation")
        mapping = config["mapping"]
        if not mapping or len(mapping) > MAX_MAPPINGS:
            raise WorkflowInputError("Column mapping must contain between 1 and 100 entries.", "invalid_transformation")
        normalized: dict[str, str] = {}
        normalized_sources: set[str] = set()
        for source, target in mapping.items():
            if not isinstance(source, str) or not isinstance(target, str):
                raise WorkflowInputError("Column mapping names must be strings.", "invalid_transformation")
            source_name, target_name = source, target.strip()
            if not source_name.strip() or not target_name or len(source_name) > MAX_COLUMN_NAME or len(target_name) > MAX_COLUMN_NAME:
                raise WorkflowInputError("Mapped column names must contain 1 to 256 characters.", "invalid_transformation")
            source_key = source_name.strip().casefold()
            if source_key in normalized_sources:
                raise WorkflowInputError("Column mapping contains duplicate source names.", "invalid_transformation")
            normalized_sources.add(source_key)
            normalized[source_name] = target_name
        targets = [target.casefold() for target in normalized.values()]
        if len(set(targets)) != len(targets):
            raise WorkflowInputError("Column mappings cannot rename multiple columns to the same name.", "invalid_transformation")
        return {"mapping": normalized}
    if step_type == "calculate_field":
        expected = {"name", "operation", "left_column", "right_column"}
        if set(config) != expected:
            raise WorkflowInputError(
                "calculate_field requires name, operation, left_column, and right_column.",
                "invalid_transformation",
            )
        values = {key: config[key] for key in expected}
        if any(not isinstance(values[key], str) for key in expected):
            raise WorkflowInputError("Calculated field settings must be strings.", "invalid_transformation")
        for key in ("name", "left_column", "right_column"):
            if key == "name":
                values[key] = values[key].strip()
            if not values[key].strip() or len(values[key]) > MAX_COLUMN_NAME:
                raise WorkflowInputError(f"{key} must contain 1 to 256 characters.", "invalid_transformation")
        if values["operation"] not in _ARITHMETIC:
            raise WorkflowInputError("Calculation operation must be add, subtract, multiply, or divide.", "invalid_transformation")
        return values
    raise WorkflowInputError("Unsupported transformation type.", "invalid_transformation")


def validate_pipeline(operations: object) -> list[dict[str, object]]:
    if operations is None:
        return []
    if not isinstance(operations, list) or len(operations) > MAX_TRANSFORM_STEPS:
        raise WorkflowInputError("Transformation pipeline has an invalid number of steps.", "invalid_transformation")
    validated: list[dict[str, object]] = []
    for operation in operations:
        if not isinstance(operation, dict) or set(operation) != {"type", "config"}:
            raise WorkflowInputError("Transformation step has an invalid shape.", "invalid_transformation")
        step_type = operation["type"]
        if step_type not in {"clean_data", "map_columns", "calculate_field"}:
            raise WorkflowInputError("Unsupported transformation type.", "invalid_transformation")
        validated.append({"type": step_type, "config": validate_transform_config(step_type, operation["config"])})
    return validated


def apply_transformations(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    source_row_numbers: Sequence[int],
    operations: object,
) -> tuple[list[str], list[list[Any]], list[int]]:
    """Apply a validated operation list to copies of already-valid source rows."""
    pipeline = validate_pipeline(operations)
    current_headers = list(headers)
    current_rows = [list(row) for row in rows]
    row_numbers = list(source_row_numbers)
    if len(current_rows) != len(row_numbers):
        raise WorkflowInputError("Transformation input row metadata is inconsistent.", "invalid_transformation")

    for operation in pipeline:
        step_type, config = operation["type"], operation["config"]
        if step_type == "clean_data":
            if config["trim_strings"]:
                current_rows = [
                    [value.strip() if isinstance(value, str) else value for value in row]
                    for row in current_rows
                ]
            if config["remove_blank_rows"]:
                kept = [
                    (number, row) for number, row in zip(row_numbers, current_rows, strict=True)
                    if not all(value is None or (isinstance(value, str) and not value.strip()) for value in row)
                ]
                row_numbers = [number for number, _ in kept]
                current_rows = [row for _, row in kept]
            continue

        if step_type == "map_columns":
            mapping = config["mapping"]
            missing = [source for source in mapping if source not in current_headers]
            if missing:
                raise WorkflowInputError(
                    f"Column mapping references missing source column '{missing[0]}'.",
                    "invalid_transformation",
                )
            renamed = [mapping.get(header, header) for header in current_headers]
            normalized = [header.strip().casefold() for header in renamed]
            if len(set(normalized)) != len(normalized):
                raise WorkflowInputError(
                    "Column mapping conflicts with an existing or mapped column name.",
                    "invalid_transformation",
                )
            current_headers = renamed
            continue

        name = config["name"]
        if any(header.strip().casefold() == name.casefold() for header in current_headers):
            raise WorkflowInputError(
                f"Calculated field '{name}' conflicts with an existing column.",
                "invalid_transformation",
            )
        left_index = _column_index(current_headers, config["left_column"])
        right_index = _column_index(current_headers, config["right_column"])
        results: list[Any] = []
        for source_number, row in zip(row_numbers, current_rows, strict=True):
            left = _finite_decimal(row[left_index], config["left_column"], source_number)
            right = _finite_decimal(row[right_index], config["right_column"], source_number)
            try:
                with localcontext() as decimal_context:
                    decimal_context.prec = 50
                    if config["operation"] == "add":
                        result = left + right
                    elif config["operation"] == "subtract":
                        result = left - right
                    elif config["operation"] == "multiply":
                        result = left * right
                    else:
                        if right == 0:
                            raise WorkflowInputError(
                                f"Cannot divide by zero at source row {source_number}.",
                                "invalid_transformation",
                            )
                        result = left / right
            except DecimalException as error:
                raise WorkflowInputError(
                    f"Calculation is not finite at source row {source_number}.",
                    "invalid_transformation",
                ) from error
            try:
                numeric = float(result)
            except OverflowError:
                numeric = math.inf
            if not math.isfinite(numeric):
                raise WorkflowInputError(
                    f"Calculation is not finite at source row {source_number}.",
                    "invalid_transformation",
                )
            results.append(numeric)
        current_headers.append(name)
        current_rows = [row + [value] for row, value in zip(current_rows, results, strict=True)]
    return current_headers, current_rows, row_numbers


def _column_index(headers: Sequence[str], column: str) -> int:
    try:
        return list(headers).index(column)
    except ValueError as error:
        raise WorkflowInputError(
            f"Calculation references missing column '{column}'.", "invalid_transformation"
        ) from error


def _finite_decimal(value: Any, column: str, source_row: int) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise WorkflowInputError(
            f"Column '{column}' must contain a finite number at source row {source_row}.",
            "invalid_transformation",
        )
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise WorkflowInputError(
            f"Column '{column}' must contain a finite number at source row {source_row}.",
            "invalid_transformation",
        ) from None
    if not number.is_finite():
        raise WorkflowInputError(
            f"Column '{column}' must contain a finite number at source row {source_row}.",
            "invalid_transformation",
        )
    return number
