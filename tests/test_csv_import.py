import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.storage.database import initialize_database
from app.storage.repository import InvalidRowDataError, get_imported_rows


def test_valid_csv_upload_persists_job_and_rows(client: TestClient, tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "test.sqlite3"
    monkeypatch.setenv("AUTOMATION_DB_PATH", str(db_path))

    response = client.post(
        "/jobs/csv",
        files={"file": ("customers.csv", "name,email\nAda,ada@example.com\nLin,lin@example.com\n", "text/csv")},
    )

    assert response.status_code == 201
    job = response.json()
    assert job["filename"] == "customers.csv"
    assert job["status"] == "completed"
    assert job["row_count"] == 2

    rows = get_imported_rows(job["id"], str(db_path))
    assert rows == [
        {"name": "Ada", "email": "ada@example.com"},
        {"name": "Lin", "email": "lin@example.com"},
    ]


@pytest.mark.parametrize(
    ("filename", "content", "expected_detail"),
    [
        ("broken.csv", 'name,email\nAda,"unterminated\n', "Could not parse CSV"),
        ("empty.csv", "", "CSV file is empty"),
        ("wrong.txt", "name\nAda\n", ".csv filename"),
        ("invalid.csv", "name,email\n,\n,\n", "non-empty data value"),
    ],
)
def test_invalid_csv_is_rejected(
    client: TestClient, filename: str, content: str, expected_detail: str
) -> None:
    response = client.post(
        "/jobs/csv",
        files={"file": (filename, content, "text/csv")},
    )

    assert response.status_code in (415, 422)
    assert expected_detail in response.json()["detail"]


@pytest.mark.parametrize("content", ["", "\n", "name,email\n"])
def test_missing_required_header_or_data_is_rejected(client: TestClient, content: str) -> None:
    response = client.post(
        "/jobs/csv",
        files={"file": ("missing-data.csv", content, "text/csv")},
    )
    assert response.status_code == 422


def test_job_is_persisted_in_sqlite(client: TestClient, tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "persist.sqlite3"
    monkeypatch.setenv("AUTOMATION_DB_PATH", str(db_path))
    initialize_database(db_path)
    create = client.post(
        "/jobs/csv",
        files={"file": ("one.csv", "item\nwidget\n", "text/csv")},
    )
    assert create.status_code == 201

    connection = sqlite3.connect(db_path)
    try:
        job_count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        row_count = connection.execute("SELECT COUNT(*) FROM imported_rows").fetchone()[0]
    finally:
        connection.close()
    assert (job_count, row_count) == (1, 1)


def test_job_status_can_be_retrieved(client: TestClient) -> None:
    create = client.post(
        "/jobs/csv",
        files={"file": ("status.csv", "task\nbackup\n", "text/csv")},
    )
    job_id = create.json()["id"]

    response = client.get(f"/jobs/{job_id}")
    assert response.status_code == 200
    assert response.json()["id"] == job_id
    assert response.json()["status"] == "completed"
    assert response.json()["row_count"] == 1


def test_unknown_job_returns_404(client: TestClient) -> None:
    response = client.get("/jobs/not-a-real-job")
    assert response.status_code == 404
    assert response.json()["detail"] == "Job not found"


def test_duplicate_source_headers_are_rejected(client: TestClient) -> None:
    response = client.post(
        "/jobs/csv",
        files={"file": ("duplicate.csv", "name,name\nAda,Lovelace\n", "text/csv")},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "CSV column headers must be unique."


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-inf"])
def test_non_finite_numeric_tokens_are_rejected(client: TestClient, value: str) -> None:
    response = client.post(
        "/jobs/csv",
        files={"file": ("non-finite.csv", f"amount\n{value}\n", "text/csv")},
    )

    assert response.status_code == 422
    assert "non-finite numeric value" in response.json()["detail"]


def test_json_serialization_failure_becomes_client_error(client: TestClient, monkeypatch) -> None:
    def fail_serialization(**_: object) -> dict[str, object]:
        raise InvalidRowDataError("CSV values cannot be stored as valid JSON.")

    monkeypatch.setattr("app.engine.csv_import.create_job", fail_serialization)
    response = client.post(
        "/jobs/csv",
        files={"file": ("serializable.csv", "value\nvalid\n", "text/csv")},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "CSV values cannot be stored as valid JSON."


def test_identifier_and_literal_na_values_are_preserved(
    client: TestClient, tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "preserved.sqlite3"
    monkeypatch.setenv("AUTOMATION_DB_PATH", str(db_path))
    initialize_database(db_path)
    response = client.post(
        "/jobs/csv",
        files={"file": ("values.csv", "account_id,label\n001,NA\n", "text/csv")},
    )

    assert response.status_code == 201
    assert get_imported_rows(response.json()["id"], str(db_path)) == [
        {"account_id": "001", "label": "NA"}
    ]
