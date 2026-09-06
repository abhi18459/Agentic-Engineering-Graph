#!/usr/bin/env python3
"""Validate finished candidates, select a winner, and promote its files."""

from __future__ import annotations

import sys
from typing import Any

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
    BASELINE_DIRNAME,
    CANDIDATES_DIRNAME,
    ParallelCandidateError,
    candidate_id,
    candidate_result,
    parallel_configuration,
    promote_candidate,
    select_winner,
)

RESULT_FIELDS = (
    "candidate_id",
    "status",
    "tests_passed",
    "quality_gate_passed",
    "sonar_finding_count",
    "fix_iterations",
    "eligible",
)


def persisted_fan_out_results(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the candidate summaries produced by the immediately prior fan-out."""
    outputs = state.get("outputs")
    fan_out = outputs.get("fan_out") if isinstance(outputs, dict) else None
    results = fan_out.get("candidates") if isinstance(fan_out, dict) else None
    if not isinstance(results, list) or not all(
        isinstance(result, dict) for result in results
    ):
        raise NodeError("Join requires fan_out candidate results")
    return results


def verify_result_unchanged(persisted: dict[str, Any], current: dict[str, Any]) -> None:
    """Reject candidate evidence changed after fan-out completed."""
    for field in RESULT_FIELDS:
        if persisted.get(field) != current.get(field):
            raise NodeError(
                f"Candidate {current['candidate_id']} changed {field!r} after fan-out"
            )


def main() -> int:
    args = node_parser("join").parse_args()
    try:
        state = read_state(args.input_state, "join")
        parent_run_dir = args.input_state.resolve().parent
        canonical_fixture = resolve_fixture_path(state, args.input_state)
        config = parallel_configuration(state)
        persisted = persisted_fan_out_results(state)
        expected_ids = [
            candidate_id(number) for number in range(1, config["candidate_count"] + 1)
        ]
        persisted_ids: list[str] = []
        for result in persisted:
            name = result.get("candidate_id")
            if not isinstance(name, str):
                raise NodeError("Fan-out results do not match configured candidates")
            persisted_ids.append(name)
        if sorted(persisted_ids) != sorted(expected_ids):
            raise NodeError("Fan-out results do not match configured candidates")
        persisted_by_id = {result["candidate_id"]: result for result in persisted}

        results: list[dict[str, Any]] = []
        for name in expected_ids:
            stored = persisted_by_id[name]
            current = candidate_result(
                parent_run_dir / CANDIDATES_DIRNAME / name,
                name=name,
                parent_run_id=state["run_id"],
                process_exit_code=stored.get("dispatcher_exit_code"),
            )
            verify_result_unchanged(stored, current)
            if not current["terminal"]:
                raise NodeError(f"Cannot join non-terminal candidate {name}")
            results.append(current)

        winner = select_winner(results)
        if winner is None:
            output: dict[str, Any] = {
                "ranking_rule": config["ranking_rule"],
                "eligible_candidates": [],
                "excluded_candidates": [
                    {
                        "candidate_id": result["candidate_id"],
                        "reason": result["exclusion_reason"],
                    }
                    for result in results
                ],
                "winner": None,
                "promotion": None,
            }
            updated = advance_state(
                state,
                current_node="join",
                next_node="give_up",
                output=output,
                workflow_status="gave_up",
            )
            updated["give_up_reason"] = (
                "No parallel candidate passed both tests and SonarQube review"
            )
        else:
            winner_id = winner["candidate_id"]
            promotion = promote_candidate(
                baseline=parent_run_dir / BASELINE_DIRNAME,
                winner_workspace=(
                    parent_run_dir / CANDIDATES_DIRNAME / winner_id / "workspace"
                ),
                canonical_fixture=canonical_fixture,
            )
            output = {
                "ranking_rule": config["ranking_rule"],
                "eligible_candidates": [
                    result["candidate_id"] for result in results if result["eligible"]
                ],
                "excluded_candidates": [
                    {
                        "candidate_id": result["candidate_id"],
                        "reason": result["exclusion_reason"],
                    }
                    for result in results
                    if not result["eligible"]
                ],
                "winner": {
                    "candidate_id": winner_id,
                    "sonar_finding_count": winner["sonar_finding_count"],
                    "fix_iterations": winner["fix_iterations"],
                },
                "selection_reason": (
                    "Passed tests and SonarQube review; selected by fewest "
                    "findings, then fewest fixes, then candidate ID"
                ),
                "promotion": promotion,
            }
            updated = advance_state(
                state,
                current_node="join",
                next_node="END",
                output=output,
                workflow_status="succeeded",
            )
            updated["selected_candidate"] = winner_id

        write_state(args.output_state, updated, args.input_state)
    except (NodeError, ParallelCandidateError) as exc:
        print(f"Join node error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
