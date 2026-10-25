"""Authenticated GitHub issue-comment origin for chat Claim mutations.

The supplied comment ID is only a locator. The mutation workflow independently
re-reads GitHub's original event and uses the devflow canonical principal gate.
"""
from __future__ import annotations

import importlib
import json
import sys
import urllib.request
from pathlib import Path

COORDINATOR_REPOSITORY = "kinoko34077/execution-coordinator"


class ChatOriginRejected(ValueError):
    pass


def verify_chat_origin(
    comment_id: object,
    *,
    command: str,
    claim_id: str | None = None,
    generation: int | None = None,
    session: str | None = None,
    token: str,
    devflow_path: str,
    opener=urllib.request.urlopen,
) -> int:
    """Return authenticated numeric actor ID, not a caller-provided identity."""
    if type(comment_id) is not int or comment_id < 1:
        raise ChatOriginRejected("chat origin comment ID must be a positive integer")
    if command not in ("pickup", "release"):
        raise ChatOriginRejected("invalid chat mutation command")
    if not token:
        raise ChatOriginRejected("comment origin verifier requires GitHub token")
    root = str(Path(devflow_path).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        shared_trust = importlib.import_module("tools.github_issue_trust")
    except ImportError as exc:
        raise ChatOriginRejected("canonical devflow principal verifier unavailable") from exc

    request = urllib.request.Request(
        f"https://api.github.com/repos/{COORDINATOR_REPOSITORY}/issues/comments/{comment_id}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with opener(request, timeout=20) as response:
            comment = json.load(response)
    except Exception as exc:
        raise ChatOriginRejected("cannot verify GitHub source comment") from exc
    if not isinstance(comment, dict) or type(comment.get("id")) is not int or comment["id"] != comment_id:
        raise ChatOriginRejected("GitHub source comment identity mismatch")
    issue_url = comment.get("issue_url")
    if not isinstance(issue_url, str) or not issue_url.startswith(
        f"https://api.github.com/repos/{COORDINATOR_REPOSITORY}/issues/"
    ):
        raise ChatOriginRejected("source comment repository mismatch")
    suffix = issue_url.rsplit("/", 1)[-1]
    if not suffix.isdecimal() or int(suffix) < 1:
        raise ChatOriginRejected("source comment issue identity invalid")
    if not shared_trust.is_trusted_principal(comment):
        raise ChatOriginRejected("source comment GitHub principal is not allowlisted")

    from .actions_pickup import parse_command, CommandRejected
    try:
        parsed = parse_command(comment.get("body") or "")
    except (ValueError, CommandRejected) as exc:
        raise ChatOriginRejected("source comment command malformed") from exc
    if parsed is None or parsed[0] != command:
        raise ChatOriginRejected("source comment does not match mutation command")
    if command == "release":
        fields = parsed[1]
        if (
            fields.get("claim_id") != claim_id
            or fields.get("session") != session
            or fields.get("generation") != str(generation)
        ):
            raise ChatOriginRejected("source release command/claim tuple mismatch")
    return comment["user"]["id"]


def authorize_internal_release_dispatch(*, actor_id: object, actor_login: object, devflow_path: str) -> int:
    """Allow existing machine lifecycle release or canonical human GitHub principal.

    Unlike chat release, this path can only release INTERNAL claims; the engine
    rejects use against chat-claimed or old unowned claims.
    """
    root = str(Path(devflow_path).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        shared_trust = importlib.import_module("tools.github_issue_trust")
    except ImportError as exc:
        raise ChatOriginRejected("canonical devflow principal verifier unavailable") from exc
    if isinstance(actor_id, str) and actor_id.isdecimal():
        numeric_id = int(actor_id)
    else:
        raise ChatOriginRejected("internal release requires authenticated workflow actor ID")
    if type(actor_login) is not str:
        raise ChatOriginRejected("internal release requires authenticated actor login")
    if numeric_id == 41898282 and actor_login == "github-actions[bot]":
        # Machine lifecycle dispatch from the existing authorized Actions lane.
        return numeric_id
    if not shared_trust.is_trusted_principal({"user": {"id": numeric_id, "login": actor_login}}):
        raise ChatOriginRejected("direct mutation requester is not allowlisted")
    return numeric_id
