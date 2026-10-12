# Automation AI Platform

A local automation platform with two synchronous data workflows: the compatible CSV-to-SQLite import API and a business CSV/XLSX validation workflow. The dashboard accepts CSV and Excel workbooks, separates valid records from row-level issues, and produces downloadable Excel artifacts. SQLite stores job and artifact metadata; openpyxl reads and writes Excel files.

## Requirements

- Python 3.11 or newer
- PowerShell on Windows (commands below use the project-local virtual environment)

## Setup

From this folder, create the local environment and install the project with its test dependencies:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

For another supported Python version, replace `-3.14` with that version. The environment and installed packages stay inside this project.

## Run the API and dashboard

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

The dashboard is available at `http://127.0.0.1:8000/`. The interactive API documentation remains available at `http://127.0.0.1:8000/docs`.

By default, the database is created at `data/automation.db` on application startup. Set `AUTOMATION_DB_PATH` to use a different SQLite file:

```powershell
$env:AUTOMATION_DB_PATH = ".\data\local.sqlite3"
```

## Workflow engine foundation

CSV uploads pass through the synchronous `csv_import` workflow after the upload parser has completed its existing validation. The workflow receives a typed filename and rows, then calls a small persistence interface; it does not depend on FastAPI or SQLite. A run receives an execution ID and moves from `running` to `succeeded` or `failed`. Invalid workflow inputs are recorded as expected failures, while unexpected persistence or programming errors are recorded with a traceback and re-raised so the API retains its server-error behavior. Logs include run metadata and timing, never CSV cell contents.

Automation executions and ordered step results are persisted in SQLite. The persistent worker queue and schedules are also stored in the same database; input files and generated artifacts are stored under the configured data directory.

## API usage

Upload a CSV with a header row and at least one non-empty data value:

```powershell
curl.exe -X POST "http://127.0.0.1:8000/jobs/csv" `
  -F "file=@customers.csv;type=text/csv"
