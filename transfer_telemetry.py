
import argparse
import base64
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# Configuration
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
LANDING_DIR = BASE_DIR / "landing"
SAMPLE_FILE = BASE_DIR / "sample_telemetry.json"
SOURCE_QUERIES_FILE = BASE_DIR / "source_queries.json"

load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("grafana-cloud-transfer")

SOURCE_URL = os.getenv("SOURCE_GRAFANA_URL", "").rstrip("/")
SOURCE_TOKEN = os.getenv("SOURCE_GRAFANA_TOKEN", "")

OTLP_ENDPOINT = os.getenv(
    "GRAFANA_CLOUD_OTLP_ENDPOINT", ""
).rstrip("/")
OTLP_INSTANCE_ID = os.getenv(
    "GRAFANA_CLOUD_OTLP_INSTANCE_ID", ""
)
OTLP_TOKEN = os.getenv(
    "GRAFANA_CLOUD_ACCESS_POLICY_TOKEN", ""
)

TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
VERIFY_TLS = os.getenv("VERIFY_TLS", "true").lower() == "true"


# ============================================================
# HTTP session with retries
# ============================================================

def session_with_retries():
    session = requests.Session()

    retries = Retry(
        total=3,
        connect=3,
        read=2,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
        respect_retry_after_header=True,
    )

    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


# ============================================================
# Configuration validation
# ============================================================

def require_cloud_config():
    missing = [
        name
        for name, value in {
            "GRAFANA_CLOUD_OTLP_ENDPOINT": OTLP_ENDPOINT,
            "GRAFANA_CLOUD_OTLP_INSTANCE_ID": OTLP_INSTANCE_ID,
            "GRAFANA_CLOUD_ACCESS_POLICY_TOKEN": OTLP_TOKEN,
        }.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            "Missing .env settings: " + ", ".join(missing)
        )

    parsed = urlparse(OTLP_ENDPOINT)

    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError(
            "GRAFANA_CLOUD_OTLP_ENDPOINT must be a real HTTPS URL."
        )

    # Fail before network requests if template values remain.
    placeholder_markers = (
        "your-",
        "your_",
        "replace_with",
        "example.com",
        "example.org",
        "placeholder",
    )

    combined = (
        OTLP_ENDPOINT + " "
        + OTLP_INSTANCE_ID + " "
        + OTLP_TOKEN
    ).lower()

    if any(marker in combined for marker in placeholder_markers):
        raise RuntimeError(
            "Grafana Cloud settings still contain placeholder values. "
            "Use --mode validate until the client provides real credentials."
        )


def require_source_config():
    missing = [
        name
        for name, value in {
            "SOURCE_GRAFANA_URL": SOURCE_URL,
            "SOURCE_GRAFANA_TOKEN": SOURCE_TOKEN,
        }.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            "Missing .env settings: " + ", ".join(missing)
        )

    parsed = urlparse(SOURCE_URL)

    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise RuntimeError(
            "SOURCE_GRAFANA_URL must be a valid HTTP/HTTPS URL."
        )

    if "example.com" in SOURCE_URL.lower():
        raise RuntimeError(
            "Replace the source Grafana example URL with the real URL."
        )


def cloud_headers():
    credentials = base64.b64encode(
        f"{OTLP_INSTANCE_ID}:{OTLP_TOKEN}".encode("utf-8")
    ).decode("ascii")

    return {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ============================================================
# Local validation — NO NETWORK REQUESTS
# ============================================================

def validate_sample():
    """
    Validate sample telemetry JSON and required OTLP structure locally.
    This mode does not access Grafana or Grafana Cloud.
    """
    if not SAMPLE_FILE.exists():
        raise FileNotFoundError(
            f"Sample telemetry file not found: {SAMPLE_FILE}"
        )

    with SAMPLE_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)

    required_sections = {
        "metrics": "resourceMetrics",
        "logs": "resourceLogs",
        "traces": "resourceSpans",
    }

    errors = []
    summary = {}

    for signal, resource_key in required_sections.items():
        payload = data.get(signal)

        if not isinstance(payload, dict):
            errors.append(
                f"{signal}: missing or invalid payload object"
            )
            continue

        resources = payload.get(resource_key)

        if not isinstance(resources, list) or not resources:
            errors.append(
                f"{signal}: missing or empty {resource_key}"
            )
            continue

        if not all(isinstance(item, dict) for item in resources):
            errors.append(
                f"{signal}: resource entries must be JSON objects"
            )
            continue

        record_key = {
            "metrics": "scopeMetrics",
            "logs": "scopeLogs",
            "traces": "scopeSpans",
        }[signal]

        record_count = 0

        for resource in resources:
            scopes = resource.get(record_key, [])

            if not isinstance(scopes, list):
                errors.append(
                    f"{signal}: {record_key} must be a list"
                )
                continue

            for scope in scopes:
                item_key = {
                    "metrics": "metrics",
                    "logs": "logRecords",
                    "traces": "spans",
                }[signal]

                items = scope.get(item_key, [])

                if not isinstance(items, list):
                    errors.append(
                        f"{signal}: {item_key} must be a list"
                    )
                    continue

                record_count += len(items)

        summary[signal] = {
            "resource_envelopes": len(resources),
            "records": record_count,
        }

        if record_count == 0:
            errors.append(
                f"{signal}: no telemetry records found"
            )

    print(
        json.dumps(
            {
                "mode": "validate",
                "network_requests": 0,
                "file": SAMPLE_FILE.name,
                "results": summary,
                "errors": errors,
                "valid": not errors,
            },
            indent=2,
        )
    )

    if errors:
        raise SystemExit(1)

    print(
        "\nLocal payload structure validation passed. "
        "No network requests were made."
    )


