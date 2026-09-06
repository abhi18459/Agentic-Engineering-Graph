"""AI-backed SonarQube MCP adapter for the review node."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from sonar_client import (
    captured_text,
    execute_sonar_scan,
    redact_secret,
    sort_findings,
)

MCP_SERVER = "sonarqube"
MCP_BACKEND = "sonarqube_mcp"
REQUIRED_TOOLS = (
    "get_project_quality_gate_status",
    "search_sonar_issues_in_projects",
    "search_security_hotspots",
)
FORBIDDEN_ITEM_TYPES = {
    "command_execution",
    "file_change",
    "web_search",
}
CONDITION_KEYS = {
    "metric",
    "comparator",
    "error_threshold",
    "actual_value",
    "status",
}
COMMON_FINDING_KEYS = {
    "kind",
    "rule",
    "type",
    "severity",
    "impacts",
    "file",
    "line",
    "message",
    "status",
    "tags",
}


class McpReviewError(RuntimeError):
    """Raised when an agent result or its MCP provenance is unsafe to trust."""


def redacted(value: str, secrets: tuple[str, ...]) -> str:
    """Redact every non-empty secret from captured subprocess text."""
    for secret in secrets:
        value = redact_secret(value, secret)
    return value


def redacted_value(value: Any, secrets: tuple[str, ...]) -> Any:
    """Redact secrets recursively without changing JSON-compatible types."""
    if isinstance(value, str):
        return redacted(value, secrets)
    if isinstance(value, list):
        return [redacted_value(item, secrets) for item in value]
    if isinstance(value, dict):
        return {str(key): redacted_value(item, secrets) for key, item in value.items()}
    return value


def canonical_tool_name(value: Any) -> str:
    """Return an unprefixed MCP tool name from a JSONL event field."""
    if not isinstance(value, str):
        return ""
    name = value
    if "__" in name:
        name = name.rsplit("__", 1)[-1]
    if "." in name:
        name = name.rsplit(".", 1)[-1]
    return name


def object_arguments(value: Any) -> dict[str, Any]:
    """Normalize MCP call arguments that may be emitted as JSON text."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise McpReviewError(f"MCP tool arguments are invalid JSON: {exc}") from exc
        if isinstance(decoded, dict):
            return decoded
    raise McpReviewError("MCP tool arguments must be a JSON object")


