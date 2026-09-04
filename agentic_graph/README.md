# Agentic Engineering Graph

This directory contains the Step 3 implementation for
[Coding Challenge #134](https://codingchallenges.substack.com/p/coding-challenge-134-agentic-engineering).
It extends the Step 2 straight line with deterministic testing and a bounded,
self-correcting repair loop:

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
└── runs/
    ├── run-001/          # Completed Step 2 run (schema version 1)
    └── run-002/
        └── 00_input.json # Prepared Step 3 run (schema version 2)
```

Nodes remain independently executable. They read one state snapshot, do their
scoped work, and write one new snapshot. Nodes do not invoke one another.

The dispatcher owns control flow. It maps trusted node names to scripts, derives
the next immutable filename from `state_sequence`, runs the selected node,
validates the transition, and stops at `END` or `give_up`.

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

## State contract

Step 3 uses `schema_version: 2`. Its core fields are:

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

## Before running

1. Review `runs/run-002/00_input.json`.
2. Confirm `fixture_path` points to the intended working tree.
3. Confirm the fixture is in the starting state you want the run to modify.
4. Confirm `.venv/bin/python -m pytest -q` is the intended test command.
5. Confirm Codex CLI is installed and authenticated.
6. Commit the pre-run state if you want a clean checkpoint and auditable diff.

No deliberate fixture defect is included. With the current fixture, the first
test may pass and route directly to `END`. To demonstrate the required repair
branch, use a fresh run directory and deliberately introduce a repairable defect
that the existing tests catch before starting the dispatcher.

## Run the prepared workflow

From the workspace root:

```bash
python3 agentic_graph/dispatcher.py
```

The default is equivalent to:

```bash
python3 agentic_graph/dispatcher.py \
  --run-dir agentic_graph/runs/run-002 \
  --start plan
```

From inside `agentic_graph/`, this shorter command is equivalent:

```bash
python3 dispatcher.py
```

`--start` is optional. When omitted, the dispatcher reads the initial node from
`00_input.json`. If supplied, it must match that file's `next_node`; this prevents
silently skipping required state.

Do not run nodes manually for the normal workflow. The dispatcher supplies their
input and output paths and follows their persisted routing decisions.

## Additional runs

For another independent run, create another directory such as `run-003`, copy
the prepared `00_input.json`, change `run_id`, and adjust the task or starting
fixture state as needed. A test/fix loop stays entirely within its one run
directory; it does not create `run-003` automatically.

The dispatcher refuses to overwrite snapshots. Step 4 will add manifest-backed
resume behavior for interrupted runs.

## Prepared recoverable-failure scenario

`runs/run-003/00_input.json` is a fault-injection run for the required Step 3
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

Run it from the workspace root with:

```bash
python3 agentic_graph/dispatcher.py \
  --run-dir agentic_graph/runs/run-003 \
  --start test
```

The exact number of fix/test cycles can be greater than one if the first repair
is incomplete, but it cannot exceed the configured three-fix limit.

## Prepared unrecoverable-failure scenario

`runs/run-004/00_input.json` supplies the final Step 3 validation. Its configured
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

Run it from the workspace root with:

```bash
python3 agentic_graph/dispatcher.py \
  --run-dir agentic_graph/runs/run-004 \
  --start test
```

The dispatcher returns exit code `2` when this expected `give_up` terminal is
reached. That nonzero exit is the success condition for this fault-injection
scenario, not an orchestration defect.

## Exit codes

- `0`: workflow reached `END` and tests passed.
- `1`: dispatcher or node contract error prevented completion.
- `2`: workflow reached `give_up` because the fix limit was exhausted or the
  test runner itself failed.

## Intentionally deferred

Step 3 does not implement manifest-based crash recovery, human approval,
SonarQube review, parallel candidates, or a transition timeline. Those belong to
later challenge steps.
