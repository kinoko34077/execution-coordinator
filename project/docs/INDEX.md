# Documentation Index

## Current operational state

- `CURRENT_STATE.md` — accepted v0.1 runtime behavior, merged-main verification, operational smoke evidence, known limitations and next action.

## Implementation plan

- `docs/superpowers/plans/2026-09-27-v01-agent-first-core.md`

## Runtime entry points

- `src/execution_coordinator/model.py` — runtime data model and bounded idempotency retention invariant.
- `src/execution_coordinator/engine.py` — pure claim/lease/fencing state transitions.
- `src/execution_coordinator/snapshot.py` — strict system-Issue snapshot codec with retention-order preservation.
- `src/execution_coordinator/github_state.py` — GitHub Issue REST state adapter.
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

## External canonical references

- devflow Work Order `#105` — parent multi-agent execution coordination objective and later-phase authority.
- devflow protocol/spec Issue `#106` / merged PR `#108` — accepted Protocol v1 authority on devflow main `c0d44e809a835f30263d87fdb2baa62ecddfd4bd`.
- devflow Repository Control `#107` — cross-repository index for this repository.

## Repository role

This repository owns runtime implementation only. Cross-repository policy and protocol semantics remain in devflow. Durable repository task truth remains in owning repositories. `kinotch-repo-monitor` is an observer, not an execution authority.