Grafana Telemetry Transfer

This script is for extracting query results from a source Grafana instance and testing sample telemetry payloads against Grafana Cloud.

Project files

  - transfer_telemetry.py — main Python script
  - source_queries.json — source Grafana queries to run
  - sample_telemetry.json — sample metrics, logs, and traces payloads
  - .env — URLs and credentials (keep this private)
  - requirements.txt — Python packages
  - landing/ — output reports and extracted query results

1. Open the project

Open the grafana_cloud_transfer folder in VS Code.

Select the project virtual environment:
1. Press Ctrl + Shift + P.
2. Choose Python: Select Interpreter.
3. Select .venv (3.14.7) — .\.venv\Scripts\python.exe.

2. Install packages

Open Terminal → New Terminal in VS Code. Make sure the terminal is in the project root folder, then run:

powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt


3. Configure .env

Create or update .env in the project root. Use the variable names below and replace the example values with credentials supplied by your team.

dotenv
SOURCE_GRAFANA_URL=https://your-source-grafana.example.com
SOURCE_GRAFANA_TOKEN=replace_with_source_token

Use the OTLP gateway endpoint supplied for your Grafana Cloud stack.
It should normally end with /otlp, not /otlp/v1/metrics.
GRAFANA_CLOUD_OTLP_ENDPOINT=https://your-otlp-gateway.example.com/otlp
GRAFANA_CLOUD_OTLP_INSTANCE_ID=replace_with_otlp_instance_id
GRAFANA_CLOUD_ACCESS_POLICY_TOKEN=replace_with_write_token

REQUEST_TIMEOUT_SECONDS=30
VERIFY_TLS=true


Don't commit .env or share tokens in screenshots or chat. Keep VERIFY_TLS=true outside controlled local testing.

You can run validate without real Grafana credentials. The extract and sample modes need the relevant real settings.

4. Run the script

Run these commands from the project root in the VS Code PowerShell terminal.

A. Validate the sample JSON (safe local check)

powershell
.\.venv\Scripts\python.exe transfer_telemetry.py --mode validate


This checks the structure of sample_telemetry.json. It makes no network requests and does not need Grafana credentials.

Expected output includes "valid": true and:

text
Local payload structure validation passed. No network requests were made.


B. Extract query results from source Grafana

powershell
.\.venv\Scripts\python.exe transfer_telemetry.py --mode extract


This uses SOURCE_GRAFANA_URL and SOURCE_GRAFANA_TOKEN, lists the source data sources, and runs enabled queries from source_queries.json.

Check source_queries.json before running:
  - Each query should have "enabled": true to run.
  - datasource_uid must match a UID in the source Grafana instance.
  - datasource_type and the query object must match the data source and query format.
  - default_from supports relative ranges such as now-15m, now-1h, and now-2d.

Outputs are written under landing/:
  - source_datasources.json — source data source inventory
  - raw/ — raw query responses
  - source_extract_report.json — status for each query

This mode contacts the source Grafana API. It does not send data to Grafana Cloud.

C. Send the sample telemetry to Grafana Cloud

powershell
.\.venv\Scripts\python.exe transfer_telemetry.py --mode sample


This sends the static payloads in sample_telemetry.json to the configured Grafana Cloud OTLP endpoint for metrics, logs, and traces.

The report is written to landing/sample_push_report.json.

Important: This mode sends sample data, not the results extracted by --mode extract. Confirm with your team before sending sample payloads to a real Cloud stack.

5. Available modes

| Mode | What it does | Network requests? | Needs credentials? |
|---|---|---|---|
| validate | Checks sample payload structure locally | No | No |
| extract | Queries source Grafana and saves raw results | Yes, source Grafana | Source Grafana |
| sample | Sends static sample OTLP payloads to Grafana Cloud | Yes, Grafana Cloud | Grafana Cloud |

To see command-line help:

powershell
.\.venv\Scripts\python.exe transfer_telemetry.py --help


Current limitation

The current script has separate extraction and sample-push modes. It does not automatically convert extracted Grafana query results into OTLP metrics, logs, or traces, and it does not automatically transfer those extracted results to Grafana Cloud. That conversion and end-to-end flow still need to be implemented and tested.

Troubleshooting

  - Pylance says imports cannot be resolved: Select .venv (3.14.7) in Python: Select Interpreter, then reload the VS Code window.
  - Missing package: Run .\.venv\Scripts\python.exe -m pip install -r requirements.txt.
  - Source API returns an error: Check the source URL, token permissions, data source UID, query format, and time range.
  - Cloud push fails: Check the OTLP endpoint, instance ID, write token, TLS settings, and Cloud permissions.

Before committing

Make sure .env is excluded from Git and only .env.example with placeholder values is committed.
