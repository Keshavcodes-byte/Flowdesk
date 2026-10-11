"""HTTP endpoints for CSV import jobs."""

import os
import re
from pathlib import Path

from fastapi import APIRouter, Body, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse

from app.engine.automation import (
    AutomationEngine,
    AutomationRunInput,
    AutomationStepError,
    BusinessFileStepHandler,
    BusinessFileSummaryStepHandler,
    ConfiguredAutomationWorkflow,
    PDFReportStepHandler,
    StepHandler,
    TransformationStepHandler,
    WorkflowDefinitionError,
    parse_workflow_definition,
)
from app.engine.contracts import ExecutionRecord
from app.engine.api_request import APIRequestStepHandler
from app.engine.email_delivery import EmailDeliveryStepHandler
from app.engine.background import (
    change_schedule_enabled,
    create_schedule_record,
    delete_schedule,
    enqueue_execution,
    get_background_execution,
    get_schedule,
    list_background_executions,
    list_schedules,
    queue_directory,
    store_upload,
    validate_schedule,
)
from app.engine.business_file import (
    MAX_BUSINESS_UPLOAD_BYTES,
    BusinessFileInput,
    BusinessFileWorkflow,
)
from app.engine.csv_import import CsvImportError, import_csv
from app.engine.runner import default_runner
from app.engine.rules import RulesetError, parse_ruleset
from app.storage.repository import (
    JobNotFoundError,
    RulesetAlreadyExistsError,
    RulesetNotFoundError,
    create_business_job,
    create_business_ruleset,
    get_business_artifact,
    get_business_ruleset,
    get_job,
    get_automation_execution,
    list_business_rulesets,
    list_automation_executions,
    list_jobs,
    register_business_artifact,
    update_business_ruleset,
)

router = APIRouter()
PROJECT_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "data" / "outputs"


def output_directory() -> Path:
    return Path(os.getenv("AUTOMATION_OUTPUT_DIR", str(PROJECT_OUTPUT_DIR))).expanduser()


def _validate_ruleset_request(payload: dict[str, object]) -> tuple[str, str, str, str]:
    if set(payload) != {"name", "configuration"}:
        raise HTTPException(status_code=422, detail="Request must contain only name and configuration.")
    name = payload.get("name")
    configuration = payload.get("configuration")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
        raise HTTPException(status_code=422, detail="name must contain 1 to 100 characters.")
    if not isinstance(configuration, str):
        raise HTTPException(status_code=422, detail="configuration must be a JSON ruleset string.")
    try:
        parsed = parse_ruleset(configuration)
    except RulesetError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if parsed is None:
        raise HTTPException(status_code=422, detail="configuration must contain a non-empty ruleset.")
    return name.strip(), parsed.ruleset_id, parsed.version, configuration


class _BusinessJobRepository:
    def create_business_job(self, **metadata: object) -> dict[str, object]:
        return create_business_job(**metadata)


def _automation_handlers() -> dict[str, StepHandler]:
    persistence = _BusinessJobRepository()
    return {
        "business_file": BusinessFileStepHandler(persistence, output_directory()),
        "business_file_summary": BusinessFileSummaryStepHandler(),
        "pdf_report": PDFReportStepHandler(output_directory(), register_business_artifact),
        "clean_data": TransformationStepHandler("clean_data"),
        "map_columns": TransformationStepHandler("map_columns"),
        "calculate_field": TransformationStepHandler("calculate_field"),
        "api_request": APIRequestStepHandler(),
        "email_delivery": EmailDeliveryStepHandler(output_directory(), get_business_artifact),
    }


def _validate_automation_definition(workflow_json: str) -> None:
    try:
        parse_workflow_definition(workflow_json, _automation_handlers())
    except WorkflowDefinitionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def _validated_optional_rules(rules: str | None) -> str | None:
    if rules is None or not rules.strip():
        return None
    try:
        parsed = parse_ruleset(rules)
    except RulesetError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return rules if parsed is not None else None


