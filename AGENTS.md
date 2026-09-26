# AGENTS.md

This repository is managed by KiNoTch. devflow.

## Required read order

1. Read live `kinoko34077/devflow/AGENTS.md`.
2. Read the open devflow `[REPO] execution-coordinator` Repository Control Issue.
3. Read `project/docs/CURRENT_STATE.md` and `project/docs/INDEX.md`.
4. Open the active local Issue / Work Order / PR referenced by the Control.
5. Read only task-relevant specification, source, and tests.

## Boundaries

- `devflow` owns protocol/policy semantics; this repository implements them.
- Do not turn this repository into a second durable task source of truth.
- GitHub Actions is the initial mutation serializer for claim authority.
- Do not mutate monitored/target repositories merely to infer execution state.
- Do not infer ChatGPT/Codex internal liveness from file activity alone.
- Normal implementation uses dedicated branches and PRs; do not write directly to `main`.
- Use Issue-first reporting for durable implementation evidence and handoff.
