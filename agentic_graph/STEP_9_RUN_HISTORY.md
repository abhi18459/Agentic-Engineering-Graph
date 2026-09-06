# Step 9: Visible Run History

Step 9 of
[Coding Challenge #134](https://codingchallenges.substack.com/p/coding-challenge-134-agentic-engineering)
adds observability to the graph. It must show what happened during a run, in
what order, and how long every node attempt took. It does not change how the
graph plans, edits, tests, reviews, repairs, or selects candidates.

## Challenge requirements

For every node attempt, record:

- The node name.
- Its start timestamp.
- Its completion timestamp.
- Its duration.
- Its outcome and next-node transition.

Provide a simple command that renders those records as a chronological
timeline. Repeated nodes must remain separate entries so a path such as
`test -> fix -> test -> fix -> test` is visible exactly as it occurred.

Validate the implementation with an end-to-end run that enters the fix loop at
least once. Every executed node must appear in order with a plausible duration,
including every repeated `fix` or `test` attempt.

## Implementation in this repository

The existing per-run `manifest.json` remains the source of truth. It already
records ordered node attempts, timestamps, statuses, transitions, errors, and
recovery decisions. Step 9 extends each newly completed attempt with
`duration_seconds` instead of introducing a second event log that could disagree
with the manifest.

The dispatcher measures ordinary attempts with Python's monotonic clock. This
keeps elapsed time reliable even if the system wall clock changes while a node
is running. Wall-clock UTC timestamps are still recorded for human correlation,
now with microsecond precision.

Recovery is a special case because a monotonic timer cannot survive the original
dispatcher process. A recovered attempt receives a non-negative wall-clock
duration derived from its persisted timestamps and remains marked `recovered`.
Historical manifests from Steps 4 through 8 remain valid even though they do not
contain the new duration field; the renderer derives their durations from their
timestamps when possible.

`render_timeline.py` reads manifests without changing them and emits Markdown.
It preserves manifest `attempt_id` order, includes failures and interruption
details, and never deduplicates repeated node names. For a Step 8 parent run, it
also renders candidate manifests under `candidates/` in deterministic order.

## Timeline command

Print a timeline without changing any files:

```bash
python3 agentic_graph/render_timeline.py \
  --run-dir agentic_graph/runs/run-012
```

Save the same report when durable evidence is wanted:

```bash
python3 agentic_graph/render_timeline.py \
  --run-dir agentic_graph/runs/run-012 \
  --output agentic_graph/runs/run-012/timeline.md
```

For a parallel parent, candidate timelines are included by default. Pass
`--no-candidates` to show only the parent `fan_out -> join` history.

## Expected shape

```text
# Run timeline: run-012

| # | Started (UTC) | Completed (UTC) | Node | Status | Duration | Next node |
| 1 | ... | ... | test | succeeded | 2.700 s | fix |
| 2 | ... | ... | fix | succeeded | 38.900 s | test |
| 3 | ... | ... | test | succeeded | 2.900 s | review |
| 4 | ... | ... | review | succeeded | 67.300 s | END |
```

The renderer also reports the sum of known node durations. That is observed
node execution time, not necessarily total wall time: a human approval pause or
time between separate dispatcher invocations is deliberately not attributed to
a node.

## Automated tests

Run only the Step 9 tests from the workspace root:

```bash
python3 -m unittest discover \
  -s agentic_graph/tests \
  -p 'test_step9_timeline.py' \
  -v
```

The tests use temporary directories and fake nodes. They do not call Codex,
SonarQube, Docker, or the real fixture. They cover monotonic dispatcher timing,
repeated fix-loop entries, failed-attempt visibility, legacy manifests, and
nested parallel-candidate timelines.

After that focused check, run the complete non-agentic suite:

```bash
python3 -m unittest discover -s agentic_graph/tests -v
```

## Manual acceptance check

The prepared `run-012` starts at `test` with direct plotting deliberately
validating only `x_data` for non-finite values. Existing `y_data` tests therefore
fail and route to `fix`, which has seeded approved context describing the narrow
repair. Commit that pre-run state, make the SonarQube MCP environment available,
and run:

```bash
python3 agentic_graph/dispatcher.py \
  --run-dir agentic_graph/runs/run-012 \
  --start test
```

The expected path is `test -> fix -> test -> review -> END`. After it completes,
persist the timeline:

```bash
python3 agentic_graph/render_timeline.py \
  --run-dir agentic_graph/runs/run-012 \
  --output agentic_graph/runs/run-012/timeline.md
```

Verify:

- All attempts appear in `attempt_id` order.
- Repeated node names appear as separate rows.
- Every new completed attempt has a non-negative duration.
- Failed or interrupted attempts are retained.
- The last transition and displayed workflow status agree with the manifest.
- Rendering without `--output` does not create or modify run files.

This is the final required challenge step. Conversational plan revision, token
and cost accounting, pull-request creation, and diagram rendering remain
optional follow-up enhancements.
