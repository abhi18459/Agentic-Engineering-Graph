#!/usr/bin/env python3
"""Run or resume graph nodes from a manifest-backed checkpoint."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Any

from approval import (
    APPROVAL_FILENAME,
    APPROVAL_NODE,
    REVIEW_FILENAME,
    ApprovalError,
    approved_state,
    ensure_review_file,
    load_valid_approval,
    plan_output,
    write_json_exclusive,
)
from run_manifest import (
    MANIFEST_FILENAME,
    ManifestError,
    RunLock,
    complete_attempt,
    create_manifest,
    finish_unsuccessful_attempt,
    load_manifest,
    record_recovery,
    start_attempt,
    write_manifest,
)

GRAPH_ROOT = Path(__file__).resolve().parent
DEFAULT_RUN_DIR = GRAPH_ROOT / "runs" / "run-011"
TERMINAL_NODES = {"END", "give_up"}
CONTROL_NODES = {APPROVAL_NODE}

# Only trusted node labels can select an executable. State files never supply paths.
NODES: dict[str, Path] = {
    "plan": GRAPH_ROOT / "nodes" / "plan.py",
    "code": GRAPH_ROOT / "nodes" / "code.py",
    "write": GRAPH_ROOT / "nodes" / "write.py",
    "test": GRAPH_ROOT / "nodes" / "test.py",
    "review": GRAPH_ROOT / "nodes" / "review.py",
    "fix": GRAPH_ROOT / "nodes" / "fix.py",
    "fan_out": GRAPH_ROOT / "nodes" / "fan_out.py",
    "join": GRAPH_ROOT / "nodes" / "join.py",
}
ROUTABLE_NODES = set(NODES) | CONTROL_NODES | TERMINAL_NODES
ENFORCED_TRANSITIONS: dict[str, set[str]] = {
    APPROVAL_NODE: {"code", "fan_out"},
    "test": {"review", "fix", "give_up"},
    "review": {"END", "fix", "give_up"},
    "fix": {"test"},
    "fan_out": {"join"},
    "join": {"END", "give_up"},
}


class DispatchError(RuntimeError):
    """Raised when the dispatcher cannot safely continue."""


class NodeInterrupted(RuntimeError):
    """Raised after an external signal interrupts an active node process."""


class TerminationRequested(RuntimeError):
    """Raised by the temporary SIGTERM/SIGHUP handler."""

    def __init__(self, signal_number: int) -> None:
        self.signal_number = signal_number
        super().__init__(f"received signal {signal_number}")


def read_state(path: Path) -> dict[str, Any]:
    """Read a state file and require a JSON object at its root."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DispatchError(f"State file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DispatchError(f"Invalid JSON in {path}: {exc}") from exc
    except OSError as exc:
        raise DispatchError(f"Could not read state file {path}: {exc}") from exc

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
        "review_config",
        "max_iterations",
        "parallel_config",
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
    if next_node not in ROUTABLE_NODES:
        raise DispatchError(
            f"Refusing unknown next_node {next_node!r} in {output_path}"
        )
    allowed_next_nodes = ENFORCED_TRANSITIONS.get(node_name)
    if allowed_next_nodes is not None and next_node not in allowed_next_nodes:
        raise DispatchError(
            f"Refusing invalid {node_name!r} transition to {next_node!r} "
            f"in {output_path}"
        )

    workflow_status = current.get("workflow_status")
    expected_status: str | tuple[str, ...]
    if next_node == "END":
        expected_status = "succeeded"
    elif next_node == "give_up":
        expected_status = ("gave_up", "error")
    elif next_node == APPROVAL_NODE:
        expected_status = "awaiting_approval"
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
        ("review_attempts", "review"),
        ("fix_attempts", "fix"),
    ):
        # review_attempts is additive in Step 6. Treat it as empty in preserved
        # pre-Step-6 snapshots so completed historical runs remain readable.
        previous_history = previous.get(history_name, [])
        current_history = current.get(history_name, [])
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


