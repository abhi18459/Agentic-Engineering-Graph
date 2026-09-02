#!/usr/bin/env python3
"""Run Step 2 nodes by following next_node in persisted state files."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GRAPH_ROOT = Path(__file__).resolve().parent
DEFAULT_RUN_DIR = GRAPH_ROOT / "runs" / "run-001"
END = "END"


@dataclass(frozen=True)
class NodeSpec:
    """Trusted dispatcher configuration for one node."""

    script: Path
    input_file: str
    output_file: str


NODES: dict[str, NodeSpec] = {
    "plan": NodeSpec(
        GRAPH_ROOT / "nodes" / "plan.py", "00_input.json", "01_plan.json"
    ),
    "code": NodeSpec(
        GRAPH_ROOT / "nodes" / "code.py", "01_plan.json", "02_code.json"
    ),
    "write": NodeSpec(
        GRAPH_ROOT / "nodes" / "write.py", "02_code.json", "03_write.json"
    ),
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


def validate_transition(
    previous: dict[str, Any],
    current: dict[str, Any],
    node_name: str,
    output_path: Path,
) -> str:
    """Validate a node's output and return its trusted next-node label."""
    if current.get("schema_version") != previous.get("schema_version"):
        raise DispatchError(f"{output_path} changed schema_version")
    if current.get("run_id") != previous.get("run_id"):
        raise DispatchError(f"{output_path} changed run_id")
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
    if next_node != END and next_node not in NODES:
        raise DispatchError(f"Refusing unknown next_node {next_node!r} in {output_path}")
    return next_node


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=DEFAULT_RUN_DIR,
        help=f"Run directory containing state files (default: {DEFAULT_RUN_DIR})",
    )
    parser.add_argument(
        "--start",
        choices=tuple(NODES),
        default="plan",
        help="First node to run (default: plan)",
    )
    return parser.parse_args()


def dispatch(run_dir: Path, start_node: str) -> None:
    """Run nodes in sequence until a state file points to END."""
    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        raise DispatchError(f"Run directory not found: {run_dir}")

    current_node = start_node

    while current_node != END:
        spec = NODES[current_node]
        input_path = run_dir / spec.input_file
        output_path = run_dir / spec.output_file
        input_state = read_state(input_path)

        if input_state.get("next_node") != current_node:
            raise DispatchError(
                f"{input_path} points to {input_state.get('next_node')!r}, "
                f"not requested node {current_node!r}"
            )
        if output_path.exists():
            raise DispatchError(
                f"Refusing to overwrite existing state: {output_path}. "
                "Use a fresh run directory."
            )

        command = [
            sys.executable,
            str(spec.script),
            "--input-state",
            str(input_path),
            "--output-state",
            str(output_path),
        ]
        print(f"Running {current_node}: {spec.script.name}", flush=True)
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

        if next_node == END:
            print("Workflow reached END", flush=True)
            return

        next_spec = NODES[next_node]
        if next_spec.input_file != spec.output_file:
            raise DispatchError(
                f"Node registry is not contiguous: {current_node!r} writes "
                f"{spec.output_file}, but {next_node!r} reads {next_spec.input_file}"
            )
        current_node = next_node


def main() -> int:
    args = parse_args()
    try:
        dispatch(args.run_dir, args.start)
    except DispatchError as exc:
        print(f"Dispatcher error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