```

A successful response has HTTP `201` and includes an `id`, safe basename `filename`, `status`, `row_count`, and UTC `created_at`. Use the returned ID to retrieve status:

```powershell
curl.exe "http://127.0.0.1:8000/jobs/<job-id>"
```

The dashboard loads the latest persisted jobs from `GET /jobs?limit=10`, submits business files to `POST /workflows/business-file`, and fetches job summaries from `GET /jobs/{job_id}`. The overview's total-job count comes from SQLite; its row count is explicitly limited to the latest ten results. The **CSV imports** sidebar item scrolls to persisted job history, where **View** opens a job summary. Upload accepts a file picker or drag-and-drop, shows the selected filename and size, and checks supported extensions and the 5 MiB limit before sending. The browser reports upload progress and API validation errors; after an error, a valid selected file can be retried. The file control remains keyboard accessible. Backend validation remains authoritative. Both business jobs and legacy CSV jobs remain in the configured SQLite database. `POST /jobs/csv` remains available with its original response contract for API clients.

## Business spreadsheet workflow

The dashboard submits CSV and XLSX files to `POST /workflows/business-file` using the multipart field `file`. An optional multipart text field named `rules` accepts a versioned JSON ruleset. The endpoint accepts UTF-8 CSV (optional BOM) and `.xlsx` workbooks up to 5 MiB. XLSX files are read from the active worksheet only; legacy `.xls`, macro-enabled workbooks, and other extensions are not accepted. XLSX compressed archives are also limited to 64 MiB expanded size, and the active sheet is limited to 200,000 cells. Both formats are limited to 100,000 data rows.

The first row is the header row. Headers must be non-empty and unique after trimming and case-insensitive comparison. The workflow does not assume sales, invoice, or transaction-specific column names. Each data row must have the same number of cells as the header and must not be entirely blank. Short/long rows, blank rows, non-finite numbers, unsupported control characters, Excel error cells, and formula cells are retained in the validation report with source row and an explanation. Formulas are never evaluated. Other cells are treated as ordinary source values; no business-specific required fields or numeric/date conversions are inferred. Valid rows are copied to the processed workbook with original cell values retained; untrusted strings are written as text to prevent formula execution.

The validation workbook is created for every accepted file and includes each invalid row's original data as JSON. A processed workbook is created when at least one row is valid. Invalid rows are never silently dropped: the report preserves them, while the processed workbook contains only valid rows. If no rows are valid, the API returns the report only and reports zero processed rows.

Generated artifacts are stored under `data/outputs/` by default. Set `AUTOMATION_OUTPUT_DIR` to use another output directory. Filenames are generated from random job IDs; downloads are served only for artifact basenames registered in SQLite at `GET /artifacts/{artifact_name}`. The business job, its valid rows, counts, status, artifact names, and (when supplied) ruleset ID, version, and validated JSON snapshot are persisted transactionally in SQLite and appear in existing job history. Automation execution records and step results are persisted in SQLite and survive application restarts; the runner's in-process cache is temporary. Job metadata and output artifacts persist. The dashboard shows counts, up to 100 issue summaries, and links to the complete report and processed workbook.

### Declarative business rules

Leave the dashboard's optional Business rules field blank to keep the existing file-integrity-only behavior. A supplied ruleset is validated strictly by the server and evaluated only for rows that pass generic file-integrity checks. Rules do not modify source rows. A row enters the processed workbook and valid-row persistence only if it passes both integrity checks and every configured rule. Rule failures are included in the validation report with original source values and structured source row, column, rule ID, and message details.

The accepted JSON shape is:

```json
{
  "schema_version": 1,
  "ruleset_id": "invoice-checks",
  "version": "1.0",
  "rules": [
    {"id": "invoice-number-required", "type": "required", "column": "Invoice No"},
    {"id": "status-values", "type": "allowed_values", "column": "Status", "values": ["Open", "Paid"]},
    {"id": "amount-range", "type": "numeric_range", "column": "Amount", "min": 0, "max": 50000},
    {"id": "invoice-number-unique", "type": "duplicate", "column": "Invoice No", "trim": true, "case_sensitive": false}
  ]
}
```

`schema_version` must be integer `1`; `ruleset_id`, `version`, and each unique rule `id` must be short identifiers. Every referenced column must exactly match a source header. Unknown fields and rule types are rejected. Configuration is limited to 64 KiB, 50 rules, 1,000 allowed values per rule, and 256-character strings/column names. No expressions, code, or callbacks are supported.

- `required`: fails for a missing cell, `null`, an empty string, or a whitespace-only string.
- `allowed_values`: compares the parsed cell value to a configured JSON scalar using exact type and value equality. There is no implicit string/number conversion, trimming, or case folding. CSV cells are strings, so an allowed value for CSV text `001` must be the JSON string `"001"`.
- `numeric_range`: parses finite numeric values safely; blank values are skipped (combine with `required` when needed). `min` and `max` are optional, at least one is required, and bounds are inclusive. Booleans, non-numeric values, NaN, and infinities fail.
- `duplicate`: requires explicit boolean `trim` and `case_sensitive` options, ignores blank values, and marks every row participating in a duplicate. When `trim` is false, surrounding whitespace remains significant. When `case_sensitive` is true, case differences remain significant. Values are compared as their string representation after the selected options.

Example PowerShell request (the file `rules.json` contains the JSON above):

```powershell
$rulesJson = Get-Content -Raw .\rules.json
curl.exe -X POST "http://127.0.0.1:8000/workflows/business-file" `
  -F "file=@invoices.csv;type=text/csv" `
  -F "rules=$rulesJson"
