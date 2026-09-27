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

`GITHUB_TOKEN` is optional for publicly readable repositories and may be supplied for authenticated reads. The command emits the validated schema-v1 state as JSON. This surface does not discover or rank durable work; `list_claimable` remains a later bounded slice.

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
- `kinoko34077/devflow` — durable workflow/control authority

No production claim implementation is included in the bootstrap commit.
