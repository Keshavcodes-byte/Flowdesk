"use strict";

const RECENT_LIMIT = 10;
const MAX_FILE_BYTES = 5 * 1024 * 1024;
const fileInput = document.querySelector("#csv-file");
const rulesInput = document.querySelector("#rules-json");
const savedRulesetSelect = document.querySelector("#saved-ruleset-select");
const rulesetNameInput = document.querySelector("#ruleset-name");
const saveRulesetButton = document.querySelector("#save-ruleset-button");
const rulesetMessage = document.querySelector("#ruleset-message");
const uploadForm = document.querySelector("#upload-form");
const uploadButton = document.querySelector("#upload-button");
const uploadMessage = document.querySelector("#upload-message");
const uploadProgress = document.querySelector("#upload-progress");
const progressBar = document.querySelector("#progress-bar");
const progressLabel = document.querySelector("#progress-label");
const progressPercent = document.querySelector("#progress-percent");
const jobsBody = document.querySelector("#jobs-body");
const historyState = document.querySelector("#history-state");
const historyEmpty = document.querySelector("#history-empty");
const historyTable = document.querySelector("#history-table-wrap");
const historyCount = document.querySelector("#history-count");
const apiStatus = document.querySelector("#api-status");
const dropZone = document.querySelector("#file-drop");
const automationForm = document.querySelector("#automation-form");
const automationFile = document.querySelector("#automation-file");
const automationRuleset = document.querySelector("#automation-ruleset");
const apiRequestEnabled = document.querySelector("#api-request-enabled");
const apiRequestOptions = document.querySelector("#api-request-options");
const apiRequestEndpoint = document.querySelector("#api-request-endpoint");
const apiRequestCredentialId = document.querySelector("#api-request-credential-id");
const apiRequestTimeout = document.querySelector("#api-request-timeout");
const apiRequestLimit = document.querySelector("#api-request-limit");
const apiQueryRows = document.querySelector("#api-query-rows");
const addApiQueryButton = document.querySelector("#add-api-query");
const automationRunButton = document.querySelector("#automation-run-button");
const automationBackground = document.querySelector("#automation-background");
const automationProgress = document.querySelector("#automation-progress");
const automationProgressLabel = document.querySelector("#automation-progress-label");
const automationMessage = document.querySelector("#automation-message");
const automationResult = document.querySelector("#automation-result");
const automationResultContent = document.querySelector("#automation-result-content");
const columnMappings = document.querySelector("#column-mappings");
const calculatedFields = document.querySelector("#calculated-fields");
const pdfReportEnabled = document.querySelector("#pdf-report-enabled");
const pdfReportTitle = document.querySelector("#pdf-report-title");
const pdfReportIncludeFindings = document.querySelector("#pdf-report-include-findings");
const emailDeliveryEnabled = document.querySelector("#email-delivery-enabled");
const emailDeliveryOptions = document.querySelector("#email-delivery-options");
const emailConfigFields = {
  smtp_host: document.querySelector("#email-smtp-host"),
  smtp_port: document.querySelector("#email-smtp-port"),
  smtp_security: document.querySelector("#email-smtp-security"),
  sender: document.querySelector("#email-sender"),
  recipient: document.querySelector("#email-recipient"),
  username_env: document.querySelector("#email-username-env"),
  password_env: document.querySelector("#email-password-env"),
  subject: document.querySelector("#email-subject"),
  message: document.querySelector("#email-message"),
  timeout_seconds: document.querySelector("#email-timeout"),
  max_attachment_bytes: document.querySelector("#email-attachment-limit"),
};
const executionHistoryState = document.querySelector("#execution-history-state");
const executionHistoryEmpty = document.querySelector("#execution-history-empty");
const executionHistoryTable = document.querySelector("#execution-history-table-wrap");
const executionHistoryCount = document.querySelector("#execution-history-count");
const executionsBody = document.querySelector("#executions-body");
const executionsPageLabel = document.querySelector("#executions-page-label");
let executionOffset = 0;
let executionTotal = 0;
let transformationRowId = 0;
let isUploading = false;
let isAutomationRunning = false;
let isSavingRuleset = false;
let selectedSavedRulesetId = "";

