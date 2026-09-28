# Phase 5 provider-independent execution-request boundary

## Goal

Implement execution-coordinator Issue #68, the explicitly released Phase 5
slice from devflow Work Order #175.

## Boundaries

- Start from accepted main `f799b352a4565a9cbddf9a7245e2fd19b32034cf`.
- Accept only an already claimed and acknowledged (`RUNNING`) authority from
  the existing runtime lifecycle.
- Bind the request to exact task/role, candidate fingerprint/source/freshness,
  required capability/environment and explicit bootstrap context.
- Return typed launch accepted/unavailable/failed outcomes and explicit
  reconciliation requirements for a claim whose worker never starts or dies
  before acknowledge.
- Keep selection, ranking, claim authority, scheduler, controller,
  repo-monitor, provider adapters, credentials and external launches out of
  scope.

## TDD slices

1. Add failing tests for request identity/evidence binding, acknowledged-only
   authority, freshness, bootstrap validation, launch outcomes, pre-acknowledge
   death reconciliation and fail-closed adapter responses.
2. Add the typed request/outcome module and narrow adapter protocol.
3. Run the complete unittest suite and compile check.
4. Update Current State and the documentation index.
5. Open a dedicated PR, verify exact-head CI/review, merge when non-blocking,
   verify merged main and read Issue #3 without mutation.

## Verification evidence

- focused execution-request tests;
- full `python -W error -m unittest discover -s tests -v`;
- `python -m compileall -q src tests`;
- exact-head GitHub Actions Verify and formal review;
- merged-main tests/compile and runtime Issue #3 readback.
