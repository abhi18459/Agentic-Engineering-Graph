#!/usr/bin/env python3
"""Produce an implementation approach without changing the fixture."""

from __future__ import annotations

import sys

from common import (
    GRAPH_ROOT,
    NodeError,
    advance_state,
    invoke_codex,
    node_parser,
    read_prompt,
    read_state,
    resolve_fixture_path,
    task_as_json,
    write_state,
)

DEFAULT_PROMPT = GRAPH_ROOT / "prompts" / "plan.txt"


def main() -> int:
    args = node_parser("plan", DEFAULT_PROMPT).parse_args()
    try:
        state = read_state(args.input_state, "plan")
        fixture = resolve_fixture_path(state, args.input_state)
        instructions = read_prompt(args.prompt_file)
        prompt = f"{instructions}\n\n## Task\n\n{task_as_json(state)}\n"
        plan = invoke_codex(fixture, prompt, "read-only")
        updated = advance_state(
            state,
            current_node="plan",
            next_node="code",
            content=plan,
            sandbox="read-only",
        )
        write_state(args.output_state, updated, args.input_state)
    except NodeError as exc:
        print(f"Plan node error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
