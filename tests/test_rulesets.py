import json
import sqlite3

from fastapi.testclient import TestClient

from app.storage.database import initialize_database


def valid_configuration(version="1.0"):
    return json.dumps({
        "schema_version": 1,
        "ruleset_id": "orders-check",
        "version": version,
        "rules": [{"id": "customer-required", "type": "required", "column": "Customer"}],
    })


def test_ruleset_create_list_retrieve_and_update(client: TestClient):
    configuration = valid_configuration()
    created = client.post("/rulesets", json={"name": "Orders", "configuration": configuration})
    assert created.status_code == 201
    saved = created.json()
    assert saved["ruleset_id"] == "orders-check"
    assert saved["name"] == "Orders" and saved["version"] == "1.0"
    assert saved["configuration"] == configuration
    assert saved["created_at"] == saved["updated_at"]

    listing = client.get("/rulesets")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert listing.json()["items"] == [{
        "ruleset_id": "orders-check", "name": "Orders", "version": "1.0",
        "created_at": saved["created_at"], "updated_at": saved["updated_at"],
    }]

    retrieved = client.get("/rulesets/orders-check")
    assert retrieved.status_code == 200
    assert retrieved.json() == saved

    updated_configuration = valid_configuration("2.0")
    updated = client.put("/rulesets/orders-check", json={
        "name": "Orders v2", "configuration": updated_configuration,
    })
    assert updated.status_code == 200
    assert updated.json()["name"] == "Orders v2"
    assert updated.json()["version"] == "2.0"
    assert updated.json()["configuration"] == updated_configuration
    assert updated.json()["created_at"] == saved["created_at"]
    assert updated.json()["updated_at"] >= saved["updated_at"]


def test_duplicate_ruleset_id_is_conflict_and_never_overwrites(client: TestClient):
    first_config = valid_configuration()
    first = client.post("/rulesets", json={"name": "First", "configuration": first_config})
    duplicate = client.post("/rulesets", json={
        "name": "Replacement", "configuration": valid_configuration("3.0"),
    })
    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert "already exists" in duplicate.json()["detail"]
    assert client.get("/rulesets/orders-check").json()["configuration"] == first_config


def test_ruleset_api_rejects_invalid_config_and_invalid_request_shape(client: TestClient):
    invalid_documents = [
        "{not-json",
        json.dumps({"schema_version": 1, "ruleset_id": "bad", "version": "1", "rules": [], "extra": 1}),
    ]
    for configuration in invalid_documents:
        response = client.post("/rulesets", json={"name": "Broken", "configuration": configuration})
        assert response.status_code == 422
    assert client.post("/rulesets", json={"name": "  ", "configuration": valid_configuration()}).status_code == 422
    assert client.post("/rulesets", json={"name": "Okay", "configuration": valid_configuration(), "extra": 1}).status_code == 422
    assert client.get("/rulesets/not-saved").status_code == 404
    assert client.put("/rulesets/not-saved", json={"name": "Missing", "configuration": valid_configuration()}).status_code == 422


def test_update_requires_existing_matching_identifier(client: TestClient):
    client.post("/rulesets", json={"name": "Orders", "configuration": valid_configuration()})
    mismatch = json.dumps({
        "schema_version": 1, "ruleset_id": "another-id", "version": "1", "rules": [],
    })
    response = client.put("/rulesets/orders-check", json={"name": "Wrong ID", "configuration": mismatch})
    assert response.status_code == 422
    absent_config = json.dumps({
        "schema_version": 1, "ruleset_id": "not-saved", "version": "1", "rules": [],
    })
    missing = client.put("/rulesets/not-saved", json={"name": "Missing", "configuration": absent_config})
    assert missing.status_code == 404


def test_saved_ruleset_configuration_applies_through_business_file_workflow(client: TestClient, tmp_path):
    configuration = valid_configuration()
    saved = client.post("/rulesets", json={"name": "Orders", "configuration": configuration}).json()
    selected = client.get(f"/rulesets/{saved['ruleset_id']}").json()

    processed = client.post(
        "/workflows/business-file",
        files={"file": ("orders.csv", "Customer,Amount\nAda,12\n,20\n", "text/csv")},
        data={"rules": selected["configuration"]},
    )
    assert processed.status_code == 201
    result = processed.json()
    assert (result["valid_rows"], result["invalid_rows"]) == (1, 1)
    assert result["rule_issues"] == [{
        "source_row_number": 3,
        "column_name": "Customer",
        "rule_id": "customer-required",
        "message": "Value is required.",
    }]
    connection = sqlite3.connect(tmp_path / "test.sqlite3")
    try:
        stored = connection.execute(
            "SELECT ruleset_id, ruleset_version, ruleset_json FROM business_jobs WHERE id = ?",
            (result["id"],),
        ).fetchone()
    finally:
        connection.close()
    assert stored == ("orders-check", "1.0", configuration)
    artifact = result["artifacts"]["validation_report"].rsplit("/", 1)[-1]
    assert client.get(f"/artifacts/{artifact}").status_code == 200