function formatDate(value, compact = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, compact
    ? { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }
    : { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function pageScrollBehavior() {
  return typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
}

function setActiveNavigation(sectionId) {
  const activeButton = document.querySelector(`.nav-item[data-scroll="${sectionId}"]`);
  if (!activeButton) return;
  document.querySelectorAll(".nav-item[data-scroll]").forEach((item) => {
    item.classList.remove("active");
    item.removeAttribute("aria-current");
  });
  activeButton.classList.add("active");
  activeButton.setAttribute("aria-current", "page");
  const label = activeButton.querySelector("span:not(.planned-tag)")?.textContent?.trim();
  const breadcrumbCurrent = document.querySelector("#breadcrumb-current");
  if (label && breadcrumbCurrent) breadcrumbCurrent.textContent = label;
}

function showUploadMessage(message, kind) {
  uploadMessage.textContent = message;
  uploadMessage.className = `form-message ${kind}`;
  uploadMessage.hidden = false;
}

function showRulesetMessage(message, kind = "") {
  rulesetMessage.textContent = message;
  rulesetMessage.className = `form-message ${kind}`;
  rulesetMessage.hidden = !message;
}

function applySavedRuleset(ruleset) {
  selectedSavedRulesetId = ruleset.ruleset_id;
  savedRulesetSelect.value = ruleset.ruleset_id;
  rulesInput.value = ruleset.configuration;
  rulesInput.readOnly = true;
  saveRulesetButton.disabled = true;
  showRulesetMessage(`Using saved ruleset “${ruleset.name}” (version ${ruleset.version}).`, "message-success");
}

async function fetchSavedRuleset(rulesetId) {
  const response = await fetch(`/rulesets/${encodeURIComponent(rulesetId)}`, {
    headers: { Accept: "application/json" },
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || `Could not load ruleset (${response.status}).`);
  applySavedRuleset(payload);
}

async function loadSavedRulesets(selectedId = "") {
  const response = await fetch("/rulesets", { headers: { Accept: "application/json" } });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || `Could not load rulesets (${response.status}).`);
  const manualOption = document.createElement("option");
  manualOption.value = "";
  manualOption.textContent = "Manual JSON (no saved ruleset)";
  savedRulesetSelect.replaceChildren(manualOption);
  const noRulesOption = document.createElement("option");
  noRulesOption.value = "";
  noRulesOption.textContent = "No business rules (default)";
  automationRuleset.replaceChildren(noRulesOption);
  for (const ruleset of payload.items || []) {
    const option = document.createElement("option");
    option.value = ruleset.ruleset_id;
    option.textContent = `${ruleset.name} · ${ruleset.ruleset_id} · v${ruleset.version}`;
    savedRulesetSelect.append(option);
    const automationOption = document.createElement("option");
    automationOption.value = ruleset.ruleset_id;
    automationOption.textContent = `${ruleset.name} · ${ruleset.ruleset_id} · v${ruleset.version}`;
    automationRuleset.append(automationOption);
  }
  if (selectedId) {
    savedRulesetSelect.value = selectedId;
    await fetchSavedRuleset(selectedId);
  }
}

function setAutomationMessage(message, kind = "") {
  automationMessage.textContent = message;
  automationMessage.className = `form-message ${kind}`;
  automationMessage.hidden = !message;
}

function updateAutomationButton() {
  const file = automationFile.files[0];
  automationRunButton.disabled = isAutomationRunning || !validateSelectedFile(file).valid;
}

function makeAutomationArtifactLinks(artifacts) {
  const links = document.createElement("div");
  links.className = "artifact-links";
  for (const [key, href] of Object.entries(artifacts || {})) {
    if (typeof href !== "string" || !href.startsWith("/artifacts/")) continue;
    const link = document.createElement("a");
    link.href = href;
    link.className = "text-button artifact-link";
    link.setAttribute("download", "");
    link.textContent = key === "processed_workbook" ? "Download processed workbook"
      : key === "pdf_report" ? "Download PDF report" : "Download validation report";
    links.append(link);
  }
  return links;
}

function renderAutomationExecution(payload) {
  automationResultContent.replaceChildren();
  const status = document.createElement("p");
  status.className = "business-counts";
  status.textContent = `Execution ${payload.execution_id || "—"} · ${payload.status || "unknown"}`;
  automationResultContent.append(status);
  if (payload.error?.message) {
    const error = document.createElement("p");
    error.className = "automation-error-detail";
    error.textContent = payload.error.message;
    automationResultContent.append(error);
  }

  const steps = document.createElement("ol");
  steps.className = "automation-steps";
  for (const step of payload.steps || []) {
    const item = document.createElement("li");
    const stepName = document.createElement("strong");
    stepName.textContent = `${step.step_id} (${step.step_type}): ${step.status}`;
    item.append(stepName);
    if (step.error?.message) {
      const detail = document.createElement("span");
      detail.textContent = ` — ${step.error.message}`;
      item.append(detail);
    }
    if (step.step_type === "email_delivery" && step.output?.accepted_by_smtp === true) {
      const delivery = document.createElement("span");
      delivery.textContent = ` - Email accepted by SMTP server with ${step.output.attachment_filename || "the PDF report"}`;
      item.append(delivery);
    }
    steps.append(item);
  }
  if (steps.childElementCount) automationResultContent.append(steps);

  const outputs = payload.output?.outputs || {};
  const summary = Object.values(outputs).find((output) => output && typeof output === "object" && "valid_rows" in output);
  if (summary) {
    const counts = document.createElement("p");
    counts.className = "business-counts";
    counts.textContent = `${summary.valid_rows} valid · ${summary.invalid_rows} with issues · ${summary.total_rows} total rows`;
    automationResultContent.append(counts);
  }
  const artifacts = {};
  for (const output of Object.values(outputs)) {
    if (output && typeof output === "object" && output.artifacts) Object.assign(artifacts, output.artifacts);
  }
  if (Object.keys(artifacts).length) automationResultContent.append(makeAutomationArtifactLinks(artifacts));
  automationResult.hidden = false;
}

function setExecutionHistoryState(message, error = false) {
  executionHistoryState.replaceChildren();
  const text = document.createElement("span");
  text.textContent = message;
  executionHistoryState.append(text);
  executionHistoryState.classList.toggle("error-state", error);
  executionHistoryState.hidden = false;
}

function renderExecutionRow(execution) {
  const row = document.createElement("tr");
  const workflow = document.createElement("td");
  const name = document.createElement("span");
  name.className = "table-file-name";
  name.textContent = `${execution.workflow_id || execution.workflow_name} · v${execution.workflow_version || "—"}`;
  workflow.append(name);
  const statusCell = document.createElement("td");
  const badge = document.createElement("span");
  badge.className = `status-badge status-${execution.status}`;
  badge.textContent = execution.status;
  statusCell.append(badge);
  const created = document.createElement("td");
  created.className = "date-cell";
  created.textContent = formatDate(execution.created_at, true);
  const duration = document.createElement("td");
  duration.className = "numeric-cell";
  duration.textContent = execution.duration_seconds == null ? "—" : `${Number(execution.duration_seconds).toFixed(2)} s`;
  const action = document.createElement("td");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "open-job-button";
  button.dataset.executionId = execution.execution_id;
  button.textContent = "View";
  button.setAttribute("aria-label", `View execution ${execution.execution_id}`);
  action.append(button);
  row.append(workflow, statusCell, created, duration, action);
  return row;
}

async function loadExecutions(offset = executionOffset) {
  executionOffset = Math.max(0, offset);
  executionHistoryTable.hidden = true;
  executionHistoryEmpty.hidden = true;
  setExecutionHistoryState("Loading executions…");
  executionHistoryCount.textContent = "Loading…";
  try {
    const response = await fetch(`/executions?limit=${RECENT_LIMIT}&offset=${executionOffset}`, {
      headers: { Accept: "application/json" },
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || `Could not load executions (${response.status}).`);
    const executions = payload.items || [];
    executionTotal = payload.total || 0;
    executionHistoryCount.textContent = `${executionTotal} total`;
    executionHistoryState.hidden = true;
    if (!executions.length && executionOffset === 0) {
      executionHistoryEmpty.hidden = false;
      return;
    }
    executionsBody.replaceChildren(...executions.map(renderExecutionRow));
    executionHistoryTable.hidden = false;
    const page = Math.floor(executionOffset / RECENT_LIMIT) + 1;
    const pages = Math.max(1, Math.ceil(executionTotal / RECENT_LIMIT));
    executionsPageLabel.textContent = `Page ${page} of ${pages}`;
    document.querySelector("#executions-previous").disabled = executionOffset === 0;
    document.querySelector("#executions-next").disabled = executionOffset + RECENT_LIMIT >= executionTotal;
  } catch (error) {
    executionHistoryCount.textContent = "Unavailable";
    setExecutionHistoryState(error instanceof Error ? error.message : "Could not load execution history.", true);
  }
}

function renderExecutionDetail(execution) {
  const section = document.querySelector("#execution-detail");
  const state = document.querySelector("#execution-detail-state");
  const content = document.querySelector("#execution-detail-content");
  state.textContent = execution.status;
  state.className = `detail-state status-${execution.status}`;
  content.replaceChildren();
  const metadata = document.createElement("div");
  metadata.className = "execution-metadata";
  for (const [label, value] of [
    ["Workflow", `${execution.workflow_id || execution.workflow_name} · v${execution.workflow_version || "—"}`],
    ["Created", formatDate(execution.created_at)], ["Started", formatDate(execution.started_at)],
    ["Completed", formatDate(execution.completed_at || execution.finished_at)],
    ["Duration", execution.duration_seconds == null ? "—" : `${Number(execution.duration_seconds).toFixed(3)} seconds`],
  ]) {
    const item = document.createElement("div");
    const term = document.createElement("span"); term.textContent = label;
    const valueElement = document.createElement("strong"); valueElement.textContent = value;
    item.append(term, valueElement); metadata.append(item);
  }
  content.append(metadata);
  if (execution.error?.message) {
    const error = document.createElement("p"); error.className = "automation-error-detail";
    error.textContent = execution.error.message; content.append(error);
  }
  const list = document.createElement("ol"); list.className = "automation-steps";
  for (const step of execution.steps || []) {
    const item = document.createElement("li");
    const heading = document.createElement("strong");
    heading.textContent = `${step.step_id} (${step.step_type}): ${step.status}`;
    item.append(heading);
    const timing = document.createElement("span");
    timing.textContent = ` · ${formatDate(step.started_at, true)}${step.duration_ms == null ? "" : ` · ${step.duration_ms} ms`}`;
    item.append(timing);
    if (step.error?.message) { const detail = document.createElement("p"); detail.textContent = step.error.message; item.append(detail); }
    if (step.step_type === "email_delivery" && step.output?.accepted_by_smtp === true) {
      const delivery = document.createElement("p");
      delivery.textContent = `SMTP accepted the email with ${step.output.attachment_filename || "the PDF report"} attached.`;
      item.append(delivery);
    }
    const artifacts = step.output?.artifacts;
    if (artifacts) item.append(makeAutomationArtifactLinks(artifacts));
    if (step.step_type === "api_request" && step.output) {
      const apiMetadata = document.createElement("p");
      apiMetadata.textContent = `HTTP ${step.output.status_code ?? "—"} · ${step.output.content_type || "unknown content type"} · ${step.output.response_bytes ?? 0} bytes`;
      item.append(apiMetadata);
      if (Object.prototype.hasOwnProperty.call(step.output, "response")) {
        const response = document.createElement("pre");
        response.className = "api-response-preview";
        response.textContent = JSON.stringify(step.output.response, null, 2);
        item.append(response);
      }
    }
    list.append(item);
  }
  if (list.childElementCount) content.append(list);
  section.hidden = false;
}

document.querySelector("#executions-previous").addEventListener("click", () => loadExecutions(executionOffset - RECENT_LIMIT));
document.querySelector("#executions-next").addEventListener("click", () => loadExecutions(executionOffset + RECENT_LIMIT));

function submitAutomation(formData, endpoint = "/automation/execute") {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", endpoint);
    request.setRequestHeader("Accept", "application/json");
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) {
        const percent = Math.min(100, Math.round((event.loaded / event.total) * 100));
        automationProgressLabel.textContent = percent < 100
          ? `Uploading file… ${percent}%`
          : "File received. Validating and running workflow…";
      }
    });
    request.addEventListener("load", () => {
      let payload;
      try { payload = JSON.parse(request.responseText); }
      catch (_error) { reject(new Error(`The server returned an unreadable response (HTTP ${request.status}).`)); return; }
      if (request.status === 201 || request.status === 202) {
        resolve({ payload, status: request.status });
      } else {
        reject(new Error(payload.detail || payload.error?.message || `Automation failed with HTTP ${request.status}.`));
        renderAutomationExecution(payload);
      }
    });
    request.addEventListener("error", () => reject(new Error("Could not reach the API. Check that the local server is running.")));
    request.send(formData);
  });
}

