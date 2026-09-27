# Current State

## Repository state

`V0.1 + PHASE 3 AGENT SURFACE / ACCEPTED`

`execution-coordinator` is the runtime implementation boundary for devflow Execution Coordination Protocol v1. Durable task truth remains in devflow and owning repository Issues/PRs; this repository owns only short-lived execution coordination and read-only runtime/eligibility projections.

The accepted feature baseline through Issue #25 / PR #26 is `299d2577d6cbef5eb87a88188996e5d9daf1385d`. The moving repository Audit SHA is owned by devflow Control #107 so this document does not self-reference documentation-only reconciliation merges.

Cross-repository authority:
- devflow Work Order #105 owns the broader multi-agent execution-coordination objective;
- devflow Issue #106 / merged PR #108 owns Protocol v1 semantics;
- devflow Repository Control #107 is the cross-repository summary/index;
- repository Issue #1 / PR #2 own runtime v0.1 evidence;
- Issue #5 / PR #6 own the minimal AgentSession bootstrap adapter;
- Issue #9 / PR #13 own acknowledge-before-work lifecycle conformance;
- Issue #15 / PR #16 own acknowledge transport-failure fencing;
- Issue #10 / PR #17 own accepted progress/wait/resume/fail AgentSession coverage;
- Issue #18 / PR #21 own wait/resume transport-failure fencing closure;
- Issue #22 / PR #23 own the accepted read-only `get_state` query surface;
- Issue #25 / PR #26 own the accepted read-only `list_claimable` eligibility projection;
- Issue #3 `[SYSTEM] Execution Coordination State` is runtime current state only.

## Accepted runtime behavior

### Claim / lease state machine

- role-specific claims for implementer/reviewer/verifier/integrator;
- exclusive same task+role ownership with compatible independent claims;
- logical conflict-key enforcement;
- same worker cannot hold implementer and reviewer authority for the same task;
- monotonic task/role generation fencing;
- default 15-minute lease with separate heartbeat and progress timestamps;
- canonical live states `CLAIMED`, `RUNNING`, and lease-bound `WAITING`;
- `resume` transitions a current unexpired `WAITING` claim back to `RUNNING` and clears wait metadata;
- stale generations fail closed;
- recovery uses explicit serialized `expire` followed by higher-generation `takeover`/`claim`;
- retry-safe idempotency with mismatched-payload rejection;
- release/failure/expiry remove live authority without marking durable work complete.

### Snapshot and state storage

Issue #3 stores one schema-v1 JSON snapshot between versioned markers.

Validation fails closed on malformed JSON, unsupported schema, marker corruption, task/role duplication, generation inconsistency, incompatible conflict-key ownership, same-worker implementer/reviewer overlap, invalid WAITING metadata, or oversized idempotency retention.

Idempotency retention is bounded to 128 records. High-frequency non-event records are evicted before lifecycle records where possible; evicted keys are not permanently deduplicated.

The Issue body PATCH is the mutation authority commit point. Lifecycle comments are secondary audit evidence; heartbeat/progress churn does not append comments.

### Serialized mutation surface

Supported mutation operations:

```text
claim
takeover
acknowledge
renew
progress
wait
resume
release
fail
expire
```

CLI:

```text
python -m execution_coordinator.mutate \
  --operation <op> \
  --payload-json <json> \
  --idempotency-key <key>
```

`.github/workflows/mutate-state.yml` serializes all authority-changing operations through global concurrency group `execution-coordinator-state-mutation` with `queue: max`, no cancel-in-progress, minimum `contents: read` / `issues: write` permissions, full-SHA-pinned external Actions, and a main-ref misuse guard.

### AgentSession accepted surface

`AgentSession` exposes the bounded caller-driven lifecycle:

```text
claim
-> acknowledge
-> active work
-> progress / renew
-> wait -> resume
   or release / fail
```

Rules:
- implementation callback/work does not begin before claim + acknowledge succeed;
- current `claim_id` / `generation` are forwarded for continuation operations;
- live responses must preserve current authority and expected execution state;
- malformed/stale/changed authority responses fence the local session;
- transport failure during acknowledge, wait, or resume also fences local authority because remote state is uncertain;
- release/fail terminal responses must remove the claim before local authority is cleared;
- callback exceptions remain primary; release-cleanup failures are retained as notes;
- safe long waits should normally release rather than consume a lease; retained WAITING remains lease-bound.

The adapter is not a scheduler and does not independently discover or rank durable work.

### Read-only `get_state` query

Issue #22 / PR #23 provides the formal read-only Protocol v1 current-state query:

```text
GitHub Issue #3
-> GitHubStateStore.load_body()
-> parse_issue_body()
-> validated CoordinatorState
```

API:

```python
from execution_coordinator.query import get_state
state = get_state(store)
```

Properties:
- uses the same fail-closed snapshot decoder as the mutation path;
- returns schema-v1 state without PATCH, comment, or serialized mutation-lane entry;
- supports empty, active, and WAITING snapshots;
- malformed snapshots and GitHub read failures surface as failures rather than inferred state;
- `GITHUB_TOKEN` is optional for publicly readable repositories.

### Read-only `list_claimable` eligibility projection

Issue #25 / PR #26 provides the first bounded Protocol v1 claimability projection.

Normalized input:

```python
ClaimCandidate(
    task="owner/repo#123",
    role=Role.IMPLEMENTER,
    entry_ref="issue:123",
    conflict_keys=("component:owner/repo:core",),
)
```