def _save_queue_upload(file: UploadFile, content: bytes) -> tuple[str, str]:
    try:
        return store_upload(file.filename or "", content)
    except OverflowError as error:
        raise HTTPException(status_code=429, detail=str(error)) from error
    except ValueError as error:
        message = str(error)
        code = 413 if "5 MiB" in message else 415 if "extension" in message else 422
        raise HTTPException(status_code=code, detail=message) from error


def _execution_payload(record: ExecutionRecord) -> dict[str, object]:
    output = record.output
    if hasattr(output, "as_dict"):
        output = output.as_dict()
    return {
        "execution_id": record.execution_id,
        "workflow_name": record.workflow_name,
        "workflow_id": record.workflow_id or record.workflow_name,
        "workflow_version": record.workflow_version,
        "created_at": (record.created_at or record.started_at).isoformat(),
        "status": record.status.value,
        "started_at": record.started_at.isoformat(),
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
        "completed_at": record.finished_at.isoformat() if record.finished_at else None,
        "duration_seconds": record.duration_seconds,
        "error": ({
            "code": record.error_code,
            "message": record.error_message,
            "type": record.error_type,
        } if record.error_code else None),
        "counts": record.counts or {},
        "steps": record.step_results,
        "output": output,
    }


def _persisted_execution_payload(record: dict[str, object]) -> dict[str, object]:
    steps = []
    for step in record.get("steps", []):
        steps.append({key: step[key] for key in (
            "step_id", "step_type", "status", "started_at", "completed_at", "duration_ms", "error", "output"
        ) if key in step})
    return {
        "execution_id": record["execution_id"],
        "workflow_name": record["workflow_name"],
        "workflow_id": record["workflow_id"],
        "workflow_version": record["workflow_version"],
        "created_at": record["created_at"],
        "status": record["status"],
        "started_at": record["started_at"],
        "finished_at": record["completed_at"],
        "completed_at": record["completed_at"],
        "duration_seconds": record["duration_seconds"],
        "error": record["error"],
        "counts": record.get("counts") or {},
        "steps": steps,
        "output": record["output"],
    }


@router.get("/jobs")
def read_jobs(
    limit: int = Query(default=10, ge=1, le=100),
) -> dict[str, object]:
    """Return newest persisted job summaries and the total job count."""
    return list_jobs(limit=limit)


@router.post("/rulesets", status_code=status.HTTP_201_CREATED)
def create_ruleset(payload: dict[str, object] = Body(...)) -> dict[str, object]:
    """Validate and save a ruleset without replacing an existing identifier."""
    name, ruleset_id, version, configuration = _validate_ruleset_request(payload)
    try:
        return create_business_ruleset(
            ruleset_id=ruleset_id, name=name, version=version, configuration_json=configuration
        )
    except RulesetAlreadyExistsError as error:
        raise HTTPException(status_code=409, detail="A ruleset with this ruleset_id already exists.") from error


@router.get("/rulesets")
def read_rulesets() -> dict[str, object]:
    """Return saved ruleset metadata for selection in the dashboard."""
    items = list_business_rulesets()
    return {"items": items, "total": len(items)}


@router.get("/rulesets/{ruleset_id}")
def read_ruleset(ruleset_id: str) -> dict[str, object]:
    """Return one saved ruleset, including its exact configuration text."""
    try:
        return get_business_ruleset(ruleset_id)
    except RulesetNotFoundError as error:
        raise HTTPException(status_code=404, detail="Ruleset not found.") from error


@router.put("/rulesets/{ruleset_id}")
def replace_ruleset(ruleset_id: str, payload: dict[str, object] = Body(...)) -> dict[str, object]:
    """Replace an explicitly addressed ruleset after validating the new configuration."""
    name, configured_id, version, configuration = _validate_ruleset_request(payload)
    if configured_id != ruleset_id:
        raise HTTPException(status_code=422, detail="configuration ruleset_id must match the URL ruleset_id.")
    try:
        return update_business_ruleset(
            ruleset_id=ruleset_id, name=name, version=version, configuration_json=configuration
        )
    except RulesetNotFoundError as error:
        raise HTTPException(status_code=404, detail="Ruleset not found.") from error


