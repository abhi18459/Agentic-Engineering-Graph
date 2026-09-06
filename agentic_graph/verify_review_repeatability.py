#!/usr/bin/env python3
"""Run the review node twice and compare only its deterministic payload."""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

GRAPH_ROOT = Path(__file__).resolve().parent
REVIEW_NODE = GRAPH_ROOT / "nodes" / "review.py"


class RepeatabilityError(RuntimeError):
    """Raised when review cannot be executed or compared safely."""


def read_object(path: Path) -> dict[str, Any]:
    """Read a JSON object from disk."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RepeatabilityError(f"Could not read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RepeatabilityError(f"{path} must contain a JSON object")
    return value


def resolve_fixture(state: dict[str, Any], input_path: Path) -> Path:
    """Resolve the fixture before copying input state into temporary directories."""
    raw_fixture = state.get("fixture_path")
    if not isinstance(raw_fixture, str) or not raw_fixture.strip():
        raise RepeatabilityError("Input state has no valid fixture_path")
    fixture = Path(raw_fixture).expanduser()
    if not fixture.is_absolute():
        fixture = input_path.resolve().parent / fixture
    fixture = fixture.resolve()
    if not fixture.is_dir():
        raise RepeatabilityError(f"Fixture directory not found: {fixture}")
    return fixture


def invoke_review(state: dict[str, Any], fixture: Path, root: Path) -> dict[str, Any]:
    """Invoke the real review node once from an isolated temporary run directory."""
    run_dir = root / "run"
    run_dir.mkdir(parents=True)
    input_state = copy.deepcopy(state)
    input_state["fixture_path"] = str(fixture)
    input_path = run_dir / "00_input.json"
    output_path = run_dir / "01_review.json"
    input_path.write_text(
        json.dumps(input_state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(REVIEW_NODE),
            "--input-state",
            str(input_path),
            "--output-state",
            str(output_path),
        ],
        cwd=GRAPH_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        diagnostic = result.stderr.strip() or result.stdout.strip() or "no output"
        raise RepeatabilityError(
            f"Review node failed with exit code {result.returncode}: {diagnostic}"
        )
    output = read_object(output_path)
    review_output = output.get("outputs", {}).get("review")
    if not isinstance(review_output, dict):
        raise RepeatabilityError("Review output has no outputs.review object")
    deterministic = review_output.get("deterministic_result")
    if not isinstance(deterministic, dict):
        error = review_output.get("error", "no deterministic result")
        raise RepeatabilityError(f"Review did not complete an analysis: {error}")
    return deterministic


def write_report(path: Path, value: dict[str, Any]) -> None:
    """Create a comparison report without overwriting prior evidence."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
    except FileExistsError as exc:
        raise RepeatabilityError(
            f"Refusing to overwrite existing report: {path}"
        ) from exc


def verify(input_path: Path, output_path: Path) -> bool:
    """Run identical review inputs sequentially and persist their comparison."""
    input_path = input_path.resolve()
    state = read_object(input_path)
    if state.get("next_node") != "review":
        raise RepeatabilityError("Repeatability input must point to next_node 'review'")
    fixture = resolve_fixture(state, input_path)

    with tempfile.TemporaryDirectory(prefix="step6-repeatability-") as temporary:
        temporary_root = Path(temporary)
        first = invoke_review(state, fixture, temporary_root / "first")
        second = invoke_review(state, fixture, temporary_root / "second")

    matches = first == second
    write_report(
        output_path,
        {
            "comparison_schema_version": 1,
            "input_state": str(input_path),
            "matches": matches,
            "first": first,
            "second": second,
        },
    )
    return matches


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-state", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        matches = verify(args.input_state, args.output)
    except RepeatabilityError as exc:
        print(f"Repeatability check error: {exc}", file=sys.stderr)
        return 1
    if not matches:
        print(f"Review results differ; inspect {args.output}", file=sys.stderr)
        return 2
    print(f"Review results are identical; wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
