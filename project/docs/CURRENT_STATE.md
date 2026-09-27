# Current State

## Repository state

`V0.1 + PHASE 3 AGENT SURFACE / ACCEPTED`

`execution-coordinator` is the runtime implementation boundary for devflow Execution Coordination Protocol v1. Durable task truth remains in devflow and owning repository Issues/PRs; this repository owns only short-lived execution coordination and read-only discovery/runtime/eligibility projections.

The accepted feature baseline through Issue #28 / PR #32 is `8eaa41ab819693e49d3407b865ef0b5319c3690f`. The moving repository Audit SHA is owned by devflow Control #107 so this document does not self-reference documentation-only reconciliation merges.

Cross-repository authority:
- devflow Work Order #105 owns the broader multi-agent execution-coordination objective;
- devflow Issue #106 / merged PR #108 owns Protocol v1 semantics;
- devflow Repository Control #107 is the cross-repository summary/index;
- Issue #1 / PR #2 own runtime v0.1 evidence;
- Issue #5 / PR #6 own the minimal AgentSession bootstrap adapter;
- Issue #9 / PR #13 own acknowledge-before-work lifecycle conformance;
- Issue #15 / PR #16 own acknowledge transport-failure fencing;
- Issue #10 / PR #17 own progress/wait/resume/fail AgentSession coverage;
- Issue #18 / PR #21 own wait/resume transport-failure fencing closure;
- Issue #22 / PR #23 own the accepted read-only `get_state` query surface;
- Issue #25 / PR #26 own the accepted read-only `list_claimable` eligibility projection;
- Issue #28 / PR #32 own trusted Repository Control task-envelope discovery/normalization;
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

`.github/workflows/mutate-state.yml` serializes authority-changing operations through global concurrency group `execution-coordinator-state-mutation` with `queue: max`, no cancel-in-progress, minimum `contents: read` / `issues: write` permissions, full-SHA-pinned external Actions, and a main-ref misuse guard.

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
- work does not begin before claim + acknowledge succeed;
- current `claim_id` / `generation` are forwarded for continuation operations;
- live responses must preserve current authority and expected execution state;
- malformed/stale/changed authority responses fence the local session;
- transport failure during acknowledge, wait, or resume also fences local authority because remote state is uncertain;
- release/fail terminal responses must remove the claim before local authority is cleared;
- safe long waits should normally release rather than consume a lease; retained WAITING remains lease-bound.

The adapter is not a scheduler and does not independently select or rank durable work.

### Read-only `get_state`

Issue #22 / PR #23 provides the validated Protocol v1 current-state read path:

```text
GitHub Issue #3
-> GitHubStateStore.load_body()
-> parse_issue_body()
-> CoordinatorState
```

It uses the same fail-closed snapshot decoder as the mutation path and performs no PATCH, comment, or serialized mutation-lane entry.

### Read-only durable candidate discovery / normalization

Issue #28 / PR #32 provides the accepted read-only discovery adapter for the devflow #125/#132 durable-candidate source contract.

Input is the exact Repository Control identity returned by live bootstrap:

```python
DurableIssueSource(
    repository="kinoko34077/devflow",
    issue_number=107,
)
```

The adapter reads one explicit `DEVFLOW_EXECUTION_CANDIDATES_V1` block from that Control. Each record is one task envelope with common task-level freshness/lifecycle/scope/blocker/confirmation/provenance fields and one or more role entries. It then fetches only the exact owning task Issues named by the block.

Validation is fail-closed for:
- trusted author association on the Control, owning task and optional Work Order;
- exact `source_ref`, repository, task and entry identity;
- Control `Repository State=ACTIVE` and current `[USER_DECISION]` / `[HUMAN_GATE]` vetoes;
- non-empty owning body and canonical `task_body_sha256` freshness;
- duplicate JSON keys, duplicate task envelopes, duplicate roles, unknown fields and malformed/partial markers;
- exact role/status/action combinations and explicit conflict-key forms;
- open Issue and `[WORK ORDER]` provenance requirements.

The deprecated singular `DEVFLOW_EXECUTION_CANDIDATE_V1` owning-Issue marker is ignored and never a fallback source. Marker absence is valid and yields no candidates. The adapter preserves diagnostic fields for `list_claimable` but does not rank, schedule, publish/refresh projections, submit claims, recover `IMPLEMENTING` work, mutate Issues or runtime Issue #3, or authorize protected release/deploy/publication/credential/permission/destructive operations.

### Read-only `list_claimable` eligibility projection

Issue #25 / PR #26 provides the Protocol v1 claimability projection over normalized `ClaimCandidate` values plus `CoordinatorState`.

The projection excludes a candidate when:
- durable scope/acceptance is not ready;
- an unresolved durable blocker is explicit;
- user confirmation is required;
- no current canonical entry reference exists;
- the same task/role already has current runtime ownership;
- an incompatible active conflict key exists;
- the supplied worker identity would create same-worker implementer/reviewer overlap.

