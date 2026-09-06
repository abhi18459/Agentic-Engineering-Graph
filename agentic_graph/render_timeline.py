#!/usr/bin/env python3
"""Render a manifest-backed graph run as a chronological Markdown timeline."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from run_manifest import (
    MANIFEST_FILENAME,
    ManifestError,
    elapsed_wall_time,
    load_manifest,
)


def markdown_text(value: object) -> str:
    """Return a value safe for one Markdown table cell."""
    return str(value).replace("|", "\\|").replace("\n", " ")


def attempt_duration(attempt: dict[str, Any]) -> float | None:
    """Return persisted duration, with a legacy timestamp fallback."""
    duration = attempt.get("duration_seconds")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        return float(duration)
    completed_at = attempt.get("completed_at")
    if not isinstance(completed_at, str):
        return None
    return elapsed_wall_time(attempt["started_at"], completed_at)


def format_duration(seconds: float | None, status: str) -> str:
    """Format elapsed seconds compactly for a human-readable timeline."""
    if seconds is None:
        return "in progress" if status == "running" else "unknown"
    if seconds < 0.001:
        return "<0.001 s"
    if seconds < 60:
        return f"{seconds:.3f} s"
    minutes, remainder = divmod(seconds, 60)
    return f"{int(minutes)}m {remainder:05.2f}s"


def render_manifest(
    manifest: dict[str, Any], *, heading_level: int = 1, label: str | None = None
) -> str:
    """Render one validated manifest without collapsing repeated nodes."""
    heading = "#" * heading_level
    title = label or manifest["run_id"]
    lines = [
        f"{heading} Run timeline: {markdown_text(title)}",
        "",
        f"- Run ID: `{markdown_text(manifest['run_id'])}`",
        f"- Workflow status: `{markdown_text(manifest['workflow_status'])}`",
        f"- Current/next node: `{markdown_text(manifest['current_node'])}`",
        "",
    ]
    attempts = manifest["attempts"]
    if not attempts:
        lines.append("No node attempts have been recorded.")
        return "\n".join(lines)

    lines.extend(
        [
            "| # | Started (UTC) | Completed (UTC) | Node | Status | Duration | Next node |",
            "|---:|---|---|---|---|---:|---|",
        ]
    )
    known_durations: list[float] = []
    for attempt in attempts:
        duration = attempt_duration(attempt)
        if duration is not None:
            known_durations.append(duration)
        status = attempt["status"]
        if attempt.get("recovered"):
            status += " (recovered)"
        next_node = attempt.get("next_node") or "—"
        completed_at = attempt.get("completed_at") or "—"
        lines.append(
            "| {attempt_id} | {started_at} | {completed_at} | `{node}` | {status} | "
            "{duration} | `{next_node}` |".format(
                attempt_id=attempt["attempt_id"],
                started_at=markdown_text(attempt["started_at"]),
                completed_at=markdown_text(completed_at),
                node=markdown_text(attempt["node"]),
                status=markdown_text(status),
                duration=format_duration(duration, attempt["status"]),
                next_node=markdown_text(next_node),
            )
        )

    total_duration = (
        format_duration(sum(known_durations), "succeeded")
        if known_durations
        else "unknown"
    )
    lines.extend(["", f"Observed node time: **{total_duration}**"])
    unknown_count = len(attempts) - len(known_durations)
    if unknown_count:
        lines.append(
            f"Durations unavailable for **{unknown_count}** "
            "in-progress or legacy attempt(s)."
        )

    failures = [attempt for attempt in attempts if attempt.get("error")]
    if failures:
        lines.extend(["", f"{heading}# Attempt errors", ""])
        for attempt in failures:
            lines.append(
                f"- Attempt {attempt['attempt_id']} (`{markdown_text(attempt['node'])}`): "
                f"{markdown_text(attempt['error'])}"
            )
    return "\n".join(lines)


def candidate_manifests(run_dir: Path) -> list[tuple[str, dict[str, Any]]]:
    """Load nested candidate manifests in deterministic directory-name order."""
    candidates_dir = run_dir / "candidates"
    if not candidates_dir.is_dir():
        return []
    candidates: list[tuple[str, dict[str, Any]]] = []
    for candidate_dir in sorted(
        path for path in candidates_dir.iterdir() if path.is_dir()
    ):
        manifest_path = candidate_dir / MANIFEST_FILENAME
        if manifest_path.exists():
            candidates.append((candidate_dir.name, load_manifest(manifest_path)))
    return candidates


def render_run_timeline(run_dir: Path, *, include_candidates: bool = True) -> str:
    """Load and render a parent run and, by default, its candidate runs."""
    manifest = load_manifest(run_dir / MANIFEST_FILENAME)
    sections = [render_manifest(manifest)]
    if include_candidates:
        for candidate_name, candidate_manifest in candidate_manifests(run_dir):
            sections.append(
                render_manifest(
                    candidate_manifest,
                    heading_level=2,
                    label=candidate_name,
                )
            )
    return "\n\n".join(sections) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="Run directory containing manifest.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional Markdown output path; omit to print without changing files",
    )
    parser.add_argument(
        "--no-candidates",
        action="store_true",
        help="Render only the parent manifest of a parallel run",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_dir = args.run_dir.resolve()
    if not run_dir.is_dir():
        print(f"Run directory not found: {run_dir}", file=sys.stderr)
        return 1
    try:
        rendered = render_run_timeline(
            run_dir, include_candidates=not args.no_candidates
        )
        if args.output is None:
            print(rendered, end="")
        else:
            output_path = args.output.resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered, encoding="utf-8")
            print(f"Wrote {output_path}")
    except (ManifestError, OSError) as exc:
        print(f"Timeline error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
