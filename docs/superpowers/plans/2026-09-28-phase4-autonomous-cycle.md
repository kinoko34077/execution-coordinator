# Phase 4 Agent-first autonomous claim cycle

## Goal

Implement execution-coordinator Issue #66, the explicitly released Phase 4
slice from devflow Work Order #175.

## Boundaries

- Start from accepted main `792c80bbfa92a3a53c28ba7ad153dc386cf5700e`.
- Consume one already refreshed `CapabilityMatchResult` for one worker.
- Select only the first deterministic eligible match in one cycle.
- Make at most one existing serialized claim attempt; a rejection ends the
  cycle and never falls through to another candidate.
- Require claim and acknowledge before invoking work, then use the existing
  AgentSession lifecycle to release or fence authority.
- Keep provider launch, controller negotiation, scheduling/work stealing,
  repo-monitor behavior, publication mutation and a second assignment store
  out of scope.

## TDD slices

1. Add failing tests for no-candidate, deterministic first-candidate
   selection, claim rejection/no-retry, acknowledge-before-work, lifecycle
   release, worker mismatch and key validation.
2. Add the typed cycle module and satisfy the focused tests.
3. Run the complete unittest suite and compile check.
4. Update Current State and the documentation index.
5. Open a dedicated PR, verify exact-head CI/review, merge when non-blocking,
   verify merged main and read back Issue #3 without leaving a live claim.

## Verification evidence

- focused autonomous-cycle tests;
- full `python -W error -m unittest discover -s tests -v`;
- `python -m compileall -q src tests`;
- exact-head GitHub Actions Verify and formal review;
- merged-main tests/compile and runtime Issue #3 readback.