def mcp_tool_calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract completed MCP calls and reject non-MCP execution tools."""
    calls: list[dict[str, Any]] = []
    for event in events:
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in FORBIDDEN_ITEM_TYPES:
            raise McpReviewError(
                f"Review agent used forbidden Codex tool type {item_type!r}"
            )
        if event.get("type") != "item.completed" or item_type != "mcp_tool_call":
            continue

        server = item.get("server") or item.get("server_name")
        tool = canonical_tool_name(
            item.get("tool") or item.get("tool_name") or item.get("name")
        )
        calls.append(
            {
                "id": str(item.get("id", "")),
                "server": str(server or ""),
                "tool": tool,
                "arguments": object_arguments(item.get("arguments", {})),
                "status": str(item.get("status", "completed")),
                "result": item.get("result"),
                "error": item.get("error"),
            }
        )
    return calls


def successful_call(call: dict[str, Any]) -> bool:
    """Return whether one captured MCP event records a completed call."""
    status = str(call.get("status", "")).lower()
    return (
        call.get("error") in (None, "")
        and call.get("result") is not None
        and status in {"completed", "success", "succeeded"}
    )


def call_targets_project(call: dict[str, Any], project_key: str) -> bool:
    """Require each Sonar tool call to target the scanner-reported project."""
    arguments = call["arguments"]
    tool = call["tool"]
    if tool == "search_sonar_issues_in_projects":
        projects = arguments.get("projects")
        statuses = arguments.get("issueStatuses")
        return (
            projects == [project_key]
            and isinstance(statuses, list)
            and all(isinstance(status, str) for status in statuses)
            and set(statuses) == {"OPEN", "CONFIRMED"}
            and arguments.get("ps") == 500
        )
    if tool == "search_security_hotspots":
        return (
            arguments.get("projectKey") == project_key
            and arguments.get("status") == "TO_REVIEW"
            and arguments.get("ps") == 500
        )
    return arguments == {"projectKey": project_key}


def validate_tool_calls(
    calls: list[dict[str, Any]], project_key: str
) -> list[dict[str, Any]]:
    """Prove every required read-only Sonar query completed for this project."""
    unexpected = [
        call["tool"]
        for call in calls
        if call["server"] != MCP_SERVER or call["tool"] not in REQUIRED_TOOLS
    ]
    if unexpected:
        raise McpReviewError(
            "Review agent made an unexpected MCP call: " + ", ".join(unexpected)
        )

    for tool in REQUIRED_TOOLS:
        matching = [
            call
            for call in calls
            if call["server"] == MCP_SERVER
            and call["tool"] == tool
            and successful_call(call)
            and call_targets_project(call, project_key)
        ]
        if not matching:
            raise McpReviewError(
                f"No successful {MCP_SERVER}.{tool} call targeted {project_key!r}"
            )
    return calls


def parse_jsonl(value: str) -> list[dict[str, Any]]:
    """Parse Codex JSONL output and require one completed turn."""
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(value.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise McpReviewError(
                f"Codex JSONL line {line_number} is invalid: {exc}"
            ) from exc
        if not isinstance(event, dict):
            raise McpReviewError(f"Codex JSONL line {line_number} is not an object")
        events.append(event)

    if not events:
        raise McpReviewError("Codex emitted no JSONL events")
    if any(event.get("type") in {"error", "turn.failed"} for event in events):
        raise McpReviewError("Codex JSONL recorded an error or failed turn")
    if not any(event.get("type") == "turn.completed" for event in events):
        raise McpReviewError("Codex JSONL has no completed turn")
    return events


def require_exact_keys(value: dict[str, Any], expected: set[str], name: str) -> None:
    """Require a schema object's keys even if CLI schema validation was bypassed."""
    if set(value) != expected:
        raise McpReviewError(f"{name} does not match the required field set")


def normalized_conditions(value: Any) -> list[dict[str, str]]:
    """Validate and sort agent-normalized quality-gate conditions."""
    if not isinstance(value, list):
        raise McpReviewError("Agent result conditions must be an array")
    conditions: list[dict[str, str]] = []
    for condition in value:
        if not isinstance(condition, dict):
            raise McpReviewError("Each quality-gate condition must be an object")
        require_exact_keys(condition, CONDITION_KEYS, "Quality-gate condition")
        if not all(isinstance(condition[key], str) for key in CONDITION_KEYS):
            raise McpReviewError("Quality-gate condition fields must be strings")
        conditions.append({key: condition[key] for key in CONDITION_KEYS})
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


def normalized_impacts(value: Any) -> list[dict[str, str]]:
    """Validate and sort normalized software-quality impacts."""
    if not isinstance(value, list):
        raise McpReviewError("Finding impacts must be an array")
    impacts: list[dict[str, str]] = []
    for impact in value:
        if not isinstance(impact, dict):
            raise McpReviewError("Each finding impact must be an object")
        require_exact_keys(impact, {"software_quality", "severity"}, "Impact")
        if not all(isinstance(impact[key], str) for key in impact):
            raise McpReviewError("Finding impact fields must be strings")
        impacts.append(
            {
                "software_quality": impact["software_quality"],
                "severity": impact["severity"],
            }
        )
    return sorted(
        impacts, key=lambda item: (item["software_quality"], item["severity"])
    )


