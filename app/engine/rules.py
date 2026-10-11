"""Strict versioned JSON schema for declarative business validation rules."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

MAX_RULESET_BYTES = 64 * 1024
MAX_RULES = 50
MAX_ALLOWED_VALUES = 1_000
MAX_TEXT_LENGTH = 256
RULESET_SCHEMA_VERSION = 1
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,31}$")


class RulesetError(ValueError):
    """A declarative rule document is invalid or incompatible with the file."""


@dataclass(frozen=True)
class RuleDefinition:
    rule_id: str
    rule_type: str
    column: str
    allowed_values: tuple[str | int | float | bool, ...] = ()
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    minimum_raw: int | float | None = None
    maximum_raw: int | float | None = None
    trim: bool = True
    case_sensitive: bool = True


@dataclass(frozen=True)
class Ruleset:
    ruleset_id: str
    version: str
    rules: tuple[RuleDefinition, ...]

    def snapshot(self) -> dict[str, object]:
        """Return the validated, normalized configuration used by this run."""
        items: list[dict[str, object]] = []
        for rule in self.rules:
            item: dict[str, object] = {
                "id": rule.rule_id,
                "type": rule.rule_type,
                "column": rule.column,
            }
            if rule.rule_type == "allowed_values":
                item["values"] = list(rule.allowed_values)
            elif rule.rule_type == "numeric_range":
                if rule.minimum_raw is not None:
                    item["min"] = rule.minimum_raw
                if rule.maximum_raw is not None:
                    item["max"] = rule.maximum_raw
            elif rule.rule_type == "duplicate":
                item["trim"] = rule.trim
                item["case_sensitive"] = rule.case_sensitive
            items.append(item)
        return {
            "schema_version": RULESET_SCHEMA_VERSION,
            "ruleset_id": self.ruleset_id,
            "version": self.version,
            "rules": items,
        }


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RulesetError("Ruleset contains a duplicate JSON property.")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise RulesetError("Ruleset numbers must be finite.")


def _finite_bound(value: object, label: str) -> tuple[Decimal, int | float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RulesetError(f"Rule '{label}' must be a finite JSON number.")
    if isinstance(value, float) and not math.isfinite(value):
        raise RulesetError(f"Rule '{label}' must be a finite JSON number.")
    if len(str(value)) > 128:
        raise RulesetError(f"Rule '{label}' is too large.")
    try:
        decimal = Decimal(str(value))
    except InvalidOperation as error:
        raise RulesetError(f"Rule '{label}' must be a finite JSON number.") from error
    if not decimal.is_finite():
        raise RulesetError(f"Rule '{label}' must be a finite JSON number.")
    return decimal, value


def parse_ruleset(raw: str | None) -> Ruleset | None:
    """Parse and strictly validate JSON rules. None/blank means no business rules."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise RulesetError("Ruleset must be provided as JSON text.")
    try:
        encoded_length = len(raw.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise RulesetError("Ruleset must contain valid Unicode text.") from error
    if encoded_length > MAX_RULESET_BYTES:
        raise RulesetError("Ruleset exceeds the 64 KiB configuration limit.")
    if not raw.strip():
        return None
    try:
        document = json.loads(raw, object_pairs_hook=_strict_object, parse_constant=_reject_constant)
    except RulesetError:
        raise
    except (ValueError, UnicodeError, RecursionError) as error:
        raise RulesetError("Ruleset must be valid JSON.") from error
    if not isinstance(document, dict):
        raise RulesetError("Ruleset must be a JSON object.")
    allowed_root = {"schema_version", "ruleset_id", "version", "rules"}
    if set(document) - allowed_root:
        raise RulesetError("Ruleset contains unknown top-level fields.")
    if type(document.get("schema_version")) is not int or document["schema_version"] != RULESET_SCHEMA_VERSION:
        raise RulesetError(f"Ruleset schema_version must be {RULESET_SCHEMA_VERSION}.")
    ruleset_id = document.get("ruleset_id")
    version = document.get("version")
    if not isinstance(ruleset_id, str) or not _IDENTIFIER.fullmatch(ruleset_id):
        raise RulesetError("ruleset_id must be a 1–64 character identifier using letters, digits, '.', '_' or '-'.")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise RulesetError("version must be a 1–32 character version identifier.")
    raw_rules = document.get("rules")
    if not isinstance(raw_rules, list) or len(raw_rules) > MAX_RULES:
        raise RulesetError("rules must be an array containing no more than 50 rules.")

    definitions: list[RuleDefinition] = []
    seen_ids: set[str] = set()
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, dict):
            raise RulesetError("Each rule must be a JSON object.")
        rule_id = raw_rule.get("id")
        rule_type = raw_rule.get("type")
        column = raw_rule.get("column")
        if not isinstance(rule_id, str) or not _IDENTIFIER.fullmatch(rule_id):
            raise RulesetError("Every rule id must be a 1–64 character identifier.")
        if rule_id in seen_ids:
            raise RulesetError("Rule ids must be unique within a ruleset.")
        seen_ids.add(rule_id)
        if not isinstance(column, str) or not column.strip() or len(column) > MAX_TEXT_LENGTH:
            raise RulesetError(f"Rule '{rule_id}' must name a non-empty column of at most 256 characters.")
        if not isinstance(rule_type, str) or rule_type not in {"required", "allowed_values", "numeric_range", "duplicate"}:
            raise RulesetError(f"Rule '{rule_id}' has an unsupported type.")

        common = {"id", "type", "column"}
        if rule_type == "required":
            expected = common
            definition = RuleDefinition(rule_id, rule_type, column)
        elif rule_type == "allowed_values":
            expected = common | {"values"}
            values = raw_rule.get("values")
            if not isinstance(values, list) or not values or len(values) > MAX_ALLOWED_VALUES:
                raise RulesetError(f"Rule '{rule_id}' values must contain between 1 and 1,000 entries.")
            if any(value is None or not (type(value) in {str, int, float, bool}) for value in values):
                raise RulesetError(f"Rule '{rule_id}' values must be non-null JSON scalars.")
            if any(isinstance(value, float) and not math.isfinite(value) for value in values):
                raise RulesetError(f"Rule '{rule_id}' values must be finite.")
            if any(isinstance(value, str) and len(value) > MAX_TEXT_LENGTH for value in values):
                raise RulesetError(f"Rule '{rule_id}' contains a value longer than 256 characters.")
            definition = RuleDefinition(rule_id, rule_type, column, allowed_values=tuple(values))
        elif rule_type == "numeric_range":
            expected = common | {"min", "max"}
            if "min" not in raw_rule and "max" not in raw_rule:
                raise RulesetError(f"Rule '{rule_id}' must define at least one of min or max.")
            minimum = minimum_raw = maximum = maximum_raw = None
            if "min" in raw_rule:
                minimum, minimum_raw = _finite_bound(raw_rule["min"], "min")
            if "max" in raw_rule:
                maximum, maximum_raw = _finite_bound(raw_rule["max"], "max")
            if minimum is not None and maximum is not None and minimum > maximum:
                raise RulesetError(f"Rule '{rule_id}' has a reversed numeric range.")
            definition = RuleDefinition(
                rule_id, rule_type, column,
                minimum=minimum, maximum=maximum,
                minimum_raw=minimum_raw, maximum_raw=maximum_raw,
            )
        else:
            expected = common | {"trim", "case_sensitive"}
            if "trim" not in raw_rule or "case_sensitive" not in raw_rule:
                raise RulesetError(f"Rule '{rule_id}' must explicitly define trim and case_sensitive.")
            trim = raw_rule["trim"]
            case_sensitive = raw_rule["case_sensitive"]
            if type(trim) is not bool or type(case_sensitive) is not bool:
                raise RulesetError(f"Rule '{rule_id}' trim and case_sensitive must be booleans.")
            definition = RuleDefinition(
                rule_id, rule_type, column, trim=trim, case_sensitive=case_sensitive
            )
        if set(raw_rule) - expected:
            raise RulesetError(f"Rule '{rule_id}' contains unknown fields.")
        definitions.append(definition)
    return Ruleset(ruleset_id, version, tuple(definitions))
