"""Isolation, result validation, ranking, and promotion for parallel candidates."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from run_manifest import MANIFEST_FILENAME, ManifestError, load_manifest

BASELINE_DIRNAME = "baseline"
CANDIDATES_DIRNAME = "candidates"
MIN_CANDIDATES = 2
MAX_CANDIDATES = 8
DEFAULT_CANDIDATE_WAIT_TIMEOUT = 3600
RANKING_RULE = "fewest_sonar_findings"
SONAR_INCLUDE_IGNORED_PROPERTY = "-Dsonar.scm.exclusions.disabled=true"
IGNORED_NAMES = {
    ".coverage",
    ".DS_Store",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".scannerwork",
    ".venv",
    "__pycache__",
    "coverage.xml",
    "htmlcov",
}
ALLOWED_OVERRIDE_FIELDS = {"constraint", "max_iterations", "test_config"}
PROMOTION_TEMP_SUFFIX = ".promotion.tmp"


class ParallelCandidateError(RuntimeError):
    """Raised when candidate state or workspace evidence cannot be trusted."""


def candidate_id(number: int) -> str:
    """Return the stable display and directory name for a candidate."""
    return f"candidate-{number:02d}"


def parallel_configuration(state: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize the parent run's bounded parallel configuration."""
    raw = state.get("parallel_config")
    if not isinstance(raw, dict):
        raise ParallelCandidateError("Input state must contain parallel_config")

    count = raw.get("candidate_count")
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or not MIN_CANDIDATES <= count <= MAX_CANDIDATES
    ):
        raise ParallelCandidateError(
            f"parallel_config.candidate_count must be between "
            f"{MIN_CANDIDATES} and {MAX_CANDIDATES}"
        )

    ranking_rule = raw.get("ranking_rule", RANKING_RULE)
    if ranking_rule != RANKING_RULE:
        raise ParallelCandidateError(
            f"parallel_config.ranking_rule must be {RANKING_RULE!r}"
        )

    wait_timeout = raw.get(
        "candidate_wait_timeout_seconds", DEFAULT_CANDIDATE_WAIT_TIMEOUT
    )
    if (
        not isinstance(wait_timeout, (int, float))
        or isinstance(wait_timeout, bool)
        or wait_timeout <= 0
        or not math.isfinite(wait_timeout)
        or wait_timeout > 86400
    ):
        raise ParallelCandidateError(
            "parallel_config.candidate_wait_timeout_seconds must be between "
            "zero and 86400"
        )

    raw_overrides = raw.get("candidate_overrides", {})
    if not isinstance(raw_overrides, dict):
        raise ParallelCandidateError(
            "parallel_config.candidate_overrides must be an object"
        )
    if not all(isinstance(name, str) for name in raw_overrides):
        raise ParallelCandidateError("candidate_overrides keys must be strings")
    valid_ids = {candidate_id(number) for number in range(1, count + 1)}
    unknown_ids = sorted(set(raw_overrides) - valid_ids)
    if unknown_ids:
        raise ParallelCandidateError(
            "candidate_overrides names candidates outside candidate_count: "
            + ", ".join(unknown_ids)
        )

    overrides: dict[str, dict[str, Any]] = {}
    for name, value in raw_overrides.items():
        if not isinstance(value, dict):
            raise ParallelCandidateError(f"Override for {name} must be an object")
        unexpected = sorted(set(value) - ALLOWED_OVERRIDE_FIELDS)
        if unexpected:
            raise ParallelCandidateError(
                f"Override for {name} has unsupported fields: " + ", ".join(unexpected)
            )
        maximum = value.get("max_iterations")
        if maximum is not None and (
            not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 1
        ):
            raise ParallelCandidateError(
                f"Override max_iterations for {name} must be positive"
            )
        test_config = value.get("test_config")
        if test_config is not None and not isinstance(test_config, dict):
            raise ParallelCandidateError(
                f"Override test_config for {name} must be an object"
            )
        constraint = value.get("constraint")
        if constraint is not None and (
            not isinstance(constraint, str) or not constraint.strip()
        ):
            raise ParallelCandidateError(
                f"Override constraint for {name} must be non-empty text"
            )
        overrides[name] = copy.deepcopy(value)

    return {
        "candidate_count": count,
        "ranking_rule": ranking_rule,
        "candidate_wait_timeout_seconds": float(wait_timeout),
        "candidate_overrides": overrides,
    }


def _copy_ignore(_directory: str, names: list[str]) -> list[str]:
    """Exclude local environments and generated tool output from a workspace."""
    return [
        name
        for name in names
        if name in IGNORED_NAMES or name.endswith((".pyc", ".pyo"))
    ]


