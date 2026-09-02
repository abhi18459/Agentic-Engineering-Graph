# Agentic Engineering Graph

This directory contains the Step 2 implementation for
[Coding Challenge #134](https://codingchallenges.substack.com/p/coding-challenge-134-agentic-engineering).
It replaces the one-shot MVP with a file-backed, three-node graph:

```text
plan -> code -> write -> END
```

The Step 1 implementation remains unchanged in `../mvp/`.

## What is supplied

```text
agentic_graph/
├── dispatcher.py
├── nodes/
│   ├── common.py
│   ├── plan.py
│   ├── code.py
│   └── write.py
├── prompts/
│   ├── plan.txt
│   ├── code.txt
│   └── write.txt
└── runs/
    └── run-001/
        └── 00_input.json
```

Each node is independently executable. It reads one state snapshot from a run
directory, invokes `codex exec` once with a narrowly scoped prompt and sandbox,
writes the next state snapshot, and exits. Nodes never launch one another.

The dispatcher owns control flow. It maps trusted node names to scripts, runs
the requested node, reads `next_node` from the state that was produced, and
continues until it sees `END`.

## State contract

Every state file is standalone JSON and carries the complete task and all
outputs produced so far. The core fields are:

- `schema_version`: state format version; currently `1`.
- `run_id`: stable identifier copied through every state snapshot.
- `state_sequence`: zero-based sequence incremented by each node.
- `task`: fixture task, requirements, and acceptance criteria.
- `fixture_path`: absolute path, or a path relative to the run directory.
- `current_node`: node that produced the current snapshot.
- `next_node`: trusted logical node name for the dispatcher, or `END`.
- `status`: `ready` for the initial input and `completed` after a node succeeds.
- `outputs`: accumulated, human-readable node results.

The supplied run uses these snapshots:

| Node | Reads | Writes | Sandbox | Next node |
|---|---|---|---|---|
| `plan` | `00_input.json` | `01_plan.json` | `read-only` | `code` |
| `code` | `01_plan.json` | `02_code.json` | `read-only` | `write` |
| `write` | `02_code.json` | `03_write.json` | `workspace-write` | `END` |

`next_node` is set by the Python node wrapper, not by the language model.

## Before starting a run

1. Review `runs/run-001/00_input.json` and adjust the task if desired.
2. Confirm its `fixture_path` resolves to the intended fixture repository.
3. Confirm the fixture working tree is in the state you want the write node to
   modify.
4. Confirm the Codex CLI is installed and authenticated.
5. Use a fresh run directory. The nodes refuse to overwrite existing state
   snapshots.

The supplied input resolves `../../../fixture` relative to
`runs/run-001/`, which points to this workspace's fixture repository.

## Starting the prepared run

From the workspace root, the defaults are sufficient:

```bash
python3 agentic_graph/dispatcher.py
```

The explicit equivalent is:

```bash
python3 agentic_graph/dispatcher.py \
  --run-dir agentic_graph/runs/run-001 \
  --start plan
```

Do not run the three nodes separately for the normal end-to-end path; the
dispatcher does that. Their individual command-line interfaces exist for
inspection and debugging.

## Scope of Step 2

This implementation intentionally has no test node, fix loop, retry policy,
manifest, resume behavior, approval checkpoint, SonarQube review, parallel
candidates, or transition timeline. Those capabilities belong to later steps.
