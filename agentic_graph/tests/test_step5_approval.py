"""Tests for the durable Step 5 human approval interrupt."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

GRAPH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GRAPH_ROOT))

import approve_plan
import dispatcher
from approval import APPROVAL_FILENAME, REVIEW_FILENAME, ApprovalError
from run_manifest import MANIFEST_FILENAME, ManifestError, load_manifest


def initial_state() -> dict:
    """Return a minimal new-run state accepted by the dispatcher."""
    return {
        "schema_version": 2,
        "run_id": "approval-test",
        "state_sequence": 0,
        "task": {"title": "test approval"},
        "fixture_path": "../../../fixture",
        "current_node": "input",
        "next_node": "plan",
        "status": "ready",
        "workflow_status": "running",
        "iteration": 0,
        "max_iterations": 3,
        "test_config": {},
        "outputs": {},
        "test_attempts": [],
        "fix_attempts": [],
    }


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


class ApprovalGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.run_dir = self.root / "run-approval"
        self.run_dir.mkdir()
        write_json(self.run_dir / "00_input.json", initial_state())

        self.fake_node = self.root / "fake_node.py"
        self.fake_node.write_text(
            """\
import argparse
import copy
import json

parser = argparse.ArgumentParser()
parser.add_argument("--input-state", required=True)
parser.add_argument("--output-state", required=True)
args = parser.parse_args()
with open(args.input_state, encoding="utf-8") as handle:
    previous = json.load(handle)
node = previous["next_node"]
current = copy.deepcopy(previous)
current["state_sequence"] += 1
current["current_node"] = node
current["status"] = "completed"
if node == "plan":
    current["next_node"] = "approval"
    current["workflow_status"] = "awaiting_approval"
    content = "Original generated plan"
else:
    current["next_node"] = "END"
    current["workflow_status"] = "succeeded"
    content = current["outputs"]["approval"]["content"]
current["outputs"][node] = {"content": content}
with open(args.output_state, "x", encoding="utf-8") as handle:
    json.dump(current, handle)
""",
            encoding="utf-8",
        )
        self.nodes = {"plan": self.fake_node, "code": self.fake_node}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def dispatch(self) -> int:
        with patch.dict(dispatcher.NODES, self.nodes, clear=True):
            return dispatcher.dispatch(self.run_dir, None)

    def test_run_pauses_after_plan_and_remains_idempotently_paused(self) -> None:
        self.assertEqual(self.dispatch(), 0)

        review_path = self.run_dir / REVIEW_FILENAME
        self.assertEqual(
            review_path.read_text(encoding="utf-8"), "Original generated plan\n"
        )
        self.assertFalse((self.run_dir / "02_approval.json").exists())
        self.assertFalse((self.run_dir / "02_code.json").exists())

        manifest_path = self.run_dir / MANIFEST_FILENAME
        manifest_before = load_manifest(manifest_path)
        self.assertEqual(manifest_before["workflow_status"], "awaiting_approval")
        self.assertEqual(manifest_before["current_node"], "approval")
        self.assertEqual(
            [attempt["node"] for attempt in manifest_before["attempts"]], ["plan"]
        )

        self.assertEqual(self.dispatch(), 0)
        manifest_after = load_manifest(manifest_path)
        self.assertEqual(manifest_after, manifest_before)
        self.assertEqual(
            review_path.read_text(encoding="utf-8"), "Original generated plan\n"
        )

    def test_approved_edited_plan_is_the_plan_consumed_by_code(self) -> None:
        self.assertEqual(self.dispatch(), 0)
        edited_plan = "Human-edited plan: preserve the API and add regression tests.\n"
        (self.run_dir / REVIEW_FILENAME).write_text(edited_plan, encoding="utf-8")

        approval_path = approve_plan.approve(self.run_dir)
        self.assertEqual(approval_path, (self.run_dir / APPROVAL_FILENAME).resolve())
        self.assertEqual(self.dispatch(), 0)

        approval_state = json.loads(
            (self.run_dir / "02_approval.json").read_text(encoding="utf-8")
        )
        code_state = json.loads(
            (self.run_dir / "03_code.json").read_text(encoding="utf-8")
        )
        self.assertEqual(approval_state["outputs"]["approval"]["content"], edited_plan)
        self.assertEqual(code_state["outputs"]["code"]["content"], edited_plan)

        manifest = load_manifest(self.run_dir / MANIFEST_FILENAME)
        self.assertEqual(
            [attempt["node"] for attempt in manifest["attempts"]],
            ["plan", "approval", "code"],
        )
        self.assertEqual(manifest["workflow_status"], "succeeded")
        self.assertEqual(manifest["current_node"], "END")

    def test_plan_changed_after_approval_is_rejected(self) -> None:
        self.assertEqual(self.dispatch(), 0)
        approve_plan.approve(self.run_dir)
        (self.run_dir / REVIEW_FILENAME).write_text(
            "Changed after approval\n", encoding="utf-8"
        )

        with self.assertRaises(dispatcher.DispatchError) as context:
            self.dispatch()
        self.assertIn("changed after approval", str(context.exception))
        self.assertFalse((self.run_dir / "02_approval.json").exists())

    def test_approval_requires_a_paused_run_and_cannot_be_repeated(self) -> None:
        with self.assertRaises((ApprovalError, ManifestError)):
            approve_plan.approve(self.run_dir)

        self.assertEqual(self.dispatch(), 0)
        approve_plan.approve(self.run_dir)
        with self.assertRaises(ApprovalError):
            approve_plan.approve(self.run_dir)


if __name__ == "__main__":
    unittest.main()
