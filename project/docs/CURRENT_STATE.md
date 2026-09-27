# Current State

## Repository state

`V0.1 OPERATIONAL / ACCEPTED`

`execution-coordinator` is the separate runtime implementation boundary for devflow Execution Coordination Protocol v1.

Accepted default `main` is `238ffb9d8a7f51d0416fd0d3f9b96f1e4fb17944`, after runtime PR #2 merged as `ed5ed58fab79c161cacdbdb9b7dfd421209bec6f` and state-reconciliation PR #4 updated the accepted Current State. The canonical protocol was accepted first in `kinoko34077/devflow` PR #108 and is present on devflow main `c0d44e809a835f30263d87fdb2baa62ecddfd4bd`.

Cross-repository authority remains:
- devflow Work Order #105 owns the broader multi-agent execution-coordination objective;
- devflow protocol/spec Issue #106 / merged PR #108 owns Protocol v1 semantics;
- devflow Repository Control #107 is the cross-repository index;
- repository Issue #1 / merged PR #2 own the v0.1 runtime implementation evidence;
- Issue #3 `[SYSTEM] Execution Coordination State` is runtime current state only.

## Accepted v0.1 behavior

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
- `WAITING` remains lease-bound and does not gain indefinite authority from wait evidence;
- `resume` transitions a live `WAITING` claim back to `RUNNING` and clears wait metadata;
- normal CI/review/user/dependency/provider waits should release execution authority whenever safe under the devflow protocol;
- stale-generation fencing;
- explicit expiry sweep followed by higher-generation takeover;
- retry-safe idempotency with mismatched-payload rejection;
- retry wall-clock time excluded from logical idempotency identity;
- release/failure/expiry lifecycle events.

### Bounded idempotency retention

- snapshot retains at most 128 idempotency records;
- high-frequency non-event records are evicted before lifecycle authority records where possible;
- newest mutation record is retained by the write that creates it;
- if only lifecycle authority records remain, the oldest record is evicted to preserve the cap;
- retention order survives snapshot serialize/parse cycles;
- oversized externally modified idempotency state fails closed;
- evicted keys are no longer permanently deduplicated.

### Strict system-Issue snapshot

- runtime current state is represented by one versioned JSON snapshot inside Issue #3;
- exactly one marker pair is accepted;
- absent marker initializes an empty state;
- malformed JSON, unsupported schema, duplicate/missing/reversed markers, and inconsistent authority state fail closed;
- active task/role boundaries are unique;
- active claim generations match the generation table;
- incompatible conflict-key ownership and same-worker implementer/reviewer overlap are rejected on decode as well as claim acquisition;
- waiting metadata must be consistent with `WAITING` state;
- timestamps serialize in UTC `Z` form;
- human-readable text outside the machine snapshot is preserved;
- heartbeat/progress churn does not create append-only comments.

### GitHub Issue state adapter

- stdlib-only `urllib` GitHub REST adapter;
- GET current system-Issue body;
- PATCH authoritative snapshot;
- POST durable lifecycle comments;
- non-2xx/transport/invalid-response failures raise explicit errors without exposing token values;
- Issue-body PATCH is the authority commit point;
- lifecycle comments are secondary audit evidence after snapshot commit.

### Mutation entrypoint

Supported v0.1 operations:
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

GitHub-backed mutation requires `GITHUB_TOKEN`, `GITHUB_REPOSITORY`, and `STATE_ISSUE_NUMBER`.

### GitHub Actions mutation serialization

`.github/workflows/mutate-state.yml` is operational on default main.

Contract:
- `workflow_dispatch` inputs: `operation`, `payload_json`, `idempotency_key`;
- one global concurrency group: `execution-coordinator-state-mutation`;
- `queue: max` and no `cancel-in-progress: true`;
- permissions limited to `contents: read` and `issues: write`;
- system-state Issue is #3;
- external Actions are full-SHA pinned;
- untrusted dispatch payload is passed through environment variables;
- mutation job has a `refs/heads/main` misuse guard;
- that main-ref guard is not an independent authorization boundary.

## Review-policy boundary

- `worker_id` is runtime coordination metadata supplied by the client, not a cryptographic identity or GitHub security principal;
- same-worker implementer/reviewer overlap is an additional runtime safety check;
- formal Review provenance and reviewer-signature requirements remain governed by devflow policy, not `worker_id`.

## Verification evidence

### Candidate / regression verification

- WAITING/resume remediation: RED `36260527341` -> GREEN `36260609780`;
- contradictory snapshot authority invariants: RED `36264453074` -> GREEN `36264592144`;
- final PR #2 exact-head verify `36264592144` PASS on `46374f07e0d9c94254119dcc276b04ae678672ce`.

### Accepted main

- PR #2 merged as `ed5ed58fab79c161cacdbdb9b7dfd421209bec6f` after Protocol PR #108 merged first;
- post-merge main verify run `36266236803` PASS;
- bounded real claim smoke on merged main: mutation run `36266294818` PASS;
- bounded release smoke on merged main: mutation run `36266348606` PASS;
- Issue #3 after release has `claims: {}` and retains generation/idempotency evidence for the completed smoke.

This is direct operational evidence that the default-main serialized mutation path can commit and release one bounded synthetic claim without leaving active authority behind.

## Known v0.1 limitations / deferred conformance

- `takeover` does not implicitly sweep expiry; v0.1 uses explicit `expire` then `takeover` under the serialized lane;
- main-ref workflow guard is misuse prevention, not a protected GitHub Environment or separate security principal;
- bounded idempotency retention means evicted keys are not deduplicated forever;
- mutation failures are reported through process exit/stderr rather than a dedicated structured failure envelope;
- Issue PATCH response equality remains an additional success check and has not been exercised against hypothetical GitHub body normalization;
- one global queue is intentionally coarse; fine-grained lanes and external storage are deferred;
- Protocol v1 includes richer `claim(..., expected_state, idempotency_key)` / bounded failure-evidence concepts than the current v0.1 CLI exposes. This is a deferred runtime-conformance gap, not evidence that the accepted v0.1 claim/lease slice implements the full future protocol surface;
- automatic task discovery/ranking, agent bootstrap automation, controller-side negotiation, and repo-monitor projection are not yet implemented.

## Safety / authority boundary

- durable task truth remains in devflow and owning repository Issues/PRs;
- this repository owns only execution-coordination runtime state;
- devflow MCP remains read-only;
- repo-monitor remains observer-only;
- no execution claim overrides release/deploy/publication/credential/permission/destructive/user-decision boundaries.

## Next action

`Reconcile repository Issue #1 and devflow Control #107 to the accepted v0.1 main/smoke evidence. Continue later execution-coordination phases only through devflow #105 and bounded repository-local Issues; do not treat v0.1 as full controller/agent negotiation implementation.`