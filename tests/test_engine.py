import logging

import pytest

from app.engine.contracts import CsvImportInput, ExecutionStatus
from app.engine.csv_workflow import CsvImportWorkflow
from app.engine.runner import WorkflowRunner
from app.storage.database import initialize_database
from app.storage.repository import create_job


class SqlitePersistence:
    def __init__(self, db_path):
        self.db_path = db_path

    def create_job(self, filename, rows):
        return create_job(filename, rows, str(self.db_path))


def test_csv_workflow_succeeds_and_persists_output(tmp_path, caplog):
    db_path = tmp_path / "workflow.sqlite3"
    initialize_database(db_path)
    runner = WorkflowRunner()
    workflow = CsvImportWorkflow(SqlitePersistence(db_path))

    with caplog.at_level(logging.INFO, logger="app.engine"):
        record = runner.execute(workflow, CsvImportInput("items.csv", [{"item": "widget"}]))

    assert record.status is ExecutionStatus.SUCCEEDED
    assert record.started_at is not None and record.finished_at is not None
    assert record.error_code is None
    assert record.output.row_count == 1
    assert runner.get(record.execution_id) is record
    assert any(getattr(item, "execution_status", None) == "succeeded" for item in caplog.records)


def test_invalid_workflow_input_is_finalized_as_failed_without_persisting():
    class NeverPersist:
        def create_job(self, filename, rows):
            pytest.fail("invalid workflow input must not reach persistence")

    runner = WorkflowRunner()
    record = runner.execute(CsvImportWorkflow(NeverPersist()), CsvImportInput("items.csv", []))

    assert record.status is ExecutionStatus.FAILED
    assert record.finished_at is not None
    assert record.error_code == "rows_required"
    assert record.error_type == "WorkflowInputError"


def test_repository_failure_is_finalized_logged_with_traceback_and_reraised(caplog):
    class BrokenPersistence:
        def create_job(self, filename, rows):
            raise OSError("database unavailable")

    runner = WorkflowRunner()
    workflow = CsvImportWorkflow(BrokenPersistence())
    with caplog.at_level(logging.ERROR, logger="app.engine"):
        with pytest.raises(OSError, match="database unavailable"):
            runner.execute(workflow, CsvImportInput("items.csv", [{"private": "do-not-log"}]))

    record = runner.records()[0]
    assert record.status is ExecutionStatus.FAILED
    assert record.finished_at is not None
    assert record.error_type == "OSError"
    failure = next(item for item in caplog.records if item.getMessage() == "Workflow execution failed")
    assert failure.exc_info is not None
    assert "do-not-log" not in caplog.text
