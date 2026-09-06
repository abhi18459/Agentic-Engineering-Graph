#!/usr/bin/env python3
"""Approve the current editable plan for a paused graph run."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from approval import (
    APPROVAL_NODE,
    ApprovalError,
    create_approval,
    plan_output,
    read_json_object,
)
from run_manifest import MANIFEST_FILENAME, ManifestError, RunLock, load_manifest

GRAPH_ROOT = Path(__file__).resolve().parent
DEFAULT_RUN_DIR = GRAPH_ROOT / "runs" / "run-006"


def approve(run_dir: Path) -> Path:
    """Validate a paused checkpoint and persist its human approval signal."""
    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        raise ApprovalError(f"Run directory not found: {run_dir}")

    with RunLock(run_dir):
        manifest = load_manifest(run_dir / MANIFEST_FILENAME)
        if manifest["workflow_status"] != "awaiting_approval":
            raise ApprovalError(
                "Run is not awaiting approval "
                f"(status: {manifest['workflow_status']})"
            )
        if manifest["current_node"] != APPROVAL_NODE:
            raise ApprovalError(
                f"Run is waiting at {manifest['current_node']!r}, not approval"
            )
        checkpoint = manifest["checkpoint"]
        if checkpoint["next_node"] != APPROVAL_NODE:
            raise ApprovalError("Manifest checkpoint does not point to approval")

        state = read_json_object(run_dir / checkpoint["state_file"], "Plan checkpoint")
        if state.get("run_id") != manifest["run_id"]:
            raise ApprovalError("Manifest and plan checkpoint run IDs do not match")
        if state.get("state_sequence") != checkpoint["state_sequence"]:
            raise ApprovalError("Manifest and plan checkpoint sequences do not match")
        plan_output(state)
        return create_approval(run_dir, manifest["run_id"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=DEFAULT_RUN_DIR,
        help=f"Paused run directory (default: {DEFAULT_RUN_DIR})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        approval_path = approve(args.run_dir)
    except (ApprovalError, ManifestError) as exc:
        print(f"Approval error: {exc}", file=sys.stderr)
        return 1
    print(f"Approved plan recorded in {approval_path}")
    print("Run the dispatcher again to continue from the approval checkpoint.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
