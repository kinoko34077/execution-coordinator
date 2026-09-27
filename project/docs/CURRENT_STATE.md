# Current State

## Repository state

`V0.1 + PHASE 3 AGENT SURFACE / ACCEPTED`

`execution-coordinator` is the runtime implementation boundary for devflow Execution Coordination Protocol v1. Durable task truth remains in devflow and owning repository Issues/PRs; this repository owns only short-lived execution coordination and read-only discovery/runtime/eligibility projections.

The accepted feature baseline through Issue #28 / PR #29 is `d03ac20cbf6e00992f9902239a0515eba4dc0b70`. The moving repository Audit SHA is owned by devflow Control #107 so this document does not self-reference documentation-only reconciliation merges.

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
- Issue #28 / PR #29 own strict exact-reference durable Issue discovery/normalization;
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

### Read-only durable Issue discovery / normalization

Issue #28 / PR #29 adds strict exact-reference durable Issue discovery.

Input is caller-owned policy plus canonical source identity:

```python
DurableIssueSource(
    repository="owner/repo",
    issue_number=123,
    role=Role.IMPLEMENTER,
    eligible_work_states=("READY_FOR_IMPLEMENTATION",),
    conflict_keys=("component:owner/repo:core",),
)
```

`GitHubIssueReader` performs GET-only reads of the exact Issue reference. Normalization succeeds only when:
- the returned identity matches the requested repository/Issue;
- the source is an open Issue and not a pull request;
- exactly one supported `Work Status` is present;
- the Work Status belongs to the Protocol v1 vocabulary owned by `devflow/.devflow/WORKFLOW.yaml`;
- Objective, Scope/Design scope, and Acceptance criteria are present and non-empty;
- the canonical Issue URL is present.

Normalization behavior:
- caller supplies the requested role and the Work Status values eligible for that role; execution-coordinator does not infer role-to-state policy;
- `BLOCKED` is derived only from explicit Work Status;
- `[USER_DECISION]` is recognized only from explicit Next Action content;
- malformed/ambiguous/unsupported sources become explicit `DiscoveryFailure` records;
- one malformed source does not hide valid sibling candidates;
- input order is preserved;
- no Issue, runtime state, or durable truth is mutated.

The recognized Work Status set mirrors Protocol v1 and must be reconciled if the canonical devflow vocabulary changes.

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

### Durable discovery Issue #28 / PR #29

- initial RED `36297981633` @ `6487031e143d1be14ff70de7e3b01988b07af623`: existing suite passed; nine new discovery contract tests failed because the module did not yet exist;
- implementation GREEN `36298061406` @ `5ba10d6417d91e5b323ba8be8ab580ee19e38375`;
- pre-PR/docs GREEN `36298106727` @ `af8b9ec885a1a0e1e84f4cd5c28f149d8e5a52ae`;
- review-hardening RED `36298263353` @ `72af30c894e4cdd61765c5138c35b84c5437a0d2` exposed unsupported uppercase Work Status acceptance;
- current-head push GREEN `36298315601` and PR-trigger exact-head GREEN `36298318545` @ `1861ca73f58d8c76e67db06474e2446c78807896`;
- formal Review Provenance v2 `5329032900`: PASS;
- PR #29 merge `d03ac20cbf6e00992f9902239a0515eba4dc0b70`;
- post-merge Verify `36298411969`: PASS;
- Issue #3 remained unchanged at `updated_at=2026-09-27T03:30:40Z` with `claims: {}`.

## Review / identity boundary

- `worker_id` is runtime coordination metadata, not a cryptographic identity or GitHub security principal;
- runtime same-worker role checks are separate from formal code-review provenance;
- formal Review requirements are governed by devflow policy.

## Known limitations / deferred protocol surface

Not yet implemented:
- GitHub-wide/frontier source selection: current durable discovery requires exact Issue references from the caller;
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

Issue #28 / PR #29 is accepted at the feature layer. After this Current State reconciliation is merged, no repository-local implementation slice should remain active until a new Issue is selected. The next smallest bounded gap is a composed read-only exact-reference path combining durable discovery with existing `list_claimable`; GitHub-wide source selection, ranking, capability matching, scheduling, automatic claim submission, controller negotiation, and repo-monitor projection remain separate later slices.