@router.post("/jobs/csv", status_code=status.HTTP_201_CREATED)
async def create_csv_job(file: UploadFile = File(...)) -> dict[str, object]:
    """Validate a CSV upload, persist its rows, and return its job record."""
    try:
        result = await import_csv(file)
    except CsvImportError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
    finally:
        await file.close()

    return result


@router.post("/workflows/business-file", status_code=status.HTTP_201_CREATED)
async def process_business_file(
    file: UploadFile = File(...),
    rules: str | None = Form(default=None),
) -> dict[str, object]:
    """Validate and process one CSV or XLSX file through the synchronous engine."""
    try:
        content = await file.read(MAX_BUSINESS_UPLOAD_BYTES + 1)
        workflow = BusinessFileWorkflow(_BusinessJobRepository(), output_directory())
        execution = default_runner.execute(
            workflow,
            BusinessFileInput(filename=file.filename or "", content=content, rules_json=rules),
        )
    finally:
        await file.close()

    if execution.status == "failed":
        status_by_code = {"unsupported_file_type": 415, "file_too_large": 413}
        raise HTTPException(
            status_code=status_by_code.get(execution.error_code, 422),
            detail=execution.error_message or "Business file input was rejected.",
        )
    payload = execution.output.as_dict()
    payload["execution_status"] = execution.status.value
    payload["execution_duration_seconds"] = execution.duration_seconds
    return payload


@router.post("/automation/execute", status_code=status.HTTP_201_CREATED)
async def execute_automation(
    file: UploadFile = File(...),
    workflow: str = Form(...),
    rules: str | None = Form(default=None),
) -> JSONResponse:
    """Execute a strict sequential workflow around the existing business-file processor."""
    try:
        content = await file.read(MAX_BUSINESS_UPLOAD_BYTES + 1)
        automation = ConfiguredAutomationWorkflow(
            workflow,
            AutomationEngine(_automation_handlers()),
        )
        try:
            record = default_runner.execute(
                automation,
                AutomationRunInput(BusinessFileInput(file.filename or "", content, rules)),
            )
        except AutomationStepError as error:
            record = default_runner.get(error.execution_id)
            if record is None:
                raise
            return JSONResponse(status_code=500, content=_execution_payload(record))
    finally:
        await file.close()

    payload = _execution_payload(record)
    if record.status.value == "failed":
        return JSONResponse(status_code=422, content=payload)
    return JSONResponse(status_code=201, content=payload)


