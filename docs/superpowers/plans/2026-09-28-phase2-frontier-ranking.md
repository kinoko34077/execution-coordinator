# Phase 2: dependency/frontier filtering and deterministic ranking

## Goal

Implement the single bounded Phase 2 slice released as
execution-coordinator#62: consume the accepted read-only managed frontier,
validate an explicit versioned ranking-metadata boundary, apply hard safety and
dependency filters, and produce a deterministic lexicographic ranking. Fresh
and recovery demand remain separate. No claim or scheduler authority is added.

## Design constraints

- Ranking metadata is typed, versioned, provenance-bound to an exact Control
  source and candidate fingerprint, and never inferred from prose, labels,
  timestamps, Projects, branches, PR existence, chat, or missing claims.
- Hard filters run before ranking: source/frontier validity, durable gates,
  current `CLAIMABLE` runtime reason, explicit dependency readiness, and
  metadata freshness/shape.
- The ordering is explicit and deterministic: Control priority P0 through P3,
  optional urgency (present before missing, higher first), dependency-frontier
  order, explicit readiness class, canonical UTC `ready_at` (oldest first,
  missing last), then task reference and role.
- `Role.RECOVERY` candidates are preserved as a separate track and never
  enter fresh-work ranking.
- The result retains omission evidence and discovery/source failures. The
  implementation must not mutate runtime Issue #3 or any external authority.

## Implementation sequence

1. Write red tests for typed metadata/fingerprint validation, hard filters,
   missing/stale evidence, deterministic tie ordering, recovery separation,
   and read-only behavior.
2. Implement the typed ranking metadata and result records with exact input
   validation and provenance checks.
3. Implement pure filtering/ranking over `ManagedFrontierResult`; reuse the
   existing claimability projection rather than duplicating runtime rules.
4. Update the canonical Current State/index and run focused, full-suite and
   compile verification.
5. Push a dedicated PR from accepted main `9b4d12f...`, obtain exact-head
   review/CI, merge only at the reviewed head, and verify merged main before
   reconciling #107/#105/#175.

## Verification evidence

- Focused tests cover malformed/missing/stale/untrusted ranking evidence,
  claimability/dependency hard filters, deterministic ties, fresh/recovery
  separation, and no writes.
- Full workflow uses the bundled Python 3.11 runtime with unittest and
  compileall.
- Post-merge verification repeats the full workflow at the exact merged SHA
  and confirms runtime Issue #3 remains empty.
