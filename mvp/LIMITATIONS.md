# MVP Limitations

This MVP is the intentionally fragile, one-shot implementation of the
agentic engineering graph.

## What it does

- Sends one fixed fixture task to Codex CLI.
- Invokes `codex exec` once.
- Writes the returned response to `attempt.txt`.
- Runs locally against the fixture repository.

## What it does not do

- No retry after a failed Codex invocation.
- No dispatcher.
- No workflow nodes.
- No branching based on results.
- No persisted run state.
- No run manifest or run identifier.
- No resume after interruption.
- No test execution by the workflow.
- No automatic fix loop.
- No iteration limit or `give_up` state.
- No human approval checkpoint.
- No SonarQube quality gate.
- No SonarQube MCP review.
- No parallel candidate attempts.
- No candidate comparison or winner selection.
- No execution timeline or transition history.
- No structured result schema.
- No durable record of failed invocations.
- The fixture is not modified automatically.

## Observed behavior

### Successful Codex invocation

- Codex returned a proposed implementation.
- The response was written to `attempt.txt`.
- The fixture remained unchanged.

### Failed Codex invocation

- The command returned an error.
- No useful implementation was produced.
- No retry occurred.
- No workflow state or recovery information was created.