```

Without `rules`, or with a blank field, the endpoint retains the prior response shape and no ruleset snapshot is stored. Supplying rules adds `rule_issues` and `rule_issues_truncated` to the response; the structured finding preview is capped at 100, while the validation workbook retains every finding. Database startup applies an additive, idempotent SQLite migration adding nullable ruleset ID, version, and JSON snapshot columns to `business_jobs`; existing tables, records, job-history queries, and artifacts are preserved. Jobs created without rules have `NULL` in all three columns.

### Reusable saved rulesets

Flowdesk can save validated rulesets locally in SQLite. In the dashboard, paste a rules JSON configuration, enter a display name, and choose **Save ruleset**. Flowdesk validates the schema before saving. After it is saved, it appears in **Reuse a saved ruleset**; selecting it loads the stored configuration read-only into the upload form. Uploading then sends that exact configuration through the existing `rules` multipart field. Choose **Manual JSON (no saved ruleset)** to return to editable JSON entry. Saving a ruleset never replaces an existing ID; use the API's explicit update endpoint to revise it.

The ruleset API uses a JSON request with `name` and `configuration`, where `configuration` is a string containing the exact rules JSON:

```json
{
  "name": "Invoice Checks",
  "configuration": "{\"schema_version\":1,\"ruleset_id\":\"invoice-checks\",\"version\":\"1.0\",\"rules\":[{\"id\":\"invoice-required\",\"type\":\"required\",\"column\":\"Invoice No\"}]}"
}
```

Endpoints:

- `POST /rulesets` validates and creates a saved ruleset. It returns `201`; an existing `ruleset_id` returns `409` and is not overwritten.
- `GET /rulesets` lists saved metadata (`ruleset_id`, name, version, and timestamps).
- `GET /rulesets/{ruleset_id}` returns metadata and the exact configuration string.
- `PUT /rulesets/{ruleset_id}` validates and updates an existing record. The ID inside the configuration must match the URL; an unknown ID returns `404`.

Malformed configurations, invalid names, and mismatched IDs return `422`. The SQLite `business_rulesets` table is created with `CREATE TABLE IF NOT EXISTS` during startup. This additive migration preserves existing jobs, business-job metadata, and artifacts; saved rulesets remain local to the configured database.

Use **Process a file** to choose or drop a CSV/XLSX file. After success, the valid/invalid counts, row-level issues, and available download links appear below the form; recent persisted jobs refresh. Choose **View** on a history row to load that job's summary. With no persisted jobs, the dashboard displays an empty state. The Automation builder also supports background runs and schedules; AI processing and notifications remain planned and unavailable.

The legacy `POST /jobs/csv` endpoint retains its original validation contract: `.csv` filename, UTF-8 (optionally with a BOM), unique non-empty headers, at least one non-empty data value, and a 5 MiB maximum. Duplicate headers are checked from the source header before Pandas parses the data. Fields are stored as strings: values such as `001` remain `001`, literal `NA` remains `NA`, and empty cells remain empty strings. Non-finite numeric tokens (`NaN`, `inf`, `Infinity`, and signed variants) are rejected. Invalid inputs return a descriptive HTTP `4xx` response. The upload is processed in memory; the client filename is stored only as metadata and is never used to construct a filesystem path.

## Reusable automation engine

`POST /automation/execute` runs a validated sequential workflow around the existing business-file processor. Submit multipart fields `file` (CSV/XLSX), `workflow` (JSON definition), and optional `rules` (the same versioned business rules JSON accepted by `/workflows/business-file`). Built-in steps include `business_file`, `business_file_summary`, `clean_data`, `map_columns`, `calculate_field`, `pdf_report`, and `api_request`. Transform steps compose a strictly validated operation list consumed by `business_file`; the existing validator still evaluates original source data, retains invalid source rows in the validation report, and creates processed artifacts and persisted rows from valid transformed data. No configured code, expressions, callbacks, or dynamic imports are executed.

Example `workflow` field:

```json
{
  "workflow_id": "invoice-processing",
  "version": "1",
  "steps": [
    {"id": "process", "type": "business_file"},
    {"id": "summary", "type": "business_file_summary", "inputs": {"result": {"from_step": "process"}}}
  ]
}
```

Definitions accept exactly `workflow_id`, `version`, and `steps`; each step accepts `id`, `type`, optional object `config`, and optional object `inputs`. IDs must be unique identifiers, definitions are limited to 32 KiB and 20 steps, and references may point only to an earlier step. The `business_file` step accepts no config or inputs. `business_file_summary` accepts only `inputs.result`, which must reference an earlier `business_file` step. These contracts make the currently supported chain explicit while keeping handler registration injectable for future step types.

The endpoint returns HTTP `201` with an execution ID, lifecycle status, per-step results, and outputs when successful. Invalid definitions or expected file/rules validation failures return HTTP `422` with a finalized failed execution record. Unexpected step failures return HTTP `500` with safe step/error metadata. Execution records and ordered step results are now stored in SQLite and survive application/service recreation. `GET /executions/{execution_id}` retrieves one record and returns `404` for an unknown ID. `GET /executions?limit=20&offset=0` lists newest records with a total count; `limit` is 1–100 and `offset` is zero or greater. The dashboard's **Execution history** section supports paging and shows workflow identity, status, timestamps, duration, step outcomes, and artifact links. Existing CSV imports, business-file processing, saved rulesets, and artifact downloads retain their contracts.

The execution tables (`automation_executions` and `automation_step_results`) and queue/schedule tables are created with additive `CREATE TABLE IF NOT EXISTS` initialization. Existing jobs, business jobs/artifacts, imported rows, and saved rulesets are not dropped or rewritten. Only bounded workflow metadata and known output summaries/artifact references are stored; uploaded file contents and arbitrary step payloads are not copied into execution history. A stale running queue item is marked `interrupted` in queue metadata and failed in the existing execution-status table (whose status constraint predates the `interrupted` state); it is never automatically restarted because a prior step may have performed an external side effect such as SMTP submission.

The dashboard also includes an **Automation builder** section. Choose the **Business file processing + summary** workflow, select a CSV/XLSX file, and optionally choose a saved ruleset. Transformation controls let the user trim text, remove fully blank rows from the transformation output, map source columns to standard names, and add calculated fields. The builder constructs the workflow JSON internally and submits it to `POST /automation/execute`; users do not need to author workflow JSON. When **No business rules (default)** is selected, the dashboard omits the multipart `rules` field. A selected ruleset is retrieved from `GET /rulesets/{ruleset_id}` and its stored configuration string is sent as `rules`. The result panel shows execution status, each step's status/error, processed counts, and the available artifact downloads. The original business-file upload form remains available.

### Background runs and schedules

The same builder can send a run to `POST /automation/queue` by selecting **Run in background and track progress**. The API validates the workflow/rules, accepts only CSV/XLSX up to 5 MiB, returns HTTP `202` with an execution ID and `queued` status, and saves an application copy of the uploaded input in `AUTOMATION_QUEUE_DIR` (default `data/queue`). The web dashboard polls `GET /automation/queue/{execution_id}` and shows current status and persisted step outcomes. `GET /automation/queue?limit=20&offset=0` provides bounded queue metadata. Pending queued/running work is capped at 100, stored upload copies at 1,000, schedules at 100, and active workers at four across processes. The pending queue cap is enforced transactionally in SQLite; stored input copies are retained and are not automatically deleted. Plan administrator-managed retention for `data/queue`, preserving files referenced by any schedule (including disabled schedules) or queued/running execution.

Run a separate worker process alongside the web process:

```powershell
.\.venv\Scripts\python.exe -m app.worker
```

The worker polls the queue and dispatches due schedules. Heartbeats are updated while a run is active. Work stale for over two minutes is marked interrupted and is not retried automatically. The execution record uses `failed` for this case to preserve the existing database constraint; the queue API exposes `interrupted`.

The dashboard schedule section saves the currently selected input, workflow, and ruleset for one-time, daily, or weekly runs. Schedules are available through `POST/GET /schedules`, `POST /schedules/{id}/enable`, `POST /schedules/{id}/disable`, and `DELETE /schedules/{id}`. Recurring times use an IANA timezone such as `America/New_York`; a nonexistent local time during a DST jump shifts forward by the gap, and ambiguous times use the first occurrence. Dispatch is transactional and has a database lease plus a unique `(schedule_id, occurrence_key)` constraint to prevent duplicate occurrence insertion. Missed recurring occurrences are coalesced to the next future occurrence; scheduled failures are not retried.

Schedules and queued workflows retain validated workflow/rules configuration and an input-file reference in SQLite. SMTP passwords and API tokens are never put in those configurations: set approved server environment variables instead. A successful `email_delivery` step means the SMTP server accepted the message, not that the recipient inbox received it.

### Security boundary and health checks

By default the app permits loopback/local access only. Remote requests require `FLOWDESK_REMOTE_ACCESS_TOKEN` with at least 32 characters, a matching `Authorization: Bearer ...` header, and an HTTPS request as seen by the application. Configure `FLOWDESK_ALLOWED_HOSTS` as a comma-separated exact hostname allowlist (no wildcards); local hostnames are allowed by default. For a reverse proxy, include the public hostname here and configure the proxy to set the upstream `Host` to that hostname. Set `FLOWDESK_TRUSTED_PROXY_CIDRS` to the proxy source CIDRs and configure the proxy to replace caller-provided `X-Forwarded-For` with one validated client IP and set `X-Forwarded-Proto` from its TLS connection. Only those proxy peers' forwarded headers are trusted; `X-Forwarded-Host` is not used. The proxy must require HTTPS externally. Browser state-changing requests must have a same-origin `Origin` (and, when present, `Sec-Fetch-Site: same-origin`); non-browser API clients may omit these browser headers. `FLOWDESK_TRUSTED_CLIENT_CIDRS` is a separate explicit network trust exception and should be set only for a tightly controlled network. Do not publish the unauthenticated local dashboard to the public internet. `/healthz` is a liveness check; `/readyz` checks SQLite availability. The app rejects request bodies over 6 MiB before multipart parsing; individual business files remain limited to 5 MiB.

### Docker Compose

Docker Compose runs the web app and one worker against a shared named volume at `/app/data`; the host port is bound to `127.0.0.1:8000`. The image runs as a non-root user and includes runtime dependencies only. The worker and web process must share the same persistent volume and database. Start from this project directory:

```powershell
docker compose up --build -d
docker compose ps
docker compose logs --tail 100 web worker
```

Open `http://127.0.0.1:8000/`. Stop with `docker compose down` (the named data volume remains); `docker compose down -v` deletes persistent application data and should be avoided unless intentionally purging it. A consistent backup must include the SQLite database plus the complete `outputs/` and `queue/` directories: an SQLite online backup captures database pages only, not those files. Safest procedure: stop both web and worker (`docker compose stop web worker`), copy the complete `/app/data` volume as one set (the SQLite database, any `-wal`/`-shm` sidecars, and both directories), then restart services. If using SQLite online backup while services remain available, first quiesce operations that create/update jobs, artifacts, queue entries, or schedules and take a coordinated filesystem/volume snapshot of `outputs/` and `queue/` at the same logical point; SQLite backup alone is not a complete Flowdesk backup. Before restoring, stop services, restore the database and directories together, verify every registered artifact exists and queued/scheduled input references resolve inside the restored queue directory, then start services and check `/readyz`, job history, and artifact downloads. Artifact metadata without its file, or vice versa, can cause missing downloads. Set SMTP/API secrets through the deployment environment/secret manager; never commit them in Compose files or `.env`.

