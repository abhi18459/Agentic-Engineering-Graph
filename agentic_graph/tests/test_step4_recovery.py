"""Tests for Step 4 manifest-backed dispatcher recovery."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

GRAPH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GRAPH_ROOT))

import dispatcher
import list_runs
from run_manifest import (
    MANIFEST_FILENAME,
    RunLock,
    RunLockedError,
    complete_attempt,
    create_manifest,
    load_manifest,
    start_attempt,
    write_manifest,
)


def state(
    *, run_id: str = "test-run", sequence: int = 0, next_node: str = "plan"
) -> dict:
    """Return a minimal schema-version-2 state accepted by the dispatcher."""
    workflow_status = "succeeded" if next_node == "END" else "running"
    current_node = "input" if sequence == 0 else ("plan" if sequence == 1 else "code")
    status = "ready" if sequence == 0 else "completed"
    return {
        "schema_version": 2,
        "run_id": run_id,
        "state_sequence": sequence,
        "task": {"title": "test"},
        "fixture_path": "../../../fixture",
        "current_node": current_node,
        "next_node": next_node,
        "status": status,
        "workflow_status": workflow_status,
        "iteration": 0,
        "max_iterations": 3,
        "test_config": {},
        "outputs": {},
        "test_attempts": [],
        "fix_attempts": [],
    }


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def completed_state(previous: dict, node: str, next_node: str) -> dict:
    result = copy.deepcopy(previous)
    result["state_sequence"] += 1
    result["current_node"] = node
    result["next_node"] = next_node
    result["status"] = "completed"
    result["workflow_status"] = "succeeded" if next_node == "END" else "running"
    result["outputs"][node] = {"content": f"completed {node}"}
    return result


class RecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.run_dir = self.root / "run-test"
        self.run_dir.mkdir()
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
next_node = "code" if node == "plan" else "END"
current = copy.deepcopy(previous)
current["state_sequence"] += 1
current["current_node"] = node
current["next_node"] = next_node
current["status"] = "completed"
current["workflow_status"] = "succeeded" if next_node == "END" else "running"
current["outputs"][node] = {"content": "fake node output"}
with open(args.output_state, "x", encoding="utf-8") as handle:
    json.dump(current, handle)
""",
            encoding="utf-8",
        )
        self.nodes = {"plan": self.fake_node, "code": self.fake_node}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_interrupted_code_run(self, *, with_output: bool = False) -> None:
        initial = state()
        plan_output = completed_state(initial, "plan", "code")
        write_json(self.run_dir / "00_input.json", initial)
        write_json(self.run_dir / "01_plan.json", plan_output)

        manifest_path = self.run_dir / MANIFEST_FILENAME
        manifest = create_manifest(
            manifest_path,
            run_id=initial["run_id"],
            initial_state_file="00_input.json",
            state_sequence=0,
            next_node="plan",
        )
        plan_attempt = start_attempt(
            manifest,
            node="plan",
            input_state="00_input.json",
            output_state="01_plan.json",
        )
        complete_attempt(
            manifest,
            attempt_id=plan_attempt,
            output_state="01_plan.json",
            state_sequence=1,
            next_node="code",
            workflow_status="running",
            exit_code=0,
        )
        start_attempt(
            manifest,
            node="code",
            input_state="01_plan.json",
            output_state="02_code.json",
        )
        write_manifest(manifest_path, manifest)

        if with_output:
            write_json(
                self.run_dir / "02_code.json",
                completed_state(plan_output, "code", "END"),
            )

    def test_new_run_records_each_success_and_terminal_checkpoint(self) -> None:
        write_json(self.run_dir / "00_input.json", state())

        with patch.dict(dispatcher.NODES, self.nodes, clear=True):
            result = dispatcher.dispatch(self.run_dir, None)

        self.assertEqual(result, 0)
        manifest = load_manifest(self.run_dir / MANIFEST_FILENAME)
        self.assertEqual(
            [entry["node"] for entry in manifest["attempts"]], ["plan", "code"]
        )
        self.assertTrue(
            all(entry["status"] == "succeeded" for entry in manifest["attempts"])
        )
        self.assertEqual(manifest["checkpoint"]["state_file"], "02_code.json")
        self.assertEqual(manifest["current_node"], "END")
        self.assertEqual(manifest["workflow_status"], "succeeded")

        missing_script = self.root / "must_not_run.py"
        with patch.dict(
            dispatcher.NODES,
            {"plan": missing_script, "code": missing_script},
            clear=True,
        ):
            resumed_result = dispatcher.dispatch(self.run_dir, None)
        self.assertEqual(resumed_result, 0)
        self.assertEqual(
            len(load_manifest(self.run_dir / MANIFEST_FILENAME)["attempts"]),
            2,
        )

    def test_interrupted_attempt_without_output_is_retried(self) -> None:
        self.create_interrupted_code_run()

        with patch.dict(dispatcher.NODES, self.nodes, clear=True):
            result = dispatcher.dispatch(self.run_dir, "plan")

        self.assertEqual(result, 0)
        manifest = load_manifest(self.run_dir / MANIFEST_FILENAME)
        self.assertEqual(
            [entry["status"] for entry in manifest["attempts"]],
            ["succeeded", "interrupted", "succeeded"],
        )
        self.assertEqual(manifest["attempts"][-1]["node"], "code")
        self.assertEqual(manifest["recoveries"][0]["action"], "retry_interrupted_node")

    def test_node_failure_is_recorded_without_advancing_checkpoint(self) -> None:
        write_json(self.run_dir / "00_input.json", state(next_node="code"))
        failing_node = self.root / "failing_node.py"
        failing_node.write_text("raise SystemExit(7)\n", encoding="utf-8")

        with (
            patch.dict(dispatcher.NODES, {"code": failing_node}, clear=True),
            self.assertRaises(dispatcher.DispatchError),
        ):
            dispatcher.dispatch(self.run_dir, None)

        manifest = load_manifest(self.run_dir / MANIFEST_FILENAME)
        self.assertEqual(manifest["attempts"][-1]["status"], "failed")
        self.assertEqual(manifest["attempts"][-1]["exit_code"], 7)
        self.assertEqual(manifest["checkpoint"]["state_file"], "00_input.json")
        self.assertEqual(manifest["current_node"], "code")
        self.assertEqual(manifest["workflow_status"], "failed")

    def test_valid_orphan_is_reconciled_without_rerunning_node(self) -> None:
        self.create_interrupted_code_run(with_output=True)
        missing_script = self.root / "must_not_run.py"

        with patch.dict(
            dispatcher.NODES,
            {"plan": missing_script, "code": missing_script},
            clear=True,
        ):
            result = dispatcher.dispatch(self.run_dir, None)

        self.assertEqual(result, 0)
        manifest = load_manifest(self.run_dir / MANIFEST_FILENAME)
        self.assertEqual(len(manifest["attempts"]), 2)
        self.assertEqual(manifest["attempts"][-1]["status"], "succeeded")
        self.assertTrue(manifest["attempts"][-1]["recovered"])
        self.assertEqual(manifest["recoveries"][-1]["action"], "reconciled_snapshot")

    def test_invalid_orphan_is_quarantined_before_retry(self) -> None:
        self.create_interrupted_code_run()
        (self.run_dir / "02_code.json").write_text('{"broken":', encoding="utf-8")

        with patch.dict(dispatcher.NODES, self.nodes, clear=True):
            result = dispatcher.dispatch(self.run_dir, None)

        self.assertEqual(result, 0)
        self.assertTrue((self.run_dir / "02_code.json").exists())
        self.assertEqual(len(list(self.run_dir.glob("02_code.json.invalid-*"))), 1)
        manifest = load_manifest(self.run_dir / MANIFEST_FILENAME)
        self.assertEqual(manifest["recoveries"][-1]["action"], "quarantined_snapshot")
        self.assertEqual(
            [entry["status"] for entry in manifest["attempts"]],
            ["succeeded", "interrupted", "succeeded"],
        )

    def test_run_lock_rejects_a_second_dispatcher(self) -> None:
        with (
            RunLock(self.run_dir),
            self.assertRaises(RunLockedError),
            RunLock(self.run_dir),
        ):
            self.fail("second lock unexpectedly acquired")

    def test_run_listing_distinguishes_live_and_stale_running_runs(self) -> None:
        initial = state()
        write_json(self.run_dir / "00_input.json", initial)
        create_manifest(
            self.run_dir / MANIFEST_FILENAME,
            run_id=initial["run_id"],
            initial_state_file="00_input.json",
            state_sequence=0,
            next_node="plan",
        )

        stale = list_runs.summarize_run(self.run_dir)
        assert stale is not None
        self.assertEqual(stale.status, "interrupted")
        with RunLock(self.run_dir):
            live = list_runs.summarize_run(self.run_dir)
        assert live is not None
        self.assertEqual(live.status, "running")


if __name__ == "__main__":
    unittest.main()
