# Phase 1: managed-frontier enumeration

## Goal

Implement the single bounded Phase 1 slice released as execution-coordinator#60:
enumerate a deterministic, read-only runnable frontier from the exact managed
repository Control identities supplied by live devflow bootstrap. Preserve
source inventory, normal and reconciliation candidates, claimability reasons,
and discovery failures as typed evidence. Do not rank, mutate, auto-claim, or
touch runtime Issue #3.

## Design constraints

- Accept only caller-provided `DurableIssueSource` identities; do not discover
  repositories from prose, GitHub Projects, or local configuration.
- Normalize valid sources by repository and Control issue number, reject
  invalid or duplicate managed-repository identities fail-closed, and retain
  deterministic source-failure evidence.
- Read each exact Issue identity from one cached snapshot while composing both
  the normal execution-candidate and reconciliation-publication validators.
- Reuse Phase 0's claimability projection and existing discovery/reconciliation
  validators. Fresh candidates and `Role.RECOVERY` candidates remain separate
  typed views.
- Keep the entire surface read-only: no claim, lease, publication, or Issue #3
  write is allowed.

## Implementation sequence

1. Write focused red tests for source normalization, duplicate/invalid
   fail-closed behavior, combined normal/reconciliation discovery, recovery
   separation, cached reads, and runtime claimability gates.
2. Extract a reusable Phase 0 composition helper that accepts a prepared
   `DiscoveryResult` without changing the existing public behavior.
3. Add `managed_frontier.py` with typed frontier evidence, deterministic
   normalization, cached Issue reads, combined discovery, and fresh/recovery
   projections.
4. Run focused tests, the full unittest suite with the bundled Python 3.11
   runtime, and compile checks. Update the canonical project index/current
   state only for the accepted Phase 1 surface.
5. Push the dedicated branch, create the Issue-linked PR, obtain an exact-head
   formal review (independent reviewer when available; otherwise record the
   self-review limitation), verify CI, and merge only at the reviewed head.
6. Verify merged main, then reconcile devflow Control #107, parent #105, and
   Work Order #175 to the new Audit SHA before releasing any Phase 2 slice.

## Verification evidence

- Focused managed-frontier tests cover normal candidates, reconciliation
  candidates, recovery separation, deterministic ordering, fail-closed input,
  discovery failures, cache behavior, and active runtime blockers.
- Full workflow: `PYTHONPATH=src` with bundled Python 3.11,
  `python -W error -m unittest discover -s tests`, followed by
  `python -m compileall -q src tests`.
- Post-merge verification repeats the full workflow at the exact merged main
  SHA and confirms runtime Issue #3 remains empty.
