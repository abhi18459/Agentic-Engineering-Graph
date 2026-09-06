#!/usr/bin/env python3
"""Repair the current fixture after a recorded, repairable test failure."""

from __future__ import annotations

import json
import sys
from typing import Any

from common import (
    GRAPH_ROOT,
    NodeError,
    advance_state,
    approved_plan_content,
    invoke_codex,
    node_parser,
    output_content,
    read_prompt,
    read_state,
    resolve_fixture_path,
    task_as_json,
    write_state,
)

DEFAULT_PROMPT = GRAPH_ROOT / "prompts" / "fix.txt"


def latest_repairable_failure(state: dict[str, Any]) -> dict[str, Any]:
    """Return the most recent test failure that the coding agent may repair."""
    if not state["test_attempts"]:
        raise NodeError("Fix requires at least one recorded test attempt")
    attempt = state["test_attempts"][-1]
    if not isinstance(attempt, dict) or attempt.get("result") != "failed":
        raise NodeError("Fix requires the latest test attempt to be a test failure")
    attempt_number = attempt.get("attempt")
    if not isinstance(attempt_number, int) or isinstance(attempt_number, bool):
        raise NodeError("Latest test failure has no valid attempt number")
    return attempt


def main() -> int:
    args = node_parser("fix", DEFAULT_PROMPT).parse_args()
    try:
        state = read_state(args.input_state, "fix")
        if state["iteration"] >= state["max_iterations"]:
            raise NodeError("Fix iteration limit has already been reached")

        fixture = resolve_fixture_path(state, args.input_state)
        instructions = read_prompt(args.prompt_file)
        plan = approved_plan_content(state)
        previous_code = output_content(state, "code")
        failure = latest_repairable_failure(state)
        prior_fixes = json.dumps(state["fix_attempts"], indent=2, ensure_ascii=False)
        prompt = (
            f"{instructions}\n\n"
            f"## Original task\n\n{task_as_json(state)}\n\n"
            f"## Human-approved plan\n\n{plan}\n\n"
            f"## Previous code proposal\n\n{previous_code}\n\n"
            f"## Latest failing test result\n\n"
            f"{json.dumps(failure, indent=2, ensure_ascii=False)}\n\n"
            f"## Earlier fix attempts\n\n{prior_fixes}\n"
        )
        fix_result = invoke_codex(fixture, prompt, "workspace-write")

        attempt_number = state["iteration"] + 1
        fix_attempt = {
            "attempt": attempt_number,
            "based_on_test_attempt": failure["attempt"],
            "content": fix_result,
            "agent_cli": "codex exec",
            "sandbox": "workspace-write",
        }
        updated = advance_state(
            state,
            current_node="fix",
            next_node="test",
            output=fix_attempt,
        )
        updated["iteration"] = attempt_number
        updated["fix_attempts"].append(fix_attempt)
        write_state(args.output_state, updated, args.input_state)
    except NodeError as exc:
        print(f"Fix node error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