def validate_checkpoint(
    manifest: dict[str, Any], run_dir: Path
) -> tuple[Path, dict[str, Any], str]:
    """Load the manifest checkpoint and verify its recovery coordinates."""
    checkpoint = manifest["checkpoint"]
    input_path = run_dir / checkpoint["state_file"]
    input_state = read_state(input_path)

    if input_state.get("run_id") != manifest["run_id"]:
        raise DispatchError("Manifest run_id does not match its checkpoint")
    state_sequence = input_state.get("state_sequence")
    if (
        not isinstance(state_sequence, int)
        or isinstance(state_sequence, bool)
        or state_sequence != checkpoint["state_sequence"]
    ):
        raise DispatchError("Manifest sequence does not match its checkpoint")
    if input_state.get("next_node") != checkpoint["next_node"]:
        raise DispatchError("Manifest next_node does not match its checkpoint")

    next_node = checkpoint["next_node"]
    if next_node not in ROUTABLE_NODES:
        raise DispatchError(f"Manifest checkpoint names unknown node {next_node!r}")
    return input_path, input_state, next_node


def initialize_run(
    run_dir: Path, requested_start: str | None
) -> tuple[dict[str, Any], Path, dict[str, Any], str]:
    """Create a manifest for a pristine run and return its initial checkpoint."""
    input_path = run_dir / "00_input.json"
    input_state = read_state(input_path)
    run_id = input_state.get("run_id")
    sequence = input_state.get("state_sequence")
    state_start = input_state.get("next_node")

    if not isinstance(run_id, str) or not run_id.strip():
        raise DispatchError(f"{input_path} has no valid run_id")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence != 0:
        raise DispatchError(f"{input_path} must begin at state_sequence 0")
    if input_state.get("schema_version") != 2:
        raise DispatchError(f"{input_path} must use state schema_version 2")
    if input_state.get("workflow_status") != "running":
        raise DispatchError(f"{input_path} must describe a running workflow")
    current_node = requested_start or state_start
    if not isinstance(current_node, str) or current_node not in NODES:
        raise DispatchError(f"Initial state points to unknown node {current_node!r}")
    if state_start != current_node:
        raise DispatchError(
            f"{input_path} points to {state_start!r}, not requested node "
            f"{current_node!r}"
        )

    existing_snapshots = sorted(
        path
        for path in run_dir.glob("[0-9][0-9]_*.json")
        if path.name != input_path.name
    )
    if existing_snapshots:
        raise DispatchError(
            "Cannot create a manifest for a run that already has output snapshots: "
            + ", ".join(path.name for path in existing_snapshots)
        )

    unexpected_control_files = [
        name
        for name in (REVIEW_FILENAME, APPROVAL_FILENAME)
        if (run_dir / name).exists()
    ]
    if unexpected_control_files:
        raise DispatchError(
            "Cannot create a manifest for a pristine run with existing control "
            "files: " + ", ".join(unexpected_control_files)
        )

    manifest = create_manifest(
        run_dir / MANIFEST_FILENAME,
        run_id=run_id,
        initial_state_file=input_path.name,
        state_sequence=sequence,
        next_node=current_node,
    )
    print(f"Created {run_dir / MANIFEST_FILENAME}", flush=True)
    return manifest, input_path, input_state, current_node


def latest_matching_attempt(
    manifest: dict[str, Any],
    *,
    node: str,
    input_state: str,
    output_state: str,
) -> dict[str, Any] | None:
    """Return the newest attempt for one exact checkpoint transition."""
    for attempt in reversed(manifest["attempts"]):
        if (
            attempt["node"] == node
            and attempt["input_state"] == input_state
            and attempt["output_state"] == output_state
        ):
            return attempt
    return None