Projection:

```python
from execution_coordinator.query import list_claimable
claimable = list_claimable(candidates, state, worker_id="worker-session")
```

The projection excludes a candidate when:
- durable scope/acceptance is explicitly not ready;
- an unresolved durable blocker is explicit in the normalized descriptor;
- user confirmation is required before continuation;
- no current canonical entry reference exists;
- the same task/role already has a current runtime claim;
- an incompatible active conflict key exists;
- the supplied worker identity would create same-worker implementer/reviewer self-review overlap.

Properties:
- deterministic and input-order preserving;
- read-only; neither candidates nor `CoordinatorState` are mutated;
- reuses the runtime's existing role/conflict compatibility semantics;
- normalized candidates remain projections of canonical durable GitHub state, not a new durable task source;
- this slice does not discover arbitrary GitHub Issues, parse durable bodies, rank candidates, match capabilities, schedule work, or submit claims automatically.

## Accepted verification evidence

### Runtime / lifecycle

- Protocol v1: devflow PR #108 merged as `c0d44e809a835f30263d87fdb2baa62ecddfd4bd`;
- runtime v0.1 PR #2 merged as `ed5ed58fab79c161cacdbdb9b7dfd421209bec6f`; post-merge Verify `36266236803` PASS; bounded claim/release smoke left Issue #3 with `claims: {}`;
- Phase 3 adapter PR #6 merge `1210b506ad1565159d0fb5d934eca66ca166542a`; post-merge Verify `36286744337` PASS;
- acknowledge lifecycle PR #13 merge `66bc5bad0176181f72b67e08dd89af349155c874`; post-merge Verify `36289737056` PASS; serialized claim/acknowledge/release smoke returned Issue #3 to `claims: {}`;
- complete AgentSession lifecycle PR #17 merge `10f47ffaf546b1f4b7108bde50779b28a17937f4`;
- wait/resume transport-fencing PR #21 exact-head run `36291145718` PASS, merge `6321e4051a0650f5448d1acff87537bf19064002`, post-merge Verify `36291323252` PASS; serialized claim -> acknowledge -> wait -> resume -> release smoke returned Issue #3 to `claims: {}`.

### `get_state` Issue #22 / PR #23

- RED `36293401784` on `db32aec0f5d4b2d6a99c9020ddb5a5589c8f440a`;
- final exact-head GREEN `36293498302` on `a318b6976aad3b8aca0dc7cfc6e287919b09adf2`;
- formal Review Provenance v2 review `5328777397`: PASS;
- PR #23 merge `0cf37f3c288b872055757aaa8c63d614b03d4f70`;
- post-merge Verify `36293581654`: PASS;
- Issue #3 remained unchanged with `claims: {}`.

### `list_claimable` Issue #25 / PR #26

- RED `36296859986` on `3aa26f39ec40c9ca3db376525a6bdc24ca9727df`: the existing suite passed and the six new contract tests failed because the new API did not yet exist;
- implementation GREEN `36296955719` on `48b1caa2fe037e284d9a01b3884fe9e3338522ae`;
- final pre-PR GREEN `36297001729` and PR-trigger exact-head GREEN `36297093247` on `fb856e1e88e7e8da5599fbc31eeaae0bad46ecf1`;
- formal Review Provenance v2 review `5328962555`: PASS with no blocking findings;
- PR #26 merge `299d2577d6cbef5eb87a88188996e5d9daf1385d`;
- post-merge Verify `36297162959`: PASS;
- Issue #3 readback remained unchanged at `updated_at=2026-09-27T03:30:40Z` with `claims: {}`.

## Review / identity boundary

- `worker_id` is runtime coordination metadata, not a cryptographic identity or GitHub security principal;
- runtime same-worker role checks are separate from formal code-review provenance;
- formal Review requirements are governed by devflow policy.

## Known limitations / deferred protocol surface

Not yet implemented:
- canonical GitHub durable-task discovery/normalization transport feeding `ClaimCandidate` values;
- arbitrary Issue/Work Order parsing into claimability descriptors;
- priority/dependency frontier ranking and self-selection;
- capability/environment matching;
- full `claim(..., expected_state, idempotency_key)` / structured failure-evidence conformance;
- controller priority/capability/availability negotiation;
- read-only repo-monitor projection;
- bounded self-scheduling/work stealing;
- atomic sweep-plus-takeover;
- fine-grained mutation lanes or external state storage.

Current v0.1 also uses bounded idempotency retention, one coarse global mutation queue, and stderr/process-exit failure reporting rather than a structured failure envelope.

## Safety / authority boundary

- durable requirements and completion truth remain in devflow / owning repository Issues and PRs;
- normalized `ClaimCandidate` values are derived input only and never replace durable GitHub truth;
- Issue #3 owns ephemeral runtime execution state only;
- devflow MCP remains read-only;
- repo-monitor remains observer-only;
- no execution claim or eligibility projection overrides release/deploy/publication/credential/permission/destructive/user-decision confirmation boundaries.

## Next action

Issue #25 / PR #26 is accepted. No repository-local implementation slice should be treated as active after reconciliation. The next bounded gap is a read-only durable-candidate discovery/normalization adapter that feeds `ClaimCandidate` from canonical devflow / owning-repository state; ranking, capability matching, scheduling, automatic claim submission, controller negotiation, and repo-monitor projection remain separate later slices.