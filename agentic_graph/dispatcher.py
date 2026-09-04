#!/usr/bin/env python3
"""Run graph nodes by following next_node in immutable state snapshots."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

GRAPH_ROOT = Path(__file__).resolve().parent
DEFAULT_RUN_DIR = GRAPH_ROOT / "runs" / "run-002"
TERMINAL_NODES = {"END", "give_up"}

# Only trusted node labels can select an executable. State files never supply paths.
NODES: dict[str, Path] = {
    "plan": GRAPH_ROOT / "nodes" / "plan.py",
    "code": GRAPH_ROOT / "nodes" / "code.py",
    "write": GRAPH_ROOT / "nodes" / "write.py",
    "test": GRAPH_ROOT / "nodes" / "test.py",
    "fix": GRAPH_ROOT / "nodes" / "fix.py",
}


class DispatchError(RuntimeError):
    """Raised when the dispatcher cannot safely continue."""


def read_state(path: Path) -> dict[str, Any]:
    """Read a state file and require a JSON object at its root."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DispatchError(f"State file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DispatchError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(value, dict):
        raise DispatchError(f"State file must contain a JSON object: {path}")
    return value


def snapshot_name(sequence: int, node_name: str) -> str:
    """Return the immutable snapshot filename produced by one node."""
    return f"{sequence:02d}_{node_name}.json"


def validate_transition(
    previous: dict[str, Any],
    current: dict[str, Any],
    node_name: str,
    output_path: Path,
) -> str:
    """Validate a node's output and return its trusted next-node label."""
    for field in (
        "schema_version",
        "run_id",
        "task",
        "fixture_path",
        "test_config",
        "max_iterations",
    ):
        if current.get(field) != previous.get(field):
            raise DispatchError(f"{output_path} changed immutable field {field!r}")

    previous_sequence = previous.get("state_sequence")
    if not isinstance(previous_sequence, int) or isinstance(previous_sequence, bool):
        raise DispatchError("Previous state has an invalid state_sequence")
    if current.get("state_sequence") != previous_sequence + 1:
        raise DispatchError(f"{output_path} did not increment state_sequence by one")
    if current.get("current_node") != node_name:
        raise DispatchError(
            f"{output_path} has current_node={current.get('current_node')!r}; "
            f"expected {node_name!r}"
        )
    if current.get("status") != "completed":
        raise DispatchError(f"{output_path} does not record a completed node")

    next_node = current.get("next_node")
    if not isinstance(next_node, str):
        raise DispatchError(f"{output_path} must contain a string next_node")
    if next_node not in NODES and next_node not in TERMINAL_NODES:
        raise DispatchError(
            f"Refusing unknown next_node {next_node!r} in {output_path}"
        )

    workflow_status = current.get("workflow_status")
    expected_status: str | tuple[str, ...]
    if next_node == "END":
        expected_status = "succeeded"
    elif next_node == "give_up":
        expected_status = ("gave_up", "error")
    else:
        expected_status = "running"
    if isinstance(expected_status, tuple):
        status_is_valid = workflow_status in expected_status
    else:
        status_is_valid = workflow_status == expected_status
    if not status_is_valid:
        raise DispatchError(
            f"{output_path} has workflow_status={workflow_status!r}, which is "
            f"inconsistent with next_node={next_node!r}"
        )

    previous_iteration = previous.get("iteration")
    current_iteration = current.get("iteration")
    if (
        not isinstance(previous_iteration, int)
        or isinstance(previous_iteration, bool)
        or not isinstance(current_iteration, int)
        or isinstance(current_iteration, bool)
    ):
        raise DispatchError(f"{output_path} has an invalid iteration value")
    expected_iteration = (
        previous_iteration + 1 if node_name == "fix" else previous_iteration
    )
    if current_iteration != expected_iteration:
        raise DispatchError(f"{output_path} has an invalid iteration transition")

    for history_name, producing_node in (
        ("test_attempts", "test"),
        ("fix_attempts", "fix"),
    ):
        previous_history = previous.get(history_name)
        current_history = current.get(history_name)
        if not isinstance(previous_history, list) or not isinstance(
            current_history, list
        ):
            raise DispatchError(f"{output_path} has an invalid {history_name}")
        expected_length = len(previous_history) + int(node_name == producing_node)
        if len(current_history) != expected_length:
            raise DispatchError(
                f"{output_path} has an invalid number of {history_name} entries"
            )
        if current_history[: len(previous_history)] != previous_history:
            raise DispatchError(f"{output_path} rewrote prior {history_name}")

    return next_node


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=DEFAULT_RUN_DIR,
        help=f"Run directory containing 00_input.json (default: {DEFAULT_RUN_DIR})",
    )
    parser.add_argument(
        "--start",
        choices=tuple(NODES),
        help="First node to run (default: use next_node from 00_input.json)",
    )
    return parser.parse_args()


def dispatch(run_dir: Path, requested_start: str | None) -> int:
    """Run nodes until state points to END or give_up."""
    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        raise DispatchError(f"Run directory not found: {run_dir}")

    input_path = run_dir / "00_input.json"
    input_state = read_state(input_path)
    state_start = input_state.get("next_node")
    current_node = requested_start or state_start
    if not isinstance(current_node, str) or current_node not in NODES:
        raise DispatchError(f"Initial state points to unknown node {current_node!r}")
    if state_start != current_node:
        raise DispatchError(
            f"{input_path} points to {state_start!r}, not requested node "
            f"{current_node!r}"
        )

    while True:
        script = NODES[current_node]
        input_state = read_state(input_path)
        if input_state.get("next_node") != current_node:
            raise DispatchError(
                f"{input_path} points to {input_state.get('next_node')!r}, "
                f"not requested node {current_node!r}"
            )

        sequence = input_state.get("state_sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise DispatchError(f"{input_path} has an invalid state_sequence")
        output_path = run_dir / snapshot_name(sequence + 1, current_node)
        if output_path.exists():
            raise DispatchError(
                f"Refusing to overwrite existing state: {output_path}. "
                "Use a fresh run directory. Resume support is added in Step 4."
            )

        command = [
            sys.executable,
            str(script),
            "--input-state",
            str(input_path),
            "--output-state",
            str(output_path),
        ]
        print(f"Running {current_node}: {script.name}", flush=True)
        result = subprocess.run(command, cwd=GRAPH_ROOT, check=False)
        if result.returncode != 0:
            raise DispatchError(
                f"Node {current_node!r} failed with exit code {result.returncode}"
            )

        output_state = read_state(output_path)
        next_node = validate_transition(
            input_state, output_state, current_node, output_path
        )
        print(f"Wrote {output_path}", flush=True)

        if next_node == "END":
            print("Workflow reached END: tests passed", flush=True)
            return 0
        if next_node == "give_up":
            reason = output_state.get("give_up_reason", "repair limit exhausted")
            print(f"Workflow reached give_up: {reason}", file=sys.stderr, flush=True)
            return 2

        input_path = output_path
        current_node = next_node


def main() -> int:
    args = parse_args()
    try:
        return dispatch(args.run_dir, args.start)
    except DispatchError as exc:
        print(f"Dispatcher error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
