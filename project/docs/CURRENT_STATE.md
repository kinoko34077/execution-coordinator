# Current State

## Repository state

`V0.1 + PHASE 3 + RECONCILIATION DEMAND ADOPTION + MANAGED FRONTIER + DETERMINISTIC RANKING + CAPABILITY MATCHING / ACCEPTED`

`execution-coordinator` is the runtime implementation boundary for devflow Execution Coordination Protocol v1. Durable task truth remains in devflow and owning repository Issues/PRs; this repository owns only short-lived execution coordination plus read-only discovery/runtime/eligibility projections.

The accepted implementation baseline through Issue #49 / PR #51 is `a0def11e8089139d933f1a405a8dabae46a28678`. The moving repository Audit SHA is owned by devflow Control #107 so this document does not self-reference later documentation-only reconciliation merges.

Cross-repository authority:
- devflow Work Order #105 owns the broader multi-agent execution-coordination objective and explicit release of bounded downstream slices;
- devflow Issue #106 / merged PR #108 owns Protocol v1 foundation semantics;
- devflow Issue #125 / PR #132 owns the accepted hash-bound durable-candidate source contract;
- devflow Issues #155/#159 own the accepted Development Reconciliation Loop and `development-reconciliation-work.v1` publication contract;
- devflow Repository Control #107 is the cross-repository summary/index for this repository;
- Issue #28 / PR #32 own trusted Repository Control durable-candidate discovery/normalization;
- Issue #44 / PR #45 own the additive runtime claimability-reason projection;
- Issue #48 / PR #50 own runtime `GitHubStateStore` observed-authority identity hardening;
- Issue #49 / PR #51 own accepted reviewer/recovery reconciliation-demand adoption;
- Issue #3 `[SYSTEM] Execution Coordination State` is runtime current state only.

Runtime Issue #3 currently has no active claims. It retains historical generation/idempotency/audit evidence only.

## Accepted runtime behavior

### Claim / lease state machine

Runtime roles are:

```text
implementer
reviewer
recovery
verifier
integrator
```

Current authority rules include:
- exclusive same task+role ownership;
- compatible independent role claims may coexist;
- `recovery` is successor execution authority and cannot race another live non-reviewer role on the same task;
- reviewer authority may coexist with execution authority for observation/review, but one logical worker cannot simultaneously hold reviewer authority and either implementer or recovery execution authority for the same task;
- logical conflict-key enforcement remains separate from same-task role compatibility;
- monotonic task/role generation fencing;
- default 15-minute lease with separate heartbeat and progress timestamps;
- canonical live states `CLAIMED`, `RUNNING`, and lease-bound `WAITING`;
- `resume` transitions a current unexpired `WAITING` claim back to `RUNNING` and clears wait metadata; it does not extend or reset `lease_until`, so the resumed claim continues under its existing unexpired lease;
- `renew` remains the explicit lease-extension operation; callers must not treat `resume` as an implicit renewal;
- stale generations fail closed;
- expired ownership is removed by serialized `expire`, followed by a later higher-generation `takeover`/`claim`;
- retry-safe idempotency with mismatched-payload rejection;
- release/failure/expiry remove live authority without marking durable work complete.

### Snapshot and state storage

Issue #3 stores one schema-v1 JSON snapshot between versioned markers.

Validation fails closed on malformed JSON, unsupported schema, marker corruption, task/role duplication, generation inconsistency, incompatible same-task role ownership, incompatible conflict-key ownership, same-worker independent-review overlap, invalid WAITING metadata, or oversized idempotency retention.

`Role.RECOVERY` is an additive runtime role introduced by the explicitly released #49 reconciliation-adoption slice. Snapshot schema ownership remains in this repository; publication of recovery demand remains a separate devflow authority.

Idempotency retention is bounded to 128 records. High-frequency non-event records are evicted before lifecycle records where possible; evicted keys are not permanently deduplicated.

The Issue body PATCH is the runtime mutation authority commit point. Lifecycle comments are secondary audit evidence; heartbeat/progress churn does not append comments.

### Serialized mutation surface

Supported authority-changing operations remain:

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