def normalized_finding(value: Any) -> dict[str, Any]:
    """Validate one agent-normalized issue or Security Hotspot."""
    if not isinstance(value, dict):
        raise McpReviewError("Each finding must be an object")
    kind = value.get("kind")
    expected_keys = set(COMMON_FINDING_KEYS)
    if kind == "security_hotspot":
        expected_keys.add("security_category")
    elif kind != "issue":
        raise McpReviewError(f"Unknown finding kind {kind!r}")
    require_exact_keys(value, expected_keys, "Finding")

    string_keys = {"kind", "rule", "type", "severity", "file", "message", "status"}
    if kind == "security_hotspot":
        string_keys.add("security_category")
    if not all(isinstance(value[key], str) for key in string_keys):
        raise McpReviewError("Finding text fields must be strings")
    line = value["line"]
    if line is not None and (
        not isinstance(line, int) or isinstance(line, bool) or line < 1
    ):
        raise McpReviewError("Finding line must be a positive integer or null")
    tags = value["tags"]
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise McpReviewError("Finding tags must be an array of strings")
    if kind == "security_hotspot" and value["type"] != "SECURITY_HOTSPOT":
        raise McpReviewError("Security Hotspot type must be SECURITY_HOTSPOT")

    finding = {key: value[key] for key in expected_keys}
    finding["impacts"] = normalized_impacts(value["impacts"])
    finding["tags"] = sorted(tags)
    return finding


def validate_agent_result(value: Any) -> dict[str, Any]:
    """Validate the final structured response and derive its trusted decision."""
    if not isinstance(value, dict):
        raise McpReviewError("Codex final response must be a JSON object")
    require_exact_keys(
        value,
        {"decision", "quality_gate_status", "conditions", "findings"},
        "Codex final response",
    )
    gate_status = value["quality_gate_status"]
    if gate_status not in {"OK", "ERROR"}:
        raise McpReviewError(f"Unexpected quality-gate status {gate_status!r}")
    decision = "passed" if gate_status == "OK" else "failed"
    if value["decision"] != decision:
        raise McpReviewError("Agent decision contradicts the quality-gate status")
    findings_value = value["findings"]
    if not isinstance(findings_value, list):
        raise McpReviewError("Agent result findings must be an array")
    conditions = normalized_conditions(value["conditions"])
    findings = sort_findings([normalized_finding(item) for item in findings_value])
    return {
        "decision": decision,
        "quality_gate_status": gate_status,
        "conditions": conditions,
        "findings": findings,
    }


def codex_review_command(
    fixture: Path, schema_path: Path, final_path: Path
) -> list[str]:
    """Build a read-only Codex command that loads trusted project MCP config."""
    return [
        "codex",
        "exec",
        "--cd",
        str(fixture),
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--strict-config",
        "--color",
        "never",
        "--json",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(final_path),
        "-",
    ]


def scan_fields(scan: dict[str, Any]) -> dict[str, Any]:
    """Return the Step 6-compatible scanner and analysis provenance fields."""
    metadata = scan["metadata"]
    return {
        "scanner_exit_code": scan["scanner_exit_code"],
        "command": scan["command"],
        "stdout": scan["stdout"],
        "stderr": scan["stderr"],
        "analysis_id": None,
        "ce_task_id": metadata["ceTaskId"],
        "dashboard_url": metadata.get("dashboardUrl", ""),
        "project_key": metadata["projectKey"],
    }


