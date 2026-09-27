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
from execution_coordinator.model import Role, WaitReason

session = AgentSession(
    gateway,
    task="owner/repository#123",
    role=Role.IMPLEMENTER,
    worker_id="agent-session-1",
    conflict_keys=("component:parser",),
)

session.claim(idempotency_key="task-123-claim-1")
session.acknowledge(idempotency_key="task-123-ack-1")

session.wait(
    reason=WaitReason.CI,
    evidence_ref="run:123456",
    idempotency_key="task-123-wait-1",
)
# A retained WAITING claim is still lease-bound; renew it before lease expiry.
session.resume(idempotency_key="task-123-resume-1")
session.release(idempotency_key="task-123-release-1")
```

The callback form remains available through `AgentSession.run(...)`; callback work begins only after both claim and `acknowledge` succeed.

`AgentSession.wait()` exposes the existing evidence-backed runtime WAITING transition. It requires a current claim/generation, an existing `WaitReason`, a non-empty `evidence_ref`, and a caller-owned idempotency key. `AgentSession.resume()` returns that same live authority to `RUNNING` and clears wait metadata through the runtime state machine. Both operations require the same claim ID/generation in the response and fence the local session if the gateway reports stale, malformed, changed, or transport-ambiguous authority.

WAITING does **not** extend or suspend the lease. If a bounded unsafe-to-transfer wait retains execution ownership, the caller must keep renewing before `lease_until`. For ordinary CI/review/user/dependency/provider waits where no mutation remains in flight, release the execution claim when safe instead of consuming a lease indefinitely; later continuation reacquires authority through the normal claim path.

A failed or ambiguous authority mutation does not trigger an invented compensating release when remote commit state is unknown. The local session is fenced and any remote claim remains governed by the existing lease/expiry rules.

This adapter is deliberately not a scheduler, controller, repo-monitor, automatic renewal loop, or full Protocol v1 expected-state/failure-evidence implementation.

## Canonical cross-repository references

- `kinoko34077/devflow#105` — parent Work Order
- `kinoko34077/devflow#106` — Execution Coordination Protocol v1 specification work
- `kinoko34077/devflow` — durable workflow/control authority

No production claim implementation is included in the bootstrap commit.
