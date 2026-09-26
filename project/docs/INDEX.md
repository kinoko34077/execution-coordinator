# Documentation Index

## Current operational state

- `CURRENT_STATE.md` — v0.1 candidate behavior, evidence, limits and next action.

## Implementation plan

- `docs/superpowers/plans/2026-09-27-v01-agent-first-core.md`

## Runtime entry points

- `src/execution_coordinator/model.py` — runtime data model.
- `src/execution_coordinator/engine.py` — pure claim/lease/fencing state transitions.
- `src/execution_coordinator/snapshot.py` — strict system-Issue snapshot codec.
- `src/execution_coordinator/github_state.py` — GitHub Issue REST state adapter.
- `src/execution_coordinator/mutate.py` — serialized mutation CLI/transaction entrypoint.
- `.github/workflows/mutate-state.yml` — global GitHub Actions mutation lane.
- `.github/workflows/verify.yml` — deterministic unit/contract/compile verification.
- Issue #3 `[SYSTEM] Execution Coordination State` — live runtime current-state snapshot.

## External canonical references

- devflow Work Order `#105` — parent multi-agent execution coordination objective.
- devflow protocol/spec Issue `#106` and PR `#108` — protocol authority under review.
- devflow Repository Control `#107` — cross-repository index for this repository.

## Repository role

This repository owns runtime implementation only. Cross-repository policy and protocol semantics remain in devflow. Durable repository task truth remains in owning repositories. `kinotch-repo-monitor` is an observer, not an execution authority.