The #49 adoption adds no second mutation lane, queue, database, assignment surface, or provider launcher.

Issue #33 completed the bounded real queue/renew pilot on 2026-09-28. Under an unsaturated lane, an isolated `renew` committed the authoritative Issue #3 snapshot about 7.6–8 seconds after workflow-run creation. A 110-dispatch saturation burst was HTTP-accepted in full, while the global `queue: max` boundary admitted one executing run plus up to 100 pending runs; 9 overflow runs terminated as `failure` before any job was created. Dispatch/run creation therefore remains transport evidence only, not mutation success. Under saturation, renewal continuity is not guaranteed: a worker may be interrupted by fencing/lease expiry, but no uncommitted renew extends authority.

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
- `renew` is authoritative only when the gateway returns the same live claim from committed runtime state; dispatch acceptance alone is insufficient;
- transport/runtime failure during continuation operations fences local authority when the remote state cannot be safely established;
- release/fail terminal responses must remove the claim before local authority is cleared;
- safe long waits should normally release rather than consume a lease; retained WAITING remains lease-bound.

The adapter is not a scheduler and does not independently select or rank durable work.

## Accepted read-only projections

### `get_state`

Issue #22 / PR #23 provides the validated current-state read path:

```text
GitHub Issue #3
-> GitHubStateStore.load_body()
-> parse_issue_body()
-> CoordinatorState
```

`GitHubStateStore` now also fails closed when the observed GitHub repository/Issue identity cannot be proven exactly across redirects or mutation preflight/response boundaries. It performs no authority mutation through the read path.

### Durable candidate discovery / normalization

Issue #28 / PR #32 provides the accepted read-only adapter for the devflow #125/#132 durable-candidate source contract.

Input is the exact trusted Repository Control identity returned by live bootstrap. The adapter reads one explicit `DEVFLOW_EXECUTION_CANDIDATES_V1` block, validates its task envelopes and role entries, then fetches only exact owning task Issues referenced by the projection.

Validation is fail-closed for:
- trusted author association on the Control, owning task and optional Work Order;
- exact Control/source/repository/task/entry identity;
- Control `Repository State=ACTIVE` and current `[USER_DECISION]` / `[HUMAN_GATE]` vetoes;
- exact owning-body SHA-256 freshness;
- duplicate JSON keys, duplicate task envelopes/roles, unknown fields and malformed/partial markers;
- exact role/status/action combinations and explicit conflict-key forms;
- open Issue and optional Work Order provenance requirements.

The deprecated singular owning-Issue marker is ignored and is never a fallback authority. Discovery remains GET-only and does not rank, schedule, publish projections, submit claims, mutate runtime Issue #3, or infer free-form work.

### `list_claimable` / claimability reason projection

The accepted claimability surface filters normalized `ClaimCandidate` values against current `CoordinatorState` without mutating authority.

A candidate is excluded when:
- durable scope/acceptance is not ready;
- a durable blocker or user-confirmation boundary is explicit;
- no current canonical entry reference exists;
- current runtime ownership has the same task/role or an incompatible same-task role relationship;
- an incompatible active conflict key exists;
- the supplied worker identity would violate independent-review worker separation.

Issue #44 / PR #45 additionally exposes read-only reasons:

```text
CLAIMABLE
BLOCKED_LIVE
EXPIRED_UNSWEPT
```

Expired-but-unswept claims remain authoritative blockers until the serialized `expire` mutation commits; the read-only projection never expires them itself.

### Composed discovery / state / claimability read

Issue #58 provides the bounded Phase 0 read-only composition required by devflow Work Order #175:

```text
explicit trusted Repository Control sources
-> discover_claim_candidates()
-> get_state_result()
-> project_claimability() / list_claimable()
```

`compose_claimability_read()` preserves per-source discovery failures, validated runtime-state freshness metadata, claimability reasons and the existing claimable tuple. It does not rank, select, claim, publish, mutate Issue #3 or infer work from non-canonical sources.

### Managed repository frontier enumeration