### Continuous integration

`.github/workflows/ci.yml` uses Python 3.12 and Node 22 to install the declared package, compile Python modules, syntax-check the dashboard JavaScript, run pytest, and validate Compose configuration. CI uses no customer data, external API credentials, or SMTP credentials. It does not prove browser interactions or production SMTP delivery.

### Troubleshooting

- **HTTP 403:** local operation must reach the app through loopback. For remote access, verify the bearer token, HTTPS at the application or trusted proxy, and the proxy's sanitized forwarding headers. Do not fix this by trusting a broad CIDR.
- **HTTP 503 from `/readyz`:** check that `AUTOMATION_DB_PATH` points to a writable persistent directory and inspect web logs with `docker compose logs --tail 100 web`.
- **Execution stays queued:** confirm the worker is running and uses the same `AUTOMATION_DB_PATH` and `AUTOMATION_QUEUE_DIR` as web: `docker compose ps` and `docker compose logs --tail 100 worker`. A queued response means accepted for execution, not started.
- **Execution is interrupted:** inspect `GET /executions/{execution_id}` and its step results before deciding what to do. Flowdesk does not retry interrupted work because an external side effect may already have occurred.
- **Artifact download returns 404:** the artifact must be registered in the database and present under `AUTOMATION_OUTPUT_DIR`. Restore the database and artifacts from the same backup set; do not edit registry rows to point at arbitrary paths.
- **Email step succeeds:** this confirms SMTP acceptance only. Check the receiving mail system for delivery or rejection; Flowdesk cannot confirm inbox arrival.