Properties:
- deterministic and input-order preserving;
- read-only;
- reuses existing runtime role/conflict compatibility semantics;
- normalized candidates remain derived projections of canonical GitHub state rather than durable truth.

## Accepted verification evidence

### Runtime / lifecycle foundation

- Protocol v1 devflow PR #108 merge `c0d44e809a835f30263d87fdb2baa62ecddfd4bd`;
- runtime v0.1 PR #2 merge `ed5ed58fab79c161cacdbdb9b7dfd421209bec6f`, post-merge Verify `36266236803` PASS;
- AgentSession bootstrap PR #6 merge `1210b506ad1565159d0fb5d934eca66ca166542a`, post-merge Verify `36286744337` PASS;
- acknowledge lifecycle PR #13 merge `66bc5bad0176181f72b67e08dd89af349155c874`, post-merge Verify `36289737056` PASS;
- complete AgentSession lifecycle PR #17 merge `10f47ffaf546b1f4b7108bde50779b28a17937f4`;
- wait/resume transport-fencing PR #21 merge `6321e4051a0650f5448d1acff87537bf19064002`, post-merge Verify `36291323252` PASS.

### `get_state` Issue #22 / PR #23

- RED `36293401784`;
- exact-head GREEN `36293498302`;
- Review Provenance v2 `5328777397`: PASS;
- merge `0cf37f3c288b872055757aaa8c63d614b03d4f70`;
- post-merge Verify `36293581654`: PASS.

### `list_claimable` Issue #25 / PR #26

- RED `36296859986`;
- implementation GREEN `36296955719`;
- PR exact-head GREEN `36297093247` @ `fb856e1e88e7e8da5599fbc31eeaae0bad46ecf1`;
- Review Provenance v2 `5328962555`: PASS;
- merge `299d2577d6cbef5eb87a88188996e5d9daf1385d`;
- post-merge Verify `36297162959`: PASS.

### Durable discovery Issue #28 / PR #32

- accepted upstream devflow #125 / PR #132 merge: `7eaf3c7be566beeb5984ac1a2a78bc44b6dde988`;
- rewritten branch exact head: `dcc27003bac969b7289f743584e88bba8b5c8878`;
- pre-merge Verify: `36311759618` SUCCESS;
- Formal Review v2: `5329886612` PASS with zero blocking findings;
- merge: `8eaa41ab819693e49d3407b865ef0b5319c3690f`;
- main `discovery.py` SHA: `a18865392ffa653a987c3e10e921ec06dddf575c`;
- main tests and README were read back with the plural Control-projection contract;
- no PR workflow run is exposed for the merge commit yet; devflow #107 remains `NEEDS_REAUDIT` pending fresh owner-side audit and Current State/control reconciliation;
- Issue #3 remained outside this docs/code slice.

## Review / identity boundary

- `worker_id` is runtime coordination metadata, not a cryptographic identity or GitHub security principal;
- runtime same-worker role checks are separate from formal code-review provenance;
- formal Review requirements are governed by devflow policy.

## Known limitations / deferred protocol surface

Not yet implemented:
- GitHub-wide/frontier source selection: current durable discovery consumes exact Control projection records and does not enumerate the world;
- arbitrary free-form Issue/Work Order interpretation outside the strict supported durable envelope;
- priority/dependency frontier ranking and self-selection;
- capability/environment matching;
- one composed read-only entrypoint that performs exact-Issue discovery -> current-state filtering end-to-end;
- full `claim(..., expected_state, idempotency_key)` / structured failure-evidence conformance;
- controller priority/capability/availability negotiation;
- read-only repo-monitor projection;
- bounded self-scheduling/work stealing;
- atomic sweep-plus-takeover;
- fine-grained mutation lanes or external state storage.

Current v0.1 also uses bounded idempotency retention, one coarse global mutation queue, and stderr/process-exit failure reporting rather than a structured failure envelope.

## Safety / authority boundary

- durable requirements and completion truth remain in devflow / owning repository Issues and PRs;
- `DurableIssueSource` and `ClaimCandidate` are derived input/projection only and never replace durable GitHub truth;
- Issue #3 owns ephemeral runtime execution state only;
- devflow MCP remains read-only;
- repo-monitor remains observer-only;
- no discovery result, execution claim, or eligibility projection overrides release/deploy/publication/credential/permission/destructive/user-decision confirmation boundaries.

## Next action

Issue #28 / PR #32 is accepted at the feature layer. After this Current State reconciliation is merged, no repository-local implementation slice should remain active until a new Issue is selected and devflow #107 records a fresh audit. The next smallest bounded gap is a composed read-only Control-projection path combining durable discovery with existing `list_claimable`; GitHub-wide source selection, ranking, capability matching, scheduling, automatic claim submission, controller negotiation, and repo-monitor projection remain separate later slices.
