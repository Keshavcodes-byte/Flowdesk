"""Evaluate validated declarative business rules against parsed source rows."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
from typing import Any

from app.engine.rules import Ruleset, RulesetError


@dataclass(frozen=True)
class BusinessRuleIssue:
    source_row_number: int
    column_name: str
    rule_id: str
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "source_row_number": self.source_row_number,
            "column_name": self.column_name,
            "rule_id": self.rule_id,
            "message": self.message,
        }


def validate_rule_columns(ruleset: Ruleset, headers: list[str]) -> None:
    """Ensure every configured column names an exact source header."""
    available = set(headers)
    missing = sorted({rule.column for rule in ruleset.rules if rule.column not in available})
    if missing:
        # Do not interpolate submitted values into logs; this is a client-facing error.
        raise RulesetError("Ruleset references one or more columns that are not present in the file.")


def evaluate_rules(
    ruleset: Ruleset,
    headers: list[str],
    rows: list[list[Any]],
    source_row_numbers: list[int],
) -> list[BusinessRuleIssue]:
    """Return all findings, preserving rule order and source row numbering.

    Allowed-value comparison is type-sensitive and exact (so integer 1 does not
    match string "1"). Numeric values are parsed from their string form without
    modifying the original source value. Range endpoints are inclusive.
    """
    validate_rule_columns(ruleset, headers)
    if len(rows) != len(source_row_numbers):
        raise ValueError("Rows and source row numbers must have matching lengths.")
    positions = {header: index for index, header in enumerate(headers)}
    findings: list[BusinessRuleIssue] = []
    for rule in ruleset.rules:
        position = positions[rule.column]
        values = [row[position] if position < len(row) else None for row in rows]
        if rule.rule_type == "duplicate":
            groups: dict[str, list[int]] = {}
            for index, value in enumerate(values):
                if _is_blank(value):
                    continue
                normalized = str(value)
                if rule.trim:
                    normalized = normalized.strip()
                if not rule.case_sensitive:
                    normalized = normalized.casefold()
                groups.setdefault(normalized, []).append(index)
            duplicate_indexes = {index for group in groups.values() if len(group) > 1 for index in group}
            for index in sorted(duplicate_indexes):
                findings.append(BusinessRuleIssue(
                    source_row_numbers[index], rule.column, rule.rule_id,
                    "Value duplicates another non-blank value in this file.",
                ))
            continue

        for index, value in enumerate(values):
            message: str | None = None
            if rule.rule_type == "required":
                if _is_blank(value):
                    message = "Value is required."
            elif rule.rule_type == "allowed_values":
                if not any(type(value) is type(candidate) and value == candidate for candidate in rule.allowed_values):
                    message = "Value is not one of the configured allowed values."
            elif rule.rule_type == "numeric_range":
                if _is_blank(value):
                    continue
                numeric = _finite_decimal(value)
                if numeric is None:
                    message = "Value must be a finite number."
                elif rule.minimum is not None and numeric < rule.minimum:
                    message = "Value is below the configured minimum."
                elif rule.maximum is not None and numeric > rule.maximum:
                    message = "Value is above the configured maximum."
            if message:
                findings.append(BusinessRuleIssue(
                    source_row_numbers[index], rule.column, rule.rule_id, message,
                ))
    return findings


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _finite_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None
