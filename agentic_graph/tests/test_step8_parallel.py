"""Tests for Step 8 candidate isolation, ranking, and promotion helpers."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

GRAPH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GRAPH_ROOT))
sys.path.insert(0, str(GRAPH_ROOT / "nodes"))

import approval
import review
from common import NodeError
from parallel_candidates import (
    MAX_CANDIDATES,
    ParallelCandidateError,
    build_candidate_state,
    candidate_result,
    parallel_configuration,
    promote_candidate,
    select_winner,
)
from run_manifest import (
    complete_attempt,
    create_manifest,
    start_attempt,
    write_manifest,
)


def parent_state() -> dict:
    """Return the shared parent fields used to construct candidate inputs."""
    return {
        "schema_version": 2,
        "run_id": "parallel-test",
        "state_sequence": 1,
        "task": {"title": "parallel test"},
        "fixture_path": "fixture",
        "current_node": "approval",
        "next_node": "fan_out",
        "status": "completed",
        "workflow_status": "running",
        "iteration": 0,
        "max_iterations": 3,
        "parallel_config": {
            "candidate_count": 3,
            "ranking_rule": "fewest_sonar_findings",
            "candidate_overrides": {},
        },
        "test_config": {
            "command": [".venv/bin/python", "-m", "pytest"],
        },
        "review_config": {"command": ["sonar-scanner"]},
        "outputs": {"approval": {"content": "Approved implementation plan"}},
        "test_attempts": [],
        "review_attempts": [],
        "fix_attempts": [],
    }


def result(
    name: str, *, eligible: bool, findings: int | None, fixes: int | None
) -> dict:
    """Build the ranking fields returned from a completed candidate."""
    return {
        "candidate_id": name,
        "eligible": eligible,
        "sonar_finding_count": findings,
        "fix_iterations": fixes,
    }


class ParallelConfigurationTest(unittest.TestCase):
    def test_candidate_count_is_bounded(self) -> None:
        state = parent_state()
        state["parallel_config"]["candidate_count"] = MAX_CANDIDATES + 1
        with self.assertRaises(ParallelCandidateError):
            parallel_configuration(state)

    def test_approval_routes_parallel_runs_to_fan_out(self) -> None:
        state = parent_state()
        state["current_node"] = "plan"
        state["next_node"] = "approval"
        state["workflow_status"] = "awaiting_approval"
        approved = approval.approved_state(
            state,
            {
                "decision": "approved",
                "approved_at": "2026-09-06T00:00:00+00:00",
                "plan_file": "plan_review.md",
                "plan_sha256": "0" * 64,
            },
            "Approved implementation plan",
        )
        self.assertEqual(approved["next_node"], "fan_out")

    def test_candidate_state_uses_an_isolated_workspace_and_clean_histories(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            interpreter = fixture / ".venv" / "bin" / "python"
            interpreter.parent.mkdir(parents=True)
            interpreter.touch()
            child = build_candidate_state(
                parent_state(),
                name="candidate-01",
                source_fixture=fixture,
                override={},
            )

        self.assertEqual(child["fixture_path"], "workspace")
        self.assertEqual(child["next_node"], "code")
        self.assertEqual(child["test_config"]["command"][0], str(interpreter))
        self.assertEqual(
            child["review_config"]["serialization_lock"],
            "../.sonar-review.lock",
        )
        self.assertNotIn("parallel_config", child)
        self.assertEqual(child["test_attempts"], [])
        self.assertEqual(child["review_attempts"], [])
        self.assertEqual(child["fix_attempts"], [])

    def test_candidate_review_lock_is_confined_to_candidates_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate_dir = Path(temporary) / "candidates" / "candidate-01"
            candidate_dir.mkdir(parents=True)
            input_path = candidate_dir / "04_test.json"
            state = {"review_config": {"serialization_lock": "../.sonar-review.lock"}}
            with review.review_serialization_lock(state, input_path):
                self.assertTrue((candidate_dir.parent / ".sonar-review.lock").is_file())

            state["review_config"]["serialization_lock"] = "/tmp/sonar.lock"
            with (
                self.assertRaises(NodeError),
                review.review_serialization_lock(state, input_path),
            ):
                self.fail("absolute lock path unexpectedly accepted")


class CandidateSelectionTest(unittest.TestCase):
    def test_terminal_manifest_must_prove_tests_and_quality_gate_passed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate_dir = Path(temporary) / "candidate-01"
            candidate_dir.mkdir()
            final_state = {
                "run_id": "parent--candidate-01",
                "parent_run_id": "parent",
                "candidate_id": "candidate-01",
                "state_sequence": 1,
                "next_node": "END",
                "iteration": 0,
                "test_attempts": [{"result": "passed"}],
                "review_attempts": [
                    {
                        "result": "passed",
                        "quality_gate_status": "OK",
                        "findings": [],
                    }
                ],
            }
            (candidate_dir / "01_review.json").write_text(
                json.dumps(final_state), encoding="utf-8"
            )
            manifest_path = candidate_dir / "manifest.json"
            manifest = create_manifest(
                manifest_path,
                run_id="parent--candidate-01",
                initial_state_file="00_input.json",
                state_sequence=0,
                next_node="review",
            )
            attempt_id = start_attempt(
                manifest,
                node="review",
                input_state="00_input.json",
                output_state="01_review.json",
            )
            complete_attempt(
                manifest,
                attempt_id=attempt_id,
                output_state="01_review.json",
                state_sequence=1,
                next_node="END",
                workflow_status="succeeded",
                exit_code=0,
            )
            write_manifest(manifest_path, manifest)

            summary = candidate_result(
                candidate_dir,
                name="candidate-01",
                parent_run_id="parent",
                process_exit_code=0,
            )

        self.assertTrue(summary["eligible"])
        self.assertEqual(summary["sonar_finding_count"], 0)

    def test_failed_candidates_are_excluded_and_fewest_findings_wins(self) -> None:
        winner = select_winner(
            [
                result("candidate-01", eligible=True, findings=2, fixes=0),
                result("candidate-02", eligible=True, findings=0, fixes=1),
                result("candidate-03", eligible=False, findings=None, fixes=1),
            ]
        )
        assert winner is not None
        self.assertEqual(winner["candidate_id"], "candidate-02")

    def test_fix_count_then_candidate_id_break_ties_deterministically(self) -> None:
        winner = select_winner(
            [
                result("candidate-02", eligible=True, findings=0, fixes=0),
                result("candidate-03", eligible=True, findings=0, fixes=1),
                result("candidate-01", eligible=True, findings=0, fixes=0),
            ]
        )
        assert winner is not None
        self.assertEqual(winner["candidate_id"], "candidate-01")

    def test_no_eligible_candidate_has_no_winner(self) -> None:
        self.assertIsNone(
            select_winner(
                [result("candidate-01", eligible=False, findings=None, fixes=3)]
            )
        )


class CandidatePromotionTest(unittest.TestCase):
    def test_only_winner_delta_is_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline"
            canonical = root / "canonical"
            winner = root / "winner"
            for directory in (baseline, canonical, winner):
                directory.mkdir()
                (directory / "changed.py").write_text("before\n", encoding="utf-8")
                (directory / "deleted.py").write_text("delete\n", encoding="utf-8")
            (winner / "changed.py").write_text("after\n", encoding="utf-8")
            (winner / "deleted.py").unlink()
            (winner / "added.py").write_text("new\n", encoding="utf-8")

            promoted = promote_candidate(
                baseline=baseline,
                winner_workspace=winner,
                canonical_fixture=canonical,
            )

            self.assertEqual(promoted["changed_files"], ["added.py", "changed.py"])
            self.assertEqual(promoted["deleted_files"], ["deleted.py"])
            self.assertEqual(
                (canonical / "changed.py").read_text(encoding="utf-8"),
                "after\n",
            )
            self.assertEqual(
                (canonical / "added.py").read_text(encoding="utf-8"), "new\n"
            )
            self.assertFalse((canonical / "deleted.py").exists())

    def test_concurrent_canonical_change_blocks_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline"
            canonical = root / "canonical"
            winner = root / "winner"
            for directory in (baseline, canonical, winner):
                directory.mkdir()
                (directory / "file.py").write_text("baseline\n", encoding="utf-8")
            (canonical / "file.py").write_text("human change\n", encoding="utf-8")

            with self.assertRaises(ParallelCandidateError):
                promote_candidate(
                    baseline=baseline,
                    winner_workspace=winner,
                    canonical_fixture=canonical,
                )

    def test_partially_promoted_winner_can_be_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = root / "baseline"
            canonical = root / "canonical"
            winner = root / "winner"
            for directory in (baseline, canonical, winner):
                directory.mkdir()
                (directory / "one.py").write_text("before\n", encoding="utf-8")
                (directory / "two.py").write_text("before\n", encoding="utf-8")
            (winner / "one.py").write_text("after one\n", encoding="utf-8")
            (winner / "two.py").write_text("after two\n", encoding="utf-8")
            (canonical / "one.py").write_text("after one\n", encoding="utf-8")

            promote_candidate(
                baseline=baseline,
                winner_workspace=winner,
                canonical_fixture=canonical,
            )

            self.assertEqual(
                (canonical / "one.py").read_text(encoding="utf-8"), "after one\n"
            )
            self.assertEqual(
                (canonical / "two.py").read_text(encoding="utf-8"), "after two\n"
            )


if __name__ == "__main__":
    unittest.main()
