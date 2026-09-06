#!/usr/bin/env python3
"""Run the deterministic SonarQube quality gate and branch on its result."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from common import (
    NodeError,
    advance_state,
    node_parser,
    read_state,
    resolve_fixture_path,
    write_state,
)
from sonar_client import error_attempt, execute_sonar_review

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
    """Validate and return deterministic Sonar review configuration."""
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
    if any(key.upper().startswith("SONAR_") for key in raw_environment):
        raise NodeError(
            "Sonar connection settings and credentials must be inherited from "
            "the process environment, not persisted in review_config"
        )

    return command, int(gate_timeout), process_timeout, api_timeout, raw_environment


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
        str(attempt.get("error", "SonarScanner or the Sonar API failed")),
    )


def main() -> int:
    args = node_parser("review").parse_args()
    try:
        state = read_state(args.input_state, "review")
        review_attempts = state.get("review_attempts")
        if not isinstance(review_attempts, list):
            raise NodeError("Input state must contain a review_attempts array")
        fixture = resolve_fixture_path(state, args.input_state)
        command, gate_timeout, process_timeout, api_timeout, overrides = (
            review_configuration(state)
        )
        environment = os.environ.copy()
        environment.update(overrides)
        token = environment.get("SONAR_TOKEN", "").strip()

        if not token:
            attempt = error_attempt(
                command=command,
                error="SONAR_TOKEN is not set in the review node environment",
            )
        else:
            output_parent = args.output_state.resolve().parent
            with tempfile.TemporaryDirectory(
                dir=output_parent, prefix=".sonar-review-"
            ) as temporary:
                attempt = execute_sonar_review(
                    fixture=fixture,
                    base_command=command,
                    quality_gate_timeout=gate_timeout,
                    process_timeout=process_timeout,
                    api_timeout=api_timeout,
                    environment=environment,
                    token=token,
                    metadata_path=Path(temporary) / "report-task.txt",
                )

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
