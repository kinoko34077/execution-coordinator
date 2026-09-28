# Phase 3 capability/environment matching

## Goal

Implement the exact, read-only worker capability/environment matching slice
released by devflow Work Order #175 as execution-coordinator Issue #64.

## Boundaries

- Start from accepted main `99097b6fb8d2bc26e9fd98b565773d820788ebae`.
- Consume only `RankedFrontierResult` from Phase 2.
- Match a candidate only when its versioned requirement evidence is fresh,
  provenance-bound to the Phase 2 metadata, and both required sets are exact
  subsets of the worker profile sets.
- Treat unknown, malformed, future, stale, or mismatched evidence as a
  per-worker omission; never infer a capability or environment value.
- Preserve `Role.RECOVERY` as a separate output track.
- Keep ranking omissions and discovery/source failures intact.
- Do not add self-selection, auto-claim, scheduling, provider/controller
  behavior, repo-monitor behavior, or Issue #3 mutation.

## TDD slices

1. Add failing tests for typed profile/requirement validation, exact subset
   matching, provenance/fingerprint mismatch, freshness boundaries, malformed
   and unsupported values, recovery separation, blockers, and independent
   results for multiple workers.
2. Add the pure capability module and satisfy the focused tests.
3. Run the complete unittest suite and compile check.
4. Update the repository Current State and documentation index with the
   accepted-scope description before opening the PR.
5. Verify the exact PR head, obtain formal review evidence, merge only after
   CI/review are non-blocking, then verify merged main and confirm Issue #3
   remains empty.

## Verification evidence

- focused capability tests;
- full `python -W error -m unittest discover -s tests -v`;
- `python -m compileall -q src tests`;
- exact-head GitHub Actions Verify and formal review;
- merged-main tests/compile and runtime Issue #3 readback.
