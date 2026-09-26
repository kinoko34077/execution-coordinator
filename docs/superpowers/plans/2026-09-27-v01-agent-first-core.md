# v0.1 Agent-First Claim/Lease Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first operational execution-coordinator slice: deterministic claim/lease/fencing state plus a globally serialized GitHub Actions mutation path backed by the system-state Issue.

**Architecture:** Pure Python domain logic owns all state transitions and is independent of GitHub. A strict snapshot codec embeds current state in one long-lived GitHub Issue. A thin GitHub adapter loads/updates that Issue, while one Actions workflow serializes all authority-changing operations through a single queued concurrency group.

**Tech Stack:** Python 3.11+ standard library, `unittest`, GitHub REST API, GitHub Actions.

**Spec:** `kinoko34077/devflow@fa8691b08f233055e38c6d7d9dd435002f1509c4:docs/superpowers/specs/2026-09-27-execution-coordination-protocol-v1-design.md`

## Global Constraints

- Production runtime remains stdlib-only in v0.1.
- Durable task truth remains in devflow / owning repository Issues; runtime state is ephemeral coordination state.
- GitHub Actions owns mutation serialization through one global non-canceling queued lane.
- Current snapshot lives in `[SYSTEM] Execution Coordination State`; heartbeats do not create comments.
- Lease default is 15 minutes; active renewal target is 5 minutes.
- Worker GitHub actor identity is not treated as agent/session identity.
- Every authority-bearing continuation operation validates `(claim_id, generation)`.
- No controller dispatch or repo-monitor changes in this plan.

## Review Focus

- Same idempotency key replayed with different payload must fail closed rather than silently reuse authority.
- Naive versus timezone-aware timestamps must not produce ambiguous lease comparisons; serialized timestamps are UTC `Z` only.
- Reviewer claim compatibility must not accidentally permit a second implementer through shared conflict keys.
- Malformed/multiple snapshot markers must fail closed and leave the existing Issue body untouched.
- A GitHub API/update failure must not be reported as a successful authority transition.

---

### Task 1: Pure claim/lease state machine

**Files:**
- Create: `src/execution_coordinator/model.py`
- Create: `src/execution_coordinator/engine.py`
- Create: `src/execution_coordinator/__init__.py`
- Create: `tests/test_engine.py`

**Interfaces:**
- Produces: `CoordinatorState`, `Claim`, `MutationResult`, `claim()`, `acknowledge()`, `renew()`, `progress()`, `wait()`, `release()`, `fail()`, `expire()`, and `takeover()`.
- Time input is explicit UTC `datetime`; domain code never calls wall-clock time implicitly.

- [ ] Write failing tests for same-boundary double claim, independent claims, conflict-key collision, reviewer compatibility, renew, stale-generation rejection, expiry/takeover, heartbeat-vs-progress, wait reasons, and idempotent retry including mismatched replay payload.
- [ ] Run `python -W error -m unittest tests.test_engine -v` and confirm failures are caused by missing production modules/behavior.
- [ ] Implement the minimal dataclasses/enums and pure mutation functions required by the tests.
- [ ] Run `python -W error -m unittest tests.test_engine -v` and confirm PASS.
- [ ] Commit the task.

### Task 2: Strict system-Issue snapshot codec

**Files:**
- Create: `src/execution_coordinator/snapshot.py`
- Create: `tests/test_snapshot.py`

**Interfaces:**
- Consumes: `CoordinatorState` from Task 1.
- Produces: `parse_issue_body(body: str) -> CoordinatorState` and `render_issue_body(existing_body: str, state: CoordinatorState) -> str` using exactly one versioned marker pair.

- [ ] Write failing tests for deterministic round-trip, preserved human text, absent marker initialization, malformed JSON, unsupported schema version, duplicate markers, and UTC `Z` timestamp serialization.
- [ ] Run `python -W error -m unittest tests.test_snapshot -v` and confirm RED.
- [ ] Implement strict parse/render behavior; malformed or ambiguous existing state raises and never guesses.
- [ ] Run snapshot tests and the whole suite; confirm PASS.
- [ ] Commit the task.

### Task 3: GitHub Issue mutation adapter

**Files:**
- Create: `src/execution_coordinator/github_state.py`
- Create: `src/execution_coordinator/mutate.py`
- Create: `tests/test_github_state.py`
- Create: `tests/test_mutate.py`

**Interfaces:**
- Consumes: Task 1 mutation functions and Task 2 codec.
- Produces: REST `GitHubStateStore.load()` / `save()` and CLI `python -m execution_coordinator.mutate --operation ... --payload-json ... --idempotency-key ...`.
- Environment: `GITHUB_TOKEN`, `GITHUB_REPOSITORY`, `STATE_ISSUE_NUMBER`.

- [ ] Write failing tests using a local fake HTTP server for successful GET/PATCH, non-2xx failures, unchanged body on mutation failure, and lifecycle-comment emission only for claim/release/fail/expire/takeover.
- [ ] Run targeted tests and confirm RED.
- [ ] Implement the stdlib `urllib` adapter and mutation command with explicit error exit codes and no secret logging.
- [ ] Run targeted tests and whole suite; confirm PASS.
- [ ] Commit the task.

### Task 4: Serialized GitHub Actions mutation lane and verification CI

**Files:**
- Create: `.github/workflows/verify.yml`
- Create: `.github/workflows/mutate-state.yml`
- Create: `tests/test_workflow_contract.py`

**Interfaces:**
- `mutate-state.yml` dispatch inputs: `operation`, `payload_json`, `idempotency_key`.
- Concurrency contract: `group: execution-coordinator-state-mutation`, `queue: max`, no `cancel-in-progress: true`.

- [ ] Write workflow-contract tests before adding the mutation workflow; tests assert pinned external Action SHAs and the exact global concurrency contract.
- [ ] Push tests/verify workflow and observe a failing Actions run because `mutate-state.yml` is absent.
- [ ] Add `mutate-state.yml` with `contents: read`, `issues: write`, pinned `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1` and `actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97`, Python 3.11, and the mutation command.
- [ ] Run whole suite/compile check in Actions and confirm PASS.
- [ ] Commit the task.

### Task 5: Operational state Issue and repository Current State

**Files:**
- Modify: `project/docs/CURRENT_STATE.md`
- Modify: `project/docs/INDEX.md`
- Create: system Issue `[SYSTEM] Execution Coordination State` through GitHub after code is green.

**Interfaces:**
- System Issue contains one canonical v1 snapshot and human-readable warning that durable task truth remains elsewhere.

- [ ] Create the system Issue with an empty valid v1 snapshot.
- [ ] Exercise one bounded mutation smoke against a disposable synthetic task identifier and verify the Issue snapshot changes exactly once; then release that claim.
- [ ] Record exact run/Issue evidence in repository Issue #1.
- [ ] Update Current State with implemented behavior, limitations, verification and next action.
- [ ] Run the full suite and exact-head Actions checks.
- [ ] Open the implementation PR with Issue #1, verification evidence, rollback boundary and implementer provenance; require independent formal Review before merge.
