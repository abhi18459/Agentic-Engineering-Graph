#!/usr/bin/env python3
"""Produce a proposed implementation based on the persisted plan."""

from __future__ import annotations

import sys

from common import (
    GRAPH_ROOT,
    NodeError,
    advance_state,
    invoke_codex,
    node_parser,
    output_content,
    read_prompt,
    read_state,
    resolve_fixture_path,
    task_as_json,
    write_state,
)

DEFAULT_PROMPT = GRAPH_ROOT / "prompts" / "code.txt"


def main() -> int:
    args = node_parser("code", DEFAULT_PROMPT).parse_args()
    try:
        state = read_state(args.input_state, "code")
        fixture = resolve_fixture_path(state, args.input_state)
        instructions = read_prompt(args.prompt_file)
        plan = output_content(state, "plan")
        prompt = (
            f"{instructions}\n\n"
            f"## Task\n\n{task_as_json(state)}\n\n"
            f"## Persisted plan\n\n{plan}\n"
        )
        code_attempt = invoke_codex(fixture, prompt, "read-only")
        updated = advance_state(
            state,
            current_node="code",
            next_node="write",
            output={
                "content": code_attempt,
                "agent_cli": "codex exec",
                "sandbox": "read-only",
            },
        )
        write_state(args.output_state, updated, args.input_state)
    except NodeError as exc:
        print(f"Code node error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
