"""Durable human-plan review and approval helpers."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from run_manifest import utc_now

REVIEW_FILENAME = "plan_review.md"
APPROVAL_FILENAME = "approval.json"
APPROVAL_SCHEMA_VERSION = 1
APPROVAL_NODE = "approval"


class ApprovalError(RuntimeError):
    """Raised when a plan review or approval cannot be trusted."""


def _atomic_create(path: Path, content: str) -> None:
    """Atomically publish a new file without overwriting an existing one."""
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
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        try:
            os.link(temporary_path, path)
        except FileExistsError as exc:
            raise ApprovalError(f"Refusing to overwrite existing file: {path}") from exc
        temporary_path.unlink()
        temporary_path = None

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
        raise ApprovalError(f"Could not write {path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def read_json_object(path: Path, description: str) -> dict[str, Any]:
    """Read a JSON object with an approval-specific diagnostic."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ApprovalError(f"{description} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ApprovalError(f"Invalid JSON in {path}: {exc}") from exc
    except OSError as exc:
        raise ApprovalError(f"Could not read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ApprovalError(f"{description} must contain a JSON object: {path}")
    return value


def plan_output(state: dict[str, Any]) -> str:
    """Return the non-empty original plan from a completed plan snapshot."""
    if state.get("current_node") != "plan" or state.get("next_node") != APPROVAL_NODE:
        raise ApprovalError("The current checkpoint is not awaiting plan approval")
    if state.get("workflow_status") != "awaiting_approval":
        raise ApprovalError("The plan checkpoint is not marked awaiting_approval")
    outputs = state.get("outputs")
    output = outputs.get("plan") if isinstance(outputs, dict) else None
    content = output.get("content") if isinstance(output, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ApprovalError("The plan checkpoint has no usable plan content")
    return content.strip()


def ensure_review_file(run_dir: Path, state: dict[str, Any]) -> Path:
    """Create the editable review copy once, preserving all later human edits."""
    review_path = run_dir / REVIEW_FILENAME
    if not review_path.exists():
        if (run_dir / APPROVAL_FILENAME).exists():
            raise ApprovalError(
                f"Approved plan file is missing and cannot be reconstructed: {review_path}"
            )
        _atomic_create(review_path, f"{plan_output(state)}\n")
        return review_path

    try:
        review = review_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ApprovalError(f"Could not read {review_path}: {exc}") from exc
    if not review.strip():
        raise ApprovalError(f"Plan review file is empty: {review_path}")
    return review_path


def read_review(run_dir: Path) -> str:
    """Read the non-empty editable plan exactly as it will be approved."""
    review_path = run_dir / REVIEW_FILENAME
    try:
        review = review_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ApprovalError(f"Plan review file not found: {review_path}") from exc
    except OSError as exc:
        raise ApprovalError(f"Could not read {review_path}: {exc}") from exc
    if not review.strip():
        raise ApprovalError(f"Plan review file is empty: {review_path}")
    return review


def plan_digest(content: str) -> str:
    """Return the digest binding approval to exact UTF-8 plan contents."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def create_approval(run_dir: Path, run_id: str) -> Path:
    """Atomically record approval of the review file's current contents."""
    approval_path = run_dir / APPROVAL_FILENAME
    if approval_path.exists():
        raise ApprovalError(f"Plan approval already exists: {approval_path}")
    review = read_review(run_dir)
    approval = {
        "approval_schema_version": APPROVAL_SCHEMA_VERSION,
        "run_id": run_id,
        "decision": "approved",
        "approved_at": utc_now(),
        "plan_file": REVIEW_FILENAME,
        "plan_sha256": plan_digest(review),
    }
    _atomic_create(
        approval_path,
        json.dumps(approval, indent=2, ensure_ascii=False) + "\n",
    )
    return approval_path


def load_valid_approval(
    run_dir: Path, run_id: str
) -> tuple[dict[str, Any], str] | None:
    """Return a valid approval and its exact plan, or None while still paused."""
    approval_path = run_dir / APPROVAL_FILENAME
    if not approval_path.exists():
        return None
    approval = read_json_object(approval_path, "Plan approval")
    expected = {
        "approval_schema_version": APPROVAL_SCHEMA_VERSION,
        "run_id": run_id,
        "decision": "approved",
        "plan_file": REVIEW_FILENAME,
    }
    for field, expected_value in expected.items():
        if approval.get(field) != expected_value:
            raise ApprovalError(
                f"{approval_path} has invalid {field}={approval.get(field)!r}"
            )
    if not isinstance(approval.get("approved_at"), str) or not approval["approved_at"]:
        raise ApprovalError(f"{approval_path} has no valid approved_at timestamp")
    digest = approval.get("plan_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ApprovalError(f"{approval_path} has no valid plan_sha256")

    review = read_review(run_dir)
    if plan_digest(review) != digest:
        raise ApprovalError(
            f"{REVIEW_FILENAME} changed after approval; restore the approved content "
            f"or remove {APPROVAL_FILENAME} and approve the revised plan explicitly"
        )
    return approval, review


def approved_state(
    state: dict[str, Any], approval: dict[str, Any], approved_plan: str
) -> dict[str, Any]:
    """Create the immutable control-point snapshot consumed by the code node."""
    updated = copy.deepcopy(state)
    updated["state_sequence"] += 1
    updated["current_node"] = APPROVAL_NODE
    updated["next_node"] = (
        "fan_out" if isinstance(state.get("parallel_config"), dict) else "code"
    )
    updated["status"] = "completed"
    updated["workflow_status"] = "running"
    updated["outputs"][APPROVAL_NODE] = {
        "content": approved_plan,
        "decision": approval["decision"],
        "approved_at": approval["approved_at"],
        "plan_file": approval["plan_file"],
        "plan_sha256": approval["plan_sha256"],
    }
    return updated


def write_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    """Atomically create an immutable JSON snapshot."""
    _atomic_create(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")
