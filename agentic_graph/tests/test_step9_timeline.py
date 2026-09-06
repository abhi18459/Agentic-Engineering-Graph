"""Tests for Step 9 dispatcher timing and run-timeline rendering."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

GRAPH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GRAPH_ROOT))

import dispatcher
import render_timeline
from run_manifest import (
    MANIFEST_FILENAME,
    complete_attempt,
    create_manifest,
    finish_unsuccessful_attempt,
    load_manifest,
    start_attempt,
    write_manifest,
)


def initial_state() -> dict:
    """Return a minimal state accepted by the dispatcher."""
    return {
        "schema_version": 2,
        "run_id": "timeline-test",
        "state_sequence": 0,
        "task": {"title": "timeline test"},
        "fixture_path": "../../../fixture",
        "current_node": "input",
        "next_node": "plan",
        "status": "ready",
        "workflow_status": "running",
        "iteration": 0,
        "max_iterations": 3,
        "test_config": {},
        "review_config": {},
        "parallel_config": {},
        "outputs": {},
        "test_attempts": [],
        "review_attempts": [],
        "fix_attempts": [],
    }


class TimelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.run_dir = self.root / "run-test"
        self.run_dir.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_manifest(
        self, run_dir: Path | None = None, run_id: str = "run-test"
    ) -> dict:
        target = run_dir or self.run_dir
        target.mkdir(parents=True, exist_ok=True)
        return create_manifest(
            target / MANIFEST_FILENAME,
            run_id=run_id,
            initial_state_file="00_input.json",
            state_sequence=0,
            next_node="test",
        )

    def add_success(
        self,
        manifest: dict,
        *,
        node: str,
        sequence: int,
        next_node: str,
        duration: float,
    ) -> None:
        attempt_id = start_attempt(
            manifest,
            node=node,
            input_state=f"{sequence - 1:02d}_input.json",
            output_state=f"{sequence:02d}_{node}.json",
        )
        complete_attempt(
            manifest,
            attempt_id=attempt_id,
            output_state=f"{sequence:02d}_{node}.json",
            state_sequence=sequence,
            next_node=next_node,
            workflow_status="succeeded" if next_node == "END" else "running",
            exit_code=0,
            duration_seconds=duration,
        )

    def test_dispatcher_records_monotonic_duration_for_each_node(self) -> None:
        (self.run_dir / "00_input.json").write_text(
            json.dumps(initial_state()), encoding="utf-8"
        )
        fake_node = self.root / "fake_node.py"
        fake_node.write_text(
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
current["next_node"] = "code" if node == "plan" else "END"
current["status"] = "completed"
current["workflow_status"] = (
    "succeeded" if current["next_node"] == "END" else "running"
)
current["outputs"][node] = {"content": "done"}
with open(args.output_state, "x", encoding="utf-8") as handle:
    json.dump(current, handle)
""",
            encoding="utf-8",
        )

        with (
            patch.dict(
                dispatcher.NODES,
                {"plan": fake_node, "code": fake_node},
                clear=True,
            ),
            patch.object(
                dispatcher.time,
                "monotonic",
                side_effect=[10.0, 11.25, 20.0, 20.5],
            ),
        ):
            result = dispatcher.dispatch(self.run_dir, None)

        self.assertEqual(result, 0)
        manifest = load_manifest(self.run_dir / MANIFEST_FILENAME)
        self.assertEqual(
            [attempt["duration_seconds"] for attempt in manifest["attempts"]],
            [1.25, 0.5],
        )

    def test_timeline_keeps_repeated_fix_attempts_in_execution_order(self) -> None:
        manifest = self.create_manifest()
        path = [
            ("test", "fix", 0.25),
            ("fix", "test", 1.0),
            ("test", "fix", 0.5),
            ("fix", "test", 2.0),
            ("test", "END", 0.75),
        ]
        for sequence, (node, next_node, duration) in enumerate(path, start=1):
            self.add_success(
                manifest,
                node=node,
                sequence=sequence,
                next_node=next_node,
                duration=duration,
            )
        write_manifest(self.run_dir / MANIFEST_FILENAME, manifest)
        manifest_before = (self.run_dir / MANIFEST_FILENAME).read_bytes()

        rendered = render_timeline.render_run_timeline(self.run_dir)

        self.assertEqual(
            (self.run_dir / MANIFEST_FILENAME).read_bytes(), manifest_before
        )
        self.assertEqual(rendered.count("| `fix` | succeeded |"), 2)
        positions = [rendered.index(f"| {number} |") for number in range(1, 6)]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("Observed node time: **4.500 s**", rendered)

    def test_failed_attempt_and_error_are_visible(self) -> None:
        manifest = self.create_manifest()
        attempt_id = start_attempt(
            manifest,
            node="fix",
            input_state="00_input.json",
            output_state="01_fix.json",
        )
        finish_unsuccessful_attempt(
            manifest,
            attempt_id=attempt_id,
            status="failed",
            error="agent exited unexpectedly",
            exit_code=7,
            duration_seconds=3.5,
        )
        write_manifest(self.run_dir / MANIFEST_FILENAME, manifest)

        rendered = render_timeline.render_run_timeline(self.run_dir)

        self.assertIn("| `fix` | failed | 3.500 s |", rendered)
        self.assertIn("agent exited unexpectedly", rendered)

    def test_parallel_parent_includes_each_candidate_timeline(self) -> None:
        parent = self.create_manifest()
        self.add_success(
            parent,
            node="fan_out",
            sequence=1,
            next_node="join",
            duration=5.0,
        )
        write_manifest(self.run_dir / MANIFEST_FILENAME, parent)

        for candidate_number in (1, 2):
            name = f"candidate-{candidate_number:02d}"
            candidate_dir = self.run_dir / "candidates" / name
            candidate = self.create_manifest(candidate_dir, name)
            self.add_success(
                candidate,
                node="test",
                sequence=1,
                next_node="END",
                duration=float(candidate_number),
            )
            write_manifest(candidate_dir / MANIFEST_FILENAME, candidate)

        rendered = render_timeline.render_run_timeline(self.run_dir)

        self.assertIn("## Run timeline: candidate-01", rendered)
        self.assertIn("## Run timeline: candidate-02", rendered)

    def test_legacy_attempt_without_duration_uses_timestamps(self) -> None:
        manifest = self.create_manifest()
        self.add_success(
            manifest,
            node="test",
            sequence=1,
            next_node="END",
            duration=1.0,
        )
        del manifest["attempts"][0]["duration_seconds"]
        manifest["attempts"][0]["started_at"] = "2026-01-01T00:00:00+00:00"
        manifest["attempts"][0]["completed_at"] = "2026-01-01T00:00:02+00:00"
        write_manifest(self.run_dir / MANIFEST_FILENAME, manifest)

        rendered = render_timeline.render_run_timeline(self.run_dir)

        self.assertIn("2.000 s", rendered)
        self.assertNotIn("unknown", rendered)


if __name__ == "__main__":
    unittest.main()
