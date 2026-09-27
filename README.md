# execution-coordinator

Runtime coordination layer for multi-agent work across KiNoTch. managed repositories.

## Authority boundary

- `devflow` owns durable cross-repository policy, protocol semantics, Work Orders, and Repository Controls.
- This repository owns the execution runtime for claim/lease/liveness/conflict coordination.
- Owning repositories retain their own Issues, specifications, code, tests, PRs, and technical Current State.
- `repo-monitor` may observe coordinator state but is not an execution authority.

## Initial scope

The first implementation target is agent-first execution coordination:

1. discover claimable work from durable GitHub state;
2. serialize claim-state mutation through GitHub Actions;
3. issue claim/lease generations;
4. track heartbeat and progress separately;
5. release/expire/take over safely with stale-worker fencing;
6. prevent incompatible conflict-key ownership;
7. distinguish active execution from CI/review/user/dependency waits.

Controller-side priority offers and bidirectional dispatch negotiation follow after the agent-first core is proven.

## Read-only current-state query

`execution_coordinator.query.get_state()` is the read-only Protocol v1 state-query boundary. It loads the existing system-Issue body through `GitHubStateStore`, validates it with the same fail-closed snapshot parser used by the mutation path, and returns `CoordinatorState` without PATCHing the Issue, appending comments, or entering the serialized mutation lane.

CLI usage:

```text
GITHUB_REPOSITORY=owner/execution-coordinator \
STATE_ISSUE_NUMBER=3 \
python -m execution_coordinator.query get_state
```

`GITHUB_TOKEN` is optional for publicly readable repositories and may be supplied for authenticated reads. The command emits the validated schema-v1 state as JSON.

## Read-only durable Issue discovery

`execution_coordinator.discovery.discover_claim_candidates()` implements the exact-reference consumer side of the accepted devflow durable-candidate source contract (`devflow#125` / merged PR #126).

The caller supplies only exact owning Issue identities through `DurableIssueSource(repository, issue_number)`. It does **not** choose candidate role, eligible Work Status, conflict keys, readiness, blocker state, or confirmation state. Those fields come only from the versioned v1 marker already published in the owning Issue:

```text
<!-- DEVFLOW_EXECUTION_CANDIDATE_V1_BEGIN -->
{
  "schema_version": 1,
  "task_ref": "owner/repository#123",
  "entry_ref": "https://github.com/owner/repository/issues/123",
  "role": "implementer",
  "scope_ready": true,
  "blocked": false,
  "requires_user_confirmation": false,
  "conflict_keys": ["component:owner/repository:parser"],
  "provenance": {
    "control_ref": "kinoko34077/devflow#17",
    "work_order_ref": "kinoko34077/devflow#105"
  }
}
<!-- DEVFLOW_EXECUTION_CANDIDATE_V1_END -->
```

Marker absence is valid and simply means the Issue is not machine-discoverable. Duplicate/partial/malformed markers, unsupported schema/fields/roles, task identity mismatches, invalid entries, or invalid provenance fail closed as `DiscoveryFailure` records.

`control_ref` is resolved read-only against the current devflow Repository Control. Control Work Status is a guard rather than source authority: ordinary implementer discovery requires `READY_FOR_IMPLEMENTATION`; reviewer/verifier/integrator require `AWAITING_REVIEW`. Current `[USER_DECISION]` and compatibility `[HUMAN_GATE]` evidence vetoes a marker that incorrectly claims `requires_user_confirmation=false`. Optional `work_order_ref` must resolve structurally to an open devflow `[WORK ORDER]` Issue.

The marker's explicit `entry_ref` may identify an open Issue or Pull Request in the same owning repository. The adapter never searches for a substitute entry and never derives candidate authority from local Objective/Scope/Acceptance prose, Project fields, branch/PR existence, Issue age, or missing runtime claims. In particular, interrupted `IMPLEMENTING` work is not rediscovered as fresh ordinary work; recovery/takeover remains a separate future contract.

`GitHubIssueReader` performs GET-only reads. Discovery does not mutate owning Issues, devflow Controls/Work Orders, runtime Issue #3, or submit claims. GitHub-wide frontier selection, ranking, capability matching, automatic claim submission, controller negotiation, repo-monitor projection, and recovery remain later bounded slices.

## Read-only claimability projection

`execution_coordinator.query.list_claimable()` accepts normalized `ClaimCandidate` values supplied by the durable discovery layer and filters them against the validated `CoordinatorState`.

The projection excludes candidates that are not scope-ready, are durably blocked, require user confirmation, have no canonical entry reference, already have current same-task/role ownership, conflict with an incompatible active conflict key, or would create a same-worker implementer/reviewer conflict when `worker_id` is supplied.

The projection is deterministic and preserves input order. It is intentionally not a ranking or scheduling surface: priority/dependency ranking, capability/environment matching, automatic claim submission, controller negotiation, and repo-monitor projection remain separate later slices.

## Minimal agent bootstrap adapter

`execution_coordinator.agent.AgentSession` is the first small integration boundary for an agent surface. It does not discover work or generate retry keys; the caller supplies stable idempotency keys and a `MutationGateway` backed by the serialized mutation workflow.

```python
from execution_coordinator.agent import AgentSession
from execution_coordinator.model import Role

session = AgentSession(
    gateway,
    task="owner/repository#123",
    role=Role.IMPLEMENTER,
    worker_id="agent-session-1",
    conflict_keys=("component:parser",),
)

session.run(
    lambda active: implement_after_claim(active),
    claim_idempotency_key="task-123-claim-1",
    acknowledge_idempotency_key="task-123-ack-1",
    release_idempotency_key="task-123-release-1",
)
```

The callback is not entered unless both claim and `acknowledge` succeed. `AgentSession.acknowledge()` forwards the current claim ID/generation and caller-owned idempotency key, and requires the same authority tuple in the response. A malformed, stale, changed, or rejected acknowledge fences the local session without attempting an unsafe compensating release; the remote claim remains lease-bound. The adapter also forwards the current authority for `renew` and `release`, and leaves intentional external waits to the caller: release the claim before waiting when safe, then claim again through the normal authority path.

The Issue #10 follow-up extends the same adapter over existing runtime operations with `progress`, evidence-backed `wait`, explicit `resume`, and terminal `fail`. It validates live execution state and terminal claim removal before updating local authority. Callback exceptions remain primary while release cleanup failures are attached as notes.

This adapter is deliberately not a scheduler, controller, repo-monitor, or full Protocol v1 expected-state/failure-evidence implementation.

## Canonical cross-repository references

- `kinoko34077/devflow#105` — parent Work Order
- `kinoko34077/devflow#106` — Execution Coordination Protocol v1 specification work
- `kinoko34077/devflow#125` / PR #126 — accepted durable-candidate source contract v1
- `kinoko34077/devflow` — durable workflow/control authority
