"""Deterministic SonarScanner and Sonar Web API adapter for the review node."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


class SonarError(RuntimeError):
    """Raised when a Sonar analysis result cannot be obtained safely."""


def captured_text(value: str | bytes | None) -> str:
    """Normalize text captured from a process, including timeout output."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def redact_secret(value: str, secret: str) -> str:
    """Remove an authentication token if a subprocess unexpectedly prints it."""
    return value.replace(secret, "<redacted>") if secret else value


def parse_report_task(path: Path) -> dict[str, str]:
    """Parse SonarScanner's report-task.properties-style metadata file."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SonarError(f"Could not read scanner metadata {path}: {exc}") from exc

    metadata: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        metadata[key.strip()] = value.strip()

    required = ("serverUrl", "ceTaskUrl", "ceTaskId", "projectKey")
    missing = [key for key in required if not metadata.get(key)]
    if missing:
        raise SonarError(
            "Scanner metadata is missing required field(s): " + ", ".join(missing)
        )
    return metadata


def api_json(url: str, token: str, timeout: float) -> dict[str, Any]:
    """Read one authenticated JSON object from the Sonar Web API."""
    request = Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        raise SonarError(
            f"Sonar API request failed with HTTP {exc.code}: {url}"
        ) from exc
    except URLError as exc:
        raise SonarError(f"Sonar API request failed for {url}: {exc.reason}") from exc
    except OSError as exc:
        raise SonarError(f"Sonar API request failed for {url}: {exc}") from exc

    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SonarError(f"Sonar API returned invalid JSON for {url}: {exc}") from exc
    if not isinstance(value, dict):
        raise SonarError(f"Sonar API returned a non-object response for {url}")
    return value


def api_url(server_url: str, endpoint: str, parameters: dict[str, str]) -> str:
    """Build a Sonar Web API URL from scanner-provided server metadata."""
    base = server_url.rstrip("/") + "/"
    return f"{urljoin(base, endpoint.lstrip('/'))}?{urlencode(parameters)}"


def paged_results(
    server_url: str,
    endpoint: str,
    item_key: str,
    parameters: dict[str, str],
    token: str,
    timeout: float,
) -> list[dict[str, Any]]:
    """Retrieve every result from a conventional Sonar paginated endpoint."""
    page = 1
    results: list[dict[str, Any]] = []
    while True:
        page_parameters = {**parameters, "p": str(page), "ps": "500"}
        payload = api_json(
            api_url(server_url, endpoint, page_parameters), token, timeout
        )
        raw_items = payload.get(item_key)
        if not isinstance(raw_items, list) or not all(
            isinstance(item, dict) for item in raw_items
        ):
            raise SonarError(
                f"Sonar API response from {endpoint} has no valid {item_key!r} array"
            )
        results.extend(raw_items)

        paging = payload.get("paging")
        if not isinstance(paging, dict):
            break
        total = paging.get("total")
        if not isinstance(total, int) or len(results) >= total or not raw_items:
            break
        page += 1
    return results


def component_path(component: Any, project_key: str) -> str:
    """Remove the project prefix from a Sonar component key when present."""
    if not isinstance(component, str):
        return ""
    prefix = f"{project_key}:"
    return component.removeprefix(prefix)


def finding_line(finding: dict[str, Any]) -> int | None:
    """Return a stable source line from either legacy or current API fields."""
    line = finding.get("line")
    if isinstance(line, int) and not isinstance(line, bool):
        return line
    text_range = finding.get("textRange")
    if isinstance(text_range, dict):
        start_line = text_range.get("startLine")
        if isinstance(start_line, int) and not isinstance(start_line, bool):
            return start_line
    return None


def normalized_impacts(value: Any) -> list[dict[str, str]]:
    """Normalize the multi-quality-rule impacts returned by newer Sonar APIs."""
    if not isinstance(value, list):
        return []
    impacts = [
        {
            "software_quality": str(impact.get("softwareQuality", "")),
            "severity": str(impact.get("severity", "")),
        }
        for impact in value
        if isinstance(impact, dict)
    ]
    return sorted(
        impacts, key=lambda item: (item["software_quality"], item["severity"])
    )


def normalize_issue(issue: dict[str, Any], project_key: str) -> dict[str, Any]:
    """Select stable, repair-relevant fields from one Sonar issue."""
    tags = issue.get("tags")
    normalized_tags = sorted(str(tag) for tag in tags) if isinstance(tags, list) else []
    return {
        "kind": "issue",
        "rule": str(issue.get("rule", "")),
        "type": str(issue.get("type", "")),
        "severity": str(issue.get("severity", "")),
        "impacts": normalized_impacts(issue.get("impacts")),
        "file": component_path(issue.get("component"), project_key),
        "line": finding_line(issue),
        "message": str(issue.get("message", "")),
        "status": str(issue.get("status", "")),
        "tags": normalized_tags,
    }


def normalize_hotspot(hotspot: dict[str, Any], project_key: str) -> dict[str, Any]:
    """Select stable, repair-relevant fields from one Sonar security hotspot."""
    return {
        "kind": "security_hotspot",
        "rule": str(hotspot.get("ruleKey", "")),
        "security_category": str(hotspot.get("securityCategory", "")),
        "type": "SECURITY_HOTSPOT",
        "severity": str(hotspot.get("vulnerabilityProbability", "")),
        "impacts": [],
        "file": component_path(hotspot.get("component"), project_key),
        "line": finding_line(hotspot),
        "message": str(hotspot.get("message", "")),
        "status": str(hotspot.get("status", "")),
        "tags": [],
    }


def normalize_conditions(value: Any) -> list[dict[str, str]]:
    """Normalize and sort quality-gate conditions for stable comparisons."""
    if not isinstance(value, list):
        raise SonarError("Quality-gate response has no valid conditions array")
    conditions = [
        {
            "metric": str(condition.get("metricKey", "")),
            "comparator": str(condition.get("comparator", "")),
            "error_threshold": str(condition.get("errorThreshold", "")),
            "actual_value": str(condition.get("actualValue", "")),
            "status": str(condition.get("status", "")),
        }
        for condition in value
        if isinstance(condition, dict)
    ]
    return sorted(
        conditions,
        key=lambda item: (
            item["metric"],
            item["comparator"],
            item["error_threshold"],
            item["actual_value"],
            item["status"],
        ),
    )


def sort_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort normalized findings without relying on volatile Sonar issue IDs."""
    return sorted(findings, key=lambda item: json.dumps(item, sort_keys=True))