def agent_error_attempt(
    *,
    scan: dict[str, Any],
    agent_command: list[str],
    error: str,
    agent_exit_code: int | None = None,
    agent_stdout: str = "",
    agent_stderr: str = "",
    calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a state-safe Codex or MCP infrastructure error result."""
    return {
        "result": "agent_error",
        "passed": False,
        "quality_gate_status": None,
        "conditions": [],
        "findings": [],
        **scan_fields(scan),
        "review_backend": MCP_BACKEND,
        "agent_cli": "codex exec",
        "agent_command": agent_command,
        "agent_exit_code": agent_exit_code,
        "agent_stdout": agent_stdout,
        "agent_stderr": agent_stderr,
        "mcp_server": MCP_SERVER,
        "mcp_tool_calls": calls or [],
        "error": error,
    }


def read_final_result(path: Path) -> Any:
    """Read the structured final Codex message from its exclusive temp path."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise McpReviewError(
            "Codex did not write its final structured response"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise McpReviewError(
            f"Could not read Codex structured response: {exc}"
        ) from exc


def execute_sonar_mcp_review(
    *,
    fixture: Path,
    base_command: list[str],
    quality_gate_timeout: int,
    scanner_timeout: float,
    agent_timeout: float,
    environment: dict[str, str],
    scanner_token: str,
    mcp_token: str,
    temporary_root: Path,
    prompt_path: Path,
    schema_path: Path,
) -> dict[str, Any]:
    """Scan current code, then let a read-only Codex agent query Sonar through MCP."""
    scan = execute_sonar_scan(
        fixture=fixture,
        base_command=base_command,
        quality_gate_timeout=quality_gate_timeout,
        process_timeout=scanner_timeout,
        environment=environment,
        token=scanner_token,
        metadata_path=temporary_root / "report-task.txt",
    )
    if scan.get("result") == "scanner_error":
        scan["review_backend"] = MCP_BACKEND
        return scan

    project_key = scan["metadata"]["projectKey"]
    try:
        template = prompt_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return agent_error_attempt(
            scan=scan,
            agent_command=[],
            error=f"Could not read MCP review prompt: {exc}",
        )
    if not template or "{{PROJECT_KEY}}" not in template:
        return agent_error_attempt(
            scan=scan,
            agent_command=[],
            error="MCP review prompt is empty or lacks {{PROJECT_KEY}}",
        )
    if not schema_path.is_file():
        return agent_error_attempt(
            scan=scan,
            agent_command=[],
            error=f"MCP review output schema was not found: {schema_path}",
        )

    prompt = template.replace("{{PROJECT_KEY}}", project_key)
    final_path = temporary_root / "agent-final.json"
    agent_command = codex_review_command(fixture, schema_path, final_path)
    secrets = tuple(secret for secret in (scanner_token, mcp_token) if secret)
    try:
        result = subprocess.run(
            agent_command,
            cwd=fixture,
            env=environment,
            input=prompt,
            text=True,
            capture_output=True,
            check=False,
            timeout=agent_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return agent_error_attempt(
            scan=scan,
            agent_command=agent_command,
            error=f"Codex MCP review exceeded {agent_timeout:g} seconds",
            agent_stdout=redacted(captured_text(exc.stdout), secrets),
            agent_stderr=redacted(captured_text(exc.stderr), secrets),
        )
    except OSError as exc:
        return agent_error_attempt(
            scan=scan,
            agent_command=agent_command,
            error=f"Could not start Codex MCP review: {exc}",
        )

    agent_stdout = redacted(result.stdout, secrets)
    agent_stderr = redacted(result.stderr, secrets)
    calls: list[dict[str, Any]] = []
    try:
        events = parse_jsonl(agent_stdout)
        calls = mcp_tool_calls(events)
        calls = validate_tool_calls(calls, project_key)
        final_result = redacted_value(read_final_result(final_path), secrets)
        deterministic = validate_agent_result(final_result)
    except McpReviewError as exc:
        return agent_error_attempt(
            scan=scan,
            agent_command=agent_command,
            agent_exit_code=result.returncode,
            agent_stdout=agent_stdout,
            agent_stderr=agent_stderr,
            calls=redacted_value(calls, secrets),
            error=str(exc),
        )
    if result.returncode != 0:
        return agent_error_attempt(
            scan=scan,
            agent_command=agent_command,
            agent_exit_code=result.returncode,
            agent_stdout=agent_stdout,
            agent_stderr=agent_stderr,
            calls=redacted_value(calls, secrets),
            error=f"Codex MCP review exited with status {result.returncode}",
        )

    decision = deterministic["decision"]
    scanner_exit_code = scan["scanner_exit_code"]
    if decision == "passed" and scanner_exit_code != 0:
        return agent_error_attempt(
            scan=scan,
            agent_command=agent_command,
            agent_exit_code=result.returncode,
            agent_stderr=agent_stderr,
            calls=redacted_value(calls, secrets),
            error="SonarScanner failed even though the MCP quality gate passed",
        )

    return {
        "result": decision,
        "passed": decision == "passed",
        "quality_gate_status": deterministic["quality_gate_status"],
        "conditions": deterministic["conditions"],
        "findings": deterministic["findings"],
        "deterministic_result": deterministic,
        **scan_fields(scan),
        "review_backend": MCP_BACKEND,
        "agent_cli": "codex exec",
        "agent_command": agent_command,
        "agent_exit_code": result.returncode,
        "agent_stderr": agent_stderr,
        "mcp_server": MCP_SERVER,
        "mcp_tool_calls": redacted_value(calls, secrets),
    }