async function pollBackgroundExecution(executionId) {
  for (let attempt = 0; attempt < 300; attempt += 1) {
    const response = await fetch(`/automation/queue/${encodeURIComponent(executionId)}`, { headers: { Accept: "application/json" } });
    const queueRecord = await response.json();
    if (!response.ok) throw new Error(queueRecord.detail || `Could not read queued execution (${response.status}).`);
    const record = queueRecord.execution;
    if (record) renderAutomationExecution({ ...record, status: queueRecord.state });
    else {
      automationResultContent.replaceChildren();
      const status = document.createElement("p");
      status.className = "business-counts";
      status.textContent = `Execution ${executionId} · ${queueRecord.state}`;
      automationResultContent.append(status);
      automationResult.hidden = false;
    }
    if (["succeeded", "failed", "interrupted"].includes(queueRecord.state)) {
      loadExecutions(0);
      if (queueRecord.state === "succeeded") {
        setAutomationMessage("Background automation completed successfully.", "message-success");
        loadJobs();
      } else {
        setAutomationMessage(`Background automation ${queueRecord.state}. Review execution history for details.`, "message-error");
      }
      return queueRecord.state;
    }
    automationProgressLabel.textContent = `Background execution ${queueRecord.state}…`;
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
  }
  setAutomationMessage(`Execution ${executionId} is still processing. Its persistent status remains available in automation history.`, "message-success");
  return "running";
}

async function buildAutomationFormData(file) {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("workflow", JSON.stringify(buildAutomationWorkflow()));
  if (automationRuleset.value) {
    const response = await fetch(`/rulesets/${encodeURIComponent(automationRuleset.value)}`, { headers: { Accept: "application/json" } });
    const saved = await response.json();
    if (!response.ok) throw new Error(saved.detail || `Could not load ruleset (${response.status}).`);
    if (typeof saved.configuration !== "string" || !saved.configuration.trim()) throw new Error("The selected ruleset has no valid configuration.");
    formData.append("rules", saved.configuration);
  }
  return formData;
}

function labeledInput(container, labelText, placeholder, className) {
  const label = document.createElement("label");
  label.textContent = labelText;
  const input = document.createElement("input");
  input.type = "text";
  input.maxLength = 256;
  input.placeholder = placeholder;
  input.className = className;
  label.append(input);
  container.append(label);
  return input;
}