@router.post("/automation/queue", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_automation(
    file: UploadFile = File(...),
    workflow: str = Form(...),
    rules: str | None = Form(default=None),
) -> dict[str, object]:
    """Persist an upload and enqueue one non-retrying background workflow run."""
    input_key = None
    try:
        content = await file.read(MAX_BUSINESS_UPLOAD_BYTES + 1)
        _validate_automation_definition(workflow)
        rules_json = _validated_optional_rules(rules)
        input_key, original_filename = _save_queue_upload(file, content)
        try:
            return enqueue_execution(
                input_key=input_key, original_filename=original_filename,
                workflow_json=workflow, rules_json=rules_json,
            )
        except OverflowError as error:
            raise HTTPException(status_code=429, detail=str(error)) from error
    finally:
        await file.close()


@router.get("/automation/queue")
def read_automation_queue(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    return list_background_executions(limit, offset)


@router.get("/automation/queue/{execution_id}")
def read_queued_execution(execution_id: str) -> dict[str, object]:
    queue_record = get_background_execution(execution_id)
    if queue_record is None:
        raise HTTPException(status_code=404, detail="Background execution not found.")
    execution = get_automation_execution(execution_id)
    return {**queue_record, "execution": _persisted_execution_payload(execution) if execution else None}


@router.post("/schedules", status_code=status.HTTP_201_CREATED)
async def create_schedule(
    file: UploadFile = File(...),
    workflow: str = Form(...),
    name: str = Form(...),
    schedule_type: str = Form(...),
    time_zone: str = Form(default="UTC"),
    run_at: str | None = Form(default=None),
    local_time_value: str | None = Form(default=None, alias="local_time"),
    weekday_value: str | None = Form(default=None, alias="weekday"),
    rules: str | None = Form(default=None),
) -> dict[str, object]:
    input_key = None
    try:
        content = await file.read(MAX_BUSINESS_UPLOAD_BYTES + 1)
        _validate_automation_definition(workflow)
        rules_json = _validated_optional_rules(rules)
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            raise HTTPException(status_code=422, detail="Schedule name must contain 1 to 100 characters.")
        if len(time_zone) > 64:
            raise HTTPException(status_code=422, detail="time_zone is too long.")
        local_time_value = local_time_value or None
        run_at = run_at or None
        try:
            weekday = int(weekday_value) if weekday_value not in {None, ""} else None
        except ValueError as error:
            raise HTTPException(status_code=422, detail="weekday must be an integer from 0 through 6.") from error
        try:
            normalized_run_at, saved_local_time, saved_weekday, next_run = validate_schedule(
                schedule_type=schedule_type, time_zone=time_zone, run_at=run_at,
                local_time_value=local_time_value, weekday=weekday,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        input_key, original_filename = _save_queue_upload(file, content)
        try:
            return create_schedule_record(
                name=name.strip(), workflow_json=workflow, rules_json=rules_json,
                input_key=input_key, original_filename=original_filename,
                schedule_type=schedule_type, time_zone=time_zone, run_at=normalized_run_at,
                local_time_value=saved_local_time, weekday=saved_weekday, next_run=next_run,
            )
        except OverflowError as error:
            raise HTTPException(status_code=429, detail=str(error)) from error
        except Exception:
            raise
    finally:
        await file.close()


@router.get("/schedules")
def read_schedules() -> dict[str, object]:
    items = list_schedules()
    return {"items": items, "total": len(items)}


@router.post("/schedules/{schedule_id}/enable")
def enable_schedule(schedule_id: str) -> dict[str, object]:
    try:
        result = change_schedule_enabled(schedule_id, True)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if result is None:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    return result


@router.post("/schedules/{schedule_id}/disable")
def disable_schedule(schedule_id: str) -> dict[str, object]:
    result = change_schedule_enabled(schedule_id, False)
    if result is None:
        raise HTTPException(status_code=404, detail="Schedule not found.")
    return result


@router.delete("/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_schedule(schedule_id: str) -> None:
    if not delete_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found.")


@router.get("/executions")
def read_executions(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """List persisted automation executions, newest first."""
    page = list_automation_executions(limit=limit, offset=offset)
    summary_fields = (
        "execution_id", "workflow_name", "workflow_id", "workflow_version", "created_at",
        "status", "started_at", "completed_at", "duration_seconds", "error", "counts",
    )
    items = [_persisted_execution_payload(item) for item in page["items"]]
    return {**page, "items": [{key: item[key] for key in summary_fields} for item in items]}


@router.get("/executions/{execution_id}")
def read_execution(execution_id: str) -> dict[str, object]:
    """Return one persisted workflow execution and ordered step results."""
    persisted = get_automation_execution(execution_id)
    if persisted is not None:
        return _persisted_execution_payload(persisted)
    record = default_runner.get(execution_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Execution not found.")
    return _execution_payload(record)


@router.get("/artifacts/{artifact_name}")
def download_artifact(artifact_name: str) -> FileResponse:
    """Serve only safe artifact basenames registered by a completed business job."""
    if (
        Path(artifact_name).name != artifact_name
        or "/" in artifact_name
        or "\\" in artifact_name
        or not re.fullmatch(r"(?:[0-9a-f-]{36}_(?:processed|validation)\.xlsx|[0-9a-f]{32}_report\.pdf)", artifact_name)
    ):
        raise HTTPException(status_code=404, detail="Artifact not found")
    record = get_business_artifact(artifact_name)
    if record is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    root = output_directory().resolve()
    path = (root / artifact_name).resolve()
    if path.parent != root or not path.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    media_type = "application/pdf" if artifact_name.endswith("_report.pdf") else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return FileResponse(path, filename=artifact_name, media_type=media_type)


@router.get("/jobs/{job_id}")
def read_job(job_id: str) -> dict[str, object]:
    """Return a persisted import job and its row count."""
    try:
        return get_job(job_id)
    except JobNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found") from error
