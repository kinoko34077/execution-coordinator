# Current State

## Repository state

`V0.1 + PHASE 5 + RECONCILIATION DEMAND ADOPTION + MANAGED FRONTIER + DETERMINISTIC RANKING + CAPABILITY MATCHING + PORTFOLIO PICKUP V2 + BOUNDED AUTONOMOUS CYCLE + CONTROLLER-FIRST ACTIONS AUTO-LAUNCH V1 + STAGE 1 WORK-CLASS RUNTIME PROPAGATION + ACTIONS CHAT PICKUP TRANSPORT + MANUAL PICKUP EXPIRY SELF-HEAL + REVIEWER PROVENANCE PICKUP ELIGIBILITY + PROVIDER-INDEPENDENT REQUEST BOUNDARY + CODEX APP-SERVER STAGE-C TRANSPORT + STAGE-D REVIEWER-ROLE AUTO-LAUNCH / ACCEPTED`

`execution-coordinator` is the runtime implementation boundary for devflow Execution Coordination Protocol v1. Durable task truth remains in devflow and owning repository Issues/PRs; this repository owns only short-lived execution coordination plus read-only discovery/runtime/eligibility projections.

The accepted runtime implementation now includes Stage-D reviewer-role auto-launch through Issue #121 / merged PR #122 on main `a7044cd4098aa1d8f8f385f7250d626b7b61c41a`. The moving repository Audit SHA is owned by devflow Control #107 so this document does not self-reference later documentation-only reconciliation merges.

Cross-repository authority:
- devflow Work Order #105 owns the broader multi-agent execution-coordination objective and explicit release of bounded downstream slices;
- devflow Issue #106 / merged PR #108 owns Protocol v1 foundation semantics;
- devflow Issue #125 / PR #132 owns the accepted hash-bound durable-candidate source contract;
- devflow Issues #155/#159 own the accepted Development Reconciliation Loop and `development-reconciliation-work.v1` publication contract;
- devflow #208 / merged PR #220 own the accepted portfolio-scope broad-pickup v2 companion-metadata and null-target classification contract;
- devflow #223 owns the accepted controller-first GitHub Actions auto-launch v1 design/plan;
- devflow #211 / merged PR #284 own the accepted reviewer-provenance pickup-eligibility contract;
- devflow Repository Control #107 is the cross-repository summary/index for this repository;
- Issue #28 / PR #32 own trusted Repository Control durable-candidate discovery/normalization;
- Issue #44 / PR #45 own the additive runtime claimability-reason projection;
- Issue #48 / PR #50 own runtime `GitHubStateStore` observed-authority identity hardening;
- Issue #49 / PR #51 own accepted reviewer/recovery reconciliation-demand adoption;
- Issue #85 owns the portfolio-v2 runtime consumer/bootstrap implementation;
- Issue #66 / PR #67 own the bounded agent-first one-claim autonomous cycle;
- Issue #68 owns the provider-independent execution-request / worker-dispatch boundary;
- Issue #87 / merged PR #88 own the accepted controller-first Actions auto-launch v1 runtime implementation;
- Issue #89 / merged PR #90 own the accepted Stage 1 work-class runtime propagation;
- Issue #94 / merged PR #95 own accepted Issue-comment work-class transport parity;
- Issue #97 / merged PR #98 own accepted Issue-comment reachability for the portfolio/null-target pickup path;
- Issue #101 / merged PR #102 own accepted executable manual-pickup expired-claim self-heal using the existing serialized `expire` authority;
- Issue #105 / merged PR #106 own accepted end-to-end reviewer-provenance propagation through repository and portfolio pickup;
- Issue #113 / merged PR #114 own the accepted multi-track portfolio eligibility repair: overall Control `Work Status=BLOCKED` is not a repository-wide veto when an explicit candidate is independently unblocked and repository-/Control-wide hard gates remain clear;
- Issue #110 / merged PR #119 own the accepted Codex app-server Stage-C provider transport using caller-supplied `ACCESS_TOKEN` through the inherited environment boundary; OAuth/token acquisition/storage/rotation and provider E2E remain outside this adapter;
- Issue #116 is completed inside PR #119: subprocess stdout is explicitly released after child termination and bounded reader join, with deterministic real-subprocess coverage;
- Issue #121 / merged PR #122 own accepted Stage-D reviewer-role auto-launch: exact structured reviewer PR/head context, live PR/head revalidation before claim, reviewer-only read credential/tooling, no implementer integration path, and pre-offer rejection of reviewers lacking structured context;
- Issue #3 `[SYSTEM] Execution Coordination State` is runtime current state only.

