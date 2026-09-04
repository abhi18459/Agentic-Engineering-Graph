#!/usr/bin/env python3
"""Run the configured fixture tests and branch on their result."""

from __future__ import annotations

import os
import subprocess
import sys
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


def test_configuration(
    state: dict[str, Any],
) -> tuple[list[str], float, set[int], dict[str, str]]:
    """Validate and return the deterministic test-runner configuration."""
    config = state["test_config"]

    command = config.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(argument, str) and argument for argument in command)
    ):
        raise NodeError("test_config.command must be a non-empty array of strings")

    timeout = config.get("timeout_seconds", 300)
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or timeout <= 0
    ):
        raise NodeError("test_config.timeout_seconds must be positive")

    raw_repairable_codes = config.get("repairable_exit_codes", [1])
    if not isinstance(raw_repairable_codes, list) or not all(
        isinstance(code, int) and not isinstance(code, bool) and code > 0
        for code in raw_repairable_codes
    ):
        raise NodeError(
            "test_config.repairable_exit_codes must contain positive integers"
        )
    repairable_codes = set(raw_repairable_codes)

    raw_environment = config.get("environment", {})
    if not isinstance(raw_environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in raw_environment.items()
    ):
        raise NodeError("test_config.environment must map strings to strings")

    return command, float(timeout), repairable_codes, raw_environment


def captured_text(value: str | bytes | None) -> str:
    """Normalize output carried by successful and timed-out subprocesses."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def execute_tests(
    fixture: Path,
    command: list[str],
    timeout: float,
    repairable_codes: set[int],
    environment_overrides: dict[str, str],
) -> dict[str, Any]:
    """Run the test process and distinguish test failures from runner failures."""
    environment = os.environ.copy()
    environment.update(environment_overrides)

    try:
        result = subprocess.run(
            command,
            cwd=fixture,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "result": "runner_error",
            "passed": False,
            "exit_code": None,
            "command": command,
            "stdout": captured_text(exc.stdout),
            "stderr": captured_text(exc.stderr),
            "error": f"Test command exceeded {timeout:g} seconds",
        }
    except OSError as exc:
        return {
            "result": "runner_error",
            "passed": False,
            "exit_code": None,
            "command": command,
            "stdout": "",
            "stderr": "",
            "error": f"Could not start test command: {exc}",
        }

    if result.returncode == 0:
        outcome = "passed"
    elif result.returncode in repairable_codes:
        outcome = "failed"
    else:
        outcome = "runner_error"

    attempt: dict[str, Any] = {
        "result": outcome,
        "passed": outcome == "passed",
        "exit_code": result.returncode,
        "command": command,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    if result.returncode < 0:
        attempt["error"] = f"Test process terminated by signal {-result.returncode}"
    elif outcome == "runner_error":
        attempt["error"] = "Test runner did not report a repairable test failure"
    return attempt


def main() -> int:
    args = node_parser("test").parse_args()
    try:
        state = read_state(args.input_state, "test")
        fixture = resolve_fixture_path(state, args.input_state)
        command, timeout, repairable_codes, environment = test_configuration(state)
        attempt = execute_tests(
            fixture, command, timeout, repairable_codes, environment
        )
        attempt["attempt"] = len(state["test_attempts"]) + 1
        attempt["fix_iteration"] = state["iteration"]

        if attempt["result"] == "passed":
            next_node = "END"
            workflow_status = "succeeded"
        elif (
            attempt["result"] == "failed"
            and state["iteration"] < state["max_iterations"]
        ):
            next_node = "fix"
            workflow_status = "running"
        elif attempt["result"] == "failed":
            next_node = "give_up"
            workflow_status = "gave_up"
        else:
            next_node = "give_up"
            workflow_status = "error"

        updated = advance_state(
            state,
            current_node="test",
            next_node=next_node,
            output=attempt,
            workflow_status=workflow_status,
        )
        updated["test_attempts"].append(attempt)
        if workflow_status == "gave_up":
            updated["give_up_reason"] = (
                f"Tests still fail after {state['max_iterations']} fix attempts"
            )
        elif workflow_status == "error":
            updated["give_up_reason"] = attempt.get(
                "error", "The configured test runner failed"
            )
        write_state(args.output_state, updated, args.input_state)
    except NodeError as exc:
        print(f"Test node error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