def copy_project(source: Path, destination: Path) -> None:
    """Publish an isolated project copy without exposing a partial directory."""
    if destination.exists():
        raise ParallelCandidateError(f"Refusing to overwrite {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
    )
    try:
        shutil.rmtree(temporary)
        shutil.copytree(source, temporary, ignore=_copy_ignore, symlinks=True)
        os.replace(temporary, destination)
    except (OSError, shutil.Error) as exc:
        raise ParallelCandidateError(
            f"Could not copy project from {source} to {destination}: {exc}"
        ) from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def prepare_baseline(source_fixture: Path, parent_run_dir: Path) -> Path:
    """Create or return the immutable common candidate baseline."""
    baseline = parent_run_dir / BASELINE_DIRNAME
    if baseline.exists():
        if not baseline.is_dir() or baseline.is_symlink():
            raise ParallelCandidateError(
                f"Baseline is not a safe directory: {baseline}"
            )
        return baseline
    copy_project(source_fixture, baseline)
    return baseline


def _load_json_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ParallelCandidateError(
            f"Could not read {description} {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ParallelCandidateError(
            f"{description} must contain a JSON object: {path}"
        )
    return value


def _normalize_command_paths(config: dict[str, Any], source_fixture: Path) -> None:
    """Make existing fixture-relative executable/script paths portable to copies."""
    command = config.get("command")
    if not isinstance(command, list):
        return
    normalized: list[Any] = []
    for argument in command:
        if isinstance(argument, str) and argument and not argument.startswith("-"):
            path = Path(argument).expanduser()
            source_path = path if path.is_absolute() else source_fixture / path
            if source_path.exists():
                # Keep a venv interpreter's symlink path. Resolving that symlink
                # to its base interpreter would bypass pyvenv.cfg discovery.
                argument = str(source_path.absolute())
        normalized.append(argument)
    config["command"] = normalized


def _include_gitignored_files_in_sonar_scan(config: dict[str, Any]) -> None:
    """Force a candidate scan to include its intentionally ignored workspace."""
    command = config.get("command")
    if not isinstance(command, list):
        return
    property_prefix = "-Dsonar.scm.exclusions.disabled="
    config["command"] = [
        argument
        for argument in command
        if not (
            isinstance(argument, str)
            and argument.lower().startswith(property_prefix.lower())
        )
    ]
    config["command"].append(SONAR_INCLUDE_IGNORED_PROPERTY)


