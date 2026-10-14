"""Pure, non-executing portable job admission and backend planning (F1).

The supplied recipe MUST have been authenticated against the trusted catalog by
a separate caller. This module does not grant execution/claim/credential rights.
No free-form shell command, environment variable, URL, or secret is accepted.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Mapping

REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
TASK = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*\Z")
ID = re.compile(r"[a-z][a-z0-9_.-]{0,79}\Z")
ATTEMPT = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}\Z")
SHA1 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
INPUT_TYPES = {"str": str, "int": int, "bool": bool}
BACKENDS = frozenset({"github_actions", "external_isolated"})
ACTION_STATES = frozenset(
    {"READY", "CAPACITY_BLOCKED", "CONTROL_OUTAGE", "POLICY_DISABLED", "UNKNOWN"}
)
EXTERNAL_STATES = frozenset({"READY", "UNAVAILABLE", "UNKNOWN"})
REFRESH_STATES = frozenset({"VERIFIED", "UNAVAILABLE", "UNKNOWN"})


@dataclass(frozen=True)
class TrustedRecipe:
    """Recipe data already authenticated by its separate catalog loader."""

    recipe_id: str
    digest_sha256: str
    input_schema: Mapping[str, str]
    backends: frozenset[str]
    read_only: bool
    requires_secrets: bool
    max_seconds: int


@dataclass(frozen=True)
class JobRequest:
    repository: str
    task_ref: str
    source_sha: str
    recipe_id: str
    recipe_sha256: str
    attempt_id: str
    inputs: Mapping[str, object]


@dataclass(frozen=True)
class RoutingEvidence:
    """Typed observations; attestation and Claim checks occur upstream."""

    actions: str
    external: str
    github_api: str
    coordinator_claim: str
    caller_authorized: bool
    external_isolated: bool
    private_source_authorized: bool


@dataclass(frozen=True)
class Plan:
    disposition: str
    reason: str
    backend: str | None = None
    request_digest: str | None = None


def _valid_recipe(recipe: TrustedRecipe) -> bool:
    return (
        isinstance(recipe, TrustedRecipe)
        and type(recipe.recipe_id) is str
        and ID.fullmatch(recipe.recipe_id) is not None
        and type(recipe.digest_sha256) is str
        and SHA256.fullmatch(recipe.digest_sha256) is not None
        and type(recipe.read_only) is bool
        and type(recipe.requires_secrets) is bool
        and recipe.read_only
        and not recipe.requires_secrets
        and type(recipe.max_seconds) is int
        and 1 <= recipe.max_seconds <= 720
        and type(recipe.backends) is frozenset
        and bool(recipe.backends)
        and recipe.backends <= BACKENDS
        and type(recipe.input_schema) is dict
        and len(recipe.input_schema) <= 16
        and all(
            type(k) is str
            and ID.fullmatch(k) is not None
            and type(t) is str
            and t in INPUT_TYPES
            for k, t in recipe.input_schema.items()
        )
    )


def _valid_repository(value: str) -> bool:
    if type(value) is not str or REPOSITORY.fullmatch(value) is None:
        return False
    return all(part not in {".", ".."} and not part.startswith(".") for part in value.split("/"))


def _valid_task(value: str) -> bool:
    if type(value) is not str or TASK.fullmatch(value) is None:
        return False
    return _valid_repository(value.split("#", 1)[0])


def _valid_request(request: JobRequest, recipe: TrustedRecipe) -> bool:
    if not isinstance(request, JobRequest):
        return False
    if not (
        _valid_repository(request.repository)
        and _valid_task(request.task_ref)
        and type(request.source_sha) is str
        and SHA1.fullmatch(request.source_sha)
        and type(request.recipe_id) is str
        and request.recipe_id == recipe.recipe_id
        and type(request.recipe_sha256) is str
        and request.recipe_sha256 == recipe.digest_sha256
        and type(request.attempt_id) is str
        and ATTEMPT.fullmatch(request.attempt_id)
        and type(request.inputs) is dict
        and request.inputs.keys() == recipe.input_schema.keys()
    ):
        return False
    for key, required in recipe.input_schema.items():
        value = request.inputs[key]
        if type(value) is not INPUT_TYPES[required]:
            return False
        if type(value) is str and (
            len(value) > 1024 or any(ord(c) < 32 for c in value)
        ):
            return False
        if type(value) is int and not -(2**53) < value < 2**53:
            return False
    return True


def plan_portable_job(
    recipe: TrustedRecipe, request: JobRequest, evidence: RoutingEvidence
) -> Plan:
    """Fail-closed plan ONLY; does not authorize or execute any work.

    Native is preferred. External is permitted only on a proven unavailable
    native control/capacity path, or if explicitly the recipe's sole backend.
    A disabled policy, missing Github/Claim evidence, or ambiguous state HOLD.
    """
    if not _valid_recipe(recipe) or not _valid_request(request, recipe):
        return Plan("HOLD", "INVALID_RECIPE_OR_REQUEST")
    if not isinstance(evidence, RoutingEvidence) or not (
        evidence.actions in ACTION_STATES
        and evidence.external in EXTERNAL_STATES
        and evidence.github_api in REFRESH_STATES
        and evidence.coordinator_claim in REFRESH_STATES
        and type(evidence.caller_authorized) is bool
        and type(evidence.external_isolated) is bool
        and type(evidence.private_source_authorized) is bool
    ):
        return Plan("HOLD", "INVALID_EVIDENCE")
    if not evidence.caller_authorized:
        return Plan("HOLD", "CALLER_UNAUTHORIZED")
    if evidence.github_api != "VERIFIED" or evidence.coordinator_claim != "VERIFIED":
        return Plan("HOLD", "AUTHORITY_UNAVAILABLE")
    if not evidence.private_source_authorized:
        return Plan("HOLD", "SOURCE_ACCESS_UNVERIFIED")
    if evidence.actions == "POLICY_DISABLED":
        return Plan("HOLD", "POLICY_DISABLED")
    if evidence.actions == "UNKNOWN":
        return Plan("HOLD", "NATIVE_STATUS_UNKNOWN")

    backend = None
    if evidence.actions == "READY" and "github_actions" in recipe.backends:
        backend = "github_actions"
    elif (
        evidence.external == "READY"
        and evidence.external_isolated
        and "external_isolated" in recipe.backends
        and (
            evidence.actions in {"CAPACITY_BLOCKED", "CONTROL_OUTAGE"}
            or "github_actions" not in recipe.backends
        )
    ):
        backend = "external_isolated"
    if backend is None:
        return Plan("HOLD", "NO_VERIFIED_EXECUTION_BACKEND")

    # This digest identifies a candidate; it is NOT authentication or a lock.
    identity = {
        "attempt_id": request.attempt_id,
        "inputs": request.inputs,
        "recipe": recipe.digest_sha256,
        "repository": request.repository,
        "source_sha": request.source_sha,
        "task_ref": request.task_ref,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return Plan("CANDIDATE_ONLY", "PREFLIGHT_MATCH", backend, digest)
