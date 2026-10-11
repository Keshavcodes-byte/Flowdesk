"""Parameterized SQLite queries for import jobs and their rows."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from app.storage.database import connect, database_path


class JobNotFoundError(LookupError):
    """Raised when a requested job ID is not present in SQLite."""


class InvalidRowDataError(ValueError):
    """Raised when imported row values cannot be represented as strict JSON."""


class RulesetAlreadyExistsError(ValueError):
    """Raised when attempting to create a ruleset with an existing ID."""


class RulesetNotFoundError(LookupError):
    """Raised when a requested saved ruleset does not exist."""


def create_job(
    filename: str,
    rows: list[dict[str, object]],
    db_path: str | None = None,
) -> dict[str, object]:
    """Persist a completed job and all imported rows in one transaction."""
    job_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    target = db_path if db_path is not None else database_path()
    try:
        serialized_rows = [
            (job_id, row_number, json.dumps(row, ensure_ascii=False, allow_nan=False))
            for row_number, row in enumerate(rows, start=1)
        ]
    except (TypeError, ValueError) as error:
        raise InvalidRowDataError("CSV values cannot be stored as valid JSON.") from error

    connection = connect(target)
    try:
        with connection:
            connection.execute(
                "INSERT INTO jobs (id, filename, status, row_count, created_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, filename, "completed", len(rows), created_at),
            )
            connection.executemany(
                "INSERT INTO imported_rows (job_id, row_number, data_json) VALUES (?, ?, ?)",
                serialized_rows,
            )
    finally:
        connection.close()

    return {
        "id": job_id,
        "filename": filename,
        "status": "completed",
        "row_count": len(rows),
        "created_at": created_at,
    }


def get_job(job_id: str, db_path: str | None = None) -> dict[str, object]:
    """Retrieve a job and its row count using a bound SQL parameter."""
    target = db_path if db_path is not None else database_path()
    connection = connect(target)
    try:
        row = connection.execute(
            "SELECT id, filename, status, row_count, created_at FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    finally:
        connection.close()

    if row is None:
        raise JobNotFoundError(job_id)
    return dict(row)


def list_jobs(limit: int = 10, db_path: str | None = None) -> dict[str, object]:
    """Return the newest job summaries and the total number of persisted jobs."""
    target = db_path if db_path is not None else database_path()
    connection = connect(target)
    try:
        rows = connection.execute(
            """
            SELECT id, filename, status, row_count, created_at
            FROM jobs
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        total = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    finally:
        connection.close()

    return {"items": [dict(row) for row in rows], "total": total, "limit": limit}


def get_imported_rows(job_id: str, db_path: str | None = None) -> list[dict[str, object]]:
    """Retrieve stored row payloads, primarily for internal use and tests."""
    target = db_path if db_path is not None else database_path()
    connection = connect(target)
    try:
        rows = connection.execute(
            "SELECT data_json FROM imported_rows WHERE job_id = ? ORDER BY row_number",
            (job_id,),
        ).fetchall()
    finally:
        connection.close()
    return [json.loads(row["data_json"]) for row in rows]


