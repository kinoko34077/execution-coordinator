"""Actions-side chat-worker pickup transport (devflow#205, Work Order #202).

A chat that can only comment on GitHub requests a discovery cycle with an
Issue comment; the ``chat-pickup.yml`` workflow runs this module.  The
transport changes nothing about authority: the same devflow contract
classifies, the same serialized ``mutate-state.yml`` lane claims, and the
worker's capabilities stay the worker's own declaration.

Command grammar (one ``key: value`` per line after the command line)::

    /pickup
    target: owner/repo                  (required)
    worker_system: codex|claude|chatgpt (required)
    session: <worker_session_id>        (optional; generated when absent)
    cycle: <N>                          (optional; default 1)
    capabilities: python, tests         (optional; exact tags)
    environment: linux                  (optional; exact tags)
    intent: <the user's words>          (optional; audit only)

    /release
    session: <worker_session_id>        (required)
    claim_id: clm_...                   (required)
    generation: <N>                     (required)
"""

from __future__ import annotations

import json
import os
import re
import secrets
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

TRUSTED = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
PICKUP_KEYS = frozenset({"target", "worker_system", "session", "cycle", "capabilities", "environment", "intent"})
RELEASE_KEYS = frozenset({"session", "claim_id", "generation"})
# Surfaces supplied by the Actions transport itself.  They describe the
# transport path, never the worker's capabilities.
TRANSPORT_SURFACES = ("surface.github_read", "surface.github_write", "surface.coordinator_claim")

_LINE = re.compile(r"^([a-z_]+)\s*:\s*(.*?)\s*$")
_CLAIM_ID = re.compile(r"^clm_[0-9a-f]{32}$")
_SECRET = re.compile(r"(ghp_|gho_|ghs_|github_pat_|sk-|bearer|token|password|secret|cookie)", re.IGNORECASE)


class CommandRejected(ValueError):
    """Typed rejection returned to the requester instead of any mutation."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def parse_command(body: str) -> tuple[str, dict[str, str]] | None:
    """Return ``(command, fields)`` or ``None`` when the comment is no command."""

    lines = [line.strip() for line in (body or "").strip().splitlines() if line.strip()]
    if not lines or lines[0] not in ("/pickup", "/release"):
        return None
    command = lines[0][1:]
    allowed = PICKUP_KEYS if command == "pickup" else RELEASE_KEYS
    fields: dict[str, str] = {}
    for line in lines[1:]:
        match = _LINE.fullmatch(line)
        if match is None:
            raise CommandRejected("COMMAND_MALFORMED", f"unparseable line: {line[:80]!r}")
        key, value = match.groups()
        if key not in allowed:
            raise CommandRejected("COMMAND_MALFORMED", f"unknown key for /{command}: {key}")
        if key in fields:
            raise CommandRejected("COMMAND_MALFORMED", f"duplicate key: {key}")
        if key != "intent" and _SECRET.search(value):
            raise CommandRejected("COMMAND_MALFORMED", f"{key} must not carry secret material")
        fields[key] = value
    missing = {"target", "worker_system"} - set(fields) if command == "pickup" else RELEASE_KEYS - set(fields)
    if missing:
        raise CommandRejected("COMMAND_MALFORMED", f"missing keys: {sorted(missing)}")
    return command, fields


def authorize(author_association: str) -> None:
    if author_association not in TRUSTED:
        raise CommandRejected("UNTRUSTED_AUTHOR", "only OWNER, MEMBER or COLLABORATOR may request pickups")


def declared_probes(fields: dict[str, str], profile_tools: Any) -> dict[str, bool]:
    """Map the worker's declared tags to Phase B probes; Actions adds none."""

    by_tag = {
        (field, tag): probe
        for probe, (field, tag) in profile_tools.PROBES.items()
        if field in ("capabilities", "environment")
    }
    probes = {probe: False for probe in profile_tools.PROBES}
    for field in ("capabilities", "environment"):
        for tag in [part.strip() for part in fields.get(field, "").split(",") if part.strip()]:
            probe = by_tag.get((field, tag))
            if probe is None:
                raise CommandRejected("COMMAND_MALFORMED", f"unknown {field} tag: {tag}")
            probes[probe] = True
    for probe in TRANSPORT_SURFACES:
        probes[probe] = True
    return probes


def build_observation(fields: dict[str, str], profile_tools: Any, now: datetime) -> dict[str, Any]:
    system = fields["worker_system"]
    session = fields.get("session") or profile_tools.new_session_id(system, now.replace(microsecond=0), secrets.token_hex(3))
    try:
        cycle = int(fields.get("cycle", "1"))
    except ValueError as exc:
        raise CommandRejected("COMMAND_MALFORMED", "cycle must be an integer") from exc
    return {
        "schema_version": profile_tools.OBSERVATION_SCHEMA,
        "worker_system": system,
        "worker_session_id": session,
        "cycle": cycle,
        "observed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "probes": declared_probes(fields, profile_tools),
    }


