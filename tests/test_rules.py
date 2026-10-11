import json
import sqlite3

import pytest

from app.engine.rule_engine import evaluate_rules, validate_rule_columns
from app.engine.rules import RulesetError, parse_ruleset
from app.storage.database import initialize_database


def rules_document(rules):
    return json.dumps({"schema_version": 1, "ruleset_id": "orders", "version": "1.2", "rules": rules})


def test_all_rule_types_and_multiple_findings_preserve_original_rows():
    raw_rows = [[" ", "pending", "-2", " A "], ["ok", "PENDING", "bad", "a"], ["ok", "done", "4", "A"]]
    original_copy = [row[:] for row in raw_rows]
    ruleset = parse_ruleset(rules_document([
        {"id": "required-name", "type": "required", "column": "Name"},
        {"id": "status-values", "type": "allowed_values", "column": "Status", "values": ["pending", "done"]},
        {"id": "amount-range", "type": "numeric_range", "column": "Amount", "min": 0, "max": 5},
        {"id": "unique-ref", "type": "duplicate", "column": "Ref", "trim": True, "case_sensitive": False},
    ]))

    issues = evaluate_rules(ruleset, ["Name", "Status", "Amount", "Ref"], raw_rows, [2, 3, 4])

    assert sorted((item.source_row_number, item.rule_id) for item in issues) == sorted([
        (2, "required-name"), (2, "amount-range"), (2, "unique-ref"),
        (3, "status-values"), (3, "amount-range"), (3, "unique-ref"),
        (4, "unique-ref"),
    ])
    required_issue = next(item for item in issues if item.rule_id == "required-name")
    assert required_issue.source_row_number == 2
    assert required_issue.column_name == "Name" and required_issue.message == "Value is required."
    assert raw_rows == original_copy


def test_allowed_values_are_exact_and_type_sensitive():
    ruleset = parse_ruleset(rules_document([
        {"id": "exact", "type": "allowed_values", "column": "Code", "values": [1, "001"]},
    ]))
    issues = evaluate_rules(ruleset, ["Code"], [["1"], ["001"], [1]], [2, 3, 4])
    assert [item.source_row_number for item in issues] == [2]


@pytest.mark.parametrize("value", ["nan", "Infinity", "not-a-number"])
def test_numeric_range_rejects_non_finite_and_unparseable_values(value):
    ruleset = parse_ruleset(rules_document([
        {"id": "finite", "type": "numeric_range", "column": "Amount", "min": 0},
    ]))
    issues = evaluate_rules(ruleset, ["Amount"], [[value]], [8])
    assert len(issues) == 1
    assert issues[0].message == "Value must be a finite number."


def test_numeric_range_bounds_are_inclusive_and_numeric_strings_are_parsed():
    ruleset = parse_ruleset(rules_document([
        {"id": "range", "type": "numeric_range", "column": "Amount", "min": 1.5, "max": 3},
    ]))
    assert evaluate_rules(ruleset, ["Amount"], [["1.5"], [3]], [2, 3]) == []
    assert [issue.source_row_number for issue in evaluate_rules(ruleset, ["Amount"], [["1"], ["4"]], [4, 5])] == [4, 5]


def test_duplicate_rule_marks_every_participant_with_explicit_normalization():
    ruleset = parse_ruleset(rules_document([
        {"id": "dupes", "type": "duplicate", "column": "Ref", "trim": True, "case_sensitive": False},
    ]))
    issues = evaluate_rules(ruleset, ["Ref"], [["A"], [" a "], ["B"], ["  "]], [2, 3, 4, 5])
    assert [item.source_row_number for item in issues] == [2, 3]
    assert all(item.rule_id == "dupes" and item.column_name == "Ref" for item in issues)


def test_duplicate_rule_can_keep_whitespace_and_case_significant():
    ruleset = parse_ruleset(rules_document([
        {"id": "dupes", "type": "duplicate", "column": "Ref", "trim": False, "case_sensitive": True},
    ]))
    assert evaluate_rules(ruleset, ["Ref"], [["A"], [" a "], ["a"]], [2, 3, 4]) == []


