"""Read-only projection of devflow KAEP liveness evidence.

This consumes a versioned *producer-reported* observation. It does not parse
KAEP wire comments (devflow owns that contract), retrieve GitHub evidence,
identify an LLM, mutate execution-coordinator Claims, or wake providers.
A valid observation merely shows format/freshness of a reported response.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any

SCHEMA = "gpt-liveness-observation.v1"
SOURCE = "devflow/kaep-v0.2"
EVENTS = {
    "MODEL_TOOL_RESPONSE_OBSERVED",
    "MAILBOX_ONLY",
    "LONG_RUNNING_DECLARED",
    "NO_RESPONSE",
}
FIELDS = {
    "schema", "source", "mailbox_ref", "task_ref", "session_id", "turn_id",
    "event", "observed_at", "valid_until", "evidence_url", "reported_actor",
    "producer_validation", "claim_authority",
}
REF = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*\Z")
IDENTITY = re.compile(r"[A-Za-z0-9_-]{8,72}\Z")
COMMENT = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)#issuecomment-([1-9][0-9]*)\Z")
ACTOR = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9-]{0,38}|github-actions\[bot\])\Z")


def _timestamp(raw: object) -> datetime:
    if type(raw) is not str or not raw.endswith("Z"):
        raise ValueError("OBSERVATION_TIMESTAMP_INVALID")
    try:
        dt = datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("OBSERVATION_TIMESTAMP_INVALID") from exc
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise ValueError("OBSERVATION_TIMESTAMP_INVALID")
    return dt


def project_liveness(
    data: dict[str, Any], *, now: datetime, expected_mailbox: str,
) -> dict[str, Any]:
    """Validate producer projection shape, output strictly advisory evidence.

    `producer_validation` is *a declaration*, not independent producer or
    model attestation. This method cannot grant an execution Claim.
    """
    if type(data) is not dict or set(data) != FIELDS:
        raise ValueError("OBSERVATION_SCHEMA_FIELDS_INVALID")
    if (data["schema"] != SCHEMA or data["source"] != SOURCE
            or data["producer_validation"] != "UPSTREAM_REPORTED"
            or data["claim_authority"] is not False):
        raise ValueError("OBSERVATION_AUTHORITY_INVALID")
    if (type(expected_mailbox) is not str or not REF.fullmatch(expected_mailbox)
            or type(data["mailbox_ref"]) is not str
            or not REF.fullmatch(data["mailbox_ref"])
            or data["mailbox_ref"] != expected_mailbox):
        raise ValueError("OBSERVATION_MAILBOX_MISMATCH")
    if type(data["task_ref"]) is not str or not REF.fullmatch(data["task_ref"]):
        raise ValueError("OBSERVATION_TASK_INVALID")
    if any(type(data[k]) is not str or not IDENTITY.fullmatch(data[k])
           for k in ("session_id", "turn_id")):
        raise ValueError("OBSERVATION_SESSION_INVALID")
    if type(data["event"]) is not str or data["event"] not in EVENTS:
        raise ValueError("OBSERVATION_EVENT_INVALID")
    if type(data["reported_actor"]) is not str or not ACTOR.fullmatch(data["reported_actor"]):
        raise ValueError("OBSERVATION_ACTOR_INVALID")
    url = data["evidence_url"]
    if type(url) is not str or (match := COMMENT.fullmatch(url)) is None:
        raise ValueError("OBSERVATION_EVIDENCE_URL_INVALID")
    if f"{match.group(1)}#{match.group(2)}" != data["mailbox_ref"]:
        raise ValueError("OBSERVATION_EVIDENCE_TARGET_INVALID")
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("OBSERVATION_NOW_INVALID")
    observed = _timestamp(data["observed_at"])
    deadline = _timestamp(data["valid_until"])
    if (deadline <= observed or deadline - observed > timedelta(hours=2)
            or observed > now + timedelta(seconds=30)):
        raise ValueError("OBSERVATION_WINDOW_INVALID")

    fresh = now < deadline
    # A transport heartbeat is never a model reply. Even a recent tool
    # response does not establish continuous live model state.
    if not fresh:
        classification = "HISTORICAL_EVIDENCE"
    elif data["event"] == "MODEL_TOOL_RESPONSE_OBSERVED":
        classification = "RECENT_REPORTED_MODEL_RESPONSE"
    elif data["event"] == "LONG_RUNNING_DECLARED":
        classification = "RECENT_DECLARED_LONG_OPERATION"
    elif data["event"] == "MAILBOX_ONLY":
        classification = "RECENT_MAILBOX_ONLY"
    else:
        classification = "RECENT_UNANSWERED_PROBE"

    return {
        "schema": "execution-coordinator.liveness-projection.v1",
        "mailbox_ref": data["mailbox_ref"],
        "task_ref": data["task_ref"],
        "session_id": data["session_id"],
        "turn_id": data["turn_id"],
        "event": data["event"],
        "classification": classification,
        "observed_at": data["observed_at"],
        "valid_until": data["valid_until"],
        "age_seconds": max(0, int((now - observed).total_seconds())),
        "evidence_url": data["evidence_url"],
        "reported_actor": data["reported_actor"],
        "provenance": "UNATTESTED_UPSTREAM_REPORT",
        "agent_identity_verified": False,
        "gpt_currently_alive_proven": False,
        "claim_authority": False,
        "allowed_claim_operations": [],
        "runtime_state_mutated": False,
    }