def build_candidate_state(
    parent_state: dict[str, Any],
    *,
    name: str,
    source_fixture: Path,
    override: dict[str, Any],
) -> dict[str, Any]:
    """Create a clean child input carrying only shared task and approval context."""
    raw_outputs = parent_state.get("outputs")
    outputs = raw_outputs if isinstance(raw_outputs, dict) else {}
    approval = outputs.get("approval")
    content = approval.get("content") if isinstance(approval, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ParallelCandidateError(
            "Parallel candidates require a non-empty human-approved plan"
        )

    child = copy.deepcopy(parent_state)
    child["run_id"] = f"{parent_state['run_id']}--{name}"
    child["parent_run_id"] = parent_state["run_id"]
    child["candidate_id"] = name
    child["state_sequence"] = 0
    child["fixture_path"] = "workspace"
    child["current_node"] = "input"
    child["next_node"] = "code"
    child["status"] = "ready"
    child["workflow_status"] = "running"
    child["iteration"] = 0
    child["test_attempts"] = []
    child["review_attempts"] = []
    child["fix_attempts"] = []
    child["outputs"] = {
        key: copy.deepcopy(outputs[key])
        for key in ("plan", "approval")
        if key in outputs
    }
    child.pop("parallel_config", None)
    child.pop("candidate_results", None)
    child.pop("selected_candidate", None)

    if "max_iterations" in override:
        child["max_iterations"] = override["max_iterations"]
    if "test_config" in override:
        child["test_config"] = copy.deepcopy(override["test_config"])
    if "constraint" in override:
        child["candidate_constraint"] = override["constraint"]

    test_config = child.get("test_config")
    if not isinstance(test_config, dict):
        raise ParallelCandidateError("Candidate test_config must be an object")
    _normalize_command_paths(test_config, source_fixture)

    review_config = child.get("review_config")
    if not isinstance(review_config, dict):
        raise ParallelCandidateError("Candidate review_config must be an object")
    _normalize_command_paths(review_config, source_fixture)
    _include_gitignored_files_in_sonar_scan(review_config)
    review_config["serialization_lock"] = "../.sonar-review.lock"
    return child


def _write_json(path: Path, value: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def prepare_candidate(
    parent_state: dict[str, Any],
    *,
    parent_run_dir: Path,
    baseline: Path,
    source_fixture: Path,
    name: str,
    override: dict[str, Any],
) -> Path:
    """Create an isolated child run atomically, or validate an existing one."""
    candidates_root = parent_run_dir / CANDIDATES_DIRNAME
    candidates_root.mkdir(exist_ok=True)
    destination = candidates_root / name
    expected_run_id = f"{parent_state['run_id']}--{name}"
    if destination.exists():
        if not destination.is_dir() or destination.is_symlink():
            raise ParallelCandidateError(
                f"Candidate path is not a safe directory: {destination}"
            )
        initial = _load_json_object(destination / "00_input.json", "candidate input")
        expected = build_candidate_state(
            parent_state,
            name=name,
            source_fixture=source_fixture,
            override=override,
        )
        if (
            initial != expected
            or initial.get("run_id") != expected_run_id
            or initial.get("parent_run_id") != parent_state["run_id"]
            or initial.get("candidate_id") != name
            or initial.get("fixture_path") != "workspace"
            or not (destination / "workspace").is_dir()
        ):
            raise ParallelCandidateError(
                f"Existing candidate directory does not match {name}: {destination}"
            )
        return destination

    temporary = Path(
        tempfile.mkdtemp(
            dir=candidates_root,
            prefix=f".{name}.",
            suffix=".tmp",
        )
    )
    try:
        copy_project(baseline, temporary / "workspace")
        child_state = build_candidate_state(
            parent_state,
            name=name,
            source_fixture=source_fixture,
            override=override,
        )
        _write_json(temporary / "00_input.json", child_state)
        os.replace(temporary, destination)
    except (OSError, ParallelCandidateError) as exc:
        if isinstance(exc, ParallelCandidateError):
            raise
        raise ParallelCandidateError(f"Could not prepare {name}: {exc}") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination


def _latest_attempt(state: dict[str, Any], field: str) -> dict[str, Any] | None:
    history = state.get(field)
    if (
        not isinstance(history, list)
        or not history
        or not isinstance(history[-1], dict)
    ):
        return None
    return history[-1]


def candidate_result(
    candidate_dir: Path,
    *,
    name: str,
    parent_run_id: str,
    process_exit_code: int | None = None,
) -> dict[str, Any]:
    """Validate one child's manifest/checkpoint and return join-safe summary data."""
    base = {
        "candidate_id": name,
        "run_dir": str(Path(CANDIDATES_DIRNAME) / name),
        "workspace": str(Path(CANDIDATES_DIRNAME) / name / "workspace"),
        "dispatcher_exit_code": process_exit_code,
    }
    expected_run_id = f"{parent_run_id}--{name}"
    try:
        manifest = load_manifest(candidate_dir / MANIFEST_FILENAME)
        if manifest["run_id"] != expected_run_id:
            raise ParallelCandidateError("Child manifest has an unexpected run_id")
        checkpoint = manifest["checkpoint"]
        final_path = candidate_dir / checkpoint["state_file"]
        final_state = _load_json_object(final_path, "candidate checkpoint")
        if (
            final_state.get("run_id") != expected_run_id
            or final_state.get("parent_run_id") != parent_run_id
            or final_state.get("candidate_id") != name
            or final_state.get("state_sequence") != checkpoint["state_sequence"]
            or final_state.get("next_node") != checkpoint["next_node"]
        ):
            raise ParallelCandidateError(
                "Child checkpoint does not match its manifest coordinates"
            )
    except (ManifestError, ParallelCandidateError) as exc:
        return {
            **base,
            "terminal": True,
            "status": "dispatcher_error",
            "tests_passed": False,
            "quality_gate_passed": False,
            "sonar_finding_count": None,
            "fix_iterations": None,
            "eligible": False,
            "exclusion_reason": str(exc),
        }

    workflow_status = manifest["workflow_status"]
    terminal = workflow_status in {"succeeded", "gave_up", "error", "failed"}
    latest_test = _latest_attempt(final_state, "test_attempts")
    latest_review = _latest_attempt(final_state, "review_attempts")
    tests_passed = latest_test is not None and latest_test.get("result") == "passed"
    quality_gate_passed = (
        latest_review is not None
        and latest_review.get("result") == "passed"
        and latest_review.get("quality_gate_status") == "OK"
    )
    findings = latest_review.get("findings") if latest_review is not None else None
    finding_count = len(findings) if isinstance(findings, list) else None
    fix_iterations = final_state.get("iteration")
    if not isinstance(fix_iterations, int) or isinstance(fix_iterations, bool):
        fix_iterations = None
    eligible = (
        terminal
        and workflow_status == "succeeded"
        and checkpoint["next_node"] == "END"
        and tests_passed
        and quality_gate_passed
        and finding_count is not None
        and fix_iterations is not None
    )

    if eligible:
        reason = None
    elif not terminal:
        reason = f"Candidate is not terminal (workflow_status={workflow_status!r})"
    elif not tests_passed:
        reason = "Latest fixture test did not pass"
    elif not quality_gate_passed:
        reason = "Latest SonarQube quality gate did not pass"
    else:
        reason = f"Candidate ended with workflow_status={workflow_status!r}"

    return {
        **base,
        "terminal": terminal,
        "status": workflow_status,
        "checkpoint": checkpoint["state_file"],
        "tests_passed": tests_passed,
        "quality_gate_passed": quality_gate_passed,
        "sonar_finding_count": finding_count,
        "fix_iterations": fix_iterations,
        "eligible": eligible,
        "exclusion_reason": reason,
    }


def select_winner(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose an eligible candidate using the documented deterministic rule."""
    eligible = [result for result in results if result.get("eligible") is True]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda result: (
            result["sonar_finding_count"],
            result["fix_iterations"],
            result["candidate_id"],
        ),
    )


def _managed_files(root: Path) -> dict[str, dict[str, Any]]:
    """Return content and mode fingerprints for non-generated regular files."""
    if not root.is_dir() or root.is_symlink():
        raise ParallelCandidateError(f"Project is not a safe directory: {root}")
    inventory: dict[str, dict[str, Any]] = {}
    try:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if any(
                part in IGNORED_NAMES for part in relative.parts
            ) or path.name.endswith((".pyc", ".pyo", PROMOTION_TEMP_SUFFIX)):
                continue
            if path.is_symlink():
                raise ParallelCandidateError(
                    f"Symlinks are not supported in candidate promotion: {relative}"
                )
            if path.is_dir():
                continue
            if not path.is_file():
                raise ParallelCandidateError(
                    f"Unsupported project entry in candidate promotion: {relative}"
                )
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            inventory[relative.as_posix()] = {
                "sha256": digest,
                "mode": stat.S_IMODE(path.stat().st_mode),
            }
    except OSError as exc:
        raise ParallelCandidateError(
            f"Could not inventory project directory {root}: {exc}"
        ) from exc
    return inventory


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=PROMOTION_TEMP_SUFFIX,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def promote_candidate(
    *, baseline: Path, winner_workspace: Path, canonical_fixture: Path
) -> dict[str, list[str]]:
    """Promote a winner unless canonical files contain incompatible changes."""
    baseline_inventory = _managed_files(baseline)
    canonical_inventory = _managed_files(canonical_fixture)
    winner_inventory = _managed_files(winner_workspace)
    for relative in sorted(
        set(baseline_inventory) | set(canonical_inventory) | set(winner_inventory)
    ):
        baseline_value = baseline_inventory.get(relative)
        canonical_value = canonical_inventory.get(relative)
        winner_value = winner_inventory.get(relative)
        if baseline_value == winner_value:
            compatible = canonical_value == baseline_value
        else:
            # A retry may observe either side of an earlier, interrupted
            # promotion. Any third value is unrelated concurrent work.
            compatible = canonical_value in (baseline_value, winner_value)
        if not compatible:
            raise ParallelCandidateError(
                "Canonical fixture changed incompatibly after fan-out at "
                f"{relative}; refusing to overwrite concurrent work"
            )

    changed = sorted(
        path
        for path, fingerprint in winner_inventory.items()
        if baseline_inventory.get(path) != fingerprint
    )
    deleted = sorted(set(baseline_inventory) - set(winner_inventory))

    try:
        for relative in changed:
            _atomic_copy(
                winner_workspace / Path(relative), canonical_fixture / Path(relative)
            )
        for relative in deleted:
            target = canonical_fixture / Path(relative)
            if not target.exists():
                continue
            if target.is_symlink() or not target.is_file():
                raise ParallelCandidateError(
                    f"Refusing to delete unexpected promotion target: {relative}"
                )
            target.unlink()
    except OSError as exc:
        raise ParallelCandidateError(
            f"Could not promote winning candidate: {exc}"
        ) from exc

    return {"changed_files": changed, "deleted_files": deleted}
