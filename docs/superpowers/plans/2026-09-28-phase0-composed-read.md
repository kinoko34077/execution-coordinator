# Phase 0 Composed Read Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one read-only execution-coordinator entry point that composes trusted durable candidate discovery, the validated runtime snapshot, and existing claimability projections.

**Architecture:** Create `execution_coordinator.frontier` as a thin orchestration module. It will call the existing `discover_claim_candidates`, `get_state_result`, `project_claimability`, and `list_claimable` functions without duplicating authority or eligibility rules. The returned immutable result keeps discovery failures, state freshness, reason projections, and the exact claimable tuple together.

**Tech Stack:** Python 3.11+ standard library, `unittest`, existing GitHub Issue reader/state reader protocols.

**Spec:** devflow Work Order #175, Phase 0; owning Issue `kinoko34077/execution-coordinator#58`.

## Global Constraints

- Start from accepted `56e1a958f166d867b8f2b967c3a4e86da3ff2160`.
- Durable task truth remains in devflow / owning repository Issues and PRs.
- The composition is read-only and must not mutate runtime Issue #3 or any external resource.
- Candidate sources remain explicit trusted `DurableIssueSource` values; no repository enumeration is added.
- No ranking, capability matching, scheduler, auto-claim, provider, controller, repo-monitor, credential, permission, release, deploy, or history-rewrite work.
- Existing fail-closed discovery, state parsing, conflict, role, lease, and recovery semantics remain authoritative.

## Review Focus

- A malformed or untrusted Control still yields discovery failure evidence and never a claimable candidate.
- A malformed or transport-failed runtime state read raises without exposing a partial claimable result.
- State freshness metadata and `worker_id` are preserved through the composition.
- Claimability reason projections and `list_claimable` remain consistent for live and expired-unswept blockers.
- No reader or input state is mutated by the new entry point.

---

### Task 1: Typed composed read surface

**Files:**
- Create: `src/execution_coordinator/frontier.py`
- Test: `tests/test_composed_read.py`

**Interfaces:**
- Consumes: `DurableIssueSource`, `IssueReader`, `DiscoveryResult`, `StateReader`, `StateReadResult`, `ClaimCandidate`, `ClaimabilityProjection`, `CoordinatorState`, and existing query functions.
- Produces: `ComposedReadResult` and `compose_claimability_read(sources, *, issue_reader, state_reader, now, worker_id=None)`.

- [ ] Write focused tests for a successful ordered discovery -> state -> claimability read, metadata preservation, discovery failure evidence, state-read failure, blocker reasons, and no-write/no-input-mutation behavior.
- [ ] Run the focused tests and confirm they fail because the new module/entry point does not exist.
- [ ] Implement the smallest orchestration dataclass/function, calling the existing primitives only; preserve discovery failures and let invalid state reads fail closed.
- [ ] Run focused tests and confirm PASS, then run the full suite and compile check.
- [ ] Commit the implementation and tests.

### Task 2: Accepted-state documentation

**Files:**
- Modify: `project/docs/CURRENT_STATE.md`
- Modify: `project/docs/INDEX.md`

**Interfaces:**
- Consumes: the accepted Phase 0 entry point from Task 1 and its verification evidence.
- Produces: a current-state projection that no longer lists the composed read as an unimplemented limitation and links the owning Issue.

- [ ] Update the accepted read-only projections and known-limitations/next-action text without claiming Phase 1 or autonomous mutation.
- [ ] Run the full suite and compile check after documentation changes.
- [ ] Commit the documentation reconciliation.

### Final verification

- [ ] Verify the branch diff against the exact accepted base and run the supported Python test command.
- [ ] Re-read the Issue/PR head and record exact-head CI and formal review before merge.
- [ ] After merge, run a bounded read-only smoke, update `CURRENT_STATE`, and reconcile devflow Control #107 while leaving Issue #3 unchanged.
