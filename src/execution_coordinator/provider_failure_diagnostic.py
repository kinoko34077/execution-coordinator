"""Fail-closed, non-sensitive classifier for Claude SDK bootstrap failures.

Never log the execution payload or its fields. This reader emits one of a
fixed set of status categories, without any raw SDK text, tokens, or IDs.
The filename and runner directory are fixed by the approved workflow.
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


def classify_execution_file(runner_temp: str | None) -> str:
    """Read a bounded SDK result only from the GitHub runner's fixed temp file.

    SDK messages are untrusted; never interpolate any of their text into logs.
    A missing or malformed SDK payload cannot change claim/fencing behavior.
    """
    if not runner_temp or not os.path.isabs(runner_temp):
        return UNAVAILABLE

    path = Path(runner_temp) / EXECUTION_FILENAME
    try:
        if path.is_symlink() or not path.is_file():
            return UNAVAILABLE
        with path.open("rb") as handle:
            raw = handle.read(MAX_EXECUTION_FILE_BYTES + 1)
        if len(raw) > MAX_EXECUTION_FILE_BYTES:
            return UNAVAILABLE
        messages = json.loads(raw)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return UNAVAILABLE

    if not isinstance(messages, list):
        return UNAVAILABLE
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("type") != "result":
            continue
        status = message.get("api_error_status")
        if type(status) is not int or not 100 <= status <= 599:
            return NO_HTTP_STATUS
        if status == 429:
            return HTTP_429
        if status in (401, 403):
            return HTTP_401_403
        if 500 <= status <= 599:
            return HTTP_5XX
        return HTTP_OTHER
    return NO_HTTP_STATUS


def main() -> int:
    # Print only one fixed literal. No paths, raw JSON, HTTP body, or IDs.
    result = classify_execution_file(os.environ.get("RUNNER_TEMP"))
    print(f"claude_bootstrap_error_class={result}")
    return 0


if __name__ == "__main__":  # pragma: no cover - workflow entry
    raise SystemExit(main())