function addTransformRow(container, kind) {
  transformationRowId += 1;
  const row = document.createElement("div");
  row.className = `transform-row ${kind}-row`;
  if (kind === "mapping") {
    labeledInput(row, "Source column", "Exact source header", "mapping-source");
    labeledInput(row, "Standard name", "New column name", "mapping-target");
  } else {
    labeledInput(row, "New field name", "e.g. Line_Total", "calculation-name");
    labeledInput(row, "First numeric column", "Exact column header", "calculation-left");
    const operationLabel = document.createElement("label");
    operationLabel.textContent = "Operation";
    const operation = document.createElement("select");
    operation.className = "calculation-operation";
    for (const [value, label] of [["multiply", "Multiply ×"], ["add", "Add +"], ["subtract", "Subtract −"], ["divide", "Divide ÷"]]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      operation.append(option);
    }
    operationLabel.append(operation);
    row.append(operationLabel);
    labeledInput(row, "Second numeric column", "Exact column header", "calculation-right");
  }
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "remove-transform-row";
  remove.textContent = "Remove";
  remove.setAttribute("aria-label", `Remove transformation row ${transformationRowId}`);
  remove.addEventListener("click", () => row.remove());
  row.append(remove);
  container.append(row);
}

function addApiQueryRow() {
  const row = document.createElement("div");
  row.className = "transform-row api-query-row";
  labeledInput(row, "Parameter name", "e.g. account_id", "api-query-key");
  labeledInput(row, "Value", "Query value", "api-query-value");
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "remove-transform-row";
  remove.textContent = "Remove";
  remove.addEventListener("click", () => row.remove());
  row.append(remove);
  apiQueryRows.append(row);
}

function buildAutomationWorkflow() {
  const steps = [];
  let previousTransform = "";
  const appendTransform = (id, type, config) => {
    const step = { id, type, config };
    if (previousTransform) step.inputs = { previous: { from_step: previousTransform } };
    steps.push(step);
    previousTransform = id;
  };

  const trimStrings = document.querySelector("#transform-trim").checked;
  const removeBlankRows = document.querySelector("#transform-remove-blank").checked;
  if (trimStrings || removeBlankRows) {
    appendTransform("clean-data", "clean_data", { trim_strings: trimStrings, remove_blank_rows: removeBlankRows });
  }

  const mapping = {};
  const mappingSources = new Set();
  for (const row of columnMappings.querySelectorAll(".mapping-row")) {
    const source = row.querySelector(".mapping-source").value;
    const target = row.querySelector(".mapping-target").value.trim();
    if (!source.trim() && !target) continue;
    if (!source.trim() || !target) throw new Error("Complete both source and standard column names for every mapping.");
    const sourceKey = source.trim().toLowerCase();
    if (mappingSources.has(sourceKey)) throw new Error(`Source column '${source}' is mapped more than once.`);
    mappingSources.add(sourceKey);
    mapping[source] = target;
  }
  if (Object.keys(mapping).length) appendTransform("map-columns", "map_columns", { mapping });

  let calculationNumber = 0;
  for (const row of calculatedFields.querySelectorAll(".calculation-row")) {
    const name = row.querySelector(".calculation-name").value.trim();
    const left = row.querySelector(".calculation-left").value;
    const right = row.querySelector(".calculation-right").value;
    if (!name && !left.trim() && !right.trim()) continue;
    if (!name || !left.trim() || !right.trim()) throw new Error("Complete the new field and both numeric columns for every calculation.");
    calculationNumber += 1;
    appendTransform(`calculate-${calculationNumber}`, "calculate_field", {
      name,
      operation: row.querySelector(".calculation-operation").value,
      left_column: left,
      right_column: right,
    });
  }

  const businessFile = { id: "process", type: "business_file" };
  if (previousTransform) businessFile.inputs = { transformations: { from_step: previousTransform } };
  steps.push(businessFile);
  steps.push({
    id: "summary",
    type: "business_file_summary",
    inputs: { result: { from_step: "process" } },
  });
  if (pdfReportEnabled.checked || emailDeliveryEnabled.checked) {
    const title = pdfReportTitle.value.trim();
    if (!title || title.length > 120 || /[\u0000-\u001f\u007f]/.test(title)) {
      throw new Error("Enter a PDF report title using 1 to 120 printable characters.");
    }
    steps.push({
      id: "pdf-report",
      type: "pdf_report",
      config: { title, include_findings: pdfReportIncludeFindings.checked },
      inputs: { result: { from_step: "process" } },
    });
  }
  if (apiRequestEnabled.checked) {
    const endpoint = apiRequestEndpoint.value.trim();
    let parsedEndpoint;
    try { parsedEndpoint = new URL(endpoint); }
    catch (_error) { throw new Error("Enter a valid public HTTPS endpoint for the API request."); }
    if (parsedEndpoint.protocol !== "https:" || (parsedEndpoint.port && parsedEndpoint.port !== "443") || parsedEndpoint.username || parsedEndpoint.password || parsedEndpoint.search || parsedEndpoint.hash) {
      throw new Error("Use an HTTPS endpoint on port 443 without credentials, query, or fragment.");
    }
    const credentialId = apiRequestCredentialId.value.trim();
    if (credentialId && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(credentialId)) {
      throw new Error("Credential ID must be a valid 1 to 64 character identifier.");
    }
    const queryParams = {};
    for (const row of apiQueryRows.querySelectorAll(".api-query-row")) {
      const key = row.querySelector(".api-query-key").value.trim();
      const value = row.querySelector(".api-query-value").value;
      if (!key && !value) continue;
      if (!key || key.length > 128 || value.length > 2048) throw new Error("Complete each API query parameter with a name and a value.");
      if (Object.prototype.hasOwnProperty.call(queryParams, key)) throw new Error(`API query parameter '${key}' is repeated.`);
      queryParams[key] = value;
    }
    if (Object.keys(queryParams).length > 50) throw new Error("An API request can include at most 50 query parameters.");
    const config = {
      endpoint,
      query_params: queryParams,
      timeout_seconds: Number(apiRequestTimeout.value),
      max_response_bytes: Number(apiRequestLimit.value),
    };
    if (credentialId) config.credential_id = credentialId;
    steps.push({ id: "api-request", type: "api_request", config });
  }
  if (emailDeliveryEnabled.checked) {
    const config = {
      smtp_host: emailConfigFields.smtp_host.value.trim(),
      smtp_port: Number(emailConfigFields.smtp_port.value),
      smtp_security: emailConfigFields.smtp_security.value,
      sender: emailConfigFields.sender.value.trim(),
      recipient: emailConfigFields.recipient.value.trim(),
      subject: emailConfigFields.subject.value.trim(),
      message: emailConfigFields.message.value,
      username_env: emailConfigFields.username_env.value.trim(),
      password_env: emailConfigFields.password_env.value.trim(),
      timeout_seconds: Number(emailConfigFields.timeout_seconds.value),
      max_attachment_bytes: Number(emailConfigFields.max_attachment_bytes.value),
    };
    const emailPattern = /^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$/;
    if (!/^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$/.test(config.smtp_host) || config.smtp_host.includes("..")) {
      throw new Error("Enter a valid SMTP hostname.");
    }
    if (!emailPattern.test(config.sender) || !emailPattern.test(config.recipient)) {
      throw new Error("Enter valid sender and recipient email addresses.");
    }
    if (!/^[A-Z_][A-Z0-9_]{0,127}$/.test(config.username_env) || !/^[A-Z_][A-Z0-9_]{0,127}$/.test(config.password_env) || config.username_env === config.password_env) {
      throw new Error("Enter separate uppercase environment-variable names for SMTP username and password. Never enter secret values.");
    }
    if (!config.subject || config.subject.length > 200 || /[\u0000-\u001f\u007f]/.test(config.subject)) {
      throw new Error("Enter an email subject using 1 to 200 printable characters.");
    }
    if (!config.message || config.message.length > 10000 || config.message.includes("\u0000")) {
      throw new Error("Enter a plain-text email message using 1 to 10000 characters.");
    }
    steps.push({
      id: "email-delivery",
      type: "email_delivery",
      config,
      inputs: { report: { from_step: "pdf-report" } },
    });
  }
  if (steps.length > 20) throw new Error("This workflow exceeds the 20-step limit. Remove a transformation and try again.");
  return { workflow_id: "business-file-processing", version: "1", steps };
}

