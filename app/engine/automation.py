"""Strict sequential workflow definitions and in-process step execution."""

from __future__ import annotations

import copy
import json
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from app.engine.business_file import BusinessFileInput, BusinessFileWorkflow, BusinessJobPersistence
from app.engine.contracts import ExecutionContext, WorkflowInputError
from app.engine.transformations import MAX_TRANSFORM_STEPS, validate_transform_config
from app.engine.pdf_report import generate_pdf_report, validate_pdf_report_config

logger = logging.getLogger("app.engine")
MAX_DEFINITION_BYTES = 32 * 1024
MAX_WORKFLOW_STEPS = 20
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class WorkflowDefinitionError(ValueError):
    """A workflow definition is malformed or incompatible with its handlers."""


class AutomationStepError(RuntimeError):
    """A step failed unexpectedly; the public message intentionally excludes input data."""

    def __init__(self, execution_id: str, step_id: str, code: str = "step_failed", message: str = "Step execution failed.") -> None:
        super().__init__("Automation workflow step failed.")
        self.execution_id = execution_id
        self.step_id = step_id
        self.code = code
        self.safe_message = message


@dataclass(frozen=True)
class WorkflowStep:
    step_id: str
    step_type: str
    config: Mapping[str, object]
    inputs: Mapping[str, object]


@dataclass(frozen=True)
class WorkflowDefinition:
    workflow_id: str
    version: str
    steps: tuple[WorkflowStep, ...]


@dataclass(frozen=True)
class AutomationRunInput:
    business_file: BusinessFileInput


class StepHandler(Protocol):
    step_type: str

    def validate(
        self,
        step: WorkflowStep,
        prior_step_types: Mapping[str, str],
    ) -> None: ...

    def run(
        self,
        step: WorkflowStep,
        resolved_inputs: Mapping[str, object],
        prior_outputs: Mapping[str, Mapping[str, object]],
        workflow_input: AutomationRunInput,
        context: ExecutionContext,
    ) -> Mapping[str, object]: ...


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkflowDefinitionError("Workflow JSON contains a duplicate property.")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise WorkflowDefinitionError("Workflow numbers must be finite.")


def _is_reference(value: object) -> bool:
    return isinstance(value, dict) and set(value) == {"from_step"}


