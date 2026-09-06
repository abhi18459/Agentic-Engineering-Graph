"""Tests for the Step 7 SonarQube MCP review contract."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

GRAPH_ROOT = Path(__file__).resolve().parents[1]
NODES_ROOT = GRAPH_ROOT / "nodes"
sys.path.insert(0, str(GRAPH_ROOT))
sys.path.insert(0, str(NODES_ROOT))

import fix
import review
from common import NodeError
from mcp_review_client import (
    McpReviewError,
    codex_review_command,
    mcp_tool_calls,
    parse_jsonl,
    validate_agent_result,
    validate_tool_calls,
)

PROJECT_KEY = "agentic-engineering-fixture"


def completed_call(tool: str, arguments: dict) -> dict:
    """Build one successful Codex JSONL MCP event."""
    return {
        "type": "item.completed",
        "item": {
            "id": f"call-{tool}",
            "type": "mcp_tool_call",
            "server": "sonarqube",
            "tool": tool,
            "arguments": arguments,
            "status": "completed",
            "result": {"content": "fixture response"},
            "error": None,
        },
    }


def required_events() -> list[dict]:
    """Build evidence for every required read-only Sonar query."""
    return [
        completed_call("get_project_quality_gate_status", {"projectKey": PROJECT_KEY}),
        completed_call(
            "search_sonar_issues_in_projects",
            {
                "projectKeys": [PROJECT_KEY],
                "issueStatuses": ["OPEN", "CONFIRMED"],
                "pageSize": 500,
            },
        ),
        completed_call(
            "search_security_hotspots",
            {
                "projectKey": PROJECT_KEY,
                "status": "TO_REVIEW",
                "pageSize": 500,
            },
        ),
    ]


class McpReviewConfigurationTest(unittest.TestCase):
    def test_step6_backend_remains_the_default(self) -> None:
        self.assertEqual(
            review.review_backend({"review_config": {}}), review.CLI_BACKEND
        )

    def test_mcp_backend_and_timeout_are_configurable(self) -> None:
        state = {
            "review_config": {
                "backend": "sonarqube_mcp",
                "mcp_process_timeout_seconds": 240,
            }
        }
        self.assertEqual(review.review_backend(state), "sonarqube_mcp")
        self.assertEqual(review.mcp_process_timeout(state), 240)

    def test_unknown_backend_is_rejected(self) -> None:
        with self.assertRaises(NodeError):
            review.review_backend({"review_config": {"backend": "guess"}})

    def test_persisted_mcp_credentials_are_rejected(self) -> None:
        with self.assertRaises(NodeError):
            review.review_configuration(
                {
                    "review_config": {
                        "command": ["sonar-scanner"],
                        "environment": {"SONARQUBE_TOKEN": "secret"},
                    }
                }
            )

    def test_agent_command_loads_trusted_project_mcp_configuration(self) -> None:
        command = codex_review_command(
            Path("fixture"), Path("schema.json"), Path("result.json")
        )
        self.assertNotIn("--ignore-user-config", command)
        self.assertIn("--strict-config", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")


class McpProvenanceTest(unittest.TestCase):
    def test_required_project_scoped_calls_are_accepted(self) -> None:
        calls = mcp_tool_calls(required_events())
        self.assertEqual(validate_tool_calls(calls, PROJECT_KEY), calls)

    def test_legacy_argument_aliases_are_accepted(self) -> None:
        events = required_events()
        events[1]["item"]["arguments"] = {
            "projects": [PROJECT_KEY],
            "issueStatuses": ["OPEN", "CONFIRMED"],
            "ps": 500,
        }
        events[2]["item"]["arguments"] = {
            "projectKey": PROJECT_KEY,
            "status": "TO_REVIEW",
            "ps": 500,
        }
        calls = mcp_tool_calls(events)
        self.assertEqual(validate_tool_calls(calls, PROJECT_KEY), calls)

    def test_wrong_page_size_is_rejected(self) -> None:
        events = required_events()
        events[1]["item"]["arguments"]["pageSize"] = 100
        with self.assertRaises(McpReviewError):
            validate_tool_calls(mcp_tool_calls(events), PROJECT_KEY)

    def test_missing_required_call_is_rejected(self) -> None:
        calls = mcp_tool_calls(required_events()[:-1])
        with self.assertRaises(McpReviewError):
            validate_tool_calls(calls, PROJECT_KEY)

    def test_call_for_another_project_is_rejected(self) -> None:
        events = required_events()
        events[0]["item"]["arguments"] = {"projectKey": "wrong-project"}
        with self.assertRaises(McpReviewError):
            validate_tool_calls(mcp_tool_calls(events), PROJECT_KEY)

    def test_non_mcp_execution_tool_is_rejected(self) -> None:
        events = [
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "git status"},
            }
        ]
        with self.assertRaises(McpReviewError):
            mcp_tool_calls(events)

    def test_jsonl_requires_a_completed_turn(self) -> None:
        value = "\n".join(json.dumps(event) for event in required_events())
        with self.assertRaises(McpReviewError):
            parse_jsonl(value)


class McpStructuredResultTest(unittest.TestCase):
    def test_result_is_normalized_to_step6_shape(self) -> None:
        value = {
            "decision": "failed",
            "quality_gate_status": "ERROR",
            "conditions": [
                {
                    "metric": "new_reliability_rating",
                    "comparator": "GT",
                    "error_threshold": "1",
                    "actual_value": "3",
                    "status": "ERROR",
                }
            ],
            "findings": [
                {
                    "kind": "issue",
                    "rule": "pythonbugs:S2583",
                    "type": "BUG",
                    "severity": "MAJOR",
                    "impacts": [
                        {"software_quality": "RELIABILITY", "severity": "MEDIUM"}
                    ],
                    "file": "starter_repo/plot_data.py",
                    "line": 62,
                    "message": "Fix this condition that always evaluates to false.",
                    "status": "OPEN",
                    "tags": [],
                }
            ],
        }
        result = validate_agent_result(value)
        self.assertEqual(result["decision"], "failed")
        self.assertEqual(result["quality_gate_status"], "ERROR")
        self.assertEqual(result["findings"][0]["rule"], "pythonbugs:S2583")

    def test_agent_cannot_override_gate_decision(self) -> None:
        with self.assertRaises(McpReviewError):
            validate_agent_result(
                {
                    "decision": "passed",
                    "quality_gate_status": "ERROR",
                    "conditions": [],
                    "findings": [],
                }
            )


class McpRepairContextTest(unittest.TestCase):
    def test_transport_diagnostics_are_not_sent_to_fix_agent(self) -> None:
        attempt = {
            "attempt": 1,
            "result": "failed",
            "quality_gate_status": "ERROR",
            "conditions": [{"status": "ERROR"}],
            "findings": [{"rule": "pythonbugs:S2583"}],
            "review_backend": "sonarqube_mcp",
            "stdout": "large scanner log",
            "mcp_tool_calls": [{"result": "large MCP response"}],
        }
        context = fix.repair_context("review", attempt)
        self.assertEqual(context["findings"], attempt["findings"])
        self.assertNotIn("stdout", context)
        self.assertNotIn("mcp_tool_calls", context)


if __name__ == "__main__":
    unittest.main()