# ============================================================
# Sample OTLP ingestion to Grafana Cloud
# ============================================================

def push_sample():
    """
    Send included sample OTLP JSON payloads to Grafana Cloud.
    Requires real Cloud endpoint and credentials.
    """
    require_cloud_config()

    if not SAMPLE_FILE.exists():
        raise FileNotFoundError(
            f"Sample telemetry file not found: {SAMPLE_FILE}"
        )

    with SAMPLE_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)

    session = session_with_retries()
    results = {}

    for signal in ("metrics", "logs", "traces"):
        payload = data.get(signal)

        if not payload:
            results[signal] = "skipped: no payload"
            log.warning(
                "No '%s' payload in sample file; skipping",
                signal,
            )
            continue

        url = f"{OTLP_ENDPOINT}/v1/{signal}"

        try:
            response = session.post(
                url,
                headers=cloud_headers(),
                json=payload,
                timeout=TIMEOUT,
                verify=VERIFY_TLS,
            )

            if not response.ok:
                raise RuntimeError(
                    f"Grafana Cloud returned HTTP "
                    f"{response.status_code}"
                )

            results[signal] = (
                f"accepted (HTTP {response.status_code})"
            )

            log.info(
                "Grafana Cloud accepted %s payload: HTTP %s",
                signal,
                response.status_code,
            )

        except requests.RequestException as exc:
            results[signal] = (
                f"request failed: {type(exc).__name__}"
            )
            log.error(
                "%s push failed: %s",
                signal,
                type(exc).__name__,
            )

        except RuntimeError as exc:
            results[signal] = str(exc)
            log.error("%s push failed: %s", signal, exc)

    LANDING_DIR.mkdir(parents=True, exist_ok=True)

    report = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "sample",
        "results": results,
    }

    report_path = LANDING_DIR / "sample_push_report.json"

    report_path.write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(report, indent=2))

    if not results or any(
        "accepted" not in value
        for value in results.values()
    ):
        raise SystemExit(1)


# ============================================================
# Source Grafana query extraction
# ============================================================

