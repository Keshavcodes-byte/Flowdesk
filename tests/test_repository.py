import sqlite3

import pytest

from app.storage.database import initialize_database
from app.storage.repository import InvalidRowDataError, create_job, get_imported_rows, get_job


def test_repository_persists_job_and_rows_atomically(tmp_path) -> None:
    db_path = tmp_path / "repository.sqlite3"
    initialize_database(db_path)

    job = create_job("data.csv", [{"value": "ok"}], str(db_path))

    assert get_job(job["id"], str(db_path)) == job
    assert get_imported_rows(job["id"], str(db_path)) == [{"value": "ok"}]


def test_database_uses_unique_row_numbers(tmp_path) -> None:
    db_path = tmp_path / "constraints.sqlite3"
    initialize_database(db_path)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "INSERT INTO jobs (id, filename, status, row_count, created_at) VALUES (?, ?, ?, ?, ?)",
            ("job", "file.csv", "completed", 1, "now"),
        )
        connection.execute(
            "INSERT INTO imported_rows (job_id, row_number, data_json) VALUES (?, ?, ?)",
            ("job", 1, "{}"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO imported_rows (job_id, row_number, data_json) VALUES (?, ?, ?)",
                ("job", 1, "{}"),
            )
    finally:
        connection.close()


def test_repository_rejects_non_finite_json_values(tmp_path) -> None:
    db_path = tmp_path / "non-finite.sqlite3"
    initialize_database(db_path)

    with pytest.raises(InvalidRowDataError, match="valid JSON"):
        create_job("data.csv", [{"value": float("inf")}], str(db_path))

    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    finally:
        connection.close()
