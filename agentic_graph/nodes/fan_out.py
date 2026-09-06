#!/usr/bin/env python3
"""Start isolated candidate subgraphs concurrently and collect their results."""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from common import (
    GRAPH_ROOT,
    NodeError,
    advance_state,
    node_parser,
    read_state,
    resolve_fixture_path,
    write_state,
)

sys.path.insert(0, str(GRAPH_ROOT))

from parallel_candidates import (
    ParallelCandidateError,
    candidate_id,
    candidate_result,
    parallel_configuration,
    prepare_baseline,
    prepare_candidate,
)
from run_manifest import run_is_locked


def timestamp() -> str:
    """Return a precise UTC timestamp for candidate process evidence."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def main() -> int:
    args = node_parser("fan_out").parse_args()
    open_logs: list[TextIO] = []
    try:
        state = read_state(args.input_state, "fan_out")
        parent_run_dir = args.input_state.resolve().parent
        source_fixture = resolve_fixture_path(state, args.input_state)
        config = parallel_configuration(state)
        baseline = prepare_baseline(source_fixture, parent_run_dir)

        candidate_dirs: dict[str, Path] = {}
        for number in range(1, config["candidate_count"] + 1):
            name = candidate_id(number)
            candidate_dirs[name] = prepare_candidate(
                state,
                parent_run_dir=parent_run_dir,
                baseline=baseline,
                source_fixture=source_fixture,
                name=name,
                override=config["candidate_overrides"].get(name, {}),
            )

        processes: dict[str, subprocess.Popen[Any]] = {}
        attached: set[str] = set()
        execution: dict[str, dict[str, Any]] = {}
        for name, candidate_dir in candidate_dirs.items():
            started_at = timestamp()
            if run_is_locked(candidate_dir):
                attached.add(name)
                execution[name] = {
                    "started_at": started_at,
                    "completed_at": None,
                    "exit_code": None,
                    "launch_error": None,
                    "attached_to_existing_dispatcher": True,
                }
                print(f"Waiting for existing dispatcher for {name}", flush=True)
                continue

            stdout_path = candidate_dir / "candidate-dispatch.stdout.log"
            stderr_path = candidate_dir / "candidate-dispatch.stderr.log"
            stdout_handle = stdout_path.open("a", encoding="utf-8")
            stderr_handle = stderr_path.open("a", encoding="utf-8")
            open_logs.extend((stdout_handle, stderr_handle))
            command = [
                sys.executable,
                str(GRAPH_ROOT / "dispatcher.py"),
                "--run-dir",
                str(candidate_dir),
            ]
            try:
                process = subprocess.Popen(
                    command,
                    cwd=GRAPH_ROOT,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                )
            except OSError as exc:
                execution[name] = {
                    "started_at": started_at,
                    "completed_at": timestamp(),
                    "exit_code": None,
                    "launch_error": str(exc),
                    "attached_to_existing_dispatcher": False,
                }
            else:
                processes[name] = process
                execution[name] = {
                    "started_at": started_at,
                    "completed_at": None,
                    "exit_code": None,
                    "launch_error": None,
                    "attached_to_existing_dispatcher": False,
                }
                print(f"Started {name} (pid {process.pid})", flush=True)

        # Every process is started before waiting for the first one.
        deadline = time.monotonic() + config["candidate_wait_timeout_seconds"]
        for name, process in processes.items():
            remaining = max(0.0, deadline - time.monotonic())
            try:
                execution[name]["exit_code"] = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                raise NodeError(
                    "Timed out waiting for candidate dispatcher(s); they remain "
                    "recoverable from their child manifests"
                ) from exc
            execution[name]["completed_at"] = timestamp()
            print(
                f"Finished {name} with dispatcher exit code {process.returncode}",
                flush=True,
            )

        while attached:
            finished = {
                name for name in attached if not run_is_locked(candidate_dirs[name])
            }
            for name in sorted(finished):
                execution[name]["completed_at"] = timestamp()
                print(f"Existing dispatcher finished for {name}", flush=True)
            attached -= finished
            if attached and time.monotonic() >= deadline:
                raise NodeError(
                    "Timed out waiting for existing candidate dispatcher(s): "
                    + ", ".join(sorted(attached))
                )
            if attached:
                time.sleep(0.5)

        for handle in open_logs:
            handle.close()
        open_logs.clear()

        results: list[dict[str, Any]] = []
        for name, candidate_dir in candidate_dirs.items():
            result = candidate_result(
                candidate_dir,
                name=name,
                parent_run_id=state["run_id"],
                process_exit_code=execution[name]["exit_code"],
            )
            result["started_at"] = execution[name]["started_at"]
            result["completed_at"] = execution[name]["completed_at"]
            if execution[name]["launch_error"] is not None:
                result["exclusion_reason"] = (
                    "Could not start candidate dispatcher: "
                    + execution[name]["launch_error"]
                )
            if not result["terminal"]:
                raise NodeError(
                    f"{name} dispatcher exited without a terminal child state"
                )
            results.append(result)

        output = {
            "candidate_count": config["candidate_count"],
            "ranking_rule": config["ranking_rule"],
            "baseline": "baseline",
            "candidates": results,
            "execution_mode": "concurrent_subprocesses",
            "sonar_isolation": "serialized_complete_review_per_project",
        }
        updated = advance_state(
            state,
            current_node="fan_out",
            next_node="join",
            output=output,
        )
        updated["candidate_results"] = results
        write_state(args.output_state, updated, args.input_state)
    except (NodeError, ParallelCandidateError) as exc:
        print(f"Fan-out node error: {exc}", file=sys.stderr)
        return 1
    finally:
        for handle in open_logs:
            handle.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