def create_business_job(
    *,
    job_id: str,
    execution_id: str,
    source_filename: str,
    total_rows: int,
    valid_rows: int,
    invalid_rows: int,
    processed_artifact: str | None,
    validation_artifact: str,
    duration_ms: int,
    rows: list[dict[str, object]],
    ruleset_id: str | None = None,
    ruleset_version: str | None = None,
    ruleset_json: str | None = None,
    db_path: str | None = None,
) -> dict[str, object]:
    """Persist validated file processing metadata with bound SQL parameters."""
    created_at = datetime.now(timezone.utc).isoformat()
    target = db_path if db_path is not None else database_path()
    try:
        serialized_rows = [
            (job_id, row_number, json.dumps(row, ensure_ascii=False, allow_nan=False))
            for row_number, row in enumerate(rows, start=1)
        ]
    except (TypeError, ValueError) as error:
        raise InvalidRowDataError("Business rows cannot be stored as valid JSON.") from error
    connection = connect(target)
    try:
        with connection:
            connection.execute(
                "INSERT INTO jobs (id, filename, status, row_count, created_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, source_filename, "completed", valid_rows, created_at),
            )
            connection.executemany(
                "INSERT INTO imported_rows (job_id, row_number, data_json) VALUES (?, ?, ?)",
                serialized_rows,
            )
            connection.execute(
                """INSERT INTO business_jobs
                (id, execution_id, source_filename, status, total_rows, valid_rows, invalid_rows,
                 processed_artifact, validation_artifact, duration_ms, created_at,
                 ruleset_id, ruleset_version, ruleset_json)
                VALUES (?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, execution_id, source_filename, total_rows, valid_rows, invalid_rows,
                 processed_artifact, validation_artifact, duration_ms, created_at,
                 ruleset_id, ruleset_version, ruleset_json),
            )
    finally:
        connection.close()
    return {
        "id": job_id, "execution_id": execution_id, "filename": source_filename,
        "status": "completed", "total_rows": total_rows, "valid_rows": valid_rows,
        "invalid_rows": invalid_rows, "processed_artifact": processed_artifact,
        "validation_artifact": validation_artifact, "duration_ms": duration_ms,
        "created_at": created_at,
        "ruleset_id": ruleset_id,
        "ruleset_version": ruleset_version,
    }


def get_business_artifact(artifact_name: str, db_path: str | None = None) -> dict[str, object] | None:
    """Find a registered business artifact without granting filesystem access by path."""
    target = db_path if db_path is not None else database_path()
    connection = connect(target)
    try:
        row = connection.execute(
            """SELECT id, source_filename, processed_artifact, validation_artifact
            FROM business_jobs WHERE processed_artifact = ? OR validation_artifact = ?""",
            (artifact_name, artifact_name),
        ).fetchone()
        if row is not None:
            return dict(row)
        pdf = connection.execute(
            """SELECT execution_id, artifact_name, artifact_type, created_at
               FROM business_artifacts WHERE artifact_name = ?""",
            (artifact_name,),
        ).fetchone()
    finally:
        connection.close()
    if pdf is None:
        return None
    return {
        "id": pdf["execution_id"],
        "source_filename": None,
        "processed_artifact": None,
        "validation_artifact": None,
        "artifact_name": pdf["artifact_name"],
        "artifact_type": pdf["artifact_type"],
        "created_at": pdf["created_at"],
    }


def register_business_artifact(
    *, execution_id: str, artifact_name: str, artifact_type: str = "pdf_report", db_path: str | None = None
) -> dict[str, object]:
    """Register a generated report only against an existing persisted business job."""
    if artifact_type != "pdf_report" or not re.fullmatch(r"[0-9a-f]{32}_report\.pdf", artifact_name):
        raise ValueError("Artifact metadata is invalid.")
    created_at = datetime.now(timezone.utc).isoformat()
    connection = connect(db_path)
    try:
        with connection:
            job = connection.execute(
                "SELECT execution_id FROM business_jobs WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            if job is None:
                raise JobNotFoundError("Business job for this execution was not found.")
            connection.execute(
                "INSERT INTO business_artifacts (artifact_name, execution_id, artifact_type, created_at) "
                "VALUES (?, ?, ?, ?)",
                (artifact_name, execution_id, artifact_type, created_at),
            )
    finally:
        connection.close()
    return {
        "artifact_name": artifact_name,
        "execution_id": execution_id,
        "artifact_type": artifact_type,
        "created_at": created_at,
    }


def create_business_ruleset(
    *,
    ruleset_id: str,
    name: str,
    version: str,
    configuration_json: str,
    db_path: str | None = None,
) -> dict[str, object]:
    """Create one saved ruleset without replacing an existing identifier."""
    now = datetime.now(timezone.utc).isoformat()
    connection = connect(db_path)
    try:
        try:
            with connection:
                connection.execute(
                    """INSERT INTO business_rulesets
                    (ruleset_id, name, version, configuration_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (ruleset_id, name, version, configuration_json, now, now),
                )
        except sqlite3.IntegrityError as error:
            if "business_rulesets.ruleset_id" in str(error):
                raise RulesetAlreadyExistsError(ruleset_id) from error
            raise
    finally:
        connection.close()
    return {
        "ruleset_id": ruleset_id,
        "name": name,
        "version": version,
        "configuration": configuration_json,
        "created_at": now,
        "updated_at": now,
    }


def list_business_rulesets(db_path: str | None = None) -> list[dict[str, object]]:
    """List saved ruleset metadata in a stable display order."""
    connection = connect(db_path)
    try:
        rows = connection.execute(
            """SELECT ruleset_id, name, version, created_at, updated_at
            FROM business_rulesets ORDER BY name COLLATE NOCASE, ruleset_id"""
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def get_business_ruleset(ruleset_id: str, db_path: str | None = None) -> dict[str, object]:
    """Retrieve saved metadata and the exact validated configuration text."""
    connection = connect(db_path)
    try:
        row = connection.execute(
            """SELECT ruleset_id, name, version, configuration_json, created_at, updated_at
            FROM business_rulesets WHERE ruleset_id = ?""",
            (ruleset_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise RulesetNotFoundError(ruleset_id)
    result = dict(row)
    result["configuration"] = result.pop("configuration_json")
    return result


def update_business_ruleset(
    *,
    ruleset_id: str,
    name: str,
    version: str,
    configuration_json: str,
    db_path: str | None = None,
) -> dict[str, object]:
    """Update an explicitly addressed existing ruleset; never create implicitly."""
    connection = connect(db_path)
    try:
        with connection:
            cursor = connection.execute(
                """UPDATE business_rulesets
                SET name = ?, version = ?, configuration_json = ?, updated_at = ?
                WHERE ruleset_id = ?""",
                (name, version, configuration_json, datetime.now(timezone.utc).isoformat(), ruleset_id),
            )
            if cursor.rowcount == 0:
                raise RulesetNotFoundError(ruleset_id)
    finally:
        connection.close()
    return get_business_ruleset(ruleset_id, db_path)


def save_automation_execution(
    *,
    execution_id: str,
    workflow_name: str,
    workflow_id: str,
    workflow_version: str | None,
    status: str,
    created_at: str,
    started_at: str | None,
    completed_at: str | None,
    duration_seconds: float | None,
    error: dict[str, object] | None,
    counts: dict[str, int] | None,
    output: dict[str, object] | None,
    steps: list[dict[str, object]],
    db_path: str | None = None,
) -> None:
    """Atomically upsert one execution snapshot and its ordered step results."""
    error_json = json.dumps(error, ensure_ascii=False, allow_nan=False) if error else None
    counts_json = json.dumps(counts, ensure_ascii=False, allow_nan=False) if counts else None
    output_json = json.dumps(output, ensure_ascii=False, allow_nan=False) if output is not None else None
    connection = connect(db_path)
    try:
        with connection:
            connection.execute(
                """INSERT INTO automation_executions
                (execution_id, workflow_name, workflow_id, workflow_version, status, created_at,
                 started_at, completed_at, duration_seconds, error_json, counts_json, output_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(execution_id) DO UPDATE SET
                    workflow_name=excluded.workflow_name, workflow_id=excluded.workflow_id,
                    workflow_version=excluded.workflow_version, status=excluded.status,
                    created_at=excluded.created_at, started_at=excluded.started_at,
                    completed_at=excluded.completed_at, duration_seconds=excluded.duration_seconds,
                    error_json=excluded.error_json, counts_json=excluded.counts_json,
                    output_json=excluded.output_json""",
                (execution_id, workflow_name, workflow_id, workflow_version, status, created_at,
                 started_at, completed_at, duration_seconds, error_json, counts_json, output_json),
            )
            for index, step in enumerate(steps):
                step_error = step.get("error")
                step_output = step.get("output")
                connection.execute(
                    """INSERT INTO automation_step_results
                    (execution_id, step_index, step_id, step_type, status, started_at, completed_at,
                     duration_ms, error_json, result_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(execution_id, step_index) DO UPDATE SET
                        step_id=excluded.step_id, step_type=excluded.step_type, status=excluded.status,
                        started_at=excluded.started_at, completed_at=excluded.completed_at,
                        duration_ms=excluded.duration_ms, error_json=excluded.error_json,
                        result_json=excluded.result_json""",
                    (execution_id, index, step["step_id"], step["step_type"], step["status"],
                     step.get("started_at"), step.get("completed_at"), step.get("duration_ms"),
                     json.dumps(step_error, ensure_ascii=False, allow_nan=False) if step_error else None,
                     json.dumps(step_output, ensure_ascii=False, allow_nan=False) if step_output is not None else None),
                )
    finally:
        connection.close()


def _execution_from_row(row: sqlite3.Row, step_rows: list[sqlite3.Row] | None = None) -> dict[str, object]:
    result: dict[str, object] = dict(row)
    for source, target in (("error_json", "error"), ("counts_json", "counts"), ("output_json", "output")):
        value = result.pop(source)
        result[target] = json.loads(value) if value else None
    if step_rows is not None:
        steps = []
        for step_row in step_rows:
            step = dict(step_row)
            for source, target in (("error_json", "error"), ("result_json", "output")):
                value = step.pop(source)
                decoded = json.loads(value) if value else None
                if decoded is not None:
                    step[target] = decoded
            steps.append(step)
        result["steps"] = steps
    return result


def get_automation_execution(execution_id: str, db_path: str | None = None) -> dict[str, object] | None:
    connection = connect(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM automation_executions WHERE execution_id = ?", (execution_id,)
        ).fetchone()
        if row is None:
            return None
        steps = connection.execute(
            """SELECT step_index, step_id, step_type, status, started_at, completed_at,
                      duration_ms, error_json, result_json
               FROM automation_step_results WHERE execution_id = ? ORDER BY step_index""",
            (execution_id,),
        ).fetchall()
    finally:
        connection.close()
    return _execution_from_row(row, steps)


def list_automation_executions(limit: int = 20, offset: int = 0, db_path: str | None = None) -> dict[str, object]:
    connection = connect(db_path)
    try:
        rows = connection.execute(
            """SELECT * FROM automation_executions
               ORDER BY created_at DESC, execution_id DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        total = connection.execute("SELECT COUNT(*) FROM automation_executions").fetchone()[0]
    finally:
        connection.close()
    return {"items": [_execution_from_row(row) for row in rows], "total": total, "limit": limit, "offset": offset}