For a local run, launch the web process and worker in separate PowerShell terminals with `.\.venv\Scripts\python.exe -m uvicorn app.main:app` and `.\.venv\Scripts\python.exe -m app.worker`. Run tests with `.\.venv\Scripts\python.exe -m pytest -q`; use the already-existing project environment and do not use the workspace-root environment.

### Optional PDF processing report

The Workflow Builder can add a PDF report step after business-file processing. PDF generation is off by default, preserving the existing workflow. When enabled, customers set a report title (1–120 printable characters) and choose whether to include the available validation finding details. The report contains its generation timestamp, workflow and execution identity, total/valid/invalid row counts, and validation findings with source row, column, rule/category, and message. It contains no uploaded cell values and does not embed the source workbook. If the business-file output reports that its finding preview was truncated, the PDF says additional findings were omitted.

The workflow step uses the following strict configuration and input reference:

```json
{
  "workflow_id": "sales-with-pdf-report",
  "version": "1",
  "steps": [
    {"id": "process", "type": "business_file"},
    {"id": "summary", "type": "business_file_summary", "inputs": {"result": {"from_step": "process"}}},
    {"id": "pdf-report", "type": "pdf_report", "config": {"title": "Sales Validation Report", "include_findings": true}, "inputs": {"result": {"from_step": "process"}}}
  ]
}
```

