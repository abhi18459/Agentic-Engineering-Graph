"""Crash-safe run manifest and locking helpers for the graph dispatcher."""

from __future__ import annotations

import fcntl
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Self, TextIO

MANIFEST_FILENAME = "manifest.json"
LOCK_FILENAME = ".dispatcher.lock"
MANIFEST_SCHEMA_VERSION = 1
ATTEMPT_STATUSES = {"running", "succeeded", "failed", "interrupted"}
WORKFLOW_STATUSES = {
    "running",
    "awaiting_approval",
    "interrupted",
    "failed",
    "succeeded",
    "gave_up",
    "error",
}


class ManifestError(RuntimeError):
    """Raised when a run manifest cannot be trusted or updated."""


class RunLockedError(ManifestError):
    """Raised when another dispatcher already owns a run directory."""


def utc_now() -> str:
    """Return an unambiguous, timezone-aware timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def elapsed_wall_time(started_at: str, completed_at: str) -> float | None:
    """Return a non-negative wall-clock duration when both timestamps parse."""
    try:
        started = datetime.fromisoformat(started_at)
        completed = datetime.fromisoformat(completed_at)
    except (TypeError, ValueError):
        return None
    if started.tzinfo is None or completed.tzinfo is None:
        return None
    return round(max(0.0, (completed - started).total_seconds()), 6)


def normalized_duration(value: float, attempt_id: int) -> float:
    """Validate and normalize an elapsed duration before persisting it."""
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ManifestError(f"Attempt {attempt_id} has an invalid duration")
    return round(float(value), 6)


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    """Publish a JSON object atomically using a temporary file beside it."""
    path = path.resolve()
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None

        # Persist the directory entry where the platform supports directory fsync.
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    except OSError as exc:
        raise ManifestError(f"Could not atomically write {path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _require_plain_filename(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"Manifest {field} must be a non-empty filename")
    path = Path(value)
    if path.name != value or value in {".", ".."}:
        raise ManifestError(f"Manifest {field} must not contain a directory")
    return value


def validate_manifest(manifest: dict[str, Any], path: Path | None = None) -> None:
    """Validate the persisted structure needed for safe recovery."""
    location = f" in {path}" if path is not None else ""
    if manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(f"Unsupported manifest schema{location}")
    if not isinstance(manifest.get("run_id"), str) or not manifest["run_id"].strip():
        raise ManifestError(f"Manifest has no valid run_id{location}")
    if manifest.get("workflow_status") not in WORKFLOW_STATUSES:
        raise ManifestError(f"Manifest has an invalid workflow_status{location}")
    if (
        not isinstance(manifest.get("current_node"), str)
        or not manifest["current_node"]
    ):
        raise ManifestError(f"Manifest has no valid current_node{location}")
    for timestamp in ("created_at", "updated_at"):
        if not isinstance(manifest.get(timestamp), str) or not manifest[timestamp]:
            raise ManifestError(f"Manifest has no valid {timestamp}{location}")

    checkpoint = manifest.get("checkpoint")
    if not isinstance(checkpoint, dict):
        raise ManifestError(f"Manifest has no checkpoint object{location}")
    _require_plain_filename(checkpoint.get("state_file"), "checkpoint.state_file")
    sequence = checkpoint.get("state_sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise ManifestError(f"Manifest checkpoint has an invalid sequence{location}")
    if not isinstance(checkpoint.get("next_node"), str):
        raise ManifestError(f"Manifest checkpoint has no valid next_node{location}")

    attempts = manifest.get("attempts")
    if not isinstance(attempts, list):
        raise ManifestError(f"Manifest attempts must be an array{location}")
    for expected_id, attempt in enumerate(attempts, start=1):
        if not isinstance(attempt, dict):
            raise ManifestError(f"Manifest attempt {expected_id} is not an object")
        if attempt.get("attempt_id") != expected_id:
            raise ManifestError("Manifest attempt IDs must be contiguous")
        if not isinstance(attempt.get("node"), str):
            raise ManifestError(f"Manifest attempt {expected_id} has no node")
        _require_plain_filename(attempt.get("input_state"), "attempt.input_state")
        _require_plain_filename(attempt.get("output_state"), "attempt.output_state")
        if attempt.get("status") not in ATTEMPT_STATUSES:
            raise ManifestError(f"Manifest attempt {expected_id} has invalid status")
        if not isinstance(attempt.get("started_at"), str):
            raise ManifestError(f"Manifest attempt {expected_id} has no start time")
        if attempt["status"] == "running":
            if expected_id != len(attempts):
                raise ManifestError("Only the latest manifest attempt may be running")
            if attempt.get("completed_at") is not None:
                raise ManifestError("A running attempt cannot have a completion time")
        elif not isinstance(attempt.get("completed_at"), str):
            raise ManifestError(
                f"Completed manifest attempt {expected_id} has no completion time"
            )
        duration = attempt.get("duration_seconds")
        if "duration_seconds" in attempt:
            if attempt["status"] == "running":
                if duration is not None:
                    raise ManifestError(
                        f"Running manifest attempt {expected_id} has a duration"
                    )
            elif (
                not isinstance(duration, (int, float))
                or isinstance(duration, bool)
                or not math.isfinite(duration)
                or duration < 0
            ):
                raise ManifestError(
                    f"Manifest attempt {expected_id} has an invalid duration"
                )
        exit_code = attempt.get("exit_code")
        if exit_code is not None and (
            not isinstance(exit_code, int) or isinstance(exit_code, bool)
        ):
            raise ManifestError(f"Manifest attempt {expected_id} has invalid exit code")
        if not isinstance(attempt.get("recovered"), bool):
            raise ManifestError(
                f"Manifest attempt {expected_id} has invalid recovery flag"
            )

    recoveries = manifest.get("recoveries")
    if not isinstance(recoveries, list) or not all(
        isinstance(entry, dict) for entry in recoveries
    ):
        raise ManifestError(f"Manifest recoveries must be an array{location}")


def load_manifest(path: Path) -> dict[str, Any]:
    """Read and validate a manifest JSON object."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"Run manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"Invalid JSON in run manifest {path}: {exc}") from exc
    except OSError as exc:
        raise ManifestError(f"Could not read run manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ManifestError(f"Run manifest must contain a JSON object: {path}")
    validate_manifest(value, path)
    return value


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """Validate and atomically persist a manifest update."""
    manifest["updated_at"] = utc_now()
    validate_manifest(manifest, path)
    atomic_write_json(path, manifest)


def create_manifest(
    path: Path,
    *,
    run_id: str,
    initial_state_file: str,
    state_sequence: int,
    next_node: str,
) -> dict[str, Any]:
    """Create the first durable checkpoint for a new run."""
    if path.exists():
        raise ManifestError(f"Refusing to overwrite run manifest: {path}")
    now = utc_now()
    manifest: dict[str, Any] = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": now,
        "updated_at": now,
        "workflow_status": "running",
        "current_node": next_node,
        "checkpoint": {
            "state_file": initial_state_file,
            "state_sequence": state_sequence,
            "next_node": next_node,
        },
        "attempts": [],
        "recoveries": [],
    }
    write_manifest(path, manifest)
    return manifest


