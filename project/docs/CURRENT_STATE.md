# Current State

## Repository state

`V0.1 CANDIDATE / CHANGES ADDRESSED / AWAITING INDEPENDENT RE-REVIEW`

`execution-coordinator` is the separate runtime implementation boundary for devflow Execution Coordination Protocol v1.

Current implementation work is owned by Issue #1 and PR #2 on branch `work/issue-1-v01-core`.

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
- same worker cannot hold implementer and independent reviewer authority for the same task;
- deterministic generation advancement by task/role boundary;
- explicit timezone-aware time input;
- default 15-minute lease;
- heartbeat and forward-progress timestamps are independent;
- explicit `WAITING` state with evidence-backed wait reason;
- `WAITING` remains lease-bound and does not acquire indefinite ownership merely because wait evidence exists;
- `resume` transitions a live `WAITING` claim back to `RUNNING` and clears wait metadata;
- normal CI/review/user/dependency/provider waits should release the execution claim whenever safe, as defined by devflow protocol;
- stale-generation fencing;
- lease expiry and higher-generation takeover after expiry sweep;
- idempotency-key replay with mismatched-payload rejection;
- retry wall-clock time is excluded from logical idempotency identity;
- release/failure/expiry lifecycle events.

### Bounded idempotency retention

- current snapshot retains at most 128 idempotency records;
- high-frequency non-event records such as renew/progress/wait/resume are evicted before lifecycle authority records when possible;
- the newest mutation record is retained by the write that creates it;
- if the retained set contains only lifecycle authority records, the oldest record is evicted to preserve the hard cap;
- idempotency insertion/retention order is preserved through Issue snapshot serialize/parse cycles;
- an oversized idempotency map in an externally modified snapshot fails closed;
- idempotency is bounded retention, not permanent deduplication: once a key has been evicted, a later reuse may be treated as a fresh logical request.

### Strict system-Issue snapshot

- runtime current state is represented by one versioned JSON snapshot inside the long-lived Issue #3 `[SYSTEM] Execution Coordination State`;
- exactly one marker pair is accepted;
- absent marker initializes an empty state;
- malformed JSON, unsupported schema version, duplicate/missing/reversed markers and inconsistent authority state fail closed;
- active task/role boundaries are unique;
- active claim generations must match the generation table;
- waiting metadata must be consistent with `WAITING` state;
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
- `resume`;
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
- untrusted dispatch payload is passed through environment variables rather than interpolated directly into shell source;
- mutation job has a `refs/heads/main` guard to prevent accidental mutation from ordinary non-default dispatches;
- the main-ref guard is an operational misuse guard, not a standalone authorization/security boundary; repository/workflow write authority remains governed by GitHub permissions and repository controls.

## Review-policy boundary

- `worker_id` is runtime coordination metadata supplied by the client; it is not a cryptographic identity or GitHub security principal;
- same-worker implementer/reviewer overlap is rejected as an additional runtime safety check;
- authoritative enforcement of independent formal Review remains in devflow Review Provenance / repository policy, not in self-asserted `worker_id` values.

## Known v0.1 operational limitations

- `takeover` does not implicitly sweep an expired claim in the same mutation; v0.1 callers use `expire` followed by `takeover` under the serialized mutation lane;
- the main-ref workflow guard is not equivalent to a protected GitHub Environment;
- bounded idempotency retention means evicted keys are no longer deduplicated forever;
- mutation failures are reported by process exit status/stderr rather than a dedicated machine-readable failure envelope;
- Issue PATCH response equality is used as an additional success check and has not yet been tested against hypothetical GitHub body normalization changes;
- fine-grained concurrency lanes and external storage are intentionally deferred.

## TDD / verification evidence

### Initial implementation slices

- claim/lease RED `36257008019` -> GREEN `36257105132`;
- snapshot RED `36257206075` -> GREEN `36257253966`;
- GitHub adapter/mutation RED `36257309796`; payload-dispatch defect -> GREEN `36257431723`;
- workflow contract RED `36257478480` -> GREEN `36257604849`;
- retry-time/self-review/main-ref hardening RED `36257798498`;
- bounded-retention RED `36257955287` -> GREEN `36258281283`;
- authority-snapshot invariant RED `36259093564` -> GREEN `36259143538`;
- expired-active-claim continuation RED `36259311066` -> GREEN `36259362685` on `990b2a4751c8b41a7501aaa9cbb49358329f5d94`.

### Independent Review findings and remediation

Claude Code DIFFERENT_AGENT review on PR #2 at `990b2a4751c8b41a7501aaa9cbb49358329f5d94` returned CHANGES_REQUESTED with two P1 findings:

1. evidence-backed `WAITING` claims could bypass lease expiry indefinitely;
2. no `WAITING -> RUNNING` resume transition existed.

Remediation TDD:
- RED run `36260527341` on test head `388e6a23914872de504447227d92f9c40e411737` failed because `resume` was absent and the dispatcher rejected `resume`;
- implementation removed the WAITING lease-expiry exemption and added explicit `resume`;
- GREEN run `36260609780` on `9d35160109d768e877d8f6f5b074eb9ee170cd74`: unit tests + compile check PASS before documentation reconciliation.

The independent review must be re-run against the post-fix current head before merge.

## Current verification boundary

The actual authority-changing `workflow_dispatch` path has **not** yet been executed against Issue #3 because the mutation workflow is not on the default `main` branch while PR #2 remains under review.

Do not represent v0.1 as operationally accepted until:
1. devflow protocol PR #108 receives acceptable independent Review and is accepted;
2. execution-coordinator PR #2 receives acceptable current-head independent formal Review and exact-head CI;
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

`Run exact-head CI after this documentation reconciliation -> obtain DIFFERENT_AGENT re-review for execution-coordinator PR #2 and independent Review for devflow protocol PR #108 -> merge only after both review gates pass -> run one bounded claim/release workflow smoke on merged main -> reconcile Issue #1 / Current State / devflow Control #107.`