function updatePDFReportOptions() {
  const enabled = pdfReportEnabled.checked || emailDeliveryEnabled.checked;
  pdfReportTitle.disabled = !enabled;
  pdfReportIncludeFindings.disabled = !enabled;
}

function updateEmailDeliveryOptions() {
  const enabled = emailDeliveryEnabled.checked;
  emailDeliveryOptions.hidden = !enabled;
  for (const control of Object.values(emailConfigFields)) control.disabled = !enabled;
  updatePDFReportOptions();
}

function updateAPIRequestOptions() {
  const enabled = apiRequestEnabled.checked;
  apiRequestOptions.hidden = !enabled;
  for (const control of apiRequestOptions.querySelectorAll("input, select, button")) control.disabled = !enabled;
}

apiRequestEnabled.addEventListener("change", updateAPIRequestOptions);
addApiQueryButton.addEventListener("click", addApiQueryRow);
updateAPIRequestOptions();

pdfReportEnabled.addEventListener("change", updatePDFReportOptions);
emailDeliveryEnabled.addEventListener("change", updateEmailDeliveryOptions);
updateEmailDeliveryOptions();

document.querySelector("#add-column-map").addEventListener("click", () => addTransformRow(columnMappings, "mapping"));
document.querySelector("#add-calculation").addEventListener("click", () => addTransformRow(calculatedFields, "calculation"));

automationFile.addEventListener("change", updateAutomationButton);
automationForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (isAutomationRunning) return;
  const file = automationFile.files[0];
  const validation = validateSelectedFile(file);
  if (!validation.valid) {
    setAutomationMessage(validation.message, "message-error");
    return;
  }

  isAutomationRunning = true;
  updateAutomationButton();
  automationProgress.hidden = false;
  automationProgressLabel.textContent = "Preparing automation…";
  automationResult.hidden = true;
  setAutomationMessage("");
  automationFile.disabled = true;
  automationRuleset.disabled = true;
  try {
    automationProgressLabel.textContent = "Preparing workflow and rules…";
    const formData = await buildAutomationFormData(file);
    const background = automationBackground.checked;
    automationProgressLabel.textContent = "Uploading file…";
    const { payload } = await submitAutomation(formData, background ? "/automation/queue" : "/automation/execute");
    if (background) {
      setAutomationMessage(`Execution ${payload.execution_id} was accepted and queued.`, "message-success");
      automationResult.hidden = false;
      await pollBackgroundExecution(payload.execution_id);
      return;
    }
    renderAutomationExecution(payload);
    loadExecutions(0);
    if (payload.status === "succeeded") {
      setAutomationMessage("Automation completed successfully.", "message-success");
      loadJobs();
    } else {
      setAutomationMessage(payload.error?.message || "Automation failed. Review the step results below.", "message-error");
    }
  } catch (error) {
    setAutomationMessage(error instanceof Error ? error.message : "Automation could not be completed.", "message-error");
  } finally {
    isAutomationRunning = false;
    automationFile.disabled = false;
    automationRuleset.disabled = false;
    automationProgress.hidden = true;
    updateAutomationButton();
  }
});

const scheduleForm = document.querySelector("#schedule-form");
const scheduleType = document.querySelector("#schedule-type");
const scheduleState = document.querySelector("#schedule-state");
const scheduleList = document.querySelector("#schedule-list");
const scheduleMessage = document.querySelector("#schedule-message");
function setScheduleMessage(message, error = false) {
  scheduleMessage.textContent = message;
  scheduleMessage.className = `form-message${error ? " message-error" : " message-success"}`;
  scheduleMessage.hidden = !message;
}
function updateScheduleFields() {
  const recurring = scheduleType.value !== "once";
  document.querySelector("#schedule-run-at-wrap").hidden = recurring;
  document.querySelector("#schedule-local-time-wrap").hidden = !recurring;
  document.querySelector("#schedule-weekday-wrap").hidden = scheduleType.value !== "weekly";
}
scheduleType.addEventListener("change", updateScheduleFields);
updateScheduleFields();