def start_attempt(
    manifest: dict[str, Any],
    *,
    node: str,
    input_state: str,
    output_state: str,
) -> int:
    """Append and return the ID of a running node attempt."""
    attempts = manifest["attempts"]
    if attempts and attempts[-1]["status"] == "running":
        raise ManifestError("Cannot start an attempt while another is still running")
    attempt_id = len(attempts) + 1
    attempts.append(
        {
            "attempt_id": attempt_id,
            "node": node,
            "input_state": input_state,
            "output_state": output_state,
            "started_at": utc_now(),
            "completed_at": None,
            "duration_seconds": None,
            "status": "running",
            "exit_code": None,
            "next_node": None,
            "error": None,
            "recovered": False,
        }
    )
    manifest["workflow_status"] = "running"
    manifest["current_node"] = node
    return attempt_id


def _attempt(manifest: dict[str, Any], attempt_id: int) -> dict[str, Any]:
    attempts = manifest["attempts"]
    if attempt_id < 1 or attempt_id > len(attempts):
        raise ManifestError(f"Unknown manifest attempt ID: {attempt_id}")
    return attempts[attempt_id - 1]


def complete_attempt(
    manifest: dict[str, Any],
    *,
    attempt_id: int,
    output_state: str,
    state_sequence: int,
    next_node: str,
    workflow_status: str,
    exit_code: int | None,
    recovered: bool = False,
    duration_seconds: float | None = None,
) -> None:
    """Commit a validated node output as the run's newest checkpoint."""
    attempt = _attempt(manifest, attempt_id)
    if attempt["status"] not in {"running", "interrupted"}:
        raise ManifestError(f"Attempt {attempt_id} cannot be completed twice")
    if attempt["output_state"] != output_state:
        raise ManifestError(f"Attempt {attempt_id} produced an unexpected snapshot")
    completed_at = utc_now()
    if duration_seconds is None:
        duration_seconds = elapsed_wall_time(attempt["started_at"], completed_at)
    if duration_seconds is None:
        raise ManifestError(f"Could not determine duration for attempt {attempt_id}")
    attempt.update(
        {
            "completed_at": completed_at,
            "duration_seconds": normalized_duration(duration_seconds, attempt_id),
            "status": "succeeded",
            "exit_code": exit_code,
            "next_node": next_node,
            "error": None,
            "recovered": recovered,
        }
    )
    manifest["checkpoint"] = {
        "state_file": output_state,
        "state_sequence": state_sequence,
        "next_node": next_node,
    }
    manifest["workflow_status"] = workflow_status
    manifest["current_node"] = next_node