Runtime Issue #3 is volatile live authority. Its current claim set must be re-read from the Issue before any consumption or mutation and is not frozen into this document.

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

### Portfolio-scope broad pickup v2 runtime

Issue #85 implements the accepted devflow #208 / PR #220 portfolio contract
on top of the existing discovery, ranking, capability and claim authorities.
Repository-scoped broad pickup remains compatible with the accepted v1 path.
For `target_repository = null`, the bootstrap reads the eligible managed
Repository Controls as one pinned Control snapshot, enumerates the managed
frontier, and requires a fresh `DEVFLOW_EXECUTION_PORTFOLIO_METADATA_V1`
companion entry for every ordinary fresh v1 candidate.

The companion consumer fails closed on malformed/duplicate/reversed markers,
unknown fields, Control source/repository/priority mismatch, duplicate
`(task, role)` entries, stale/future evidence, task-body digest mismatch, and
candidate-fingerprint mismatch. Valid entries are converted into the existing
`RankingMetadata` and `CandidateRequirements` types; no ranking, capability or
environment value is inferred from Issue prose, Projects, provider/model
identity, branch activity or chat history.

Portfolio candidate readiness is passed through the existing
`rank_managed_frontier()` hard filters, including runtime claimability,
dependency readiness and future `ready_at`. The runtime projects a portable
rank-class key that excludes canonical task/role tie identity so the accepted
devflow classifier can apply worker-scoped deterministic spread only after
fixing the best track and rank class. Exact requirement tags are projected
from the validated `CandidateRequirements`; recovery demand remains separate
and does not require ordinary portfolio metadata.

