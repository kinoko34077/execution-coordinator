# GPT liveness evidence — read-only coordinator projection (F5A–D)

**Owner:** [execution-coordinator #138](https://github.com/kinoko34077/execution-coordinator/issues/138). **Protocol authority:** [devflow #516](https://github.com/kinoko34077/devflow/issues/516); [KAEP v0.2 #503](https://github.com/kinoko34077/devflow/issues/503). **Upstream real pilot:** [devflow #519](https://github.com/kinoko34077/devflow/issues/519).

## Separation of responsibilities

- **devflow** owns KAEP decoding, comment-chain correlation, nonce/session/turn/actor/timestamps and whether a reply is a valid liveness observation.
- **execution-coordinator** consumes only a *producer-reported versioned summary*, with strict format / identity scope / observation-freshness checks. It does **not** decode KAEP wire comments or represent itself as independent provenance verification.
- **runtime Issue #3 remains the only live Claim/Lease/Fencing authority**. This adapter is not consulted by claim, renew, expire, takeover, autonomous allocation, offer or release code. An unanswered probe is **not** proof of worker death.

The query reports `UNATTESTED_UPSTREAM_REPORT` even when the input says `UPSTREAM_REPORTED`; a JSON document and a shared GitHub actor do not prove the model identity. A recent reply is *recent evidence of a reported connected-tool response*, not proof that GPT is still computing now.

## Input contract

```json
{
  "schema": "gpt-liveness-observation.v1",
  "source": "devflow/kaep-v0.2",
  "mailbox_ref": "kinoko34077/devflow#519",
  "task_ref": "kinoko34077/devflow#519",
  "session_id": "session_pilot20261014_1755",
  "turn_id": "turn_pilot20261014_1755",
  "event": "MODEL_TOOL_RESPONSE_OBSERVED",
  "observed_at": "2026-10-14T17:58:26Z",
  "valid_until": "2026-10-14T18:12:29Z",
  "evidence_url": "https://github.com/kinoko34077/devflow/issues/519#issuecomment-6174861252",
  "reported_actor": "kinoko34077",
  "producer_validation": "UPSTREAM_REPORTED",
  "claim_authority": false
}
```

This is a **demonstration of the downstream format**, not an automatically generated or cryptographically attested file. A future trusted producer adapter must validate actual GitHub Issue/comment identity and KAEP content *before* supplying it. The reader checks strict keys, URI-to-mailbox binding, UTC bounds, limited TTL, exact mailbox allowlist, identity fields and no-Claim-grant flag; it deliberately cannot attest the underlying source.

Supported events are `MODEL_TOOL_RESPONSE_OBSERVED`, `MAILBOX_ONLY`, `LONG_RUNNING_DECLARED`, `NO_RESPONSE`. Outputs are `RECENT_REPORTED_MODEL_RESPONSE`, `RECENT_MAILBOX_ONLY`, `RECENT_DECLARED_LONG_OPERATION`, `RECENT_UNANSWERED_PROBE`, or `HISTORICAL_EVIDENCE` (when expiry elapsed). None grants an execution privilege.

## Native CLI

```sh
PYTHONPATH=src python -m execution_coordinator.query get_liveness \
  --evidence-json /path/to/prevalidated-liveness.json \
  --expected-mailbox 'kinoko34077/devflow#519'
```

The command reads only the caller-supplied JSON and produces a deterministic projection. No GitHub API call, credential, provider, subprocess, daemon, Claim mutation or task-store copy occurs. Existing `get_state` continues to read runtime Issue #3 unchanged.

## Acceptance boundary

- **F5A–D:** schema/strict query, deterministic tests, exact-head CI and review.
- **F5E not yet accepted:** end-to-end trusted producer materialization and native query consumption, plus release/rollout criteria. Implement this only after verifying the accepted KAEP producer interface; do not give `producer_validation` an upgraded meaning via text alone.
- No automatic takeover, 5min scheduler changes, ChatGPT wake, credential change, or cross-repository deployment is authorized by this projection.
