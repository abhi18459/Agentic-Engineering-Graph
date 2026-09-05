#!/usr/bin/env python3
"""List agent-graph runs from their per-run manifests."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import NamedTuple

from run_manifest import MANIFEST_FILENAME, ManifestError, load_manifest, run_is_locked

GRAPH_ROOT = Path(__file__).resolve().parent
DEFAULT_RUNS_DIR = GRAPH_ROOT / "runs"


class RunSummary(NamedTuple):
    run_id: str
    node: str
    status: str
    updated_at: str


def summarize_run(run_dir: Path) -> RunSummary | None:
    """Return one manifest summary, or None for a legacy run."""
    manifest_path = run_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        return None
    manifest = load_manifest(manifest_path)
    status = manifest["workflow_status"]
    if status == "running" and not run_is_locked(run_dir):
        status = "interrupted"
    return RunSummary(
        run_id=manifest["run_id"],
        node=manifest["current_node"],
        status=status,
        updated_at=manifest["updated_at"],
    )


def render_table(rows: list[RunSummary]) -> str:
    """Render aligned, dependency-free terminal output."""
    headings = RunSummary("RUN ID", "CURRENT/NEXT NODE", "STATUS", "LAST UPDATED (UTC)")
    all_rows = [headings, *rows]
    widths = [max(len(str(row[index])) for row in all_rows) for index in range(4)]
    lines = []
    for row_number, row in enumerate(all_rows):
        lines.append(
            "  ".join(
                str(value).ljust(widths[index]) for index, value in enumerate(row)
            )
        )
        if row_number == 0:
            lines.append("  ".join("-" * width for width in widths))
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=DEFAULT_RUNS_DIR,
        help=f"Directory containing run folders (default: {DEFAULT_RUNS_DIR})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runs_dir = args.runs_dir.resolve()
    if not runs_dir.is_dir():
        print(f"Runs directory not found: {runs_dir}")
        return 1

    summaries: list[RunSummary] = []
    invalid_runs: list[tuple[str, str]] = []
    for run_dir in sorted(path for path in runs_dir.iterdir() if path.is_dir()):
        try:
            summary = summarize_run(run_dir)
        except ManifestError as exc:
            invalid_runs.append((run_dir.name, str(exc)))
            continue
        if summary is not None:
            summaries.append(summary)

    if summaries:
        print(render_table(summaries))
    else:
        print("No manifest-backed runs found.")

    if invalid_runs:
        print("\nInvalid manifests:")
        for run_name, error in invalid_runs:
            print(f"- {run_name}: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