def error_attempt(
    *,
    command: list[str],
    error: str,
    exit_code: int | None = None,
    stdout: str = "",
    stderr: str = "",
) -> dict[str, Any]:
    """Build a state-safe scanner/infrastructure error result."""
    return {
        "result": "scanner_error",
        "passed": False,
        "quality_gate_status": None,
        "conditions": [],
        "findings": [],
        "scanner_exit_code": exit_code,
        "command": command,
        "stdout": stdout,
        "stderr": stderr,
        "error": error,
    }


def execute_sonar_review(
    *,
    fixture: Path,
    base_command: list[str],
    quality_gate_timeout: int,
    process_timeout: float,
    api_timeout: float,
    environment: dict[str, str],
    token: str,
    metadata_path: Path,
) -> dict[str, Any]:
    """Run SonarScanner, query its exact analysis, and return a review attempt."""
    command = [
        *base_command,
        "-Dsonar.qualitygate.wait=true",
        f"-Dsonar.qualitygate.timeout={quality_gate_timeout}",
        f"-Dsonar.scanner.metadataFilePath={metadata_path}",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=fixture,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=process_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return error_attempt(
            command=command,
            error=f"SonarScanner exceeded {process_timeout:g} seconds",
            stdout=redact_secret(captured_text(exc.stdout), token),
            stderr=redact_secret(captured_text(exc.stderr), token),
        )
    except OSError as exc:
        return error_attempt(
            command=command, error=f"Could not start SonarScanner: {exc}"
        )

    stdout = redact_secret(result.stdout, token)
    stderr = redact_secret(result.stderr, token)
    if not metadata_path.is_file():
        return error_attempt(
            command=command,
            exit_code=result.returncode,
            stdout=stdout,
            stderr=stderr,
            error="SonarScanner did not produce report-task metadata",
        )

    try:
        metadata = parse_report_task(metadata_path)
        ce_payload = api_json(metadata["ceTaskUrl"], token, api_timeout)
        task = ce_payload.get("task")
        if not isinstance(task, dict):
            raise SonarError("Compute-engine response has no task object")
        task_status = task.get("status")
        if task_status != "SUCCESS":
            raise SonarError(f"Sonar compute-engine task ended with {task_status!r}")
        analysis_id = task.get("analysisId")
        if not isinstance(analysis_id, str) or not analysis_id:
            raise SonarError("Completed Sonar task has no analysisId")

        gate_payload = api_json(
            api_url(
                metadata["serverUrl"],
                "api/qualitygates/project_status",
                {"analysisId": analysis_id},
            ),
            token,
            api_timeout,
        )
        project_status = gate_payload.get("projectStatus")
        if not isinstance(project_status, dict):
            raise SonarError("Quality-gate response has no projectStatus object")
        gate_status = project_status.get("status")
        if gate_status not in {"OK", "ERROR"}:
            raise SonarError(f"Unexpected Sonar quality-gate status {gate_status!r}")
        conditions = normalize_conditions(project_status.get("conditions"))

        project_key = metadata["projectKey"]
        issues = paged_results(
            metadata["serverUrl"],
            "api/issues/search",
            "issues",
            {"componentKeys": project_key, "resolved": "false"},
            token,
            api_timeout,
        )
        hotspots = paged_results(
            metadata["serverUrl"],
            "api/hotspots/search",
            "hotspots",
            {"projectKey": project_key, "status": "TO_REVIEW"},
            token,
            api_timeout,
        )
        findings = sort_findings(
            [normalize_issue(issue, project_key) for issue in issues]
            + [normalize_hotspot(hotspot, project_key) for hotspot in hotspots]
        )
    except SonarError as exc:
        return error_attempt(
            command=command,
            exit_code=result.returncode,
            stdout=stdout,
            stderr=stderr,
            error=str(exc),
        )

    decision = "passed" if gate_status == "OK" and result.returncode == 0 else "failed"
    if gate_status == "OK" and result.returncode != 0:
        return error_attempt(
            command=command,
            exit_code=result.returncode,
            stdout=stdout,
            stderr=stderr,
            error="SonarScanner failed even though the queried quality gate passed",
        )

    deterministic_result = {
        "decision": decision,
        "quality_gate_status": gate_status,
        "conditions": conditions,
        "findings": findings,
    }
    return {
        "result": decision,
        "passed": decision == "passed",
        "quality_gate_status": gate_status,
        "conditions": conditions,
        "findings": findings,
        "deterministic_result": deterministic_result,
        "scanner_exit_code": result.returncode,
        "command": command,
        "stdout": stdout,
        "stderr": stderr,
        "analysis_id": analysis_id,
        "ce_task_id": metadata["ceTaskId"],
        "dashboard_url": metadata.get("dashboardUrl", ""),
        "project_key": project_key,
    }