def quarantine_snapshot(path: Path) -> Path:
    """Move an untrusted snapshot aside without destroying diagnostic evidence."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.invalid-{timestamp}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.invalid-{timestamp}-{counter}")
        counter += 1
    try:
        path.replace(candidate)
    except OSError as exc:
        raise DispatchError(
            f"Could not quarantine invalid snapshot {path}: {exc}"
        ) from exc
    return candidate


def recover_interrupted_attempt(
    manifest_path: Path,
    manifest: dict[str, Any],
    run_dir: Path,
    input_path: Path,
    input_state: dict[str, Any],
    current_node: str,
) -> tuple[dict[str, Any], Path, dict[str, Any], str]:
    """Reconcile or quarantine the one output expected after the checkpoint."""
    if current_node in TERMINAL_NODES:
        return manifest, input_path, input_state, current_node

    sequence = input_state.get("state_sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise DispatchError(f"{input_path} has an invalid state_sequence")
    output_path = run_dir / snapshot_name(sequence + 1, current_node)
    attempt = latest_matching_attempt(
        manifest,
        node=current_node,
        input_state=input_path.name,
        output_state=output_path.name,
    )

    if not output_path.exists():
        if attempt is not None and attempt["status"] == "running":
            finish_unsuccessful_attempt(
                manifest,
                attempt_id=attempt["attempt_id"],
                status="interrupted",
                error="Dispatcher stopped before a state snapshot was committed",
                exit_code=None,
            )
            record_recovery(
                manifest,
                action="retry_interrupted_node",
                snapshot=output_path.name,
                detail=f"No output existed; retrying {current_node} from {input_path.name}",
            )
            write_manifest(manifest_path, manifest)
            print(f"Marked interrupted {current_node} attempt for retry", flush=True)
        return manifest, input_path, input_state, current_node

    if attempt is None:
        raise DispatchError(
            f"Found untracked snapshot {output_path}; refusing to infer success "
            "from its filename"
        )

    validation_error: DispatchError | None = None
    try:
        output_state = read_state(output_path)
        next_node = validate_transition(
            input_state, output_state, current_node, output_path
        )
    except DispatchError as exc:
        validation_error = exc

    if validation_error is None and attempt["status"] in {"running", "interrupted"}:
        complete_attempt(
            manifest,
            attempt_id=attempt["attempt_id"],
            output_state=output_path.name,
            state_sequence=output_state["state_sequence"],
            next_node=next_node,
            workflow_status=output_state["workflow_status"],
            exit_code=None,
            recovered=True,
        )
        record_recovery(
            manifest,
            action="reconciled_snapshot",
            snapshot=output_path.name,
            detail=(
                f"Validated {output_path.name} against {input_path.name} and "
                "advanced the checkpoint"
            ),
        )
        write_manifest(manifest_path, manifest)
        print(f"Recovered completed {current_node} from {output_path.name}", flush=True)
        return manifest, output_path, output_state, next_node

    if validation_error is None:
        reason = (
            f"The corresponding attempt is {attempt['status']!r}, not an "
            "interrupted attempt eligible for reconciliation"
        )
    else:
        reason = str(validation_error)
    quarantined = quarantine_snapshot(output_path)
    if attempt["status"] == "running":
        finish_unsuccessful_attempt(
            manifest,
            attempt_id=attempt["attempt_id"],
            status="interrupted",
            error=f"Untrusted output was quarantined: {reason}",
            exit_code=None,
        )
    record_recovery(
        manifest,
        action="quarantined_snapshot",
        snapshot=output_path.name,
        detail=f"Moved to {quarantined.name}: {reason}",
    )
    write_manifest(manifest_path, manifest)
    print(f"Quarantined {output_path.name} as {quarantined.name}", flush=True)
    return manifest, input_path, input_state, current_node


def process_approval_gate(
    manifest_path: Path,
    manifest: dict[str, Any],
    run_dir: Path,
    input_path: Path,
    input_state: dict[str, Any],
) -> tuple[dict[str, Any], Path, dict[str, Any], str] | None:
    """Pause for review or commit the exact approved plan as a checkpoint."""
    try:
        plan_output(input_state)
        review_path = ensure_review_file(run_dir, input_state)
        approval_result = load_valid_approval(run_dir, manifest["run_id"])
    except ApprovalError as exc:
        raise DispatchError(str(exc)) from exc

    if approval_result is None:
        changed = (
            manifest["workflow_status"] != "awaiting_approval"
            or manifest["current_node"] != APPROVAL_NODE
        )
        manifest["workflow_status"] = "awaiting_approval"
        manifest["current_node"] = APPROVAL_NODE
        if changed:
            write_manifest(manifest_path, manifest)
        print(f"Plan is ready for human review: {review_path}", flush=True)
        print(
            "Edit it as many times as needed, then approve it with:\n"
            f"  {sys.executable} {GRAPH_ROOT / 'approve_plan.py'} "
            f"--run-dir {run_dir}",
            flush=True,
        )
        print("The workflow is paused; no code node has run.", flush=True)
        return None

    approval, approved_plan = approval_result
    sequence = input_state.get("state_sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise DispatchError(f"{input_path} has an invalid state_sequence")
    output_path = run_dir / snapshot_name(sequence + 1, APPROVAL_NODE)
    if output_path.exists():
        raise DispatchError(
            f"Refusing to overwrite unhandled approval snapshot: {output_path}"
        )

    attempt_started = time.monotonic()
    attempt_id = start_attempt(
        manifest,
        node=APPROVAL_NODE,
        input_state=input_path.name,
        output_state=output_path.name,
    )
    write_manifest(manifest_path, manifest)
    try:
        output_state = approved_state(input_state, approval, approved_plan)
        write_json_exclusive(output_path, output_state)
        next_node = validate_transition(
            input_state, output_state, APPROVAL_NODE, output_path
        )
    except (ApprovalError, DispatchError) as exc:
        finish_unsuccessful_attempt(
            manifest,
            attempt_id=attempt_id,
            status="failed",
            error=str(exc),
            exit_code=None,
            duration_seconds=time.monotonic() - attempt_started,
        )
        write_manifest(manifest_path, manifest)
        if isinstance(exc, DispatchError):
            raise
        raise DispatchError(str(exc)) from exc

    complete_attempt(
        manifest,
        attempt_id=attempt_id,
        output_state=output_path.name,
        state_sequence=output_state["state_sequence"],
        next_node=next_node,
        workflow_status=output_state["workflow_status"],
        exit_code=0,
        duration_seconds=time.monotonic() - attempt_started,
    )
    write_manifest(manifest_path, manifest)
    print(f"Recorded human-approved plan in {output_path}", flush=True)
    return manifest, output_path, output_state, next_node


def stop_process_group(process: subprocess.Popen[Any]) -> None:
    """Terminate an active node and descendants started in its process group."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def execute_node(command: list[str], run_lock_fd: int) -> int:
    """Run one node while forwarding controlled dispatcher termination."""
    try:
        process = subprocess.Popen(
            command,
            cwd=GRAPH_ROOT,
            start_new_session=True,
            pass_fds=(run_lock_fd,),
        )
    except OSError as exc:
        raise DispatchError(f"Could not start node process: {exc}") from exc

    def request_termination(signal_number: int, _frame: FrameType | None) -> None:
        raise TerminationRequested(signal_number)

    handled_signals = [signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        handled_signals.append(signal.SIGHUP)
    previous_handlers = {
        signal_number: signal.signal(signal_number, request_termination)
        for signal_number in handled_signals
    }
    try:
        return process.wait()
    except KeyboardInterrupt as exc:
        stop_process_group(process)
        raise NodeInterrupted("interrupted by user") from exc
    except TerminationRequested as exc:
        stop_process_group(process)
        raise NodeInterrupted(str(exc)) from exc
    finally:
        for signal_number, previous_handler in previous_handlers.items():
            signal.signal(signal_number, previous_handler)


def terminal_result(state: dict[str, Any], terminal: str, *, resumed: bool) -> int:
    """Report a terminal checkpoint and return the workflow exit code."""
    prefix = "Workflow already at" if resumed else "Workflow reached"
    if terminal == "END":
        if state.get("selected_candidate"):
            result = (
                f"selected {state['selected_candidate']} after parallel tests "
                "and quality review"
            )
        elif state.get("review_attempts"):
            result = "tests and quality gate passed"
        else:
            result = "tests passed"
        print(f"{prefix} END: {result}", flush=True)
        return 0
    reason = state.get("give_up_reason", "repair limit exhausted")
    print(f"{prefix} give_up: {reason}", file=sys.stderr, flush=True)
    return 2


def dispatch_locked(
    run_dir: Path, requested_start: str | None, run_lock_fd: int
) -> int:
    """Run or resume nodes while the caller owns the run lock."""
    manifest_path = run_dir / MANIFEST_FILENAME
    if manifest_path.exists():
        manifest = load_manifest(manifest_path)
        input_path, input_state, current_node = validate_checkpoint(manifest, run_dir)
        if requested_start is not None:
            print(
                f"Manifest exists; resuming {current_node!r} and ignoring "
                f"--start {requested_start!r}",
                flush=True,
            )
        manifest, input_path, input_state, current_node = recover_interrupted_attempt(
            manifest_path,
            manifest,
            run_dir,
            input_path,
            input_state,
            current_node,
        )
        if current_node in TERMINAL_NODES:
            return terminal_result(input_state, current_node, resumed=True)
        print(
            f"Resuming {manifest['run_id']} from {input_path.name}; "
            f"next node is {current_node}",
            flush=True,
        )
    else:
        manifest, input_path, input_state, current_node = initialize_run(
            run_dir, requested_start
        )

    while True:
        if input_state.get("next_node") != current_node:
            raise DispatchError(
                f"{input_path} points to {input_state.get('next_node')!r}, "
                f"not requested node {current_node!r}"
            )

        if current_node == APPROVAL_NODE:
            approval_transition = process_approval_gate(
                manifest_path,
                manifest,
                run_dir,
                input_path,
                input_state,
            )
            if approval_transition is None:
                return 0
            manifest, input_path, input_state, current_node = approval_transition
            continue

        sequence = input_state.get("state_sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise DispatchError(f"{input_path} has an invalid state_sequence")
        output_path = run_dir / snapshot_name(sequence + 1, current_node)
        if output_path.exists():
            raise DispatchError(
                f"Refusing to overwrite unhandled state snapshot: {output_path}"
            )

        attempt_started = time.monotonic()
        attempt_id = start_attempt(
            manifest,
            node=current_node,
            input_state=input_path.name,
            output_state=output_path.name,
        )
        write_manifest(manifest_path, manifest)

        script = NODES[current_node]
        command = [
            sys.executable,
            str(script),
            "--input-state",
            str(input_path),
            "--output-state",
            str(output_path),
        ]
        print(f"Running {current_node}: {script.name}", flush=True)
        try:
            return_code = execute_node(command, run_lock_fd)
        except NodeInterrupted as exc:
            finish_unsuccessful_attempt(
                manifest,
                attempt_id=attempt_id,
                status="interrupted",
                error=str(exc),
                exit_code=None,
                duration_seconds=time.monotonic() - attempt_started,
            )
            write_manifest(manifest_path, manifest)
            print(
                f"Dispatcher interrupted while running {current_node}", file=sys.stderr
            )
            return 130
        except DispatchError as exc:
            finish_unsuccessful_attempt(
                manifest,
                attempt_id=attempt_id,
                status="failed",
                error=str(exc),
                exit_code=None,
                duration_seconds=time.monotonic() - attempt_started,
            )
            write_manifest(manifest_path, manifest)
            raise

        if return_code != 0:
            message = f"Node {current_node!r} failed with exit code {return_code}"
            finish_unsuccessful_attempt(
                manifest,
                attempt_id=attempt_id,
                status="failed",
                error=message,
                exit_code=return_code,
                duration_seconds=time.monotonic() - attempt_started,
            )
            write_manifest(manifest_path, manifest)
            raise DispatchError(message)

        try:
            output_state = read_state(output_path)
            next_node = validate_transition(
                input_state, output_state, current_node, output_path
            )
        except DispatchError as exc:
            finish_unsuccessful_attempt(
                manifest,
                attempt_id=attempt_id,
                status="failed",
                error=str(exc),
                exit_code=return_code,
                duration_seconds=time.monotonic() - attempt_started,
            )
            write_manifest(manifest_path, manifest)
            raise

        complete_attempt(
            manifest,
            attempt_id=attempt_id,
            output_state=output_path.name,
            state_sequence=output_state["state_sequence"],
            next_node=next_node,
            workflow_status=output_state["workflow_status"],
            exit_code=return_code,
            duration_seconds=time.monotonic() - attempt_started,
        )
        write_manifest(manifest_path, manifest)
        print(f"Wrote {output_path}", flush=True)

        if next_node in TERMINAL_NODES:
            return terminal_result(output_state, next_node, resumed=False)

        input_path = output_path
        input_state = output_state
        current_node = next_node


def dispatch(run_dir: Path, requested_start: str | None) -> int:
    """Acquire one run exclusively, then execute or resume it."""
    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        raise DispatchError(f"Run directory not found: {run_dir}")
    with RunLock(run_dir) as run_lock:
        return dispatch_locked(run_dir, requested_start, run_lock.fileno())


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
        help=(
            "First node for a new run only (default: next_node in 00_input.json; "
            "an existing manifest always controls resume)"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return dispatch(args.run_dir, args.start)
    except (DispatchError, ManifestError) as exc:
        print(f"Dispatcher error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Dispatcher interrupted between nodes", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
