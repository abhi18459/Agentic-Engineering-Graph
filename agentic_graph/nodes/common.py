"""Shared state and Codex helpers for Step 2 nodes."""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
from pathlib import Path
from typing import Any

GRAPH_ROOT = Path(__file__).resolve().parents[1]


class NodeError(RuntimeError):
    """Raised when a node cannot produce a valid next state."""


def node_parser(node_name: str, default_prompt: Path) -> argparse.ArgumentParser:
    """Build the common command-line interface used by every node."""
    parser = argparse.ArgumentParser(description=f"Run the {node_name} node")
    parser.add_argument("--input-state", required=True, type=Path)
    parser.add_argument("--output-state", required=True, type=Path)
    parser.add_argument(
        "--prompt-file",
        type=Path,
        default=default_prompt,
        help=f"Prompt template (default: {default_prompt})",
    )
    return parser


def read_state(path: Path, expected_next_node: str) -> dict[str, Any]:
    """Load and minimally validate a node's input state."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise NodeError(f"Input state not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise NodeError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(value, dict):
        raise NodeError(f"Input state must contain a JSON object: {path}")
    if value.get("schema_version") != 1:
        raise NodeError("Only schema_version 1 is supported")
    if not isinstance(value.get("run_id"), str) or not value["run_id"].strip():
        raise NodeError("Input state must contain a non-empty run_id")
    if not isinstance(value.get("task"), dict):
        raise NodeError("Input state must contain a task object")
    if value.get("next_node") != expected_next_node:
        raise NodeError(
            f"Input state points to {value.get('next_node')!r}; "
            f"expected {expected_next_node!r}"
        )
    if not isinstance(value.get("outputs"), dict):
        raise NodeError("Input state must contain an outputs object")
    sequence = value.get("state_sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise NodeError("Input state must contain a non-negative integer state_sequence")
    return value


def read_prompt(path: Path) -> str:
    """Read a non-empty prompt template."""
    try:
        prompt = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise NodeError(f"Prompt file not found: {path}") from exc
    if not prompt:
        raise NodeError(f"Prompt file is empty: {path}")
    return prompt


def resolve_fixture_path(state: dict[str, Any], input_path: Path) -> Path:
    """Resolve fixture_path relative to the run directory when necessary."""
    raw_path = state.get("fixture_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise NodeError("Input state must contain a non-empty fixture_path")

    fixture = Path(raw_path).expanduser()
    if not fixture.is_absolute():
        fixture = input_path.resolve().parent / fixture
    fixture = fixture.resolve()

    if not fixture.is_dir():
        raise NodeError(f"Fixture directory not found: {fixture}")
    try:
        git_check = subprocess.run(
            ["git", "-C", str(fixture), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise NodeError("Git was not found on PATH") from exc
    if git_check.returncode != 0:
        raise NodeError(f"Fixture is not inside a Git working tree: {fixture}")
    return fixture


def task_as_json(state: dict[str, Any]) -> str:
    """Render the task deterministically for inclusion in a node prompt."""
    return json.dumps(state["task"], indent=2, ensure_ascii=False)


def output_content(state: dict[str, Any], node_name: str) -> str:
    """Retrieve a prior node's non-empty content from accumulated outputs."""
    output = state["outputs"].get(node_name)
    if not isinstance(output, dict):
        raise NodeError(f"Input state has no {node_name!r} output")
    content = output.get("content")
    if not isinstance(content, str) or not content.strip():
        raise NodeError(f"Input state has no usable content for {node_name!r}")
    return content


def invoke_codex(fixture: Path, prompt: str, sandbox: str) -> str:
    """Invoke Codex once and return its non-empty final stdout."""
    command = [
        "codex",
        "exec",
        "--cd",
        str(fixture),
        "--sandbox",
        sandbox,
        "--ephemeral",
        "--color",
        "never",
        "-",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=fixture,
            input=prompt,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise NodeError("Codex CLI was not found on PATH") from exc

    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise NodeError(f"Codex invocation failed ({result.returncode}): {details}")

    content = result.stdout.strip()
    if not content:
        raise NodeError("Codex invocation succeeded but returned no output")
    return content


def advance_state(
    state: dict[str, Any],
    *,
    current_node: str,
    next_node: str,
    content: str,
    sandbox: str,
) -> dict[str, Any]:
    """Copy the previous snapshot and add one completed node output."""
    updated = copy.deepcopy(state)
    updated["state_sequence"] += 1
    updated["current_node"] = current_node
    updated["next_node"] = next_node
    updated["status"] = "completed"
    updated["outputs"][current_node] = {
        "content": content,
        "agent_cli": "codex exec",
        "sandbox": sandbox,
    }
    return updated


def write_state(path: Path, state: dict[str, Any], input_path: Path) -> None:
    """Write a new state snapshot without overwriting an existing one."""
    input_parent = input_path.resolve().parent
    output_parent = path.resolve().parent
    if output_parent != input_parent:
        raise NodeError("Input and output state files must be in the same run directory")
    if path.exists():
        raise NodeError(f"Refusing to overwrite existing state: {path}")

    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
    except OSError as exc:
        raise NodeError(f"Could not write state file {path}: {exc}") from exc
