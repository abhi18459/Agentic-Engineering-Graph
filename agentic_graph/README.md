# Agentic Engineering Graph

This directory contains the Step 4 implementation for
[Coding Challenge #134](https://codingchallenges.substack.com/p/coding-challenge-134-agentic-engineering).
It preserves the Step 3 test/fix loop and adds a per-run manifest, atomic
checkpoints, crash recovery, exclusive run locking, and a run-listing command:

```text
plan -> code -> write -> test --pass-----------------> END
                         |
                         +--repairable failure-> fix --+
                                              ^        |
                                              +--test<-+
                         |
                         +--limit/error----------------> give_up
```

Every `fix` invocation edits the same fixture working tree left by `write` and
earlier fixes. It does not reset to a baseline between iterations.

The Step 1 MVP remains in `../mvp/`. The completed Step 2 evidence remains in
`runs/run-001/` and is not modified.

## Files

```text
agentic_graph/
├── dispatcher.py
├── run_manifest.py
├── list_runs.py
├── nodes/
│   ├── common.py
│   ├── plan.py
│   ├── code.py
│   ├── write.py
│   ├── test.py
│   └── fix.py
├── prompts/
│   ├── plan.txt
│   ├── code.txt
│   ├── write.txt
│   └── fix.txt
├── tests/
│   └── test_step4_recovery.py
└── runs/
    ├── run-001/          # Completed Step 2 evidence
    ├── run-002..004/     # Completed Step 3 evidence
    └── run-005/
        └── 00_input.json # Prepared Step 4 crash-recovery run
```

Nodes remain independently executable. They read one state snapshot, do their
scoped work, and write one new snapshot. Nodes do not invoke one another.

The dispatcher owns control flow. It maps trusted node names to scripts, derives
the next immutable filename from `state_sequence`, runs the selected node,
validates the transition, records it in the run manifest, and stops at `END` or
`give_up`.

`run_manifest.py` is reusable program code beside the dispatcher. The actual
manifest data is isolated inside its own run directory as `manifest.json`; runs
never share a manifest.

## Dynamic snapshots

Fixed filenames cannot represent a loop because `test` and `fix` may execute
more than once. Step 3 therefore derives each filename dynamically:

```text
00_input.json
01_plan.json
02_code.json
03_write.json
04_test.json       # failed
05_fix.json        # first in-place repair
06_test.json       # failed
07_fix.json        # second in-place repair
08_test.json       # passed
```

Snapshots are never overwritten. Every file is standalone JSON containing the
task, configuration, current state, accumulated outputs, and complete test/fix
histories.

Snapshot publication is atomic. A node first writes and flushes a temporary file
beside the intended snapshot, then publishes the completed file under its final
name in one operation. The final snapshot name therefore never intentionally
exposes partially written JSON.

## Run manifest

The dispatcher creates `manifest.json` before the first node starts. It records:

- The last validated checkpoint state and its `next_node`.
- Every node attempt with input/output filenames and timestamps.
- Whether each attempt is running, succeeded, failed, or interrupted.
- Process exit codes and orchestration errors when available.
- Recovery decisions for missing, reconciled, or quarantined outputs.
- The run's current node and workflow status.

Manifest updates use a flushed temporary file and atomic replacement. A node is
recorded as successful only after its output snapshot passes the existing state
transition validation.

The checkpoint, rather than the highest-numbered filename, controls recovery.
On restart the dispatcher calculates the single output expected after that
checkpoint:

- No output: mark the stale running attempt interrupted and retry the node.
- Valid output from a running/interrupted attempt: record the missing success
  and advance without rerunning the node.
- Invalid output: preserve it under an `.invalid-<timestamp>` name and retry
  from the trusted checkpoint.
- Untracked output: stop rather than infer success from its filename.

Each run also has an advisory `.dispatcher.lock`. A second dispatcher cannot
operate on the same run concurrently, and the operating system releases the
lock if its owner dies. Lock files and abandoned atomic-write temporary files
are ignored by Git.

## State contract

The graph state continues to use `schema_version: 2`. Its core fields are:

- `run_id`: stable identifier copied through every snapshot.
- `state_sequence`: incremented once by every completed node.
- `task`: fixture task, requirements, and acceptance criteria.
- `fixture_path`: absolute path or a path relative to the run directory.
- `current_node`: node that produced the current snapshot.
- `next_node`: trusted executable node or terminal label.
- `status`: completion status of the most recent node.
- `workflow_status`: `running`, `succeeded`, `gave_up`, or `error`.
- `iteration`: number of completed fix attempts.
- `max_iterations`: maximum permitted fix attempts.
- `test_config`: deterministic command, timeout, environment, and exit-code policy.
- `outputs`: latest output from each node, for convenient access.
- `test_attempts`: ordered history of every test execution.
- `fix_attempts`: ordered history of every Codex repair.

`iteration` counts fixes, not tests. With `max_iterations: 3`, the graph performs
an initial test and permits at most three agentic repair attempts.

## Test-result routing

`test.py` does not invoke Codex. It executes the configured command without a
shell and captures its exit code, stdout, and stderr.

The prepared pytest configuration classifies exit codes as follows:

| Exit | Classification | Route |
|---|---|---|
| `0` | Tests passed | `END` |
| `1` | Repairable test failure | `fix`, if budget remains |
| `1` after final fix | Repair budget exhausted | `give_up` |
| Any other exit, signal, timeout, or launch failure | Test-runner error | `give_up` |

This prevents a broken interpreter or test runner from consuming agentic repair
iterations. The configured subprocess environment sets a valid macOS locale to
avoid the previously diagnosed Anaconda `readline` crash.

## Fix behavior

`fix.py` invokes Codex with `workspace-write`. Its prompt includes:

- The original task.
- The persisted plan.
- The original code proposal.
- The latest failing test result and captured output.
- Every earlier fix result.

The prompt explicitly requires Codex to inspect and repair the current fixture,
build on previous changes, avoid resets or checkouts, preserve meaningful tests,
and leave validation to the deterministic test node. After each fix, the graph
routes back to `test`.

## Before running Step 4

1. Review `runs/run-005/00_input.json`.
2. Confirm `fixture_path` points to the intended working tree.
3. Confirm the fixture is in the starting state you want the run to modify.
4. Confirm `.venv/bin/python -m pytest -q` is the intended test command.
5. Confirm Codex CLI is installed and authenticated.
6. Commit the pre-run state if you want a clean checkpoint and auditable diff.

No deliberate fixture defect is included. With the current fixture, the first
test may pass and route directly to `END`. To demonstrate the required repair
branch, use a fresh run directory and deliberately introduce a repairable defect
that the existing tests catch before starting the dispatcher.

## Run or resume the prepared workflow

From the workspace root:

```bash
python3 agentic_graph/dispatcher.py
```

The default is equivalent to:

```bash
python3 agentic_graph/dispatcher.py \
  --run-dir agentic_graph/runs/run-005
```

From inside `agentic_graph/`, this shorter command is equivalent:

```bash
python3 dispatcher.py
```

For a new run, `--start` is optional. When omitted, the dispatcher reads the
initial node from `00_input.json`. If supplied on a new run, it must match that
file's `next_node`. Once `manifest.json` exists, the checkpoint always controls
resume and `--start` is ignored so it cannot accidentally rewind the run.

Do not run nodes manually for the normal workflow. The dispatcher supplies their
input and output paths and follows their persisted routing decisions.

## Additional runs

For another independent run, create another directory such as `run-006`, copy
the prepared `00_input.json`, change `run_id`, and adjust the task or starting
fixture state as needed. A test/fix loop stays entirely within its one run
directory; the dispatcher does not create the next numbered directory.

Do not reuse a completed run directory for a different attempt. Restarting the
same manifest-backed directory means "resume this run," not "start it again."

## List runs

From the workspace root:

```bash
python3 agentic_graph/list_runs.py
```

The command reads manifests under `agentic_graph/runs` and displays each run ID,
current or next node, workflow status, and last update time. Completed legacy
Step 2/3 directories have no manifests and are intentionally omitted.

## Step 4 crash-recovery validation

Use two terminals.

In terminal 1, start the prepared run:

```bash
python3 agentic_graph/dispatcher.py \
  --run-dir agentic_graph/runs/run-005
```

In terminal 2, watch its status:

```bash
python3 agentic_graph/list_runs.py
```

After `plan` is recorded as succeeded and the listing shows `code` running,
press `Ctrl+C` in terminal 1. This controlled interruption terminates the active
node process group and records the code attempt as interrupted.

Then run the exact same dispatcher command again. It must use `01_plan.json` as
its checkpoint, retry `code`, and never rerun `plan`. After completion, the list
command must show `run-005`, `END`, and `succeeded`.

`SIGTERM` and `SIGHUP` receive the same controlled handling. `SIGKILL` cannot be
caught, so the active node inherits the run lock. An immediate replacement
dispatcher will refuse to overlap with that node. After the node exits and
releases the inherited lock, the next invocation detects the stale `running`
attempt, validates any output it left, and resumes safely.

## Prepared recoverable-failure scenario

`runs/run-003/` is preserved evidence from the required Step 3
failure-path demonstration. It starts at `test` and carries concise plan and code
context from the accepted implementation so that `fix` has the state required by
its contract.

The corresponding setup deliberately removes missing-column validation from
`fixture/starter_repo/plot_data.py` while retaining the tests that require a
descriptive `ValueError`. Its expected route is:

```text
00_input.json
  -> 01_test.json  (pytest exit 1)
  -> 02_fix.json   (repairs the current fixture in place)
  -> 03_test.json  (pytest exit 0)
  -> END
```

It is already complete and has no Step 4 manifest. Do not rerun it in place; copy
its input into a fresh directory with a new `run_id` to repeat the scenario.

## Prepared unrecoverable-failure scenario

`runs/run-004/` preserves the final Step 3 validation. Its configured
test command invokes `scenarios/unavailable_external_gate.py`, which first runs
the fixture's real pytest suite and then returns exit code `1` for a simulated
external approval service that is unavailable.

The harness lives outside the fixture and the fix node is explicitly prohibited
from modifying external files or weakening tests. Consequently, fixture edits
cannot make the injected gate pass. With `max_iterations` set to three, the
expected route is:

```text
00_input.json
  -> 01_test.json  (external gate fails)
  -> 02_fix.json   (attempt 1)
  -> 03_test.json  (external gate still fails)
  -> 04_fix.json   (attempt 2)
  -> 05_test.json  (external gate still fails)
  -> 06_fix.json   (attempt 3)
  -> 07_test.json  (external gate still fails)
  -> give_up
```

It is already complete and has no Step 4 manifest. Do not rerun it in place. The
recorded `give_up` remains the expected success condition for that deliberately
unrecoverable scenario.

## Exit codes

- `0`: workflow reached `END` and tests passed.
- `1`: dispatcher or node contract error prevented completion.
- `2`: workflow reached `give_up` because the fix limit was exhausted or the
  test runner itself failed.
- `130`: the dispatcher handled an interruption while a node was active.

## Non-agentic recovery tests

The recovery machinery can be validated without calling Codex or changing the
fixture:

```bash
python3 -m unittest discover -s agentic_graph/tests -v
```

These tests use temporary run directories and fake nodes. They cover fresh-run
manifest creation, interrupted-node retry, valid orphan reconciliation, invalid
orphan quarantine, terminal recovery, and exclusive run locking.

## Intentionally deferred

Step 4 does not implement human approval, SonarQube review, parallel candidates,
or a rendered transition timeline. Those belong to later challenge steps.