async function loadSchedules() {
  scheduleState.hidden = false;
  scheduleState.textContent = "Loading schedules…";
  scheduleList.replaceChildren();
  try {
    const response = await fetch("/schedules", { headers: { Accept: "application/json" } });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || `Could not load schedules (${response.status}).`);
    scheduleState.textContent = payload.items.length ? `${payload.total} saved schedule(s)` : "No schedules yet.";
    for (const schedule of payload.items) {
      const card = document.createElement("article");
      card.className = "schedule-card";
      const description = document.createElement("p");
      description.textContent = `${schedule.name} · ${schedule.schedule_type} · ${schedule.time_zone} · ${schedule.enabled ? `Next: ${formatDate(schedule.next_run_at)}` : "Disabled"} · Last: ${schedule.last_run_status || "never run"}`;
      const toggle = document.createElement("button");
      toggle.type = "button"; toggle.className = "text-button"; toggle.dataset.scheduleAction = schedule.enabled ? "disable" : "enable"; toggle.dataset.scheduleId = schedule.schedule_id; toggle.textContent = schedule.enabled ? "Disable" : "Enable";
      const remove = document.createElement("button");
      remove.type = "button"; remove.className = "text-button"; remove.dataset.scheduleAction = "delete"; remove.dataset.scheduleId = schedule.schedule_id; remove.textContent = "Delete";
      card.append(description, toggle, remove); scheduleList.append(card);
    }
  } catch (error) {
    scheduleState.textContent = error instanceof Error ? error.message : "Could not load schedules.";
    scheduleState.classList.add("error-state");
  }
}

scheduleForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = automationFile.files[0];
  const validation = validateSelectedFile(file);
  if (!validation.valid) { setScheduleMessage(validation.message, true); return; }
  const button = document.querySelector("#create-schedule");
  button.disabled = true;
  setScheduleMessage("");
  try {
    const formData = await buildAutomationFormData(file);
    formData.append("name", document.querySelector("#schedule-name").value.trim());
    formData.append("schedule_type", scheduleType.value);
    formData.append("time_zone", document.querySelector("#schedule-time-zone").value.trim());
    if (scheduleType.value === "once") formData.append("run_at", document.querySelector("#schedule-run-at").value);
    else formData.append("local_time", document.querySelector("#schedule-local-time").value);
    if (scheduleType.value === "weekly") formData.append("weekday", document.querySelector("#schedule-weekday").value);
    const response = await fetch("/schedules", { method: "POST", body: formData, headers: { Accept: "application/json" } });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || `Could not save schedule (${response.status}).`);
    setScheduleMessage("Schedule saved. The worker will enqueue due runs automatically.");
    await loadSchedules();
  } catch (error) {
    setScheduleMessage(error instanceof Error ? error.message : "Could not save schedule.", true);
  } finally { button.disabled = false; }
});

scheduleList.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-schedule-action]");
  if (!button) return;
  button.disabled = true;
  try {
    const url = `/schedules/${encodeURIComponent(button.dataset.scheduleId)}`;
    const method = button.dataset.scheduleAction === "delete" ? "DELETE" : "POST";
    const endpoint = button.dataset.scheduleAction === "delete" ? url : `${url}/${button.dataset.scheduleAction}`;
    const response = await fetch(endpoint, { method, headers: { Accept: "application/json" } });
    if (!response.ok) { const body = await response.json(); throw new Error(body.detail || `Schedule action failed (${response.status}).`); }
    setScheduleMessage("Schedule updated."); await loadSchedules();
  } catch (error) { setScheduleMessage(error instanceof Error ? error.message : "Schedule action failed.", true); }
  finally { button.disabled = false; }
});

function setApiStatus(online, message) {
  apiStatus.classList.toggle("offline", !online);
  apiStatus.querySelector("span:last-child").textContent = message;
}

function makeDetailRow(label, value, code = false) {
  const row = document.createElement("div");
  row.className = "detail-row";
  const term = document.createElement("span");
  term.className = "detail-term";
  term.textContent = label;
  const description = document.createElement("strong");
  if (code) description.className = "detail-code";
  description.textContent = value == null ? "—" : String(value);
  row.append(term, description);
  return row;
}

function renderJobSummary(job, heading = "Job details") {
  const section = document.querySelector("#job-detail");
  const title = document.querySelector("#detail-title");
  const state = document.querySelector("#detail-state");
  const content = document.querySelector("#detail-content");
  title.textContent = heading;
  state.textContent = job.status || "Unknown status";
  state.className = `detail-state ${job.status === "completed" ? "completed" : ""}`;
  content.replaceChildren(
    makeDetailRow("File", job.filename),
    makeDetailRow("Job ID", job.id, true),
    makeDetailRow("Status", job.status),
    makeDetailRow("Rows imported", job.row_count),
    makeDetailRow("Created", formatDate(job.created_at)),
  );
  section.hidden = false;
}

function renderBusinessResult(result) {
  const container = document.querySelector("#business-result");
  container.replaceChildren();
  container.hidden = false;
  const title = document.createElement("h3");
  title.textContent = "Processing result";
  const counts = document.createElement("p");
  counts.className = "business-counts";
  counts.textContent = `${result.valid_rows} valid · ${result.invalid_rows} with issues · ${result.total_rows} total rows`;
  const links = document.createElement("div");
  links.className = "artifact-links";
  for (const [label, href] of Object.entries(result.artifacts || {})) {
    const link = document.createElement("a");
    link.href = href;
    link.textContent = label === "processed_workbook" ? "Download processed workbook" : "Download validation report";
    link.className = "text-button artifact-link";
    link.setAttribute("download", "");
    links.append(link);
  }
  container.append(title, counts, links);

  if (result.issues?.length) {
    const issueTitle = document.createElement("h4");
    issueTitle.textContent = "Validation issues";
    const list = document.createElement("ul");
    list.className = "validation-issues";
    for (const issue of result.issues) {
      const item = document.createElement("li");
      item.textContent = `Source row ${issue.row_number}: ${(issue.issues || []).join("; ")}`;
      list.append(item);
    }
    container.append(issueTitle, list);
    if (result.issues_truncated) {
      const note = document.createElement("p");
      note.className = "issue-note";
      note.textContent = "Showing the first 100 affected rows. The validation report contains all issues and original row data.";
      container.append(note);
    }
  } else {
    const clean = document.createElement("p");
    clean.className = "issue-note";
    clean.textContent = "No row-level validation issues found.";
    container.append(clean);
  }
  if (result.rule_issues_truncated) {
    const note = document.createElement("p");
    note.className = "issue-note";
    note.textContent = "The response preview contains the first 100 business-rule findings. The validation workbook contains the complete findings and original row data.";
    container.append(note);
  }
}

