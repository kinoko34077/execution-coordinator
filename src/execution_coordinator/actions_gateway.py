from __future__ import annotations

import json
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .engine import CoordinationError
from .github_state import GitHubApiError
from .model import MutationResult
from .mutate import REJECTION_ANNOTATION_TITLE
from .snapshot import parse_issue_body


class MutationRunRejected(CoordinationError):
    """The serialized mutation run concluded without committing authority.

    A failed or cancelled run makes no authority change (see #33), so it is a
    rejection of this mutation, never a success.  ``error_class`` carries the
    typed engine rejection (e.g. ``ClaimConflict``) when the run published
    one; it is ``None`` when no typed reason could be read back.
    """

    def __init__(self, message: str, *, error_class: str | None = None) -> None:
        super().__init__(message)
        self.error_class = error_class


class MutationOutcomeUnknown(RuntimeError):
    """The mutation outcome could not be established within the bound.

    This is deliberately not a ``CoordinationError``: ``AgentSession`` treats
    it as ambiguous and fences instead of assuming either outcome.
    """


class ActionsMutationGateway:
    """Real ``MutationGateway`` over the serialized ``mutate-state.yml`` lane.

    One ``mutate`` call dispatches exactly one workflow run, waits for that
    run to complete, and reads the committed result back from the idempotency
    record in the validated runtime state snapshot.  Only a successful run
    whose idempotency record is present is reported as a mutation result.
    """

    def __init__(
        self,
        *,
        token: str,
        repository: str,
        state_reader: Callable[[], str],
        workflow: str = "mutate-state.yml",
        ref: str = "main",
        api_base_url: str = "https://api.github.com",
        poll_interval_seconds: float = 3.0,
        timeout_seconds: float = 300.0,
        request_timeout_seconds: float = 20.0,
        opener=urlopen,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not token:
            raise ValueError("token is required to dispatch mutations")
        if repository.count("/") != 1:
            raise ValueError("repository must be owner/name")
        owner, name = repository.split("/", 1)
        base = api_base_url.rstrip("/")
        self._repo_url = f"{base}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        self._dispatch_url = (
            f"{self._repo_url}/actions/workflows/{quote(workflow, safe='')}/dispatches"
        )
        self._token = token
        self._ref = ref
        self._state_reader = state_reader
        self._poll_interval = poll_interval_seconds
        self._timeout = timeout_seconds
        self._request_timeout = request_timeout_seconds
        self._opener = opener
        self._sleep = sleep
        self._monotonic = monotonic
        self.last_run_id: int | None = None

    def _request(self, method: str, url: str, payload: dict[str, object] | None = None) -> object:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "execution-coordinator/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener(request, timeout=self._request_timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            raise GitHubApiError(f"GitHub API {method} failed with HTTP {exc.code}") from exc
        except URLError as exc:
            raise GitHubApiError(f"GitHub API {method} transport failure") from exc
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubApiError("GitHub API returned invalid JSON") from exc

    def _dispatch(self, operation: str, payload: dict[str, object], idempotency_key: str) -> int:
        response = self._request(
            "POST",
            self._dispatch_url,
            {
                "ref": self._ref,
                "inputs": {
                    "operation": operation,
                    "payload_json": json.dumps(payload, sort_keys=True),
                    "idempotency_key": idempotency_key,
                },
                "return_run_details": True,
            },
        )
        run_id = response.get("workflow_run_id") if isinstance(response, dict) else None
        if type(run_id) is not int or run_id < 1:
            # Dispatch was accepted but its run cannot be identified; the
            # mutation may still commit, so the outcome is unknown.
            raise MutationOutcomeUnknown("dispatch did not return a workflow run id")
        return run_id

    def _wait_for_conclusion(self, run_id: int) -> str:
        deadline = self._monotonic() + self._timeout
        url = f"{self._repo_url}/actions/runs/{run_id}"
        while True:
            run = self._request("GET", url)
            if not isinstance(run, dict) or run.get("id") != run_id:
                raise MutationOutcomeUnknown(f"run {run_id} readback did not match")
            if run.get("status") == "completed":
                conclusion = run.get("conclusion")
                return conclusion if isinstance(conclusion, str) else "unknown"
            if self._monotonic() >= deadline:
                raise MutationOutcomeUnknown(
                    f"run {run_id} did not complete within {self._timeout:.0f}s"
                )
            self._sleep(self._poll_interval)

    def _rejection_reason(self, run_id: int) -> tuple[str, str] | None:
        """Best-effort read of the typed rejection annotation of a failed run.

        Any read problem yields ``None``: the run is still a rejection, only
        without a typed reason.
        """

        try:
            jobs = self._request("GET", f"{self._repo_url}/actions/runs/{run_id}/jobs")
            for job in (jobs or {}).get("jobs", []) if isinstance(jobs, dict) else []:
                job_id = job.get("id") if isinstance(job, dict) else None
                if type(job_id) is not int:
                    continue
                annotations = self._request(
                    "GET", f"{self._repo_url}/check-runs/{job_id}/annotations"
                )
                for annotation in annotations if isinstance(annotations, list) else []:
                    if not isinstance(annotation, dict):
                        continue
                    if annotation.get("title") != REJECTION_ANNOTATION_TITLE:
                        continue
                    record = json.loads(annotation.get("message") or "")
                    error_class = record.get("error_class")
                    message = record.get("message")
                    if isinstance(error_class, str) and isinstance(message, str):
                        return error_class, message
        except (GitHubApiError, ValueError, AttributeError, TypeError):
            return None
        return None

    def mutate(
        self,
        *,
        operation: str,
        payload: dict[str, object],
        idempotency_key: str,
    ) -> MutationResult:
        run_id = self._dispatch(operation, payload, idempotency_key)
        self.last_run_id = run_id
        conclusion = self._wait_for_conclusion(run_id)
        state = parse_issue_body(self._state_reader())
        record = state.idempotency.get(idempotency_key)
        if conclusion != "success":
            if record is not None:
                # A committed record contradicts a non-success conclusion.
                raise MutationOutcomeUnknown(
                    f"run {run_id} concluded {conclusion} but its idempotency record exists"
                )
            reason = self._rejection_reason(run_id) if conclusion == "failure" else None
            if reason is None:
                raise MutationRunRejected(
                    f"serialized mutation run {run_id} concluded {conclusion}"
                )
            error_class, detail = reason
            raise MutationRunRejected(
                f"serialized mutation run {run_id} rejected: {error_class}: {detail}",
                error_class=error_class,
            )
        if record is None:
            raise MutationOutcomeUnknown(
                f"run {run_id} succeeded but no idempotency record was committed"
            )
        return MutationResult(
            state=state,
            claim_id=record.claim_id,
            generation=record.generation,
            events=record.events,
            metadata={"workflow_run_id": run_id},
        )