The existing `execute_selection()` remains the only mutation bridge for this
bootstrap path: one selected candidate is re-observed by task/role/fingerprint,
then exactly one serialized claim is attempted and acknowledged. Claim
rejection ends the cycle; there is no fallback to candidate 2. The command-line
bootstrap accepts omitted `--target` for portfolio scope. The accepted Issue-comment transport now mirrors that reachability: explicit `target:` preserves repository scope, while omitting `target:` maps to `target_repository=None` (Issue #97 / merged PR #98). No provider launcher,
controller negotiation, work stealing, daemon scheduler or second assignment
authority is introduced.

### Manual pickup expired-claim self-heal

Issue #101 / merged PR #102 close the availability gap where a lease-expired
claim could remain `EXPIRED_UNSWEPT` and block a conflicting manual pickup until
an unrelated scheduled controller maintenance run eventually executed.

For executable pickup only (`gateway_factory` present), `run_pickup()` now reads
current runtime authority before evidence classification. If at least one claim
has `lease_until <= now`, it dispatches the already-accepted globally serialized
`expire` mutation with the current execution-attempt idempotency key. Only after
that mutation returns does normal evidence gathering run, so claimability is
re-read from the committed post-sweep snapshot.

Boundaries remain unchanged:
- a state with no expired claim dispatches no extra expiry mutation;
- live claims are never expired early; the existing server-side `expire` operation remains final authority;
- read-only pickup without a mutation gateway remains GET-only;
- expiry transport/commit failure propagates before classification and fails closed;
- lease duration, conflict semantics, release semantics, provider routing and durable task authority are unchanged.

Accepted evidence:
- RED head `0157c78a460bbfff79d8e858521a75653c94a2e5` / Verify `36791474092`;
- GREEN exact PR head `e105efddaa26e9c58c5dcfbfecca49c488a2cedb` / Verify `36791560180`;
- Formal Review `5373163544`, blocking findings none;
- squash merge `9a7b7bf0e06a2d830d8ad1bef229e17d8290c0b1`;
- post-main Verify `36791704211` SUCCESS;
- merged-main pickup smoke dispatched expire run `36791763888` SUCCESS, removed the expired devflow#211 claim while preserving the live #101 claim, then classified the re-read state as `NO_CANDIDATES_PUBLISHED`;
- #101 release mutation `36791826011` SUCCESS; final runtime Issue #3 readback returned `claims: {}`.

### Reviewer-provenance pickup eligibility

Issue #105 / merged PR #106 implement the accepted devflow #211 reviewer-provenance eligibility contract through the real execution-coordinator pickup runtime.

The accepted runtime boundary:
- accepts optional direct pickup `review_provenance: {system, model}` evidence and never infers it from `worker_system`, provider name, GitHub actor, or runtime session identity;
- projects candidate `different_reviewer_requirement: {implementer_system, implementer_model}` only from accepted structured repository/portfolio evidence;
- preserves ordinary reviewer pickup behavior when no explicit different-reviewer requirement exists;
- fails closed when an explicit different-reviewer candidate has missing, malformed, or ambiguous reviewer provenance;
- omits a same-signature reviewer as `REVIEWER_INDEPENDENCE_CONFLICT`;
- keeps a differing accepted reviewer signature eligible when every other gate passes;
- keeps exact-head submitted Review freshness in devflow merge/readiness authority rather than treating pickup eligibility as proof that Review completion occurred.

Accepted evidence:
- pre-fix live cycle 5 reproduced the unsupported direct field as `REQUEST_INVALID`; no claim formed;
- RED head `0f951723eeee173e4691bc5438d1f7ab595891b8` / Verify `36798849087` failed only on the new #105 expectations;
- GREEN reviewed head `ecedded0d7b9933b1ea79688e7092bb782b63d66` / exact-head Verify `36799188340` SUCCESS;
- Formal Review `5373746812`, blocking findings none;
- expected-head squash merge -> `3437f6fa3abe991d89d1168372cc10b4f4c73036`; post-main Verify `36799401103` SUCCESS;
- canary cycle 6: missing direct signature -> `NEEDS_EVIDENCE / EVIDENCE_INVALID`, no claim;
- canary cycle 7: same `ChatGPT / GPT-5.6 Sol` signature -> `REVIEWER_INDEPENDENCE_CONFLICT`, no claim;
- canary cycle 8: direct `Human / unknown` signature -> `REVIEW_WORK / ELIGIBLE_REVIEW_DEMAND`, reviewer claim formed and acknowledged;
- serialized release `36799961806` succeeded; final runtime Issue #3 readback returned `claims: {}`.
### Agent-first bounded autonomous cycle

Issue #66 provides the bounded `run_autonomous_cycle()` execution boundary
released by devflow Work Order #175. It consumes one already refreshed,
worker-scoped `CapabilityMatchResult`, selects only its first deterministic
eligible match, and submits at most one existing serialized claim. A claim
rejection ends that cycle without trying a second candidate. A successful
claim must be acknowledged before the caller's work callback runs, and the
existing `AgentSession` lifecycle performs fencing and terminal release (or
release-on-work-failure). The cycle returns typed `NO_CANDIDATE`,
`CLAIM_REJECTED`, or `COMPLETED` results.

The cycle does not refresh discovery/state/capability evidence, rank or
schedule work, retry a rejected candidate, publish durable work, launch a
provider/controller, monitor repositories, or add a second runtime authority.
Issue #3 remains the only claim/lease authority, and callers must provide a
fresh read result before starting a cycle.

### Provider-independent execution-request / worker-dispatch boundary

Issue #68 provides the typed `ExecutionRequest` / `DispatchOutcome` baseline
released by devflow Work Order #175. The legacy `execution-request.v1` path
remains bound to an already acknowledged `RUNNING` claim. Issue #87 / merged
PR #88 add the versioned auto-launch path, which may construct an exact
candidate-bound request from current `CLAIMED` authority so that provider
context can be established before `acknowledge -> RUNNING`. Both paths retain
exact task/role/fingerprint/source/freshness and capability/environment
binding; neither request creates ownership outside the serialized claim lane.

The adapter protocol distinguishes `LAUNCH_ACCEPTED`, `LAUNCH_UNAVAILABLE`,
`LAUNCH_FAILED`, and the auto-launch `LAUNCH_AMBIGUOUS` disposition. Accepted
starts must return the requested worker and session identity. Unavailable,
failed, or ambiguous starts carry explicit reconciliation evidence for the
existing claim; ambiguous launch is handled by the v1 lease-bound
`WAITING:PROVIDER` path described below. Stale requests, mismatched outcomes
and unexpected transport failures fail closed; the request/dispatch boundary
itself adds no second scheduling or ownership authority.

### Controller-first GitHub Actions auto-launch v1

Issue #87 / merged PR #88 implement the accepted devflow #223 controller-first
auto-launch design on main `1feb8a03983f6fa47b2b0051d8e30f927c0d14ef`.
The controller wakes by schedule or manual `workflow_dispatch`, reuses the
accepted portfolio ranking/capability evidence, and emits at most one bounded
offer per cycle. ACCEPT/DECLINE/DEFER remains advisory until the existing
serialized claim succeeds; no second queue or ownership authority is added.

For normal auto-launch, one accepted offer is revalidated and claimed into
`CLAIMED`; the versioned auto-launch request then establishes one Claude Code
provider context before the same current claim is acknowledged to `RUNNING`.
Unavailable launch releases, definite failed launch fails, and ambiguous launch
enters lease-bound `WAITING:PROVIDER`; confirmed-live recovery uses `resume`.
Stale generation/freshness/conflict/Human gates remain fail-closed.

The v1 provider boundary is Claude/Claude Code with GitHub OIDC direction.
Claude receives repository file tools only; target GitHub write operations use
a separately scoped GitHub App token and deterministic workflow steps outside
Claude. Credential, federation-rule, service-account, secret/variable and
permission creation/change remain Human-gated. Ordinary ChatGPT remains on the
manual-start #190/#202 path; Codex and event-driven wake remain deferred.

Final PR-head verification passed 275 tests plus compile, a mandatory
different-reviewer Claude Code review reported zero blocking findings on the
exact final head, and post-main Verify run 36550128045 succeeded. A real manual
controller run 36550418568 also succeeded through stale-claim expiry and offer
preparation, returning `has_offer: false` with no claim or repository mutation.
Runtime Issue #3 remained `claims: {}`.

The external GitHub App / Claude OIDC identity configuration is Human-reported complete as of 2026-10-07. Credential values remain Human-owned and are not recorded here. A real Claude/OIDC provider-backed E2E has still not been accepted or executed as part of Stage D; it remains a separate security/environment gate and must not be inferred from configuration presence alone. Adoption remains `PILOT`, and devflow #188/#105 PARK is not released solely by controller/provider setup.

### Stage 1 work-class runtime propagation

Issue #89 / merged PR #90 establish the accepted devflow #215 Stage 1 runtime contract.

The accepted runtime boundary:
- accepts optional closed-vocabulary `work_class` in `DEVFLOW_EXECUTION_PORTFOLIO_METADATA_V1` companion entries;
- preserves omitted `work_class` as backward-compatible legacy evidence;
- projects explicit portfolio `work_class` into `chat-worker-bootstrap-evidence.v1` candidates;
- keeps repository-scoped v1 candidates without explicit metadata unchanged so the devflow classifier applies its accepted conservative role fallback;
- threads optional `accepted_work_classes` through manual-start `run_pickup()` / CLI into the accepted devflow classifier contract;
- fails closed on canonical work-class vocabulary drift when the loaded devflow tools expose the vocabulary;
- preserves existing freshness, capability/environment, ranking, claim, no-fallthrough, provider-launch, and Issue #3 authority boundaries.

Issue #94 / merged PR #95 extend only the Issue-comment transport surface for that accepted contract:
- optional `work_class:` values are parsed as comma-separated entries, whitespace-trimmed, and forwarded in declared order to `run_pickup(... accepted_work_classes=...)`;
- omission preserves `accepted_work_classes=None` and legacy unconstrained behavior;
- canonical vocabulary and semantic validation remain in devflow;
- no discovery, ranking, claim/lease, provider-launch, scheduler, or second-taxonomy semantics were added.

Accepted #94/#95 evidence: reviewed head `77822567e9c3394e62ecd2acaea6d2f5a7e8aafc`; PR-head Verify `36663015030` SUCCESS; Formal Review v2 `5361037472` with zero blocking findings; merge advanced main to `d097cd3f2cac791f5118165df681537431d0b0d2`; post-main Verify `36663126842` SUCCESS.

Issue #97 / merged PR #98 then expose the already-accepted portfolio/null-target path through the same Issue-comment transport. `target:` is optional for `/pickup`; explicit values preserve repository scope and omission maps to `target_repository=None`. `worker_system` remains required and `/release` grammar is unchanged. Accepted #97/#98 evidence: reviewed head `d115131ba958a69a7f590ad130dc24257a7252af`; exact-head Verify `36664746320` SUCCESS; clean current-head Formal Review; merge advanced main to `c8e1c7972dca8f8a0c6065815152da8ef72f5989`; post-main Verify `36664825599` SUCCESS.

Stage 1 transport acceptance did not itself release later maintenance stages. Those later rollout stages have since advanced independently: devflow Stage 2 is accepted through S2.8, the first bounded Stage-3 rotation is accepted at 3/3, and Stage 4 richer scale/fairness controls were evaluated as `NOT_NEEDED`. The real Claude/OIDC provider E2E Human Gate and `PILOT` / devflow #188/#105 PARK boundaries remain unchanged.

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
- direct reviewer provenance used by #105 is explicit eligibility evidence and is not inferred from runtime transport identity;
- formal code-review provenance and exact-head Review freshness remain separate devflow Review Provenance v2 / merge-readiness concerns;
- multiple agent surfaces may share one GitHub actor, so GitHub actor identity alone is not proof of reviewer independence.

## Known limitations / deferred protocol surface

Not implemented or not released by #68:
- GitHub-wide/frontier source selection beyond exact trusted Control inputs;
- arbitrary free-form Issue/Work Order interpretation;
- autonomous refresh and self-selection over the full priority/dependency/capability frontier;
- an autonomous discovery -> state -> claimability -> capability-refresh loop; Phases 4–5 consume caller-supplied fresh evidence and keep selection/dispatch bounded;
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
- #49, #66 and #68 acceptance does not release any scheduler/provider/controller/repo-monitor slice;
- #87/#88 controller-first acceptance, #89/#90 Stage 1 runtime acceptance, #94/#95/#97/#98 transport acceptance, and #105/#106 reviewer-provenance acceptance do not release real provider identity configuration, adoption expansion, or devflow #188/#105 PARK;
- devflow Stage 2 is accepted through S2.8; the first bounded Stage-3 rotation is accepted at 3/3 under completed #302; Stage 4 evidence owner #307 concluded `NOT_NEEDED`; completed pilot items are retired from supply and current runnable lightweight supply is none.

### Stage D reviewer-role auto-launch acceptance

Issue #121 / merged PR #122 establish the accepted Stage-D reviewer execution path.

Accepted behavior:
- reviewer demand is launchable only with exact structured `review_pr_number` and `review_pr_head_sha`; context-less reviewer candidates are filtered before `ControllerOffer` creation;
- reviewer target checkout is pinned to the published exact PR head and the live PR identity/state/head is re-read immediately before claim;
- stale, moved, closed, missing, or mismatched review targets fail closed before claim continuation;
- reviewer checkout uses the existing read-only `COORDINATOR_READ_TOKEN`; the write-capable GitHub App token remains implementer-only;
- reviewer Claude execution exposes `Read` only and cannot enter deterministic implementer commit/push/draft-PR/Issue-comment integration;
- reviewer completion produces bounded review evidence only and does not itself submit or infer a GitHub Review.

Accepted evidence:
- repair exact head `2615dbd6001b949dc2d38198a926dd84251905cd`;
- PR-head Verify `37615896078`: SUCCESS;
- same-system Formal Review `5441949882`: blocking findings none;
- different-system Codex `gpt-6-astra` Formal Review `5442040496`: blocking findings 0 and the prior structured-context blocker confirmed resolved;
- prior blocking review thread resolved after the repaired exact-head review;
- squash merge `a7044cd4098aa1d8f8f385f7250d626b7b61c41a`;
- post-main Verify `37619042288`: SUCCESS.

Stage D acceptance does not prove real Claude/OIDC provider-backed E2E. Human reports the external identity configuration is present, but provider E2E remains a separate later gate.

## Next action

Controller-first GitHub Actions auto-launch v1 (#87/#88), Stage 1 work-class runtime propagation (#89/#90), Issue-comment pickup transport (#94/#95/#97/#98), manual expiry self-heal (#101/#102), and reviewer-provenance pickup eligibility (#105/#106) are accepted. The moving accepted-main SHA remains owned by devflow Control #107 rather than this document.

Runtime Issue #3 remains the volatile live claim/lease authority and must be re-read immediately before any runtime consumption or mutation. Stage C and Stage D are accepted on main through `a7044cd4098aa1d8f8f385f7250d626b7b61c41a`; the accepted implementation still does not acquire, refresh, persist or rotate credentials. Human reports the external GitHub App / Claude OIDC identity configuration is complete, but real provider-backed E2E remains separately unaccepted and must not be synthesized or run across its security/environment boundary without the applicable authority. Adoption remains `PILOT` and devflow #188/#105 PARK remains unreleased. The lightweight-work rollout is accepted through the first Stage-3 3/3 cycle, with Stage 4 `NOT_NEEDED`; standing future supply remains governed by devflow #209.