`title` is required and limited to 120 printable characters. `include_findings` is optional and defaults to `true`; it must be a JSON boolean. No other configuration keys are accepted. The `result` reference must target an earlier `business_file` step. Invalid definitions fail the workflow before PDF creation; an unexpected generation or artifact-registration failure marks the workflow step and execution failed.

PDF files are written under the configured output directory (`AUTOMATION_OUTPUT_DIR`, default `data/outputs`) using a generated UUID filename. The application creates the file exclusively, registers it against the persisted business job in the additive `business_artifacts` table, and serves it through the existing `GET /artifacts/{artifact_name}` route with PDF media type and the same root confinement check. Execution-history JSON stores only PDF metadata and the download reference, never binary contents. The migration creates this registry table and index without rewriting existing job or artifact records.

ReportLab is declared as `reportlab>=4.2,<6.0`. Its Platypus `LongTable` and wrapped `Paragraph` components provide repeated table headings and multi-page flow for longer finding previews.

The `rules` field on `/automation/execute` is optional. Omitted and whitespace-only values preserve file-integrity-only processing. A non-blank malformed ruleset is rejected as a client input error and its execution is finalized as failed with `invalid_ruleset`.

### Reusable transformations

Transformation steps are optional and must appear before the `business_file` step. Each transformation after the first references the previous step with `inputs.previous`; `business_file` consumes the final plan through `inputs.transformations`. The dashboard creates this definition from form controls. The supported workflow JSON shape is illustrated here for API users:

```json
{
  "workflow_id": "sales-cleanup",
  "version": "1",
  "steps": [
    {"id": "clean", "type": "clean_data", "config": {"trim_strings": true, "remove_blank_rows": true}},
    {"id": "map", "type": "map_columns", "config": {"mapping": {"Item": "Product", "Units": "Quantity"}}, "inputs": {"previous": {"from_step": "clean"}}},
    {"id": "total", "type": "calculate_field", "config": {"name": "Line_Total", "operation": "multiply", "left_column": "Quantity", "right_column": "Price"}, "inputs": {"previous": {"from_step": "map"}}},
    {"id": "process", "type": "business_file", "inputs": {"transformations": {"from_step": "total"}}},
    {"id": "summary", "type": "business_file_summary", "inputs": {"result": {"from_step": "process"}}}
  ]
}
```

- `clean_data` requires boolean `trim_strings` and `remove_blank_rows`. Trimming affects string cells only. Fully blank rows are removed from the transform output; source blank rows remain represented by the existing validation report.
- `map_columns` accepts only a `mapping` object whose exact source-header names map to non-empty standard names. Missing source headers, duplicate target names, or conflicts with unmapped columns fail the workflow.
- `calculate_field` requires `name`, `left_column`, `right_column`, and one `operation`: `add`, `subtract`, `multiply`, or `divide`. Inputs must be finite numeric values. Division by zero, missing columns, non-numeric cells, and non-finite results fail clearly; no source values are silently coerced into a different business meaning.

Transformations run in order on rows that pass the existing file-integrity and business-rule checks. Rules and validation findings continue to refer to original source headers and values. Invalid rows are not transformed or discarded; they remain unchanged in the validation workbook. The uploaded source is never overwritten. The processed workbook and valid-row SQLite records use the transformed headers and values. No formulas or arbitrary expressions are evaluated.

Example PowerShell invocation:

```powershell
$workflow = '{"workflow_id":"invoice-processing","version":"1","steps":[{"id":"process","type":"business_file"},{"id":"summary","type":"business_file_summary","inputs":{"result":{"from_step":"process"}}}]}'
curl.exe -X POST "http://127.0.0.1:8000/automation/execute" `
  -F "file=@invoices.csv;type=text/csv" `
  -F "workflow=$workflow"
```

