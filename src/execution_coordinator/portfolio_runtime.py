from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .capability import CandidateRequirements
from .discovery import (
    TRUSTED_AUTHOR_ASSOCIATIONS,
    DurableIssueSource,
    IssueDocument,
    IssueReader,
    _sections,
    _strip_code_value,
)
from .managed_frontier import ManagedFrontierResult, enumerate_managed_frontier
from .model import Role
from .portfolio_metadata import PortfolioMetadataError, parse_portfolio_metadata
from .query import StateReader
from .ranking import RankedFrontierResult, rank_managed_frontier

_HUMAN_TOKENS = ("[HUMAN_GATE]", "[USER_DECISION]")


@dataclass(frozen=True, slots=True)
class PortfolioRuntimeRead:
    frontier: ManagedFrontierResult
    ranked: RankedFrontierResult | None
    requirements: tuple[CandidateRequirements, ...]
    complete: bool
    metadata_error: str | None = None


def _section(document: IssueDocument, name: str) -> str:
    values = _sections(document.body).get(name.casefold(), [])
    return _strip_code_value(values[0]).strip() if len(values) == 1 else ""


def _eligible_control(document: IssueDocument) -> bool:
    repository = _section(document, "repository")
    next_action = _section(document, "next action")
    return (
        document.state.casefold() == "open"
        and not document.is_pull_request
        and bool(repository)
        and document.author_association in TRUSTED_AUTHOR_ASSOCIATIONS
        and document.title.strip() == f"[REPO] {repository.split('/', 1)[-1]}"
        and _section(document, "repository state") == "ACTIVE"
        and _section(document, "work status") != "BLOCKED"
        and not any(token in next_action for token in _HUMAN_TOKENS)
    )


class _ControlSnapshotReader:
    def __init__(self, controls: tuple[IssueDocument, ...], delegate: IssueReader) -> None:
        self._controls = {(item.repository, item.number): item for item in controls}
        self._delegate = delegate

    def read_issue(self, repository: str, issue_number: int) -> IssueDocument:
        item = self._controls.get((repository, issue_number))
        return item if item is not None else self._delegate.read_issue(repository, issue_number)


def read_portfolio_runtime(
    control_documents: tuple[IssueDocument, ...],
    *,
    issue_reader: IssueReader,
    state_reader: StateReader,
    worker_id: str,
    now: datetime,
) -> PortfolioRuntimeRead:
    """Compose one read-only managed portfolio snapshot for controller use."""

    selected = tuple(document for document in control_documents if _eligible_control(document))
    sources = tuple(
        DurableIssueSource(document.repository, document.number)
        for document in selected
    )
    frontier = enumerate_managed_frontier(
        sources,
        issue_reader=_ControlSnapshotReader(control_documents, issue_reader),
        state_reader=state_reader,
        now=now,
        worker_id=worker_id,
    )
    if frontier.read is None:
        return PortfolioRuntimeRead(
            frontier=frontier,
            ranked=None,
            requirements=(),
            complete=False,
            metadata_error="managed frontier is unavailable",
        )

    if frontier.read.discovery.failures:
        return PortfolioRuntimeRead(
            frontier=frontier,
            ranked=None,
            requirements=(),
            complete=False,
            metadata_error="managed frontier discovery is incomplete",
        )

    metadata_by_key: dict[tuple[str, Role], object] = {}
    try:
        for document in selected:
            for item in parse_portfolio_metadata(
                document,
                candidates=frontier.fresh_candidates,
                now=now,
            ):
                key = (item.ranking.task, item.ranking.role)
                if key in metadata_by_key:
                    raise PortfolioMetadataError(
                        "portfolio metadata duplicates one candidate across Controls"
                    )
                metadata_by_key[key] = item
    except PortfolioMetadataError as exc:
        return PortfolioRuntimeRead(
            frontier=frontier,
            ranked=None,
            requirements=(),
            complete=False,
            metadata_error=str(exc),
        )

    fresh_keys = {(item.task, item.role) for item in frontier.fresh_candidates}
    if set(metadata_by_key) != fresh_keys:
        return PortfolioRuntimeRead(
            frontier=frontier,
            ranked=None,
            requirements=(),
            complete=False,
            metadata_error="portfolio metadata does not exactly cover fresh candidates",
        )

    metadata_items = tuple(metadata_by_key.values())
    ranked = rank_managed_frontier(
        frontier,
        [item.ranking for item in metadata_items],
        now=now,
    )
    requirements = tuple(
        item.requirements
        for item in sorted(
            metadata_items,
            key=lambda value: (value.requirements.task, value.requirements.role.value),
        )
    )
    return PortfolioRuntimeRead(
        frontier=frontier,
        ranked=ranked,
        requirements=requirements,
        complete=True,
    )
