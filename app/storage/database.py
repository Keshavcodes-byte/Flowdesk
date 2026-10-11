"""SQLite connection and schema management."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DATABASE_PATH = Path(__file__).resolve().parents[2] / "data" / "automation.db"


def database_path() -> Path:
    """Return the configured database path (or the project-local default)."""
    configured = os.getenv("AUTOMATION_DB_PATH")
    return Path(configured).expanduser() if configured else DEFAULT_DATABASE_PATH


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open a configured SQLite connection and enable foreign-key checks."""
    db_path = Path(path) if path is not None else database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(path: str | Path | None = None) -> None:
    """Create the initial tables when they do not yet exist."""
    connection = connect(path)
    try:
        with connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('completed')),
                    row_count INTEGER NOT NULL CHECK (row_count >= 0),
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS business_jobs (
                    id TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL UNIQUE,
                    source_filename TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('completed')),
                    total_rows INTEGER NOT NULL CHECK (total_rows >= 0),
                    valid_rows INTEGER NOT NULL CHECK (valid_rows >= 0),
                    invalid_rows INTEGER NOT NULL CHECK (invalid_rows >= 0),
                    processed_artifact TEXT,
                    validation_artifact TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
                    created_at TEXT NOT NULL,
                    ruleset_id TEXT,
                    ruleset_version TEXT,
                    ruleset_json TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS imported_rows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    row_number INTEGER NOT NULL CHECK (row_number >= 1),
                    data_json TEXT NOT NULL,
                    UNIQUE (job_id, row_number)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS business_rulesets (
                    ruleset_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    configuration_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Additive execution-history tables. Existing business/job data is untouched.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS automation_executions (
                    execution_id TEXT PRIMARY KEY,
                    workflow_name TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    workflow_version TEXT,
                    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    duration_seconds REAL,
                    error_json TEXT,
                    counts_json TEXT,
                    output_json TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS automation_step_results (
                    execution_id TEXT NOT NULL REFERENCES automation_executions(execution_id) ON DELETE CASCADE,
                    step_index INTEGER NOT NULL CHECK (step_index >= 0),
                    step_id TEXT NOT NULL,
                    step_type TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'skipped')),
                    started_at TEXT,
                    completed_at TEXT,
                    duration_ms INTEGER,
                    error_json TEXT,
                    result_json TEXT,
                    PRIMARY KEY (execution_id, step_index),
                    UNIQUE (execution_id, step_id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_automation_executions_created "
                "ON automation_executions(created_at DESC, execution_id DESC)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS business_artifacts (
                    artifact_name TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL REFERENCES business_jobs(execution_id) ON DELETE CASCADE,
                    artifact_type TEXT NOT NULL CHECK (artifact_type IN ('pdf_report')),
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_business_artifacts_execution "
                "ON business_artifacts(execution_id)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS background_executions (
                    execution_id TEXT PRIMARY KEY REFERENCES automation_executions(execution_id) ON DELETE CASCADE,
                    state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'succeeded', 'failed', 'interrupted')),
                    input_key TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    workflow_json TEXT NOT NULL,
                    rules_json TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    worker_id TEXT,
                    heartbeat_at TEXT,
                    schedule_id TEXT,
                    occurrence_key TEXT,
                    error_code TEXT,
                    UNIQUE (schedule_id, occurrence_key)
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_background_executions_state "
                "ON background_executions(state, created_at)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS automation_schedules (
                    schedule_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    workflow_json TEXT NOT NULL,
                    rules_json TEXT,
                    input_key TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    schedule_type TEXT NOT NULL CHECK (schedule_type IN ('once', 'daily', 'weekly')),
                    time_zone TEXT NOT NULL,
                    run_at TEXT,
                    local_time TEXT,
                    weekday INTEGER,
                    next_run_at TEXT,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_execution_id TEXT,
                    last_run_status TEXT
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_automation_schedules_due "
                "ON automation_schedules(enabled, next_run_at)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS background_worker_slots (
                    slot_id INTEGER PRIMARY KEY,
                    execution_id TEXT NOT NULL UNIQUE REFERENCES background_executions(execution_id) ON DELETE CASCADE
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS scheduler_leases (
                    lease_name TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    lease_until TEXT NOT NULL
                )"""
            )
            # Additive migration: retain all existing business job rows and artifacts.
            existing_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(business_jobs)").fetchall()
            }
            for column in ("ruleset_id", "ruleset_version", "ruleset_json"):
                if column not in existing_columns:
                    connection.execute(f"ALTER TABLE business_jobs ADD COLUMN {column} TEXT")
    finally:
        connection.close()