def parse_workflow_definition(
    raw: str,
    handlers: Mapping[str, StepHandler],
) -> WorkflowDefinition:
    """Parse a small JSON DSL; only registered step handlers can execute."""
    if not isinstance(raw, str):
        raise WorkflowDefinitionError("Workflow definition must be JSON text.")
    try:
        if len(raw.encode("utf-8")) > MAX_DEFINITION_BYTES:
            raise WorkflowDefinitionError("Workflow definition exceeds the 32 KiB limit.")
        document = json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except WorkflowDefinitionError:
        raise
    except (ValueError, UnicodeError, RecursionError) as error:
        raise WorkflowDefinitionError("Workflow definition must be valid JSON.") from error
    if not isinstance(document, dict) or set(document) != {"workflow_id", "version", "steps"}:
        raise WorkflowDefinitionError("Workflow must contain only workflow_id, version, and steps.")
    workflow_id = document["workflow_id"]
    version = document["version"]
    raw_steps = document["steps"]
    if not isinstance(workflow_id, str) or not _IDENTIFIER.fullmatch(workflow_id):
        raise WorkflowDefinitionError("workflow_id must be a 1 to 64 character identifier.")
    if not isinstance(version, str) or not _IDENTIFIER.fullmatch(version):
        raise WorkflowDefinitionError("version must be a 1 to 64 character identifier.")
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= MAX_WORKFLOW_STEPS:
        raise WorkflowDefinitionError("steps must contain between 1 and 20 step definitions.")

    steps: list[WorkflowStep] = []
    prior_step_types: dict[str, str] = {}
    seen_ids: set[str] = set()
    for raw_step in raw_steps:
        if not isinstance(raw_step, dict) or set(raw_step) - {"id", "type", "config", "inputs"}:
            raise WorkflowDefinitionError("Each step must contain only id, type, config, and inputs.")
        step_id, step_type = raw_step.get("id"), raw_step.get("type")
        config, inputs = raw_step.get("config", {}), raw_step.get("inputs", {})
        if not isinstance(step_id, str) or not _IDENTIFIER.fullmatch(step_id):
            raise WorkflowDefinitionError("Every step id must be a 1 to 64 character identifier.")
        if step_id in seen_ids:
            raise WorkflowDefinitionError("Step ids must be unique within a workflow.")
        if not isinstance(step_type, str) or step_type not in handlers:
            raise WorkflowDefinitionError(f"Step '{step_id}' has an unsupported type.")
        if not isinstance(config, dict) or not isinstance(inputs, dict):
            raise WorkflowDefinitionError(f"Step '{step_id}' config and inputs must be JSON objects.")
        _validate_input_value(config)
        _validate_input_value(inputs)
        if not all(isinstance(key, str) for key in inputs):
            raise WorkflowDefinitionError(f"Step '{step_id}' input names must be strings.")
        for value in inputs.values():
            _validate_input_value(value)
            if _is_reference(value):
                source = value["from_step"]
                if not isinstance(source, str) or source not in prior_step_types:
                    raise WorkflowDefinitionError(
                        f"Step '{step_id}' may reference only an earlier step output."
                    )
        step = WorkflowStep(step_id, step_type, config, inputs)
        handlers[step_type].validate(step, prior_step_types)
        seen_ids.add(step_id)
        prior_step_types[step_id] = step_type
        steps.append(step)
    return WorkflowDefinition(workflow_id, version, tuple(steps))