def format_reply(payload: dict[str, Any]) -> str:
    return (
        "### Chat worker pickup result\n\n```json\n"
        + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n```\n\n_Posted by execution-coordinator `chat-pickup.yml` (devflow#205)._"
    )


def handle(
    event: dict[str, Any],
    *,
    now: datetime,
    devflow_tools: Any,
    run_pickup_fn: Callable[..., dict[str, Any]],
    release_fn: Callable[[str, int, str], None],
) -> dict[str, Any] | None:
    """Process one ``issue_comment`` event.  Returns the reply payload."""

    comment = event.get("comment") or {}
    body = str(comment.get("body") or "")
    first = body.strip().splitlines()[0].strip() if body.strip() else ""
    if first not in ("/pickup", "/release"):
        return None
    try:
        authorize(str(comment.get("author_association") or ""))
    except CommandRejected:
        # Untrusted requests are ignored silently: no reply, no mutation.
        return None
    try:
        parsed = parse_command(body)
    except CommandRejected as rejection:
        return {"status": "REJECTED", "reason_code": rejection.code, "detail": str(rejection)}
    if parsed is None:
        return None
    command, fields = parsed
    try:
        if command == "release":
            if _CLAIM_ID.fullmatch(fields["claim_id"]) is None:
                raise CommandRejected("COMMAND_MALFORMED", "claim_id is malformed")
            generation = int(fields["generation"])
            key = f"{fields['session']}:release:{fields['claim_id']}"
            release_fn(fields["claim_id"], generation, key)
            return {"status": "RELEASED", "claim_id": fields["claim_id"], "generation": generation}
        observation = build_observation(fields, devflow_tools.chat_worker_profile, now)
        outcome = run_pickup_fn(
            target_repository=fields["target"],
            observation=observation,
            work_intent=fields.get("intent"),
        )
    except CommandRejected as rejection:
        return {"status": "REJECTED", "reason_code": rejection.code, "detail": str(rejection)}
    except ValueError as error:
        return {"status": "REJECTED", "reason_code": "PROFILE_INVALID", "detail": str(error)}
    session = outcome.get("session")
    result = outcome["result"]
    return {
        "status": "CLAIMED" if outcome.get("claim_id") else "NO_CLAIM",
        "worker_session_id": observation["worker_session_id"],
        "next_cycle": observation["cycle"] + 1,
        "result": result,
        "claim_id": outcome.get("claim_id"),
        "generation": session.generation if session is not None else None,
        "release_command": (
            f"/release\nsession: {observation['worker_session_id']}\n"
            f"claim_id: {outcome['claim_id']}\ngeneration: {session.generation}"
            if session is not None
            else None
        ),
    }


def main() -> int:  # pragma: no cover - exercised by the workflow
    from .actions_gateway import ActionsMutationGateway
    from .bootstrap_pickup import list_control_documents, load_devflow_tools, run_pickup
    from .discovery import GitHubIssueReader
    from .github_state import GitHubStateStore

    event = json.loads(open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8").read())
    lane_token = os.environ["GITHUB_TOKEN"]
    # Reads of private managed repositories need a separately provisioned
    # read token (Human-gated); public targets work with the workflow token.
    read_token = os.environ.get("COORDINATOR_READ_TOKEN") or lane_token
    repository = os.environ["GITHUB_REPOSITORY"]
    tools = load_devflow_tools(os.environ["DEVFLOW_PATH"])
    state = GitHubStateStore(token=lane_token, repository=repository, issue_number=3)
    reader = GitHubIssueReader(token=read_token)
    now = datetime.now(timezone.utc).replace(microsecond=0)

    def gateway() -> ActionsMutationGateway:
        return ActionsMutationGateway(token=lane_token, repository=repository, state_reader=state.load_body)

    def pickup(**kwargs: Any) -> dict[str, Any]:
        return run_pickup(
            devflow_tools=tools,
            issue_reader=reader,
            state_reader=state,
            control_documents=list_control_documents(read_token, reader),
            agents_md_read=True,
            now=now,
            gateway_factory=gateway,
            **kwargs,
        )

    def release(claim_id: str, generation: int, key: str) -> None:
        gateway().mutate(operation="release", payload={"claim_id": claim_id, "generation": generation}, idempotency_key=key)

    try:
        reply = handle(event, now=now, devflow_tools=tools, run_pickup_fn=pickup, release_fn=release)
    except Exception as error:  # report, never leave the requester without an answer
        reply = {"status": "FAILED", "reason_code": type(error).__name__, "detail": str(error)[:300]}
    if reply is None:
        return 0
    issue = event["issue"]["number"]
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/issues/{issue}/comments",
        data=json.dumps({"body": format_reply(reply)}).encode("utf-8"),
        headers={"Authorization": f"Bearer {lane_token}", "Content-Type": "application/json", "Accept": "application/vnd.github+json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        response.read()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