function renderJob(job) {
  const row = document.createElement("tr");
  const fileCell = document.createElement("td");
  const fileName = document.createElement("span");
  fileName.className = "table-file-name";
  fileName.textContent = job.filename || "Unnamed CSV";
  fileCell.append(fileName);

  const statusCell = document.createElement("td");
  const badge = document.createElement("span");
  badge.className = `status-badge ${job.status === "completed" ? "status-completed" : ""}`;
  badge.textContent = job.status || "Unknown";
  statusCell.append(badge);

  const rowsCell = document.createElement("td");
  rowsCell.className = "numeric-cell";
  rowsCell.textContent = Number(job.row_count || 0).toLocaleString();

  const dateCell = document.createElement("td");
  dateCell.className = "date-cell";
  dateCell.textContent = formatDate(job.created_at, true);

  const actionCell = document.createElement("td");
  const open = document.createElement("button");
  open.type = "button";
  open.className = "open-job-button";
  open.dataset.jobId = job.id;
  open.textContent = "View";
  open.setAttribute("aria-label", `View job ${job.filename || job.id}`);
  actionCell.append(open);
  row.append(fileCell, statusCell, rowsCell, dateCell, actionCell);
  return row;
}

function showHistoryError(message) {
  historyTable.hidden = true;
  historyEmpty.hidden = true;
  historyState.className = "history-state error-state";
  historyState.textContent = message;
  historyState.hidden = false;
  historyCount.textContent = "Unavailable";
  setApiStatus(false, "API unavailable");
}

async function loadJobs() {
  historyState.hidden = false;
  historyState.className = "history-state";
  historyState.replaceChildren();
  const spinner = document.createElement("span");
  spinner.className = "loading-spinner";
  spinner.setAttribute("aria-hidden", "true");
  const label = document.createElement("span");
  label.textContent = "Loading recent jobs…";
  historyState.append(spinner, label);
  historyTable.hidden = true;
  historyEmpty.hidden = true;

  try {
    const response = await fetch(`/jobs?limit=${RECENT_LIMIT}`, { headers: { Accept: "application/json" } });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || `Could not load jobs (${response.status}).`);
    const jobs = Array.isArray(payload.items) ? payload.items : [];
    document.querySelector("#total-jobs").textContent = Number(payload.total || 0).toLocaleString();
    document.querySelector("#recent-rows").textContent = jobs
      .reduce((sum, job) => sum + Number(job.row_count || 0), 0).toLocaleString();
    document.querySelector("#latest-job").textContent = jobs.length ? formatDate(jobs[0].created_at, true) : "No imports yet";
    historyCount.textContent = `${jobs.length} of ${Number(payload.total || 0).toLocaleString()}`;
    setApiStatus(true, "API connected");
    historyState.hidden = true;

    if (jobs.length === 0) {
      historyEmpty.hidden = false;
      return;
    }
    jobsBody.replaceChildren(...jobs.map(renderJob));
    historyTable.hidden = false;
  } catch (error) {
    showHistoryError(error instanceof Error ? error.message : "Could not load recent jobs.");
  }
}

function parseErrorResponse(xhr) {
  try {
    const body = JSON.parse(xhr.responseText);
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) return body.detail.map((item) => item.msg).join("; ");
  } catch (_error) {
    // Use the HTTP status fallback below for non-JSON responses.
  }
  return `Upload failed with HTTP ${xhr.status || "network"}.`;
}

function setUploading(uploading) {
  isUploading = uploading;
  const file = fileInput.files[0];
  uploadButton.disabled = uploading || !validateSelectedFile(file).valid;
  uploadForm.setAttribute("aria-busy", String(uploading));
  uploadButton.querySelector(".button-label").textContent = uploading ? "Processing…" : "Process file";
  uploadButton.querySelector(".button-spinner").hidden = !uploading;
  fileInput.disabled = uploading;
  rulesInput.disabled = uploading;
  savedRulesetSelect.disabled = uploading || isSavingRuleset;
  rulesetNameInput.disabled = uploading || isSavingRuleset;
  saveRulesetButton.disabled = uploading || isSavingRuleset || Boolean(selectedSavedRulesetId);
}

savedRulesetSelect.addEventListener("change", async () => {
  const rulesetId = savedRulesetSelect.value;
  if (!rulesetId) {
    selectedSavedRulesetId = "";
    rulesInput.readOnly = false;
    saveRulesetButton.disabled = isUploading || isSavingRuleset;
    showRulesetMessage("");
    return;
  }
  savedRulesetSelect.disabled = true;
  showRulesetMessage("Loading saved ruleset…");
  try {
    await fetchSavedRuleset(rulesetId);
  } catch (error) {
    selectedSavedRulesetId = "";
    savedRulesetSelect.value = "";
    rulesInput.readOnly = false;
    saveRulesetButton.disabled = isUploading || isSavingRuleset;
    showRulesetMessage(error instanceof Error ? error.message : "Could not load ruleset.", "message-error");
  } finally {
    savedRulesetSelect.disabled = isUploading || isSavingRuleset;
  }
});

saveRulesetButton.addEventListener("click", async () => {
  if (isSavingRuleset || isUploading) return;
  const name = rulesetNameInput.value.trim();
  const configuration = rulesInput.value;
  if (!name) {
    showRulesetMessage("Enter a display name for this ruleset.", "message-error");
    rulesetNameInput.focus();
    return;
  }
  try {
    JSON.parse(configuration);
  } catch (_error) {
    showRulesetMessage("Rules configuration must be valid JSON before it can be saved.", "message-error");
    rulesInput.focus();
    return;
  }
  isSavingRuleset = true;
  saveRulesetButton.disabled = true;
  savedRulesetSelect.disabled = true;
  showRulesetMessage("Validating and saving ruleset…");
  try {
    const response = await fetch("/rulesets", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ name, configuration }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || `Could not save ruleset (${response.status}).`);
    rulesetNameInput.value = "";
    await loadSavedRulesets(payload.ruleset_id);
    showRulesetMessage(`Saved and selected “${payload.name}” (version ${payload.version}).`, "message-success");
  } catch (error) {
    showRulesetMessage(error instanceof Error ? error.message : "Could not save ruleset.", "message-error");
  } finally {
    isSavingRuleset = false;
    savedRulesetSelect.disabled = isUploading;
    saveRulesetButton.disabled = isUploading || Boolean(selectedSavedRulesetId);
  }
});

