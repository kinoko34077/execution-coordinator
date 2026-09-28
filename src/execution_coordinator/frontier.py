from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .discovery import (
    DiscoveryResult,
    DurableIssueSource,
    IssueReader,
    discover_claim_candidates,
)
from .query import (
    ClaimabilityProjection,
    ClaimCandidate,
    StateReadResult,
    StateReader,
    get_state_result,
    list_claimable,
    project_claimability,
)


@dataclass(frozen=True, slots=True)
class ComposedReadResult:
    """Read-only discovery, runtime state, and claimability evidence."""

    discovery: DiscoveryResult
    state: StateReadResult
    claimable: tuple[ClaimCandidate, ...]
    claimability: tuple[ClaimabilityProjection, ...]


def compose_claimability_read(
    sources: Iterable[DurableIssueSource],
    *,
    issue_reader: IssueReader,
    state_reader: StateReader,
    now: datetime,
    worker_id: str | None = None,
) -> ComposedReadResult:
    """Compose trusted discovery with the current read-only runtime projection.

    The caller supplies exact Control identities from the live devflow
    bootstrap. Discovery retains per-source failures, while an invalid or
    unavailable runtime snapshot raises before any claimability result is
    returned. No operation in this composition mutates an external authority.
    """

    discovery = discover_claim_candidates(sources, issue_reader)
    state = get_state_result(state_reader)
    claimability = project_claimability(
        discovery.candidates,
        state.state,
        now=now,
        worker_id=worker_id,
    )
    claimable = list_claimable(
        discovery.candidates,
        state.state,
        worker_id=worker_id,
    )
    return ComposedReadResult(
        discovery=discovery,
        state=state,
        claimable=claimable,
        claimability=claimability,
    )
