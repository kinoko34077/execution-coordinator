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
    lambda active: implement_after_acknowledge(active),
    claim_idempotency_key="task-123-claim-1",
    acknowledge_idempotency_key="task-123-ack-1",
    release_idempotency_key="task-123-release-1",
)
```

The callback is not entered unless both claim and `acknowledge` succeed. The canonical adapter lifecycle is `claim -> acknowledge (CLAIMED -> RUNNING) -> work/renew -> release`. The adapter forwards the current claim ID/generation for `acknowledge`, `renew`, and `release`, and fences the local session after stale-generation or malformed/changed-authority responses.

If an acknowledge response is rejected or cannot prove the same current authority, the adapter does not enter work and does not invent a compensating release while ownership is ambiguous. The local session is fenced; any remote claim that cannot safely be released remains governed by the existing lease and expiry rules. Intentional external waits remain caller-managed: release the claim before waiting when safe, then claim again through the normal authority path.

This adapter is deliberately not a scheduler, controller, repo-monitor, or full Protocol v1 expected-state/failure-evidence implementation.

## Canonical cross-repository references

- `kinoko34077/devflow#105` — parent Work Order
- `kinoko34077/devflow#106` — Execution Coordination Protocol v1 specification work
- `kinoko34077/devflow` — durable workflow/control authority

No production claim implementation is included in the bootstrap commit.
