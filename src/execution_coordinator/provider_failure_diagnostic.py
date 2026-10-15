"""Fail-closed, non-sensitive classifier for Claude SDK bootstrap failures.

Never log the execution payload or its fields. This reader emits one of a
fixed set of status categories plus, when the SDK reports one, the exact
validated integer HTTP status (100..599) or the literal ``UNAVAILABLE``. It
never emits raw SDK text, tokens, or IDs. The filename and runner directory are
fixed by the approved workflow.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

EXECUTION_FILENAME = "claude-execution-output.json"
MAX_EXECUTION_FILE_BYTES = 2 * 1024 * 1024

HTTP_429 = "HTTP_429"
HTTP_401_403 = "HTTP_401_403"
HTTP_5XX = "HTTP_5XX"
HTTP_OTHER = "HTTP_OTHER"
NO_HTTP_STATUS = "NO_HTTP_STATUS"
UNAVAILABLE = "UNAVAILABLE"

# The finite values in this tuple are the only permitted diagnostic outputs.
ERROR_CLASSES = frozenset(
    {HTTP_429, HTTP_401_403, HTTP_5XX, HTTP_OTHER, NO_HTTP_STATUS, UNAVAILABLE}
)

MIN_HTTP_STATUS = 100
MAX_HTTP_STATUS = 599


def _is_valid_http_status(status: object) -> bool:
    # Exact int only: bool, float, str and int subclasses are never accepted.
    return type(status) is int and MIN_HTTP_STATUS <= status <= MAX_HTTP_STATUS


def _class_for_status(status: int) -> str:
    if status == 429:
        return HTTP_429
    if status in (401, 403):
        return HTTP_401_403
    if 500 <= status <= 599:
        return HTTP_5XX
    return HTTP_OTHER


def format_http_status(status: int | None) -> str:
    """Return a 3-digit validated status, else the fixed literal sentinel.

    Re-validates so no value other than 100..599 or ``UNAVAILABLE`` can reach
    the log, whatever the caller passes.
    """
    if not _is_valid_http_status(status):
        return UNAVAILABLE
    return f"{status:03d}"


def inspect_execution_file(runner_temp: str | None) -> tuple[str, int | None]:
    """Return ``(fixed error class, validated HTTP status or None)``.

    Reads a bounded SDK result only from the GitHub runner's fixed temp file.
    SDK messages are untrusted; never interpolate any of their text into logs.
    A missing or malformed SDK payload cannot change claim/fencing behavior.
    The status is non-None only when the last ``type == "result"`` message has
    an exact ``int`` ``api_error_status`` in 100..599.
    """
    if not runner_temp or not os.path.isabs(runner_temp):
        return UNAVAILABLE, None

    path = Path(runner_temp) / EXECUTION_FILENAME
    try:
        if path.is_symlink() or not path.is_file():
            return UNAVAILABLE, None
        with path.open("rb") as handle:
            raw = handle.read(MAX_EXECUTION_FILE_BYTES + 1)
        if len(raw) > MAX_EXECUTION_FILE_BYTES:
            return UNAVAILABLE, None
        messages = json.loads(raw)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return UNAVAILABLE, None

    if not isinstance(messages, list):
        return UNAVAILABLE, None
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("type") != "result":
            continue
        status = message.get("api_error_status")
        if not _is_valid_http_status(status):
            return NO_HTTP_STATUS, None
        return _class_for_status(status), status
    return NO_HTTP_STATUS, None


def classify_execution_file(runner_temp: str | None) -> str:
    """Public, unchanged contract: only the fixed finite error category."""
    return inspect_execution_file(runner_temp)[0]


def main() -> int:
    # Print only fixed literals and one validated 3-digit status. No paths,
    # raw JSON, HTTP body, or IDs.
    error_class, status = inspect_execution_file(os.environ.get("RUNNER_TEMP"))
    print(f"claude_bootstrap_error_class={error_class}")
    print(f"claude_bootstrap_http_status={format_http_status(status)}")
    return 0


if __name__ == "__main__":  # pragma: no cover - workflow entry
    raise SystemExit(main())
