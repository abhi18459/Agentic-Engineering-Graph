"""Tests for deterministic Step 6 review state and routing."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

GRAPH_ROOT = Path(__file__).resolve().parents[1]
NODES_ROOT = GRAPH_ROOT / "nodes"
sys.path.insert(0, str(GRAPH_ROOT))
sys.path.insert(0, str(NODES_ROOT))

import dispatcher
import fix
import review
from common import NodeError
from sonar_client import (
    SonarError,
    execute_sonar_scan,
    indexed_file_count,
    normalize_conditions,
    normalize_hotspot,
    normalize_issue,
)


def state(*, iteration: int = 0, maximum: int = 3) -> dict:
    """Return the relevant portion of a Step 6 graph state."""
    return {
        "current_node": "review",
        "iteration": iteration,
        "max_iterations": maximum,
        "test_attempts": [],
        "review_attempts": [],
        "fix_attempts": [{} for _ in range(iteration)],
    }


class ReviewRoutingTest(unittest.TestCase):
    def test_review_configuration_uses_bounded_documented_defaults(self) -> None:
        command, gate_timeout, process_timeout, api_timeout, environment = (
            review.review_configuration(
                {"review_config": {"command": ["sonar-scanner"]}}
            )
        )
        self.assertEqual(command, ["sonar-scanner"])
        self.assertEqual(gate_timeout, 300)
        self.assertEqual(process_timeout, 360)
        self.assertEqual(api_timeout, 30)
        self.assertEqual(environment, {})

    def test_review_configuration_rejects_persisted_token(self) -> None:
        with self.assertRaises(NodeError):
            review.review_configuration(
                {
                    "review_config": {
                        "command": ["sonar-scanner", "-Dsonar.token=secret"]
                    }
                }
            )

    def test_pass_routes_to_end(self) -> None:
        self.assertEqual(
            review.review_route(state(), {"result": "passed"}),
            ("END", "succeeded", None),
        )

    def test_failed_gate_routes_to_fix_while_budget_remains(self) -> None:
        self.assertEqual(
            review.review_route(state(iteration=1), {"result": "failed"}),
            ("fix", "running", None),
        )

    def test_failed_gate_gives_up_after_final_fix(self) -> None:
        next_node, workflow_status, reason = review.review_route(
            state(iteration=3), {"result": "failed"}
        )
        self.assertEqual((next_node, workflow_status), ("give_up", "gave_up"))
        self.assertIn("3 fix attempts", reason or "")

    def test_scanner_error_is_not_sent_to_fix(self) -> None:
        next_node, workflow_status, reason = review.review_route(
            state(), {"result": "scanner_error", "error": "authentication failed"}
        )
        self.assertEqual((next_node, workflow_status), ("give_up", "error"))
        self.assertEqual(reason, "authentication failed")


class ReviewNormalizationTest(unittest.TestCase):
    def test_scanner_requires_a_positive_indexed_file_count(self) -> None:
        self.assertEqual(
            indexed_file_count("12:00:00 INFO  3 files indexed (done) | time=1ms"),
            3,
        )
        with self.assertRaisesRegex(SonarError, "indexed zero files"):
            indexed_file_count("12:00:00 INFO  0 files indexed (done) | time=1ms")
        with self.assertRaisesRegex(SonarError, "did not report"):
            indexed_file_count("12:00:00 INFO  EXECUTION SUCCESS")

    def test_zero_file_scan_returns_a_scanner_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            metadata = root / "report-task.txt"
            metadata.write_text(
                "serverUrl=https://sonar.example\n"
                "ceTaskUrl=https://sonar.example/api/ce/task?id=one\n"
                "ceTaskId=one\n"
                "projectKey=fixture\n",
                encoding="utf-8",
            )
            completed = subprocess.CompletedProcess(
                args=["sonar-scanner"],
                returncode=0,
                stdout="12:00:00 INFO  0 files indexed (done) | time=1ms",
                stderr="",
            )
            with patch("sonar_client.subprocess.run", return_value=completed):
                result = execute_sonar_scan(
                    fixture=root,
                    base_command=["sonar-scanner"],
                    quality_gate_timeout=300,
                    process_timeout=360,
                    environment={},
                    token="token",
                    metadata_path=metadata,
                )

        self.assertEqual(result["result"], "scanner_error")
        self.assertEqual(result["error"], "SonarScanner indexed zero files")

    def test_volatile_issue_fields_do_not_affect_normalized_finding(self) -> None:
        shared = {
            "rule": "python:S2068",
            "type": "VULNERABILITY",
            "severity": "BLOCKER",
            "component": "project:starter_repo/plot_data.py",
            "line": 10,
            "message": "Remove this hard-coded password.",
            "status": "OPEN",
            "tags": ["cwe", "security"],
        }
        first = {**shared, "key": "volatile-one", "creationDate": "2026-01-01"}
        second = {**shared, "key": "volatile-two", "creationDate": "2026-09-06"}
        self.assertEqual(
            normalize_issue(first, "project"), normalize_issue(second, "project")
        )

    def test_conditions_and_findings_have_stable_order(self) -> None:
        conditions = [
            {"metricKey": "coverage", "status": "ERROR", "actualValue": "20"},
            {"metricKey": "new_issues", "status": "ERROR", "actualValue": "1"},
        ]
        self.assertEqual(
            [
                item["metric"]
                for item in normalize_conditions(list(reversed(conditions)))
            ],
            ["coverage", "new_issues"],
        )
        hotspot = {
            "securityCategory": "credentials",
            "component": "project:starter_repo/plot_data.py",
            "textRange": {"startLine": 8},
            "message": "Review this credential.",
            "status": "TO_REVIEW",
        }
        self.assertEqual(normalize_hotspot(hotspot, "project")["line"], 8)


class FixTriggerTest(unittest.TestCase):
    def test_fix_accepts_latest_failed_review(self) -> None:
        graph_state = state()
        graph_state["review_attempts"] = [
            {"attempt": 1, "result": "failed", "findings": [{"rule": "python:S2068"}]}
        ]
        trigger, attempt = fix.latest_repairable_failure(graph_state)
        self.assertEqual(trigger, "review")
        self.assertEqual(attempt["attempt"], 1)

    def test_dispatcher_knows_review_node(self) -> None:
        self.assertIn("review", dispatcher.NODES)
        self.assertIn("review", dispatcher.ROUTABLE_NODES)
        self.assertEqual(
            dispatcher.ENFORCED_TRANSITIONS["test"],
            {"review", "fix", "give_up"},
        )
        self.assertEqual(
            dispatcher.ENFORCED_TRANSITIONS["review"],
            {"END", "fix", "give_up"},
        )


if __name__ == "__main__":
    unittest.main()