function formatFileSize(bytes) {
  if (bytes < 1024) return `${bytes} bytes`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function validateSelectedFile(file) {
  if (!file) return { valid: false, message: "Choose a CSV or XLSX file before uploading." };
  if (!/\.(csv|xlsx)$/i.test(file.name)) {
    return { valid: false, message: "Choose a file with a .csv or .xlsx extension." };
  }
  if (file.size > MAX_FILE_BYTES) {
    return { valid: false, message: "This file is larger than the 5 MiB upload limit." };
  }
  return { valid: true, message: "" };
}

function updateSelectedFile() {
  const file = fileInput.files[0];
  document.querySelector("#file-name").textContent = file ? file.name : "Choose a CSV or Excel file";
  document.querySelector("#file-hint").textContent = file
    ? `${formatFileSize(file.size)} · selected`
    : "or drag it here · CSV or XLSX · up to 5 MiB";
  const validation = validateSelectedFile(file);
  uploadButton.disabled = isUploading || !validation.valid;
  if (file && !validation.valid) {
    showUploadMessage(validation.message, "message-error");
  } else {
    uploadMessage.hidden = true;
  }
}

fileInput.addEventListener("change", updateSelectedFile);

dropZone.addEventListener("dragover", (event) => {
  event.preventDefault();
  if (!isUploading) dropZone.classList.add("dragging");
});

dropZone.addEventListener("dragleave", (event) => {
  if (!dropZone.contains(event.relatedTarget)) dropZone.classList.remove("dragging");
});

dropZone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropZone.classList.remove("dragging");
  if (isUploading) return;
  if (!event.dataTransfer?.files.length) return;
  fileInput.files = event.dataTransfer.files;
  fileInput.dispatchEvent(new Event("change", { bubbles: true }));
});

uploadForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (isUploading) return;
  const file = fileInput.files[0];
  const validation = validateSelectedFile(file);
  if (!validation.valid) {
    showUploadMessage(validation.message, "message-error");
    return;
  }
  const rulesText = rulesInput.value.trim();
  if (rulesText) {
    try {
      JSON.parse(rulesText);
    } catch (_error) {
      showUploadMessage("Rules configuration must be valid JSON.", "message-error");
      rulesInput.focus();
      return;
    }
  }

  uploadMessage.hidden = true;
  document.querySelector("#business-result").hidden = true;
  uploadProgress.hidden = false;
  progressBar.value = 0;
  progressLabel.textContent = "Uploading your file…";
  progressPercent.textContent = "0%";
  setUploading(true);

  const formData = new FormData();
  formData.append("file", file);
  if (rulesText) formData.append("rules", rulesText);
  const request = new XMLHttpRequest();
  request.open("POST", "/workflows/business-file");
  request.setRequestHeader("Accept", "application/json");
  request.upload.addEventListener("progress", (progressEvent) => {
    if (!progressEvent.lengthComputable) {
      progressLabel.textContent = "Uploading your file…";
      return;
    }
    const percent = Math.min(100, Math.round((progressEvent.loaded / progressEvent.total) * 100));
    progressBar.value = percent;
    progressPercent.textContent = `${percent}%`;
    progressLabel.textContent = percent === 100 ? "Validating and processing…" : "Uploading your file…";
  });
  request.addEventListener("load", () => {
    setUploading(false);
    uploadProgress.hidden = true;
    if (request.status === 201) {
      try {
        const result = JSON.parse(request.responseText);
        showUploadMessage("Processing complete. Your job and artifact metadata are saved.", "message-success");
        renderBusinessResult(result);
        loadJobs();
      } catch (_error) {
        showUploadMessage("The server returned an unreadable response.", "message-error");
      }
    } else {
      showUploadMessage(parseErrorResponse(request), "message-error");
    }
  });
  request.addEventListener("error", () => {
    setUploading(false);
    uploadProgress.hidden = true;
    showUploadMessage("Could not reach the API. Check that the local server is running.", "message-error");
  });
  request.send(formData);
});

document.addEventListener("click", async (event) => {
  const scrollButton = event.target.closest("[data-scroll]");
  if (scrollButton) {
    const target = document.getElementById(scrollButton.dataset.scroll);
    target?.scrollIntoView({ behavior: pageScrollBehavior(), block: "start" });
    if (scrollButton.dataset.scroll === "overview") window.scrollTo({ top: 0, behavior: pageScrollBehavior() });
    if (scrollButton.classList.contains("nav-item")) {
      setActiveNavigation(scrollButton.dataset.scroll);
    }
    return;
  }

  const jobButton = event.target.closest("[data-job-id]");
  const executionButton = event.target.closest("[data-execution-id]");
  if (executionButton) {
    executionButton.disabled = true;
    executionButton.textContent = "Loading…";
    try {
      const response = await fetch(`/executions/${encodeURIComponent(executionButton.dataset.executionId)}`, {
        headers: { Accept: "application/json" },
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || `Could not load execution (${response.status}).`);
      renderExecutionDetail(payload);
      document.querySelector("#execution-detail").scrollIntoView({ behavior: pageScrollBehavior(), block: "nearest" });
    } catch (error) {
      setExecutionHistoryState(error instanceof Error ? error.message : "Could not load execution details.", true);
    } finally {
      executionButton.disabled = false;
      executionButton.textContent = "View";
    }
    return;
  }
  if (!jobButton) return;
  jobButton.disabled = true;
  jobButton.textContent = "Loading…";
  try {
    const response = await fetch(`/jobs/${encodeURIComponent(jobButton.dataset.jobId)}`, {
      headers: { Accept: "application/json" },
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || `Could not load job (${response.status}).`);
    renderJobSummary(payload);
      document.querySelector("#job-detail").scrollIntoView({ behavior: pageScrollBehavior(), block: "nearest" });
  } catch (error) {
    showUploadMessage(error instanceof Error ? error.message : "Could not load job details.", "message-error");
  } finally {
    jobButton.disabled = false;
    jobButton.textContent = "View";
  }
});

if ("IntersectionObserver" in window) {
  const navigationObserver = new IntersectionObserver((entries) => {
    const current = entries
      .filter((entry) => entry.isIntersecting)
      .sort((left, right) => right.intersectionRatio - left.intersectionRatio)[0];
    if (current) setActiveNavigation(current.target.id);
  }, { rootMargin: "-18% 0px -68% 0px", threshold: [0, 0.1, 0.25, 0.5] });
  document.querySelectorAll(".nav-item[data-scroll]").forEach((item) => {
    const target = document.getElementById(item.dataset.scroll);
    if (target && item.dataset.scroll !== "overview") navigationObserver.observe(target);
  });
  window.addEventListener("scroll", () => {
    if (window.scrollY < 60) setActiveNavigation("overview");
  }, { passive: true });
}

loadJobs();
loadExecutions();
loadSchedules();
loadSavedRulesets().catch((error) => {
  showRulesetMessage(error instanceof Error ? error.message : "Could not load saved rulesets.", "message-error");
});