@pytest.mark.parametrize("raw", [
    "{", "[]", '{"schema_version":2,"ruleset_id":"x","version":"1","rules":[]}',
    '{"schema_version":1,"ruleset_id":"x","version":"1","rules":[],"other":true}',
    '{"schema_version":1,"ruleset_id":"x","version":"1","rules":[{"id":"a","type":"script","column":"x"}]}',
    '{"schema_version":1,"ruleset_id":"x","version":"1","rules":[{"id":"a","type":"required","column":"x","extra":true}]}',
    '{"schema_version":1,"ruleset_id":"x","version":"1","rules":[{"id":"a","type":"required","column":"x"},{"id":"a","type":"required","column":"x"}]}',
    '{"schema_version":1,"ruleset_id":"x","version":"1","rules":[{"id":"a","type":"numeric_range","column":"x","min":2,"max":1}]}',
    '{"schema_version":1,"ruleset_id":"x","version":"1","rules":[{"id":"a","type":"numeric_range","column":"x","min":NaN}]}',
    '{"schema_version":1,"ruleset_id":"x","version":"1","rules":[{"id":"a","type":"duplicate","column":"x","trim":"yes"}]}',
    '{"schema_version":1,"ruleset_id":"x","version":"1","rules":[{"id":"a","type":"duplicate","column":"x","trim":"yes","case_sensitive":true}]}',
    '{"schema_version":1,"ruleset_id":"x","version":"1","rules":[{"id":"a","type":"numeric_range","column":"x","min":true}]}',
    '{"schema_version":1,"ruleset_id":"x","version":"1","rules":[{"id":"a","type":"allowed_values","column":"x","values":[[]]}]}',
    '{"schema_version":1,"schema_version":1,"ruleset_id":"x","version":"1","rules":[]}',
])
def test_malformed_or_unsupported_rulesets_are_rejected(raw):
    with pytest.raises(RulesetError):
        parse_ruleset(raw)


def test_configuration_size_and_rule_count_are_bounded():
    with pytest.raises(RulesetError, match="64 KiB"):
        parse_ruleset(" " * (64 * 1024 + 1))
    with pytest.raises(RulesetError, match="50 rules"):
        parse_ruleset(rules_document([{"id": f"r{i}", "type": "required", "column": "x"} for i in range(51)]))


def test_missing_referenced_column_is_rejected():
    ruleset = parse_ruleset(rules_document([{"id": "need-date", "type": "required", "column": "Date"}]))
    with pytest.raises(RulesetError, match="columns"):
        validate_rule_columns(ruleset, ["Invoice"])


def test_absent_or_blank_ruleset_is_backward_compatible():
    assert parse_ruleset(None) is None
    assert parse_ruleset(" \n\t") is None


def test_additive_migration_preserves_existing_business_jobs(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.executescript("""
            CREATE TABLE jobs (id TEXT PRIMARY KEY, filename TEXT NOT NULL, status TEXT NOT NULL,
              row_count INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE business_jobs (id TEXT PRIMARY KEY, execution_id TEXT NOT NULL UNIQUE,
              source_filename TEXT NOT NULL, status TEXT NOT NULL, total_rows INTEGER NOT NULL,
              valid_rows INTEGER NOT NULL, invalid_rows INTEGER NOT NULL, processed_artifact TEXT,
              validation_artifact TEXT NOT NULL, duration_ms INTEGER NOT NULL, created_at TEXT NOT NULL);
            INSERT INTO jobs VALUES ('old-id', 'old.csv', 'completed', 1, '2026-01-01');
            INSERT INTO business_jobs VALUES ('old-id', 'old-exec', 'old.csv', 'completed', 1, 1, 0,
              'old_processed.xlsx', 'old_validation.xlsx', 12, '2026-01-01');
        """)
        connection.commit()
    finally:
        connection.close()

    initialize_database(database)
    initialize_database(database)

    connection = sqlite3.connect(database)
    try:
        row = connection.execute("SELECT id, source_filename, valid_rows, ruleset_id, ruleset_version, ruleset_json FROM business_jobs").fetchone()
        legacy_job = connection.execute("SELECT id, filename, row_count FROM jobs").fetchone()
        columns = {record[1] for record in connection.execute("PRAGMA table_info(business_jobs)")}
    finally:
        connection.close()
    assert row == ("old-id", "old.csv", 1, None, None, None)
    assert legacy_job == ("old-id", "old.csv", 1)
    assert {"ruleset_id", "ruleset_version", "ruleset_json"} <= columns