def finish_unsuccessful_attempt(
    manifest: dict[str, Any],
    *,
    attempt_id: int,
    status: str,
    error: str,
    exit_code: int | None,
    duration_seconds: float | None = None,
) -> None:
    """Finish an attempt without advancing the trusted checkpoint."""
    if status not in {"failed", "interrupted"}:
        raise ManifestError(f"Invalid unsuccessful attempt status: {status}")
    attempt = _attempt(manifest, attempt_id)
    if attempt["status"] != "running":
        raise ManifestError(f"Attempt {attempt_id} is not running")
    completed_at = utc_now()
    if duration_seconds is None:
        duration_seconds = elapsed_wall_time(attempt["started_at"], completed_at)
    if duration_seconds is None:
        raise ManifestError(f"Could not determine duration for attempt {attempt_id}")
    attempt.update(
        {
            "completed_at": completed_at,
            "duration_seconds": normalized_duration(duration_seconds, attempt_id),
            "status": status,
            "exit_code": exit_code,
            "error": error,
        }
    )
    manifest["workflow_status"] = status
    manifest["current_node"] = attempt["node"]


def record_recovery(
    manifest: dict[str, Any],
    *,
    action: str,
    snapshot: str,
    detail: str,
) -> None:
    """Append an auditable recovery decision."""
    manifest["recoveries"].append(
        {
            "recorded_at": utc_now(),
            "action": action,
            "snapshot": snapshot,
            "detail": detail,
        }
    )


class RunLock:
    """An advisory lock held for the lifetime of one dispatcher process."""

    def __init__(self, run_dir: Path) -> None:
        self.path = run_dir / LOCK_FILENAME
        self._handle: TextIO | None = None

    def __enter__(self) -> Self:
        try:
            self._handle = self.path.open("a+", encoding="utf-8")
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            raise RunLockedError(
                f"Another dispatcher is already using {self.path.parent}"
            ) from exc
        except OSError as exc:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            raise ManifestError(f"Could not lock run directory: {exc}") from exc

        try:
            self._handle.seek(0)
            self._handle.truncate()
            json.dump({"pid": os.getpid(), "acquired_at": utc_now()}, self._handle)
            self._handle.write("\n")
            self._handle.flush()
        except OSError as exc:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None
            raise ManifestError(f"Could not record run-lock owner: {exc}") from exc
        return self

    def __exit__(self, *_: object) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None

    def fileno(self) -> int:
        """Return the descriptor inherited by an active node process."""
        if self._handle is None:
            raise ManifestError("Run lock is not currently held")
        return self._handle.fileno()


def run_is_locked(run_dir: Path) -> bool:
    """Return whether a live dispatcher currently holds the run lock."""
    lock_path = run_dir / LOCK_FILENAME
    if not lock_path.exists():
        return False
    try:
        with lock_path.open("r+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        return False
    return False
