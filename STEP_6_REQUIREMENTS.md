# Step 6: Deterministic SonarQube Quality Gate

## Goal

Step 6 adds a deterministic SonarQube quality gate after the test node. Unlike
the planning, coding, and fixing nodes, the review decision is produced by
SonarQube rules rather than an AI agent. Unchanged source and unchanged Sonar
configuration must therefore produce the same normalized result.

The challenge requires:

- A `review` node that runs `sonar-scanner`.
- `sonar.qualitygate.wait=true`.
- A suitable `sonar.qualitygate.timeout`.
- Quality-gate status and findings persisted in graph state.
- Failed reviews routed to `fix` with the Sonar findings attached.
- A repeatability check showing identical findings for identical input.

Source: [Coding Challenge #134, Step 6](https://codingchallenges.substack.com/p/coding-challenge-134-agentic-engineering)

Sonar references:

- [SonarScanner CLI](https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/scanners/sonarscanner-cli)
- [Analysis parameters, including quality-gate waiting and timeout](https://docs.sonarsource.com/sonarqube-server/analyzing-source-code/analysis-parameters/parameters-not-settable-in-ui)
- [SonarQube Cloud Web API](https://docs.sonarsource.com/sonarqube-cloud/appendices/web-api)

## Updated graph

```text
plan
  |
approval
  |
code
  |
write
  |
test -------- failed ----------> fix
  |                                |
  | passed                         |
  v                                |
review ------ failed gate --------+
  |
  | passed
  v
 END
```

After every fix, tests run again. Sonar review runs only after the tests pass.
The workflow reaches `END` only when both tests and the quality gate pass.

## Developer requirements

### SonarQube prerequisites

- SonarQube Cloud or a self-managed SonarQube instance must be available.
- `sonar-scanner` must be installed.
- The fixture must have a Sonar project key and scanner configuration.
- Authentication must be supplied securely through `SONAR_TOKEN`, never in a
  committed file or persisted graph state.
- The active quality profile and quality gate must contain a rule that the
  validation scenario can reliably fail.

### Review node

The review node must:

1. Read its input state and resolve the fixture directory.
2. Run `sonar-scanner` without a shell.
3. Force `sonar.qualitygate.wait=true` and the configured timeout.
4. Read the scanner metadata and Sonar Web API result.
5. Persist the gate status, gate conditions, normalized findings, scanner exit
   code, and diagnostic output.
6. Route a passed gate to `END`.
7. Route a failed gate to `fix` while repair budget remains.
8. Route an exhausted repair budget or scanner/infrastructure error to
   `give_up`.

Scanner failure must be distinguished from a valid failed gate. Source changes
can repair a gate violation, but they cannot repair a missing credential,
unreachable server, or failed analysis task.

### Review state

State must contain an ordered `review_attempts` history. Each completed review
records enough stable information for the dispatcher, fix agent, and a human to
understand the decision. Useful finding fields include:

- Finding kind (`issue` or `security_hotspot`)
- Rule identifier
- Issue type or software-quality impact
- Severity
- File and line
- Message
- Current status

Quality-gate conditions must also be retained because a coverage or duplication
failure may not correspond to a single source issue.

### Deterministic comparison

Analysis IDs, task IDs, timestamps, URLs, processing durations, and raw scanner
logs may legitimately differ between scans. They must remain outside the stable
comparison payload.

The deterministic payload contains only:

- Decision
- Quality-gate status
- Normalized, sorted gate conditions
- Normalized, sorted findings

Two reviews of unchanged code and configuration pass the repeatability check
when these payloads are equal.

### Dispatcher changes

- Add `review` to the trusted node map.
- Route a passing `test` to `review`, not directly to `END`.
- Accept and validate `review_attempts` as append-only history.
- Accept `review -> END`, `review -> fix`, and `review -> give_up` outcomes.
- Preserve manifest-backed crash recovery for review snapshots.

### Fix changes

The fix node must accept either a failing test or a failed Sonar gate as its
repair trigger. For a review failure, the fix prompt receives the gate status,
failed conditions, and normalized findings. The prompt must prohibit weakening
tests or Sonar configuration merely to obtain a passing result.

Every `fix` invocation consumes one existing repair iteration, regardless of
whether tests or Sonar triggered it.

## Required validation

### Failure and repair

Introduce a safe, deliberately insecure or overly complex fixture construct
that the active Sonar profile reliably catches while tests still pass. The
expected route is:

```text
test passed
-> review failed
-> fix
-> test passed
-> review passed
-> END
```

Merely creating a Sonar issue is insufficient: the configured quality gate must
actually fail because of it.

### Repeatability

Run the review node twice against identical source, input state, and Sonar
configuration. Confirm that both deterministic payloads are identical. Complete
snapshots need not be byte-for-byte identical because diagnostic metadata may
vary.

## Scope boundary

The review node remains deterministic in Step 6. Step 7 will replace its Sonar
CLI/Web API implementation with an AI agent using the SonarQube MCP server while
preserving the same state shape and dispatcher decisions.