def extract_source_queries():
    """
    Query the source Grafana API and preserve raw responses.

    NOTE:
    This extracts Grafana query results to landing/raw.
    It does not automatically convert arbitrary data-source
    responses into equivalent native OTLP telemetry.
    """
    require_source_config()

    if not SOURCE_QUERIES_FILE.exists():
        raise FileNotFoundError(
            f"Query configuration not found: {SOURCE_QUERIES_FILE}"
        )

    with SOURCE_QUERIES_FILE.open(
        "r", encoding="utf-8"
    ) as file:
        config = json.load(file)

    session = session_with_retries()

    session.headers.update({
        "Authorization": f"Bearer {SOURCE_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })

    inventory_response = session.get(
        f"{SOURCE_URL}/api/datasources",
        timeout=TIMEOUT,
        verify=VERIFY_TLS,
    )

    inventory_response.raise_for_status()
    inventory = inventory_response.json()

    valid_uids = {
        item.get("uid")
        for item in inventory
        if item.get("uid")
    }

    LANDING_DIR.mkdir(parents=True, exist_ok=True)

    safe_inventory = [
        {
            "uid": item.get("uid"),
            "name": item.get("name"),
            "type": item.get("type"),
        }
        for item in inventory
    ]

    (LANDING_DIR / "source_datasources.json").write_text(
        json.dumps(safe_inventory, indent=2),
        encoding="utf-8",
    )

    now_ms = int(time.time() * 1000)

    def relative_ms(value):
        match = re.fullmatch(
            r"now-(\d+)([mhd])",
            str(value),
        )

        if not match:
            raise ValueError(
                f"Unsupported time range {value!r}. "
                "Use now-Nm, now-Nh, or now-Nd."
            )

        amount = int(match.group(1))
        factor = {
            "m": 60_000,
            "h": 3_600_000,
            "d": 86_400_000,
        }[match.group(2)]

        return now_ms - amount * factor

    from_ms = relative_ms(
        config.get("default_from", "now-15m")
    )
    to_ms = now_ms

    if from_ms >= to_ms:
        raise ValueError("Source query start must precede its end.")

    report = []

    enabled_queries = [
        item
        for item in config.get("queries", [])
        if item.get("enabled", False)
    ]

    if not enabled_queries:
        raise RuntimeError(
            "No source queries are enabled in source_queries.json."
        )

    for item in enabled_queries:
        name = item.get("name", "unnamed_query")
        uid = item.get("datasource_uid")

        if uid not in valid_uids:
            report.append({
                "query": name,
                "status": "failed",
                "error": "Datasource UID not found",
            })
            continue

        query = dict(item.get("query", {}))
        ref_id = query.get("refId", "A")
        query["refId"] = ref_id

        query["datasource"] = {
            "uid": uid,
            "type": item.get("datasource_type", ""),
        }

        query.setdefault("maxDataPoints", 5000)
        query.setdefault("intervalMs", 15000)

        payload = {
            "from": str(from_ms),
            "to": str(to_ms),
            "queries": [query],
        }

        try:
            response = session.post(
                f"{SOURCE_URL}/api/ds/query",
                json=payload,
                timeout=TIMEOUT,
                verify=VERIFY_TLS,
            )

            response.raise_for_status()
            result = response.json()

            query_result = result.get(
                "results", {}
            ).get(ref_id, {})

            if query_result.get("error"):
                raise RuntimeError(
                    f"Grafana query failed: "
                    f"{query_result['error']}"
                )

            timestamp = datetime.now(
                timezone.utc
            ).strftime("%Y%m%dT%H%M%SZ")

            safe_query_name = re.sub(
                r"[^A-Za-z0-9_.-]",
                "_",
                name,
            )

            output_path = (
                LANDING_DIR
                / "raw"
                / f"{safe_query_name}_{timestamp}.json"
            )

            output_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            output_path.write_text(
                json.dumps(result, indent=2),
                encoding="utf-8",
            )

            report.append({
                "query": name,
                "status": "extracted",
                "raw_response": str(
                    output_path.relative_to(BASE_DIR)
                ),
            })

            log.info(
                "Extracted query '%s' to %s",
                name,
                output_path,
            )

        except Exception as exc:
            report.append({
                "query": name,
                "status": "failed",
                "error": (
                    f"{type(exc).__name__}: {exc}"
                ),
            })

            log.exception(
                "Source query '%s' failed",
                name,
            )

    report_path = LANDING_DIR / "source_extract_report.json"

    report_path.write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(report, indent=2))

    if not report or any(
        item["status"] == "failed"
        for item in report
    ):
        raise SystemExit(1)


# ============================================================
# CLI entry point
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Validate sample telemetry locally, send sample "
            "OTLP telemetry to Grafana Cloud, or extract source "
            "Grafana query results."
        )
    )

    parser.add_argument(
        "--mode",
        choices=["validate", "sample", "extract"],
        required=True,
        help=(
            "validate: local-only JSON structure checks; "
            "sample: send sample OTLP data to Grafana Cloud; "
            "extract: save source Grafana query results."
        ),
    )

    args = parser.parse_args()

    if not VERIFY_TLS:
        log.warning(
            "TLS certificate verification is disabled. "
            "Avoid this outside controlled local testing."
        )

    if args.mode == "validate":
        validate_sample()

    elif args.mode == "sample":
        push_sample()

    elif args.mode == "extract":
        extract_source_queries()


if __name__ == "__main__":
    try:
        main()

    except requests.HTTPError as exc:
        status = (
            exc.response.status_code
            if exc.response is not None
            else "unknown"
        )

        log.error(
            "HTTP request failed (status %s). "
            "Check URL, token, permissions, and query.",
            status,
        )

        sys.exit(1)

    except requests.RequestException as exc:
        log.error(
            "Network request failed: %s",
            type(exc).__name__,
        )
        sys.exit(1)

    except (ValueError, RuntimeError, OSError) as exc:
        log.error("%s", exc)
        sys.exit(1)