### External API request step

The workflow engine registers `api_request`, a synchronous HTTPS GET step. In the Automation builder, enable **Add an HTTPS GET request after file processing**, enter an endpoint and optional query parameters, then choose a 5/10/20 second timeout and a 64 KiB/256 KiB/1 MiB response cap. The step is appended after the file workflow and its bounded JSON result is returned as an ordinary step output. A later API step may reference that output as `inputs.query_params`; when the previous response is a JSON object of scalar values, those properties become query parameters. Only configured query parameters are sent to the external service; the uploaded spreadsheet is not forwarded. API results are never used to change or replace the uploaded workbook.

Workflow JSON configuration accepts only these fields:

```json
{
  "endpoint": "https://api.example.com/v1/status",
  "query_params": {"account_id": "A-102", "active": true},
  "timeout_seconds": 10,
  "max_response_bytes": 262144,
  "credential_id": "billing-api"
}
```

`endpoint` is required and must use HTTPS on port 443, without embedded credentials, query, or fragment. `query_params` is optional, supports at most 50 names and 100 expanded values, and accepts scalar strings, finite numbers, booleans, or arrays of those values. `timeout_seconds` defaults to 10 and must be from 0.1 through 20. `max_response_bytes` defaults to 262144 and must be from 1024 through 1048576. Only JSON media types (`application/json` and `application/*+json`) are accepted; malformed JSON, oversized responses, redirects, non-2xx statuses, timeouts, and connection errors fail the step with safe summaries. Automatic retries are not performed.

For bearer authentication, the workflow supplies only a server-defined `credential_id`; it cannot name or read environment variables. Administrators configure credentials in `FLOWDESK_API_CREDENTIALS_JSON`, passed to both Compose services from an untracked deployment environment or secret manager. Its value is a JSON object keyed by credential ID; every entry must contain exactly an HTTPS origin and token, for example (the token below is a placeholder, not a credential):

```json
{"billing-api":{"origin":"https://api.example.com","token":"REPLACE_WITH_A_SECRET_FROM_YOUR_SECRET_MANAGER"}}
```

Origins must use HTTPS on port 443 and contain no path except an optional trailing slash. Matching normalizes scheme/hostname case and the default port, then requires exact origin equality; subdomains and suffix lookalikes do not match. A credential is sent only when the requested endpoint has the configured origin. Missing IDs, origin mismatches, or malformed credential JSON fail before DNS resolution or network transport. Never put actual tokens in workflow JSON, tracked Compose files, source control, logs, or execution history. Prefer a deployment secret manager; Compose environment values are visible to Docker administrators. Flowdesk resolves the destination host before connecting, rejects every non-public resolved address, pins the HTTPS connection to the checked public IP, verifies TLS against the hostname, and does not follow redirects. This blocks local/private, loopback, link-local, multicast, unspecified, and other non-global destinations, including redirect-based access. The request uses no proxy settings or caller-provided headers. Treat returned API data as untrusted.

API-step workflow example (the URLs are placeholders and are not contacted by tests):

```json
{
  "workflow_id": "process-and-enrich",
  "version": "1",
  "steps": [
    {"id": "process", "type": "business_file"},
    {"id": "summary", "type": "business_file_summary", "inputs": {"result": {"from_step": "process"}}},
    {"id": "lookup", "type": "api_request", "config": {
      "endpoint": "https://api.example.com/v1/account-lookup",
      "query_params": {"account_id": "A-102"},
      "timeout_seconds": 10,
      "max_response_bytes": 262144
    }},
    {"id": "follow-up", "type": "api_request", "config": {
      "endpoint": "https://api.example.com/v1/account-status"
    }, "inputs": {"query_params": {"from_step": "lookup"}}}
  ]
}
```

The immediate synchronous execution response includes the bounded parsed JSON response so an API caller can use it in the workflow. Persisted execution history and `GET /executions/{execution_id}` store/return only status code, content type, response byte count, and duration for this step; they omit the response body, endpoint, query values, token variable value, and authorization header. Structured logs include execution/step identifiers and response metadata only. No external API is contacted by automated tests; request behavior is exercised with injected mocks.

### Secure email delivery of a PDF report

