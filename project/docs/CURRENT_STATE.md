# Current State

## Repository state

`V0.1 CANDIDATE / AWAITING REVIEW`

`execution-coordinator` is the separate runtime implementation boundary for devflow Execution Coordination Protocol v1.

Current implementation work is owned by Issue #1 and Draft/Review PR #2 on branch `work/issue-1-v01-core`.

Cross-repository authority remains:
- devflow Work Order #105;
- devflow protocol/spec Issue #106 and PR #108;
- devflow Repository Control #107.

## v0.1 candidate implemented behavior

### Pure claim/lease state machine

- role-specific claims for implementer/reviewer/verifier/integrator;
- same task+role exclusive ownership;
- independent non-conflicting claims;
- logical conflict-key rejection for incompatible ownership;
- reviewer compatibility with implementation ownership for independent inspection;
- deterministic generation advancement by task/role boundary;
- explicit timezone-aware time input;
- default 15-minute lease;
- heartbeat and forward-progress timestamps are independent;
- explicit `WAITING` state with evidence-backed wait reason;
- stale-generation fencing;
- lease expiry and higher-generation takeover;
- idempotency-key replay with mismatched-payload rejection;
- release/failure/expiry lifecycle events.

### Strict system-Issue snapshot

- runtime current state is represented by one versioned JSON snapshot inside the long-lived Issue #3 `[SYSTEM] Execution Coordination State`;
- exactly one marker pair is accepted;
- absent marker initializes an empty state;
- malformed JSON, unsupported schema version, duplicate/missing/reversed markers and inconsistent claim keys fail closed;
- timestamps serialize in UTC `Z` form;
- human-readable text outside the machine snapshot is preserved;
- heartbeats/progress do not create append-only comments.

### GitHub Issue state adapter

- stdlib-only `urllib` GitHub REST adapter;
- GET current system-Issue body;
- PATCH authoritative snapshot;
- POST durable lifecycle comments;
- non-2xx/transport/invalid-response failures raise explicit errors without exposing token values;
- Issue-body PATCH success is the authority commit point;
- lifecycle comments are secondary audit evidence after the snapshot commit.

### Mutation entrypoint

Logical operations currently supported:
- `claim`;
- `takeover`;
- `acknowledge`;
- `renew`;
- `progress`;
- `wait`;
- `release`;
- `fail`;
- `expire`.

CLI entry:

```text
python -m execution_coordinator.mutate --operation <op> --payload-json <json> --idempotency-key <key>
```

Required runtime environment for GitHub-backed mutation:
- `GITHUB_TOKEN`;
- `GITHUB_REPOSITORY`;
- `STATE_ISSUE_NUMBER`.

### GitHub Actions mutation serialization

Candidate workflow: `.github/workflows/mutate-state.yml`.

Contract:
- manually dispatched mutation inputs: `operation`, `payload_json`, `idempotency_key`;
- one global concurrency group: `execution-coordinator-state-mutation`;
- `queue: max`;
- no `cancel-in-progress: true`;
- permissions limited to `contents: read` and `issues: write`;
- current system-state Issue is #3;
- external checkout/setup-python Actions are full-SHA pinned;
- untrusted dispatch payload is passed through environment variables rather than interpolated directly into shell source.

## TDD / verification evidence

### Task 1 — claim/lease engine

- RED run `36257008019`: test import failed before production package existed.
- GREEN run `36257105132` on `e3536f06fdb4981efbd94a3b39a3394c91b33959`.

### Task 2 — snapshot codec

- RED run `36257206075`: existing engine tests remained green; only missing snapshot module failed.
- GREEN run `36257253966` on `9f7f6e98b5c75576512730565d76c83224d0cd30`.

### Task 3 — GitHub state adapter / mutation transaction

- RED run `36257309796`: existing engine/snapshot tests remained green; only missing GitHub/mutation modules failed.
- first implementation run `36257376516` exposed one operation-dispatch defect: existing-claim operations were incorrectly required to supply new-claim fields;
- root cause fixed narrowly in `e5a2dbe9ed0108dc1ca0f7061e1e8f0cbcb1f165`;
- exact-head run `36257431723`: full unit/HTTP/snapshot suite + compile check PASS.

### Task 4 — Actions mutation contract

- RED run `36257478480`: existing 28 tests passed; four workflow-contract tests failed only because `mutate-state.yml` was absent;
- workflow added at `b46cb58eefb005126f0a40a7bef924d75e13e3b7`;
- exact-head run `36257604849`: full suite + workflow contract + compile check PASS.

## Current verification boundary

The actual authority-changing `workflow_dispatch` path has **not** yet been executed against Issue #3 because the mutation workflow is not on the default `main` branch while PR #2 remains under review.

Do not represent v0.1 as operationally accepted until:
1. devflow protocol PR #108 is accepted;
2. execution-coordinator PR #2 receives current-head independent formal Review and exact-head CI;
3. PR #2 is merged through normal policy;
4. one bounded real `workflow_dispatch` claim/release smoke succeeds against Issue #3 on merged `main`;
5. post-smoke Current State / Issue #1 / devflow Control #107 are reconciled.

## Not yet implemented

- automatic discovery/ranking of claimable work from devflow/repository Issues;
- agent bootstrap adapter that automatically calls claim/renew/release;
- controller-side priority/capability negotiation;
- repo-monitor projection;
- fine-grained concurrency lanes beyond the single global mutation lane;
- external database/service;
- production deployment.

## Safety / authority boundary

- durable task truth remains in devflow and owning repository Issues/PRs;
- this repository owns only execution-coordination runtime state;
- the existing devflow MCP remains read-only;
- repo-monitor remains observer-only;
- no execution claim overrides release/deploy/publication/credential/permission/destructive/user-decision boundaries.

## Next action

`Complete changed-scope review of PR #2 -> obtain independent formal Review on exact head -> merge only after protocol PR #108 acceptance -> run one bounded claim/release workflow smoke on merged main -> reconcile Issue #1 / Current State / devflow Control #107.`
