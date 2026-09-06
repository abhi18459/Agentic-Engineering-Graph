#!/usr/bin/env python3
"""Run the selected SonarQube review backend and branch on its result."""

from __future__ import annotations

import fcntl
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from common import (
    GRAPH_ROOT,
    NodeError,
    advance_state,
    node_parser,
    read_state,
    resolve_fixture_path,
    write_state,
)
from mcp_review_client import MCP_BACKEND, execute_sonar_mcp_review
from sonar_client import error_attempt, execute_sonar_review

CLI_BACKEND = "sonar_cli"
REVIEW_BACKENDS = {CLI_BACKEND, MCP_BACKEND}
MCP_PROMPT = GRAPH_ROOT / "prompts" / "review_mcp.txt"
MCP_OUTPUT_SCHEMA = GRAPH_ROOT / "schemas" / "review_mcp_output.schema.json"

RESERVED_PROPERTY_KEYS = (
    "sonar.login",
    "sonar.password",
    "sonar.qualitygate.timeout",
    "sonar.qualitygate.wait",
    "sonar.scanner.metadatafilepath",
    "sonar.token",
)


def positive_number(value: Any, field: str) -> float:
    """Require a positive numeric configuration value."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise NodeError(f"review_config.{field} must be positive")
    return float(value)


def review_configuration(
    state: dict[str, Any],
) -> tuple[list[str], int, float, float, dict[str, str]]:
    """Validate and return the shared Sonar review configuration."""
    config = state.get("review_config")
    if not isinstance(config, dict):
        raise NodeError("Input state must contain a review_config object")

    command = config.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(argument, str) and argument for argument in command)
    ):
        raise NodeError("review_config.command must be a non-empty array of strings")
    if any(
        reserved in argument.lower()
        for argument in command
        for reserved in RESERVED_PROPERTY_KEYS
    ):
        raise NodeError(
            "review_config.command must not override credentials, quality-gate "
            "waiting, timeout, or metadata output"
        )

    raw_gate_timeout = config.get("quality_gate_timeout_seconds", 300)
    gate_timeout = positive_number(raw_gate_timeout, "quality_gate_timeout_seconds")
    if not gate_timeout.is_integer():
        raise NodeError("review_config.quality_gate_timeout_seconds must be an integer")

    process_timeout = positive_number(
        config.get("process_timeout_seconds", gate_timeout + 60),
        "process_timeout_seconds",
    )
    if process_timeout <= gate_timeout:
        raise NodeError(
            "review_config.process_timeout_seconds must exceed the quality-gate timeout"
        )
    api_timeout = positive_number(
        config.get("api_timeout_seconds", 30), "api_timeout_seconds"
    )

    raw_environment = config.get("environment", {})
    if not isinstance(raw_environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in raw_environment.items()
    ):
        raise NodeError("review_config.environment must map strings to strings")
    if any(key.upper().startswith(("SONAR_", "SONARQUBE_")) for key in raw_environment):
        raise NodeError(
            "Sonar connection settings and credentials must be inherited from "
            "the process environment, not persisted in review_config"
        )

    return command, int(gate_timeout), process_timeout, api_timeout, raw_environment


def review_backend(state: dict[str, Any]) -> str:
    """Return a supported review backend, preserving Step 6 as the default."""
    config = state.get("review_config")
    if not isinstance(config, dict):
        raise NodeError("Input state must contain a review_config object")
    backend = config.get("backend", CLI_BACKEND)
    if backend not in REVIEW_BACKENDS:
        raise NodeError(
            f"review_config.backend must be one of {sorted(REVIEW_BACKENDS)!r}"
        )
    return str(backend)


def mcp_process_timeout(state: dict[str, Any]) -> float:
    """Return the bounded timeout for one non-interactive MCP agent turn."""
    config = state["review_config"]
    return positive_number(
        config.get("mcp_process_timeout_seconds", 300),
        "mcp_process_timeout_seconds",
    )


def missing_environment_attempt(
    command: list[str], backend: str, names: list[str]
) -> dict[str, Any]:
    """Return a terminal configuration error without exposing secret values."""
    attempt = error_attempt(
        command=command,
        error=f"Required review environment variable(s) are not set: {', '.join(names)}",
    )
    if backend == MCP_BACKEND:
        attempt["result"] = "agent_error"
    attempt["review_backend"] = backend
    return attempt


@contextmanager
def review_serialization_lock(
    state: dict[str, Any], input_path: Path
) -> Iterator[None]:
    """Serialize a complete scan/query cycle when candidates share a project key."""
    config = state["review_config"]
    raw_path = config.get("serialization_lock")
    if raw_path is None:
        yield
        return
    if not isinstance(raw_path, str) or not raw_path:
        raise NodeError("review_config.serialization_lock must be a relative path")
    relative = Path(raw_path)
    if relative.is_absolute():
        raise NodeError("review_config.serialization_lock must be a relative path")

    run_dir = input_path.resolve().parent
    candidates_root = run_dir.parent.resolve()
    lock_path = (run_dir / relative).resolve()
    if lock_path.parent != candidates_root or lock_path.name != ".sonar-review.lock":
        raise NodeError(
            "review_config.serialization_lock must resolve to ../.sonar-review.lock"
        )
    try:
        handle = lock_path.open("a+", encoding="utf-8")
    except OSError as exc:
        raise NodeError(
            f"Could not use Sonar review serialization lock: {exc}"
        ) from exc
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError as exc:
        handle.close()
        raise NodeError(
            f"Could not use Sonar review serialization lock: {exc}"
        ) from exc
    try:
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def review_route(
    state: dict[str, Any], attempt: dict[str, Any]
) -> tuple[str, str, str | None]:
    """Return next node, workflow status, and optional terminal reason."""
    result = attempt.get("result")
    if result == "passed":
        return "END", "succeeded", None
    if result == "failed" and state["iteration"] < state["max_iterations"]:
        return "fix", "running", None
    if result == "failed":
        return (
            "give_up",
            "gave_up",
            f"Sonar quality gate still fails after {state['max_iterations']} fix attempts",
        )
    return (
        "give_up",
        "error",
        str(attempt.get("error", "Sonar review infrastructure failed")),
    )


def main() -> int:
    args = node_parser("review").parse_args()
    try:
        state = read_state(args.input_state, "review")
        review_attempts = state.get("review_attempts")
        if not isinstance(review_attempts, list):
            raise NodeError("Input state must contain a review_attempts array")
        fixture = resolve_fixture_path(state, args.input_state)
        backend = review_backend(state)
        command, gate_timeout, process_timeout, api_timeout, overrides = (
            review_configuration(state)
        )
        environment = os.environ.copy()
        environment.update(overrides)
        token = environment.get("SONAR_TOKEN", "").strip()

        if not token:
            attempt = missing_environment_attempt(command, backend, ["SONAR_TOKEN"])
        elif backend == MCP_BACKEND and not all(
            environment.get(name, "").strip()
            for name in ("SONARQUBE_TOKEN", "SONARQUBE_ORG")
        ):
            missing = [
                name
                for name in ("SONARQUBE_TOKEN", "SONARQUBE_ORG")
                if not environment.get(name, "").strip()
            ]
            attempt = missing_environment_attempt(command, backend, missing)
        else:
            with review_serialization_lock(state, args.input_state):
                output_parent = args.output_state.resolve().parent
                with tempfile.TemporaryDirectory(
                    dir=output_parent, prefix=".sonar-review-"
                ) as temporary:
                    temporary_root = Path(temporary)
                    if backend == CLI_BACKEND:
                        attempt = execute_sonar_review(
                            fixture=fixture,
                            base_command=command,
                            quality_gate_timeout=gate_timeout,
                            process_timeout=process_timeout,
                            api_timeout=api_timeout,
                            environment=environment,
                            token=token,
                            metadata_path=temporary_root / "report-task.txt",
                        )
                    else:
                        attempt = execute_sonar_mcp_review(
                            fixture=fixture,
                            base_command=command,
                            quality_gate_timeout=gate_timeout,
                            scanner_timeout=process_timeout,
                            agent_timeout=mcp_process_timeout(state),
                            environment=environment,
                            scanner_token=token,
                            mcp_token=environment["SONARQUBE_TOKEN"].strip(),
                            temporary_root=temporary_root,
                            prompt_path=MCP_PROMPT,
                            schema_path=MCP_OUTPUT_SCHEMA,
                        )

        attempt.setdefault("review_backend", backend)

        attempt["attempt"] = len(review_attempts) + 1
        attempt["fix_iteration"] = state["iteration"]
        next_node, workflow_status, reason = review_route(state, attempt)
        updated = advance_state(
            state,
            current_node="review",
            next_node=next_node,
            output=attempt,
            workflow_status=workflow_status,
        )
        updated["review_attempts"].append(attempt)
        if reason is not None:
            updated["give_up_reason"] = reason
        write_state(args.output_state, updated, args.input_state)
    except NodeError as exc:
        print(f"Review node error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