Issue #60 provides the bounded Phase 1 read-only enumeration surface released
by devflow Work Order #175. `enumerate_managed_frontier()` accepts only the
exact `DurableIssueSource` identities supplied by live devflow bootstrap,
normalizes them deterministically, and fails closed on invalid or duplicate
managed-repository Control identities.

The enumerator reads each exact Control/task Issue through one cached snapshot,
combines the existing normal durable-candidate and reconciliation-publication
validators, then reuses the Phase 0 state/claimability projection. Its result
retains source inventory, source-input failures, discovery failures, runtime
freshness, claimability reasons, and separate fresh versus `Role.RECOVERY`
candidate views. It performs no ranking, capability matching, selection,
claim/lease mutation, publication, scheduling, provider/controller work, or
runtime Issue #3 mutation.

### Dependency/frontier filtering and deterministic ranking

Issue #62 provides the bounded Phase 2 pure ranking projection released by
devflow Work Order #175. `rank_managed_frontier()` consumes the accepted
managed-frontier result plus an explicit, versioned `RankingMetadata` record
for each candidate. Metadata is bound to the enumerated Control source and a
stable candidate fingerprint; missing, stale, malformed, untrusted, or
dependency-not-ready evidence is retained as omission evidence.

Hard filters run before ordering: durable eligibility, current
`CLAIMABLE` runtime reason, explicit dependency readiness, metadata freshness,
and exact provenance. Eligible fresh candidates are ordered by Control
priority, optional urgency, dependency-frontier order, explicit readiness
class, canonical UTC `ready_at`, and task/role tie identity. Recovery demand
is returned separately and is never folded into fresh ranking. This remains a
read-only projection with no claim, scheduler, provider, controller or Issue
#3 mutation.

### Exact capability/environment matching

Issue #64 provides the bounded Phase 3 pure matching projection released by
devflow Work Order #175. `WorkerProfile` and `CandidateRequirements` are
versioned, UTC-freshness-bounded, provenance-bound evidence types. The matcher
rechecks ranking freshness and candidate fingerprints, requires the
requirements source to equal the ranked Control provenance, and accepts a
candidate only when both required capability and required environment tag sets
are exact subsets of one worker profile. Tags are opaque exact values: no
aliasing, normalization, or capability inference is performed.

Unknown, malformed, future, stale, duplicate, or mismatched evidence becomes
worker-local omission evidence. Fresh candidates retain rank order;
`Role.RECOVERY` candidates remain a separate output track. Existing ranking
omissions, source failures, and discovery failures are preserved for every
worker, so one worker's stale or incompatible profile cannot suppress another
worker's independent projection. The surface performs no selection, claim or
lease mutation, scheduling, provider/controller work, repo-monitor work, or
Issue #3 mutation.

### Development Reconciliation demand adoption

Issue #49 / PR #51 consumes the accepted devflow #159 `development-reconciliation-work.v1` projection from the trusted Repository Control.

Authority chain:

```text
owning Issue / Work Order durable truth
-> accepted devflow Development Reconciler disposition
-> development-reconciliation-work.v1 demand publication
-> trusted Repository Control projection
-> execution-coordinator read-only validation/adoption
-> existing ClaimCandidate / claimability surface
-> existing serialized runtime claim/lease/generation authority
```

The consumer supports only roles currently emitted by the accepted upstream contract:
- `reviewer` -> `Role.REVIEWER`;
- `recovery` -> `Role.RECOVERY`.

`integrator` is not accepted from this projection unless a later upstream contract explicitly supports/publishes it.

The consumer fails closed on:
- missing/duplicate/reversed publication markers;
- unsupported schema or unknown/missing projection/publication fields;
- duplicate JSON keys, publication IDs, or `(task, role)` demand;
- non-devflow/untrusted/wrong Control source identity;
- mismatched canonical Control title, full `Repository` identity, or non-`ACTIVE` Repository State;
- current Control `[USER_DECISION]` / `[HUMAN_GATE]` vetoes;
- task/entry repository or Issue identity mismatch;
- stale owning-task body digest;
- malformed publication ID or logical publication-ID mismatch;
- unsupported role/disposition combinations;
- malformed reviewer exact-PR/head context;
- malformed recovery predecessor/checkpoint/next-action/transition/artifact context;
- publication confirmation requirements or malformed freshness evidence.