The Workflow Builder can optionally email a PDF report created earlier in the same workflow. Delivery is disabled by default. The dashboard asks for SMTP host and port, STARTTLS or implicit TLS, sender and one recipient, a subject, plain-text message, timeout, attachment-size limit, and the generated report selection. It automatically inserts the PDF step before email delivery, so users do not need to write workflow JSON. When email is enabled, the PDF report is generated even if the standalone PDF option was not selected.

SMTP credentials are read only from the approved server environment variables `FLOWDESK_SMTP_USERNAME` and `FLOWDESK_SMTP_PASSWORD`. The dashboard exposes those names as read-only references; it never accepts credential values. The SMTP host must also appear in the server-controlled comma-separated `FLOWDESK_SMTP_ALLOWED_HOSTS` environment variable (for example, `smtp.example.com`). Configure these settings through your operating system or deployment secret manager before starting the app. Never put the credentials in workflow JSON, the dashboard, source control, or logs. Flowdesk does not store resolved values in SQLite.

`email_delivery` accepts exactly this configuration:

```json
{
  "smtp_host": "smtp.example.com",
  "smtp_port": 587,
  "smtp_security": "starttls",
  "sender": "flowdesk@example.com",
  "recipient": "operator@example.net",
  "subject": "Processing report",
  "message": "The processing report is attached.",
  "username_env": "FLOWDESK_SMTP_USERNAME",
  "password_env": "FLOWDESK_SMTP_PASSWORD",
  "timeout_seconds": 10,
  "max_attachment_bytes": 5242880
}
```

The security mode is `starttls` or `implicit_tls`; both use the system's default certificate-verifying SSL context. Configuration permits one recipient address, ports 1–65535, timeouts from 0.1 to 30 seconds, and PDF limits from 1 KiB to 10 MiB (the dashboard offers 1, 5, or 10 MiB). Email addresses and all configuration are validated before connecting. The requested SMTP host must match the server-side allowlist before credentials are read or a network connection is made. STARTTLS must be advertised by the server. Attachment resolution accepts only the generated UUID-named PDF from an earlier `pdf_report` step, verifies its registry record, confines it to `AUTOMATION_OUTPUT_DIR`, checks the PDF signature, and reads no more than the configured limit. Arbitrary paths and spreadsheet files are never attached. A successful step means the SMTP server accepted the message; it does not confirm delivery to the recipient's inbox.

Email delivery submits one message and does not retry. A failed or ambiguous SMTP submission fails the email step and workflow; inspect the receiving mail system before manually retrying to avoid duplicates. The dashboard and execution history show the step status, a safe error if it failed, or SMTP acceptance and the attached report name. Logs and persisted results omit recipients, subject/body, SMTP host, credential variable names/values, and message contents. No database migration is needed: the existing execution result allowlist stores only delivery status and bounded attachment/timing metadata.

Example workflow (server values are never placed in this JSON):

```json
{
  "workflow_id": "report-and-email",
  "version": "1",
  "steps": [
    {"id": "process", "type": "business_file"},
    {"id": "report", "type": "pdf_report", "config": {"title": "Processing Report"}, "inputs": {"result": {"from_step": "process"}}},
    {"id": "email", "type": "email_delivery", "config": {
      "smtp_host": "smtp.example.com", "smtp_port": 587, "smtp_security": "starttls",
      "sender": "flowdesk@example.com", "recipient": "operator@example.net",
      "subject": "Processing report", "message": "The report is attached.",
      "username_env": "FLOWDESK_SMTP_USERNAME", "password_env": "FLOWDESK_SMTP_PASSWORD",
      "timeout_seconds": 10, "max_attachment_bytes": 5242880
    }, "inputs": {"report": {"from_step": "report"}}}
  ]
}
```

Automated email tests use mocked SMTP transports; they do not send real messages. A deliberate end-to-end delivery should be performed only by the user after configuring their SMTP server and reviewing the sender, recipient, subject, and message in the dashboard.

## Run tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Tests use temporary SQLite databases and do not use the default `data/automation.db` file.

## Project structure

- `app/api/` contains FastAPI routes and HTTP error mapping.
- `app/engine/` validates CSV and business files, runs synchronous workflows, and coordinates artifact creation through injected persistence interfaces.
- `app/storage/` manages SQLite connections/schema and parameterized repository queries.
- `app/web/` contains the FastAPI-served dashboard HTML, CSS, and JavaScript.
- `tests/` covers uploads, validation, persistence, repository behavior, and job retrieval.
