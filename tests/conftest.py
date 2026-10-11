from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.storage.database import initialize_database


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    db_path = tmp_path / "test.sqlite3"
    output_path = tmp_path / "outputs"
    monkeypatch.setenv("AUTOMATION_DB_PATH", str(db_path))
    monkeypatch.setenv("AUTOMATION_OUTPUT_DIR", str(output_path))
    initialize_database(db_path)
    with TestClient(app) as test_client:
        yield test_client
