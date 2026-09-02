#!/usr/bin/env python3
"""Apply the persisted implementation proposal to the fixture."""

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

DEFAULT_PROMPT = GRAPH_ROOT / "prompts" / "write.txt"


def main() -> int:
    args = node_parser("write", DEFAULT_PROMPT).parse_args()
    try:
        state = read_state(args.input_state, "write")
        fixture = resolve_fixture_path(state, args.input_state)
        instructions = read_prompt(args.prompt_file)
        plan = output_content(state, "plan")
        code_attempt = output_content(state, "code")
        prompt = (
            f"{instructions}\n\n"
            f"## Task\n\n{task_as_json(state)}\n\n"
            f"## Persisted plan\n\n{plan}\n\n"
            f"## Persisted implementation proposal\n\n{code_attempt}\n"
        )
        write_result = invoke_codex(fixture, prompt, "workspace-write")
        updated = advance_state(
            state,
            current_node="write",
            next_node="END",
            content=write_result,
            sandbox="workspace-write",
        )
        write_state(args.output_state, updated, args.input_state)
    except NodeError as exc:
        print(f"Write node error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
