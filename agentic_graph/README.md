# Agentic Engineering Graph

This directory contains the Step 7 implementation for
[Coding Challenge #134](https://codingchallenges.substack.com/p/coding-challenge-134-agentic-engineering).
It preserves the test/fix loop, crash recovery, and durable human plan approval,
and supports both deterministic and MCP-backed SonarQube review gates:

```text
plan -> approval pause -> code -> write -> test --pass-> review --pass-> END
                                     |                 |
                                     |                 +--failed gate--+
                                     +--failed test--------------------> fix
                                                                        |
                                                       review <- test <-+

test/review --limit or infrastructure error-----------------------> give_up
```

Every `fix` invocation edits the same fixture working tree left by `write` and
earlier fixes. It does not reset to a baseline between iterations.

The Step 1 MVP remains in `../mvp/`. The completed Step 2 evidence remains in
`runs/run-001/` and is not modified.

## Files

```text
agentic_graph/
├── dispatcher.py
├── approval.py
├── approve_plan.py
├── run_manifest.py
├── list_runs.py
├── verify_review_repeatability.py
├── nodes/
│   ├── common.py
│   ├── plan.py
│   ├── code.py
│   ├── write.py
│   ├── test.py
│   ├── review.py
│   ├── sonar_client.py
│   ├── mcp_review_client.py
│   └── fix.py
├── prompts/
│   ├── plan.txt
│   ├── code.txt
│   ├── write.txt
│   ├── review_mcp.txt
│   └── fix.txt
├── schemas/
│   └── review_mcp_output.schema.json
├── tests/
│   ├── test_step4_recovery.py
│   ├── test_step5_approval.py
│   ├── test_step6_review.py
│   └── test_step7_mcp_review.py
└── runs/
    ├── run-001/          # Completed Step 2 evidence
    ├── run-002..004/     # Completed Step 3 evidence
    ├── run-005/          # Completed Step 4 evidence
    ├── run-006..007/     # Completed Step 5 evidence
    ├── run-008/          # Completed Step 6 failed-gate/fix evidence
    ├── run-009/          # Completed Step 6 repeatability evidence
    └── run-010/          # Prepared Step 7 MCP review validation
```

Nodes remain independently executable. They read one state snapshot, do their
scoped work, and write one new snapshot. Nodes do not invoke one another.

The dispatcher owns control flow. It maps trusted node names to scripts, derives
the next immutable filename from `state_sequence`, runs the selected node,
validates the transition, records it in the run manifest, pauses at `approval`,
and stops at `END` or `give_up`. A run reaches `END` only after both tests and
SonarQube review pass.

`run_manifest.py` is reusable program code beside the dispatcher. The actual
manifest data is isolated inside its own run directory as `manifest.json`; runs
never share a manifest.

## Dynamic snapshots

Fixed filenames cannot represent a loop because `test` and `fix` may execute
more than once. Step 3 therefore derives each filename dynamically:

```text
00_input.json
01_test.json        # passed
02_review.json      # failed quality gate
03_fix.json         # first in-place repair
04_test.json        # passed
05_review.json      # passed quality gate
```

Snapshots are never overwritten. Every file is standalone JSON containing the
task, configuration, current state, accumulated outputs, and complete test,
review, and fix histories.

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
- `next_node`: trusted executable node, approval control point, or terminal label.
- `status`: completion status of the most recent node.
- `workflow_status`: `running`, `awaiting_approval`, `succeeded`, `gave_up`,
  or `error`.
- `iteration`: number of completed fix attempts.
- `max_iterations`: maximum permitted fix attempts.
- `test_config`: deterministic command, timeout, environment, and exit-code policy.
- `review_config`: selected backend, scanner command, and bounded scanner,
  quality-gate, API, and MCP-agent timeouts.
- `outputs`: latest output from each node, for convenient access.
- `test_attempts`: ordered history of every test execution.
- `review_attempts`: ordered history of every SonarQube review.
- `fix_attempts`: ordered history of every Codex repair.

`iteration` counts fixes, not tests. With `max_iterations: 3`, the graph performs
an initial test and permits at most three agentic repair attempts.

## Test-result routing

`test.py` does not invoke Codex. It executes the configured command without a
shell and captures its exit code, stdout, and stderr.

The prepared pytest configuration classifies exit codes as follows:

| Exit | Classification | Route |
|---|---|---|
| `0` | Tests passed | `review` |
| `1` | Repairable test failure | `fix`, if budget remains |
| `1` after final fix | Repair budget exhausted | `give_up` |
| Any other exit, signal, timeout, or launch failure | Test-runner error | `give_up` |

This prevents a broken interpreter or test runner from consuming agentic repair
iterations. The configured subprocess environment sets a valid macOS locale to
avoid the previously diagnosed Anaconda `readline` crash.

## Fix behavior

`fix.py` invokes Codex with `workspace-write`. Its prompt includes:

- The original task.
- The human-approved plan.
- The original code proposal.
- The latest failing test result or failed Sonar review, including findings.
- Every earlier fix result.

The prompt explicitly requires Codex to inspect and repair the current fixture,
build on previous changes, avoid resets or checkouts, preserve meaningful tests,
and leave validation to the deterministic test and review nodes. It also forbids
weakening Sonar configuration or adding suppressions merely to pass the gate.
After each fix, the graph routes back to `test`, followed by `review` when tests
pass.

## Deterministic SonarQube review

`review.py` runs SonarScanner without a shell and forces these settings even if
the properties file changes:

```text
sonar.qualitygate.wait=true
sonar.qualitygate.timeout=300
```

The five-minute quality-gate timeout is Sonar's documented default. It is ample
for the small fixture and a normal SonarQube Cloud compute queue while remaining
bounded. The outer 360-second process timeout provides one additional minute for
scanner startup, local analysis, and report upload. Each Web API request has a
30-second network timeout.

The scanner token is read only from `SONAR_TOKEN`. It is never accepted inside
`review_config`, passed on the command line, or written to state. The node reads
the scanner's temporary `report-task.txt`, looks up that exact analysis, and
records:

- Quality-gate status and conditions.
- Unresolved issues and security hotspots.
- Stable normalized findings for repair and comparison.
- Scanner diagnostics and volatile analysis identifiers outside the stable
  comparison payload.

A failed gate is a successful review-node execution whose state routes to
`fix`. Missing credentials, scanner launch failures, timeouts, failed compute
tasks, or unusable API responses route to `give_up` as infrastructure errors.

The `deterministic_result` object deliberately excludes timestamps, durations,
analysis/task IDs, URLs, and raw logs. Findings are normalized and sorted by
their stable content.

## MCP-backed SonarQube review

`review_config.backend` selects the review implementation. Existing runs omit
the field and continue to use `sonar_cli`; Step 7 uses `sonarqube_mcp`. The
dispatcher still executes the same logical `review` node and validates the same
state transition either way.

The MCP path first runs SonarScanner with the same bounded quality-gate wait so
SonarQube contains a current analysis. It then invokes `codex exec`
non-interactively, ephemerally, and in a read-only sandbox. Codex receives a
strict output schema and may query only these tools on the project-scoped
`sonarqube` MCP server:

- `get_project_quality_gate_status`
- `search_sonar_issues_in_projects`
- `search_security_hotspots`

The committed `.codex/config.toml` starts the official SonarQube MCP container
with read-only mode enabled and forwards `SONARQUBE_TOKEN` and `SONARQUBE_ORG`
from the invoking shell. It contains no credentials. User-level Codex
configuration and authentication remain available, and trusted repository-local
configuration supplies the scoped MCP server for this invocation.

The node captures Codex's JSONL event stream, rejects shell, file-change, and
web-search events, and proves all three required MCP calls completed for the
scanner-reported project key. A model assertion that it used MCP is not accepted
as evidence. The final response must also pass the committed JSON Schema and
local consistency validation before it can affect routing.

Successful MCP attempts retain the Step 6 decision fields and normalized
findings. Backend-specific provenance is stored under `review_backend`,
`agent_cli`, `mcp_server`, and `mcp_tool_calls`. Scanner, Codex, MCP,
provenance, or structured-output failures route to `give_up` as infrastructure
errors rather than consuming a repair iteration.

## Human plan approval

After `plan.py` writes the immutable `01_plan.json`, the dispatcher creates
`plan_review.md`, marks the manifest `awaiting_approval`, and exits successfully.
It does not run `code.py` while approval is absent.

`plan_review.md` may be edited zero, one, or many times. The original plan in
`01_plan.json` remains unchanged so crash-recovery evidence stays immutable.
When the plan is ready, `approve_plan.py` writes `approval.json` atomically. The
approval includes a SHA-256 digest of the exact reviewed contents. Editing the
plan after approval makes the signal invalid and prevents execution.

On the next dispatcher invocation, the approval control point writes
`02_approval.json`. This immutable snapshot carries the exact approved plan to
`code`, `write`, and `fix`. The approval transition is recorded in the manifest
like other completed transitions and participates in the same orphan-snapshot
recovery behavior.

## Before running Step 6

1. Review `runs/run-008/00_input.json` and `runs/run-009/00_input.json`.
2. Confirm `fixture_path` points to the intended iterative fixture working tree.
3. Confirm Codex CLI is installed and authenticated for the repair node.
4. Confirm `sonar-scanner` is installed and the Sonar project in
   `../fixture/sonar-project.properties` is accessible.
5. Make `SONAR_TOKEN` available in the shell without printing or committing it.
6. Confirm the active quality profile enables the reliability rule for identical
   expressions on both sides of a binary comparison.
7. Commit the pre-run state if you want an auditable checkpoint.

The fixture currently contains an intentionally incorrect comparison of
`len(x_data)` with itself inside `create_plot()`. Existing tests still pass, but
Sonar should report the duplicated comparison as a reliability issue. `run-008`
begins at `test`, then the failed review asks `fix` to compare `x_data` with
`y_data` and add regression coverage for the error branch.

The test command uses `pytest-cov` to create `fixture/coverage.xml`, and
`sonar.python.coverage.reportPaths=coverage.xml` imports it. The report and
coverage database are ignored by Git; the quality gate is not bypassed.

## Verify repeatability before the repair

While the deliberate finding is still present, run the real review node twice
against identical input and source:

```bash
python3 agentic_graph/verify_review_repeatability.py \
  --input-state agentic_graph/runs/run-009/00_input.json \
  --output agentic_graph/runs/run-009/repeatability_result_v2.json
```

The command exits `0` only when both `deterministic_result` objects are exactly
equal. It writes both results and `matches: true` to the requested report. It
does not invoke `fix` or modify fixture source code. The output path is exclusive;
remove or rename an existing report before intentionally repeating this check.

## Run or resume the failed-gate/fix validation

From the workspace root:

```bash
python3 agentic_graph/dispatcher.py \
  --run-dir agentic_graph/runs/run-008 \
  --start test
```

Because `run-008/00_input.json` already names `test`, `--start test` is explicit
but optional. Keep `--run-dir` when referring to this historical Step 6 run;
the dispatcher's current default is the Step 7 `run-010` setup.

The expected route is:

```text
00_input.json
-> 01_test.json       (passed)
-> 02_review.json     (failed gate with Sonar findings)
-> 03_fix.json        (corrects the comparison and adds regression coverage)
-> 04_test.json       (passed)
-> 05_review.json     (passed gate)
-> END
```

If interrupted, rerun the same dispatcher command. Once `manifest.json` exists,
its checkpoint controls resume and `--start` is ignored. Do not run individual
nodes for the normal workflow.

## Run the Step 7 MCP-backed validation

The prepared `run-010` setup reintroduces the same duplicated-comparison issue
used for Step 6 and removes the regression that would otherwise stop execution
at the test node. The initial tests should therefore pass while SonarQube still
detects the reliability issue.

In the same terminal that starts the dispatcher, provide scanner and MCP
credentials without printing them:

```bash
export SONARQUBE_TOKEN="$SONAR_TOKEN"
export SONARQUBE_ORG="abhi18459"
```

Docker Desktop must be running, and Codex CLI must already be authenticated.
Then run from the workspace root:

```bash
python3 agentic_graph/dispatcher.py \
  --run-dir agentic_graph/runs/run-010 \
  --start test
```

Because `run-010` is the default and its input names `test`, a pristine run is
also equivalent to `python3 agentic_graph/dispatcher.py`. The explicit form is
preferred for auditable validation.

The expected route is:

```text
00_input.json
-> 01_test.json       (passed)
-> 02_review.json     (MCP calls recorded; quality gate failed)
-> 03_fix.json        (comparison and regression coverage repaired)
-> 04_test.json       (passed)
-> 05_review.json     (MCP calls recorded; quality gate passed)
-> END
```

Inspect both review snapshots after completion. Each `mcp_tool_calls` array must
show successful calls to the quality-gate, issue-search, and hotspot-search
tools for `agentic-engineering-fixture`. The first decision should match the
recorded failed Step 6 gate, and the final decision should be `passed`.

## Additional runs

For another independent run, create another numbered directory, copy
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

While Step 5 is paused, the listing shows `approval` and `awaiting_approval`.

## Step 5 validation

Start `run-006` and confirm the first invocation creates `01_plan.json` and
`plan_review.md`, then exits without a code snapshot. Make a recognizable edit
to the review file and run the approval command. Restart the dispatcher and
confirm `02_approval.json` contains the edited text and the manifest continues
with `code` rather than rerunning `plan`.

For the durable-pause check, invoke the dispatcher again before approving. It
must remain at the same checkpoint with exactly one successful plan attempt.
The run-listing command must continue to show `awaiting_approval`. This
demonstrates that no dispatcher process or in-memory state is required while a
human is away.

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

- `0`: workflow paused safely for approval, or reached `END` with passing tests
  and a passing quality gate.
- `1`: dispatcher or node contract error prevented completion.
- `2`: workflow reached `give_up` because the fix limit was exhausted or a
  test/review dependency failed.
- `130`: the dispatcher handled an interruption while a node was active.

## Non-agentic recovery tests

The recovery machinery can be validated without calling Codex or changing the
fixture:

```bash
python3 -m unittest discover -s agentic_graph/tests -v
```

These tests use temporary run directories, fake nodes, and static Sonar payloads.
They cover fresh-run manifest creation, interrupted-node retry, orphan
reconciliation and quarantine, exclusive locking, durable approval pauses,
edited-plan propagation, stale approval rejection, review routing, stable
normalization, MCP provenance validation, structured review output, and
review-triggered repair selection. They do not contact Sonar or Docker.

## Intentionally deferred

Step 7 does not implement conversational plan revision, parallel candidates, or
a rendered transition timeline. Those enhancements belong after the required
challenge steps or in their later designated steps.
