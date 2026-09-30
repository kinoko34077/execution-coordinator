# Documentation Index

## Current operational state

- `CURRENT_STATE.md` — accepted v0.1 runtime behavior, merged-main verification, operational smoke evidence, known limitations and next action.

## Implementation plan

- `docs/superpowers/plans/2026-09-27-v01-agent-first-core.md`
- `docs/superpowers/plans/2026-09-28-phase1-managed-frontier.md`
- `docs/superpowers/plans/2026-09-28-phase2-frontier-ranking.md`
- `docs/superpowers/plans/2026-09-28-phase3-capability-match.md`
- `docs/superpowers/plans/2026-09-28-phase5-execution-request.md`
- `docs/superpowers/plans/2026-09-28-phase4-autonomous-cycle.md`

## Runtime entry points

- `src/execution_coordinator/model.py` — runtime data model and bounded idempotency retention invariant.
- `src/execution_coordinator/engine.py` — pure claim/lease/fencing state transitions.
- `src/execution_coordinator/snapshot.py` — strict system-Issue snapshot codec with retention-order preservation.
- `src/execution_coordinator/github_state.py` — GitHub Issue REST state adapter.
- `src/execution_coordinator/frontier.py` — read-only composition of trusted discovery, runtime state and claimability projections.
- `src/execution_coordinator/managed_frontier.py` — deterministic read-only enumeration of normal and reconciliation demand from exact managed-repository Control identities.
- `src/execution_coordinator/ranking.py` — explicit metadata validation, hard dependency/claimability filters, and deterministic read-only frontier ranking.
- `src/execution_coordinator/portfolio_metadata.py` — strict portfolio-v2 companion metadata binding into existing ranking/requirements evidence.
- `src/execution_coordinator/bootstrap_pickup.py` — repository-scoped v1 plus portfolio null-target evidence composition and the existing single-selection claim bridge.
- `src/execution_coordinator/actions_pickup.py` — trusted Issue-comment `/pickup`/`/release` transport, including optional Stage-1 work-class forwarding and omitted-target portfolio pickup.
- `src/execution_coordinator/capability.py` — versioned worker evidence, exact capability/environment subset matching, and worker-local omission projections.
- `src/execution_coordinator/autonomous.py` — bounded worker-scoped first-match selection, one serialized claim, acknowledge-before-work, and existing lifecycle release/fencing.
- `src/execution_coordinator/execution_request.py` — versioned acknowledged-claim request evidence, bootstrap context, provider-neutral launch outcomes, and explicit reconciliation requirements.
- `src/execution_coordinator/mutate.py` — serialized mutation CLI/transaction entrypoint.
- `.github/workflows/mutate-state.yml` — global GitHub Actions mutation lane on default main.
- `.github/workflows/verify.yml` — deterministic unit/contract/compile verification.
- Issue #3 `[SYSTEM] Execution Coordination State` — live runtime current-state snapshot.

## Accepted v0.1 surfaces

- Issue #1 — repository-local v0.1 implementation and acceptance evidence; reconciliation/closure follows accepted merged-main smoke.
- PR #2 — merged v0.1 runtime implementation at main `ed5ed58fab79c161cacdbdb9b7dfd421209bec6f`.
- post-merge Verify run `36266236803` — PASS.
- merged-main claim smoke `36266294818` — PASS.
- merged-main release smoke `36266348606` — PASS; Issue #3 returns to no active claims.

## Phase 0 composed-read slice

- Issue #58 — bounded `compose_claimability_read()` path from explicit trusted Control discovery through validated runtime state to existing claimability projections; no runtime mutation.

## Phase 1 managed-frontier slice

- Issue #60 — bounded `enumerate_managed_frontier()` path from exact bootstrap-resolved Control identities through cached normal/reconciliation discovery and the existing claimability projection; fresh and recovery demand remain separate, with no ranking or runtime mutation.

## Phase 2 deterministic-ranking slice

- Issue #62 — bounded `rank_managed_frontier()` path with versioned provenance-bound metadata, explicit dependency/claimability hard filters, deterministic lexicographic ordering, omission evidence, and a separate recovery track; no ownership or runtime mutation.

## Phase 3 exact capability/environment matching

- Issue #64 — bounded `match_ranked_frontier()` / `match_ranked_frontier_for_workers()` projection with versioned worker and candidate evidence, exact subset matching, per-worker fail-closed omissions, and a separate recovery track; no ownership or runtime mutation.

## Portfolio-scope broad pickup v2

- Issue #85 — accepted devflow #208 / PR #220 runtime consumer: strict companion metadata validation, binding to current v1 candidate body digest + fingerprint, existing ranking hard-filter reuse, exact requirement projection, null-target managed-portfolio evidence composition, and one-selection/no-fallthrough claim integration. Repository-scoped v1 remains compatible; recovery stays separate.

## Stage 1 work-class and Issue-comment pickup transport

- Issue #89 / PR #90 — accepted Stage-1 work-class runtime propagation and `accepted_work_classes` integration.
- Issue #94 / PR #95 — accepted Issue-comment `work_class` parsing/forwarding into the existing `run_pickup()` contract; omission preserves legacy unconstrained behavior. Reviewed head `77822567e9c3394e62ecd2acaea6d2f5a7e8aafc`, PR-head Verify `36663015030`, Formal Review v2 `5361037472`, post-main Verify `36663126842`.
- Issue #97 / PR #98 — accepted Issue-comment portfolio reachability: `target:` is optional for `/pickup`; omission maps to the already-accepted `target_repository=None` path while explicit repository targets remain unchanged. Reviewed head `d115131ba958a69a7f590ad130dc24257a7252af`, exact-head Verify `36664746320`, post-main Verify `36664825599`.
- Both transport slices are reachability/parsing layers only. Canonical work-class semantics remain in devflow; discovery, ranking, claim/lease and provider authority are unchanged.

## Phase 4 agent-first autonomous cycle

- Issue #66 — bounded `run_autonomous_cycle()` boundary over one already refreshed `CapabilityMatchResult`: first eligible match only, one claim attempt, no fallback after rejection, acknowledge-before-work, and existing `AgentSession` release/fencing. No refresh, scheduler, provider/controller, repo-monitor or second authority.

## Phase 5 provider-independent execution-request boundary

- Issue #68 — typed `ExecutionRequest` / `DispatchOutcome` boundary over an already acknowledged `RUNNING` claim: exact evidence/freshness binding, explicit capability/environment and bootstrap context, launch accepted/unavailable/failed outcomes, and pre-acknowledge reconciliation evidence. No provider adapter, scheduler, controller, repo-monitor or second authority.

## Future follow-up candidate

- Issue #10 — extends the accepted acknowledge-before-work adapter over existing progress/wait/resume/fail runtime operations.
- The candidate is not accepted until exact-head CI, formal Review, merge, post-merge Verify, and merged-main smoke complete.

## External canonical references

- devflow Work Order `#105` — parent multi-agent execution coordination objective and later-phase authority.
- devflow protocol/spec Issue `#106` / merged PR `#108` — accepted Protocol v1 authority on devflow main `c0d44e809a835f30263d87fdb2baa62ecddfd4bd`.
- devflow Repository Control `#107` — cross-repository index for this repository.

## Repository role

This repository owns runtime implementation only. Cross-repository policy and protocol semantics remain in devflow. Durable repository task truth remains in owning repositories. `kinotch-repo-monitor` is an observer, not an execution authority.
