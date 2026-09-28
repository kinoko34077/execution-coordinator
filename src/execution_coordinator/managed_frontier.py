from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .discovery import (
    DiscoveryResult,
    DurableIssueSource,
    IssueDocument,
    IssueReader,
    discover_claim_candidates,
)
from .frontier import ComposedReadResult, compose_claimability_result
from .model import Role
from .query import ClaimabilityProjection, ClaimCandidate, StateReader
from .reconciliation import discover_reconciliation_claim_candidates


@dataclass(frozen=True, slots=True)
class FrontierSourceFailure:
    """A source-input failure that prevents a safe frontier read."""

    source_ref: str
    reason: str


@dataclass(frozen=True, slots=True)
class ManagedFrontierResult:
    """Read-only managed-repository frontier evidence.

    ``read`` is absent when the caller supplied an invalid or duplicate source
    identity.  Discovery failures from valid sources remain inside ``read`` so
    callers can distinguish a partial source read from an invalid input set.
    """

    sources: tuple[DurableIssueSource, ...]
    source_failures: tuple[FrontierSourceFailure, ...]
    read: ComposedReadResult | None

    @property
    def candidates(self) -> tuple[ClaimCandidate, ...]:
        if self.read is None:
            return ()
        return self.read.discovery.candidates

    @property
    def claimable(self) -> tuple[ClaimCandidate, ...]:
        if self.read is None:
            return ()
        return self.read.claimable

    @property
    def claimability(self) -> tuple[ClaimabilityProjection, ...]:
        if self.read is None:
            return ()
        return self.read.claimability

    @property
    def fresh_candidates(self) -> tuple[ClaimCandidate, ...]:
        return tuple(candidate for candidate in self.candidates if candidate.role is not Role.RECOVERY)

    @property
    def recovery_candidates(self) -> tuple[ClaimCandidate, ...]:
        return tuple(candidate for candidate in self.candidates if candidate.role is Role.RECOVERY)


class _CachedIssueReader:
    """Share one exact Issue snapshot across the normal and recovery readers."""

    def __init__(self, reader: IssueReader) -> None:
        self._reader = reader
        self._cache: dict[tuple[str, int], IssueDocument | Exception] = {}

    def read_issue(self, repository: str, issue_number: int) -> IssueDocument:
        key = (repository, issue_number)
        cached = self._cache.get(key)
        if cached is not None:
            if isinstance(cached, Exception):
                raise cached
            return cached
        try:
            document = self._reader.read_issue(repository, issue_number)
        except Exception as exc:
            self._cache[key] = exc
            raise
        self._cache[key] = document
        return document


def _source_ref(value: object) -> str:
    if isinstance(value, DurableIssueSource):
        return f"{value.repository}#{value.issue_number}"
    if isinstance(value, str):
        return value
    return f"<{type(value).__name__}>"


def _normalize_sources(
    raw_sources: Iterable[object],
) -> tuple[tuple[DurableIssueSource, ...], tuple[FrontierSourceFailure, ...]]:
    valid: list[DurableIssueSource] = []
    failures: list[FrontierSourceFailure] = []
    for raw in raw_sources:
        if not isinstance(raw, DurableIssueSource):
            failures.append(
                FrontierSourceFailure(
                    source_ref=_source_ref(raw),
                    reason="source must be a DurableIssueSource",
                )
            )
            continue
        valid.append(raw)

    ordered = sorted(valid, key=lambda source: (source.repository, source.issue_number))
    unique: list[DurableIssueSource] = []
    seen_repositories: set[str] = set()
    seen_exact: set[tuple[str, int]] = set()
    for source in ordered:
        exact_key = (source.repository, source.issue_number)
        if exact_key in seen_exact:
            failures.append(
                FrontierSourceFailure(
                    source_ref=_source_ref(source),
                    reason="duplicate Repository Control source",
                )
            )
            continue
        seen_exact.add(exact_key)
        unique.append(source)
        if source.repository in seen_repositories:
            failures.append(
                FrontierSourceFailure(
                    source_ref=_source_ref(source),
                    reason="duplicate managed repository Control identity",
                )
            )
            continue
        seen_repositories.add(source.repository)

    failures.sort(key=lambda failure: (failure.source_ref, failure.reason))
    return tuple(unique), tuple(failures)


def _merge_discovery(
    normal: DiscoveryResult,
    reconciliation: DiscoveryResult,
) -> DiscoveryResult:
    return DiscoveryResult(
        candidates=normal.candidates + reconciliation.candidates,
        failures=normal.failures + reconciliation.failures,
    )


def enumerate_managed_frontier(
    sources: Iterable[object],
    *,
    issue_reader: IssueReader,
    state_reader: StateReader,
    now: datetime,
    worker_id: str | None = None,
) -> ManagedFrontierResult:
    """Enumerate the deterministic, read-only frontier for exact Control sources.

    The normal execution-candidate and reconciliation validators both consume
    one cached Issue snapshot.  Runtime state is projected only after both
    validators finish; no mutation or ranking is performed here.
    """

    normalized, source_failures = _normalize_sources(sources)
    if source_failures:
        return ManagedFrontierResult(
            sources=normalized,
            source_failures=source_failures,
            read=None,
        )

    reader = _CachedIssueReader(issue_reader)
    normal = discover_claim_candidates(normalized, reader)
    reconciliation = discover_reconciliation_claim_candidates(normalized, reader)
    read = compose_claimability_result(
        _merge_discovery(normal, reconciliation),
        state_reader=state_reader,
        now=now,
        worker_id=worker_id,
    )
    return ManagedFrontierResult(
        sources=normalized,
        source_failures=(),
        read=read,
    )
