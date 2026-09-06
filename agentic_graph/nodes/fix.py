#!/usr/bin/env python3
"""Repair the current fixture after a test or quality-gate failure."""

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


def latest_repairable_failure(
    state: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Return the test or review failure that routed directly to fix."""
    trigger_node = state.get("current_node")
    if trigger_node == "test":
        history_name = "test_attempts"
    elif trigger_node == "review":
        history_name = "review_attempts"
    else:
        raise NodeError("Fix must be routed directly from test or review")

    history = state.get(history_name)
    if not isinstance(history, list) or not history:
        raise NodeError(f"Fix requires a recorded {trigger_node} attempt")
    attempt = history[-1]
    if not isinstance(attempt, dict) or attempt.get("result") != "failed":
        raise NodeError(
            f"Fix requires the latest {trigger_node} attempt to have failed"
        )
    attempt_number = attempt.get("attempt")
    if not isinstance(attempt_number, int) or isinstance(attempt_number, bool):
        raise NodeError(f"Latest {trigger_node} failure has no valid attempt number")
    return trigger_node, attempt


def repair_context(trigger_node: str, attempt: dict[str, Any]) -> dict[str, Any]:
    """Keep repair evidence while omitting bulky review transport diagnostics."""
    if trigger_node != "review":
        return attempt
    fields = (
        "attempt",
        "result",
        "quality_gate_status",
        "conditions",
        "findings",
        "error",
        "review_backend",
    )
    return {field: attempt[field] for field in fields if field in attempt}


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
        trigger_node, failure = latest_repairable_failure(state)
        prior_fixes = json.dumps(state["fix_attempts"], indent=2, ensure_ascii=False)
        prompt = (
            f"{instructions}\n\n"
            f"## Original task\n\n{task_as_json(state)}\n\n"
            f"## Human-approved plan\n\n{plan}\n\n"
            f"## Previous code proposal\n\n{previous_code}\n\n"
            f"## Repair trigger\n\nNode: {trigger_node}\n\n"
            f"{json.dumps(repair_context(trigger_node, failure), indent=2, ensure_ascii=False)}\n\n"
            f"## Earlier fix attempts\n\n{prior_fixes}\n"
        )
        fix_result = invoke_codex(fixture, prompt, "workspace-write")

        attempt_number = state["iteration"] + 1
        fix_attempt = {
            "attempt": attempt_number,
            "trigger": {"node": trigger_node, "attempt": failure["attempt"]},
            "content": fix_result,
            "agent_cli": "codex exec",
            "sandbox": "workspace-write",
        }
        fix_attempt[f"based_on_{trigger_node}_attempt"] = failure["attempt"]
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