def test_ruleset_table_migration_preserves_legacy_jobs_and_artifact_metadata(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.executescript("""
            CREATE TABLE jobs (id TEXT PRIMARY KEY, filename TEXT NOT NULL, status TEXT NOT NULL,
              row_count INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE business_jobs (id TEXT PRIMARY KEY, execution_id TEXT NOT NULL UNIQUE,
              source_filename TEXT NOT NULL, status TEXT NOT NULL, total_rows INTEGER NOT NULL,
              valid_rows INTEGER NOT NULL, invalid_rows INTEGER NOT NULL, processed_artifact TEXT,
              validation_artifact TEXT NOT NULL, duration_ms INTEGER NOT NULL, created_at TEXT NOT NULL,
              ruleset_id TEXT, ruleset_version TEXT, ruleset_json TEXT);
            INSERT INTO jobs VALUES ('old-job', 'legacy.csv', 'completed', 1, '2026-01-01');
            INSERT INTO business_jobs VALUES ('old-job', 'old-exec', 'legacy.csv', 'completed', 1, 1, 0,
              'legacy_processed.xlsx', 'legacy_validation.xlsx', 4, '2026-01-01', NULL, NULL, NULL);
        """)
        connection.commit()
    finally:
        connection.close()

    initialize_database(database)
    initialize_database(database)

    connection = sqlite3.connect(database)
    try:
        job = connection.execute("SELECT id, filename, row_count FROM jobs").fetchone()
        business = connection.execute(
            "SELECT id, processed_artifact, validation_artifact FROM business_jobs"
        ).fetchone()
        ruleset_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'business_rulesets'"
        ).fetchone()
    finally:
        connection.close()
    assert job == ("old-job", "legacy.csv", 1)
    assert business == ("old-job", "legacy_processed.xlsx", "legacy_validation.xlsx")
    assert ruleset_table == ("business_rulesets",)


def test_migration_preserves_existing_ruleset_jobs_and_artifact_metadata(tmp_path):
    database = tmp_path / "legacy-with-ruleset.sqlite3"
    configuration = valid_configuration("4.2")
    connection = sqlite3.connect(database)
    try:
        connection.executescript("""
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, status TEXT NOT NULL,
                row_count INTEGER NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE business_jobs (
                id TEXT PRIMARY KEY, execution_id TEXT NOT NULL UNIQUE,
                source_filename TEXT NOT NULL, status TEXT NOT NULL,
                total_rows INTEGER NOT NULL, valid_rows INTEGER NOT NULL,
                invalid_rows INTEGER NOT NULL, processed_artifact TEXT,
                validation_artifact TEXT NOT NULL, duration_ms INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE business_rulesets (
                ruleset_id TEXT PRIMARY KEY, name TEXT NOT NULL, version TEXT NOT NULL,
                configuration_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO jobs VALUES ('legacy-job', 'legacy.csv', 'completed', 2, '2025-01-01');
            INSERT INTO business_jobs VALUES (
                'legacy-job', 'legacy-execution', 'legacy.csv', 'completed', 3, 2, 1,
                'legacy-processed.xlsx', 'legacy-validation.xlsx', 17, '2025-01-01'
            );
            INSERT INTO business_rulesets VALUES (
                'orders-check', 'Legacy orders', '4.2', '{}', '2025-01-02', '2025-01-03'
            );
        """)
        connection.execute(
            "UPDATE business_rulesets SET configuration_json = ? WHERE ruleset_id = ?",
            (configuration, "orders-check"),
        )
        connection.commit()
    finally:
        connection.close()

    initialize_database(database)
    initialize_database(database)

    connection = sqlite3.connect(database)
    try:
        job = connection.execute(
            "SELECT id, filename, row_count, created_at FROM jobs WHERE id = ?", ("legacy-job",)
        ).fetchone()
        business_job = connection.execute(
            """SELECT id, execution_id, source_filename, total_rows, valid_rows, invalid_rows,
                      processed_artifact, validation_artifact, duration_ms, created_at,
                      ruleset_id, ruleset_version, ruleset_json
               FROM business_jobs WHERE id = ?""",
            ("legacy-job",),
        ).fetchone()
        ruleset = connection.execute(
            """SELECT ruleset_id, name, version, configuration_json, created_at, updated_at
               FROM business_rulesets WHERE ruleset_id = ?""",
            ("orders-check",),
        ).fetchone()
        business_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(business_jobs)")
        }
        ruleset_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(business_rulesets)")
        }
    finally:
        connection.close()

    assert job == ("legacy-job", "legacy.csv", 2, "2025-01-01")
    assert business_job == (
        "legacy-job", "legacy-execution", "legacy.csv", 3, 2, 1,
        "legacy-processed.xlsx", "legacy-validation.xlsx", 17, "2025-01-01",
        None, None, None,
    )
    assert ruleset == (
        "orders-check", "Legacy orders", "4.2", configuration, "2025-01-02", "2025-01-03"
    )
    assert {"ruleset_id", "ruleset_version", "ruleset_json"} <= business_columns
    assert {
        "ruleset_id", "name", "version", "configuration_json", "created_at", "updated_at"
    } <= ruleset_columns