def _validate_input_value(value: object, depth: int = 0) -> None:
    if depth > 8:
        raise WorkflowDefinitionError("Workflow input values are nested too deeply.")
    if value is None or type(value) in {str, int, bool}:
        if isinstance(value, str) and len(value) > 8192:
            raise WorkflowDefinitionError("Workflow input strings cannot exceed 8 KiB.")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WorkflowDefinitionError("Workflow input numbers must be finite.")
        return
    if isinstance(value, list):
        if len(value) > 100:
            raise WorkflowDefinitionError("Workflow input arrays cannot exceed 100 values.")
        for item in value:
            _validate_input_value(item, depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 100 or not all(isinstance(key, str) for key in value):
            raise WorkflowDefinitionError("Workflow input objects have an invalid shape.")
        # Reference objects are validated by the caller; other values are literals.
        for key, item in value.items():
            if len(key) > 256:
                raise WorkflowDefinitionError("Workflow input keys cannot exceed 256 characters.")
            _validate_input_value(item, depth + 1)
        return
    raise WorkflowDefinitionError("Workflow inputs must be JSON values.")


class AutomationEngine:
    """Sequentially execute a validated definition with per-run output isolation."""

    def __init__(self, handlers: Mapping[str, StepHandler]) -> None:
        self._handlers = dict(handlers)

    def parse_definition(self, raw: str) -> WorkflowDefinition:
        return parse_workflow_definition(raw, self._handlers)

    def run(
        self,
        definition: WorkflowDefinition,
        workflow_input: AutomationRunInput,
        context: ExecutionContext,
    ) -> dict[str, object]:
        outputs: dict[str, dict[str, object]] = {}
        results: list[dict[str, object]] = []
        context.step_results.clear()
        for index, step in enumerate(definition.steps):
            started = time.perf_counter()
            started_at = datetime.now(timezone.utc).isoformat()
            current_result: dict[str, object] = {
                "step_id": step.step_id,
                "step_type": step.step_type,
                "status": "running",
                "started_at": started_at,
                "completed_at": None,
                "duration_ms": None,
            }
            results.append(current_result)
            context.step_results[:] = results
            logger.info("Workflow step started", extra={
                "execution_id": context.execution_id,
                "workflow_id": definition.workflow_id,
                "step_id": step.step_id,
                "step_type": step.step_type,
                "step_status": "running",
            })
            try:
                if context.step_result_callback:
                    context.step_result_callback(list(results))
                resolved = {
                    key: copy.deepcopy(outputs[value["from_step"]])
                    if _is_reference(value) else copy.deepcopy(value)
                    for key, value in step.inputs.items()
                }
                output = self._handlers[step.step_type].run(
                    step, resolved, copy.deepcopy(outputs), workflow_input, context
                )
                if not isinstance(output, Mapping) or not all(isinstance(key, str) for key in output):
                    raise TypeError("Step handler must return an object with string keys.")
                outputs[step.step_id] = copy.deepcopy(dict(output))
            except WorkflowInputError as error:
                result = self._failed_step(step, error.code, str(error), type(error).__name__, started, started_at)
                results[-1] = result
                self._skip_remaining(definition.steps[index + 1:], results)
                context.step_results[:] = results
                if context.step_result_callback:
                    context.step_result_callback(list(results))
                logger.warning("Workflow step rejected input", extra={
                    "execution_id": context.execution_id,
                    "workflow_id": definition.workflow_id,
                    "step_id": step.step_id,
                    "error_code": error.code,
                })
                raise
            except Exception as error:
                safe_code = getattr(error, "safe_error_code", "step_failed")
                safe_message = getattr(error, "safe_error_message", "Step execution failed.")
                if not isinstance(safe_code, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", safe_code):
                    safe_code = "step_failed"
                if not isinstance(safe_message, str) or len(safe_message) > 500:
                    safe_message = "Step execution failed."
                result = self._failed_step(
                    step, safe_code, safe_message, type(error).__name__, started, started_at
                )
                results[-1] = result
                self._skip_remaining(definition.steps[index + 1:], results)
                context.step_results[:] = results
                if context.step_result_callback:
                    context.step_result_callback(list(results))
                logger.error("Workflow step failed", extra={
                    "execution_id": context.execution_id,
                    "workflow_id": definition.workflow_id,
                    "step_id": step.step_id,
                    "step_type": step.step_type,
                    "error_type": type(error).__name__,
                })
                raise AutomationStepError(context.execution_id, step.step_id, safe_code, safe_message) from None

            duration_ms = max(0, round((time.perf_counter() - started) * 1000))
            results[-1] = {
                "step_id": step.step_id,
                "step_type": step.step_type,
                "status": "succeeded",
                "started_at": started_at,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "duration_ms": duration_ms,
                "output": outputs[step.step_id],
            }
            context.step_results[:] = results
            if context.step_result_callback:
                context.step_result_callback(list(results))
            logger.info("Workflow step finalized", extra={
                "execution_id": context.execution_id,
                "workflow_id": definition.workflow_id,
                "step_id": step.step_id,
                "step_type": step.step_type,
                "step_status": "succeeded",
                "duration_ms": duration_ms,
            })
        return {
            "workflow_id": definition.workflow_id,
            "version": definition.version,
            "outputs": outputs,
        }

    @staticmethod
    def _failed_step(
        step: WorkflowStep,
        code: str,
        message: str,
        error_type: str,
        started: float,
        started_at: str,
    ) -> dict[str, object]:
        completed_at = datetime.now(timezone.utc).isoformat()
        return {
            "step_id": step.step_id,
            "step_type": step.step_type,
            "status": "failed",
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_ms": max(0, round((time.perf_counter() - started) * 1000)),
            "error": {"code": code, "message": message, "type": error_type},
        }

    @staticmethod
    def _skip_remaining(steps: tuple[WorkflowStep, ...], results: list[dict[str, object]]) -> None:
        results.extend({
            "step_id": step.step_id,
            "step_type": step.step_type,
            "status": "skipped",
            "started_at": None,
            "completed_at": None,
            "duration_ms": 0,
            "error": {"code": "dependency_failed", "message": "An earlier step failed."},
        } for step in steps)


class ConfiguredAutomationWorkflow:
    """Adapter that runs declarative definitions through the existing WorkflowRunner."""

    name = "automation_workflow"

    def __init__(self, raw_definition: str, engine: AutomationEngine) -> None:
        self._raw_definition = raw_definition
        self._engine = engine

    def execution_metadata(self) -> tuple[str, str | None]:
        """Expose validated identity to the runner while leaving errors for execution."""
        try:
            definition = self._engine.parse_definition(self._raw_definition)
        except WorkflowDefinitionError:
            return self.name, None
        return definition.workflow_id, definition.version

    def run(self, workflow_input: AutomationRunInput, context: ExecutionContext) -> dict[str, object]:
        try:
            definition = self._engine.parse_definition(self._raw_definition)
        except WorkflowDefinitionError as error:
            raise WorkflowInputError(str(error), "invalid_workflow") from error
        return self._engine.run(definition, workflow_input, context)


class BusinessFileStepHandler:
    step_type = "business_file"

    def __init__(self, persistence: BusinessJobPersistence, output_dir: str | Path) -> None:
        self._persistence = persistence
        self._output_dir = output_dir

    def validate(self, step: WorkflowStep, prior_step_types: Mapping[str, str]) -> None:
        if step.config or set(step.inputs) - {"transformations"}:
            raise WorkflowDefinitionError("business_file accepts only a transformations reference and no config.")
        has_prior_transform = any(value in TRANSFORMATION_STEP_TYPES for value in prior_step_types.values())
        if has_prior_transform and "transformations" not in step.inputs:
            raise WorkflowDefinitionError("business_file must consume the preceding transformation pipeline.")
        if "transformations" in step.inputs:
            reference = step.inputs["transformations"]
            source = reference.get("from_step") if _is_reference(reference) else None
            latest_transform = next(
                (step_id for step_id, kind in reversed(tuple(prior_step_types.items()))
                 if kind in TRANSFORMATION_STEP_TYPES),
                None,
            )
            if not isinstance(source, str) or source != latest_transform:
                raise WorkflowDefinitionError("transformations must reference the preceding transformation step.")

    def run(self, step, resolved_inputs, prior_outputs, workflow_input, context):
        pipeline: object = None
        if "transformations" in resolved_inputs:
            reference = resolved_inputs["transformations"]
            pipeline = reference.get("operations") if isinstance(reference, Mapping) else None
        output = BusinessFileWorkflow(self._persistence, self._output_dir).run(
            workflow_input.business_file, context, transformations=pipeline
        )
        return output.as_dict()


TRANSFORMATION_STEP_TYPES = {"clean_data", "map_columns", "calculate_field"}


class TransformationStepHandler:
    """Compose one safe declarative operation for the business-file processor."""

    def __init__(self, step_type: str) -> None:
        if step_type not in TRANSFORMATION_STEP_TYPES:
            raise ValueError("Unsupported transformation step type.")
        self.step_type = step_type

    def validate(self, step: WorkflowStep, prior_step_types: Mapping[str, str]) -> None:
        try:
            validate_transform_config(self.step_type, step.config)
        except WorkflowInputError as error:
            raise WorkflowDefinitionError(str(error)) from error
        if any(value in {"business_file", "business_file_summary"} for value in prior_step_types.values()):
            raise WorkflowDefinitionError("Transformation steps must precede business_file processing.")
        if set(step.inputs) - {"previous"}:
            raise WorkflowDefinitionError(f"{self.step_type} accepts only a previous transformation reference.")
        has_prior_transform = any(value in TRANSFORMATION_STEP_TYPES for value in prior_step_types.values())
        if has_prior_transform and "previous" not in step.inputs:
            raise WorkflowDefinitionError("Each transformation must consume the preceding transformation output.")
        if "previous" in step.inputs:
            reference = step.inputs["previous"]
            source = reference.get("from_step") if _is_reference(reference) else None
            latest_transform = next(
                (step_id for step_id, kind in reversed(tuple(prior_step_types.items()))
                 if kind in TRANSFORMATION_STEP_TYPES),
                None,
            )
            if not isinstance(source, str) or source != latest_transform:
                raise WorkflowDefinitionError("previous must reference the preceding transformation step.")

    def run(self, step, resolved_inputs, prior_outputs, workflow_input, context):
        previous = resolved_inputs.get("previous")
        if previous is None:
            operations: list[dict[str, object]] = []
        elif isinstance(previous, Mapping) and isinstance(previous.get("operations"), list):
            operations = list(previous["operations"])
        else:
            raise WorkflowInputError("Previous transformation output is invalid.", "invalid_transformation")
        if len(operations) >= MAX_TRANSFORM_STEPS:
            raise WorkflowInputError("Transformation pipeline cannot exceed 20 steps.", "invalid_transformation")
        operations.append({"type": self.step_type, "config": dict(step.config)})
        return {"operations": operations}


class BusinessFileSummaryStepHandler:
    step_type = "business_file_summary"

    def validate(self, step: WorkflowStep, prior_step_types: Mapping[str, str]) -> None:
        if step.config or set(step.inputs) != {"result"}:
            raise WorkflowDefinitionError(
                "business_file_summary requires only a result reference and no config."
            )
        reference = step.inputs["result"]
        if not _is_reference(reference) or prior_step_types.get(reference.get("from_step")) != "business_file":
            raise WorkflowDefinitionError("result must reference an earlier business_file step.")

    def run(self, step, resolved_inputs, prior_outputs, workflow_input, context):
        result = resolved_inputs["result"]
        if not isinstance(result, Mapping):
            raise TypeError("Business-file result must be an object.")
        return {
            key: result[key]
            for key in ("id", "execution_id", "status", "total_rows", "valid_rows", "invalid_rows", "artifacts")
            if key in result
        }


class PDFReportStepHandler:
    """Generate a summary-only PDF from a preceding business_file step result."""

    step_type = "pdf_report"

    def __init__(self, output_dir: str | Path, register_artifact) -> None:
        self._output_dir = Path(output_dir)
        self._register_artifact = register_artifact

    def validate(self, step: WorkflowStep, prior_step_types: Mapping[str, str]) -> None:
        try:
            validate_pdf_report_config(step.config)
        except ValueError as error:
            raise WorkflowDefinitionError(str(error)) from error
        if set(step.inputs) != {"result"}:
            raise WorkflowDefinitionError("pdf_report requires only a result reference and no other inputs.")
        reference = step.inputs["result"]
        source = reference.get("from_step") if _is_reference(reference) else None
        if not isinstance(source, str) or prior_step_types.get(source) != "business_file":
            raise WorkflowDefinitionError("pdf_report result must reference an earlier business_file step.")

    def run(self, step, resolved_inputs, prior_outputs, workflow_input, context):
        result = resolved_inputs.get("result")
        if not isinstance(result, Mapping):
            raise WorkflowInputError("PDF report input is not a business-file result.", "invalid_pdf_input")
        title, include_findings = validate_pdf_report_config(step.config)
        root = self._output_dir.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid.uuid4().hex}_report.pdf"
        path = (root / filename).resolve()
        if path.parent != root:
            raise WorkflowInputError("PDF report output path is invalid.", "invalid_pdf_output")
        created = False
        try:
            stream = path.open("xb")
            created = True
            with stream:
                metadata = generate_pdf_report(
                    stream,
                    title=title,
                    include_findings=include_findings,
                    result=result,
                    context=context,
                )
            self._register_artifact(
                execution_id=context.execution_id,
                artifact_name=filename,
                artifact_type="pdf_report",
            )
        except Exception:
            if created:
                path.unlink(missing_ok=True)
            raise
        return {
            **metadata,
            "filename": filename,
            "artifacts": {"pdf_report": f"/artifacts/{filename}"},
        }