Accepted publication is still demand only. Consumption does not choose a provider/worker, create a claim/lease, mark durable work complete, mutate the owning Issue/PR, or bypass the existing serialized authority lane.

Recovery authority is intentionally constrained:
- no recovery claim may race another live non-reviewer role on the same task;
- a reviewer may coexist with recovery, but the same worker cannot satisfy independent reviewer authority while holding recovery execution authority;
- these invariants are checked consistently in read-only claimability, claim mutation, and snapshot restoration.

## Accepted verification evidence

### Runtime / lifecycle foundation

- Protocol v1 devflow PR #108 merge `c0d44e809a835f30263d87fdb2baa62ecddfd4bd`;
- runtime v0.1 PR #2 merge `ed5ed58fab79c161cacdbdb9b7dfd421209bec6f`;
- AgentSession bootstrap PR #6 merge `1210b506ad1565159d0fb5d934eca66ca166542a`;
- acknowledge lifecycle PR #13 merge `66bc5bad0176181f72b67e08dd89af349155c874`;
- complete AgentSession lifecycle PR #17 merge `10f47ffaf546b1f4b7108bde50779b28a17937f4`;
- wait/resume transport-fencing PR #21 merge `6321e4051a0650f5448d1acff87537bf19064002`.

### Read-only query/discovery foundation

- `get_state` PR #23 merge `0cf37f3c288b872055757aaa8c63d614b03d4f70`, post-merge Verify `36293581654` PASS;
- `list_claimable` PR #26 merge `299d2577d6cbef5eb87a88188996e5d9daf1385d`, post-merge Verify `36297162959` PASS;
- durable discovery PR #32 merge `8eaa41ab819693e49d3407b865ef0b5319c3690f`, pre-merge Verify `36311759618` SUCCESS, Formal Review v2 `5329886612` PASS;
- claimability reasons PR #45 merge `7289469e294ee726b44ed9aa677976bd42508597`;
- durable discovery observed-identity repair PR #47 merge `3a783945614ae99cbc26836b661b55e3909c8ec4`, exact-head Verify `36354885619`, re-audit Formal Review v2 `5335071059`;
- runtime state-store identity repair PR #50 merge `a58dfe820fb0a4c953b9fda43b5fe05cfae9e9bd`, post-merge Verify `36389834403` SUCCESS.

### Development Reconciliation adoption Issue #49 / PR #51

Upstream authority:
- devflow #159 / PR #170 merge `5779de4bf3192cd6f6900e2830b4d3aece2b739a`;
- devflow #105 explicitly released only the bounded #49 runtime-adoption slice;
- devflow Control #107 audited implementation base `a58dfe820fb0a4c953b9fda43b5fe05cfae9e9bd`.

Implementation/review evidence:
- initial adapter RED Verify `36391230645`;
- recovery snapshot-invariant RED `36392092209` -> GREEN `36395683175`;
- canonical Control-title RED `36395989427` -> GREEN `36396246475`;
- live Control Repository/State/Human-gate RED `36396579709` -> GREEN `36396839443`;
- recovery/reviewer worker-separation RED `36397271216`;
- final exact-head push Verify `36397350063` SUCCESS;
- final exact-head PR Verify `36397354372` SUCCESS;
- Formal Review v2 `5335888443`: no blocking findings;
- PR #51 squash merge `a0def11e8089139d933f1a405a8dabae46a28678`;
- post-merge Verify `36397698631`: SUCCESS.

### Issue #33 residual resolution / queue pilot

- canonical Protocol v1 resolution: `resume` preserves the existing unexpired `lease_until`; explicit `renew` remains the only lease-extension operation;
- no production runtime code change was required for that decision because accepted Protocol v1 and current `engine.resume()` already agree;
- disposable pilot claim `clm_8444762b9d4b5b9ba9c2cd3449adfc53@1` was created, acknowledged, renewed, and finally released through the serialized main workflow;
- isolated renew run `36401699306`: workflow created `2026-09-28T09:09:26Z`, committed heartbeat `2026-09-28T09:09:33.594167Z`, approximately 7.6–8 seconds to authoritative snapshot commit;
- 110-dispatch `queue: max` burst: all dispatch calls returned HTTP 204; 100 queue slots were admitted behind the serialized lane while 9 overflow runs failed before any job was created; representative overflow run `36402267910` had `jobs=[]`;
- final burst outcomes after bounded cleanup: 11 idempotent-success/replay runs, 90 cleanup cancellations, 9 pre-job overflow failures;
- serialized release run `36402770620` succeeded and final Issue #3 readback returned `claims: {}`.

## Review / identity boundary

- `worker_id` is runtime coordination metadata, not cryptographic identity or a GitHub security principal;
- reviewer worker-separation checks prevent one runtime worker from carrying implementer/recovery execution authority and independent reviewer authority for the same task;
- formal code-review provenance remains a separate devflow Review Provenance v2 concern;
- multiple agent surfaces may share one GitHub actor, so GitHub actor identity alone is not proof of reviewer independence.

## Known limitations / deferred protocol surface

Not implemented or not released by #49:
- GitHub-wide/frontier source selection beyond exact trusted Control inputs;
- arbitrary free-form Issue/Work Order interpretation;
- autonomous self-selection over the accepted priority/dependency frontier;
- an autonomous discovery -> state -> claimability -> claim loop with ranking, selection or claim mutation; the Phase 0 composition and Phase 1 managed-frontier enumeration remain read-only surfaces owned by Issues #58 and #60;
- full `claim(..., expected_state, idempotency_key)` / structured failure-evidence conformance;
- controller priority/capability/availability negotiation;
- provider-specific worker selection/launch;
- read-only repo-monitor reconciliation UI;
- bounded self-scheduling/work stealing;
- Manual Execution Session stale-detection policy changes;
- atomic sweep-plus-takeover;
- fine-grained mutation lanes or external state storage.

Current v0.1 also uses bounded idempotency retention, one coarse global mutation queue, and stderr/process-exit failure reporting rather than a structured failure envelope. The fixed `queue: max` saturation boundary is an availability limit: deep backlog can outlast a lease, so the normal 5-minute renewal target is not a saturation guarantee; authority still fails closed at the last committed lease.

## Safety / authority boundary

- durable requirements and completion truth remain in devflow / owning repository Issues and PRs;
- `DurableIssueSource`, `ClaimCandidate`, and reconciliation publications are derived input/projection only and never replace durable GitHub truth;
- Issue #3 owns ephemeral runtime execution state only;
- devflow MCP remains read-only;
- repo-monitor remains observer-only;
- no discovery/publication result, execution claim, or eligibility projection overrides release/deploy/publication/credential/permission/destructive/user-decision confirmation boundaries;
- #49 acceptance does not release any ranking/scheduler/provider/controller/repo-monitor slice.

## Next action

Issue #49 / PR #51 remains accepted at the implementation layer. Issue #33's two residuals are now resolved: `resume`/lease semantics were confirmed against Protocol v1 without code churn, and the bounded real queue/renew pilot completed with Issue #3 restored to no active claims.

Phase 0 Issue #58 / PR #59 is accepted at merged main `2bea4c5ee1ae4c85e6a54b3c095f9d6871f633aa`. Phase 1 Issue #60 / PR #61 is accepted at merged main `9b4d12f262defe4dcab49dbf607d98bea476807e`; its source-input and discovery evidence remain fail-closed and runtime Issue #3 remains unchanged. Phase 2 Issue #62 adds explicit dependency/frontier hard filtering and deterministic ranking, and Phase 3 Issue #64 adds only worker-local exact capability/environment matching on top of that read-only frontier. Neither slice selects, claims, schedules, launches providers/controllers, or mutates Issue #3. The next execution-coordination slice must still be selected explicitly by devflow Work Order #105; self-selection, scheduler/work stealing, automatic claim, controller negotiation, provider launch/selection, repo-monitor work, Manual Session policy changes, and finer mutation-lane architecture remain separately gated.
