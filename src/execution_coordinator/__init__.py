"""Execution Coordinator runtime package."""

import sys

# The runtime uses enum.StrEnum (3.11+).  Fail with a clear message instead
# of an ImportError deep inside the package (devflow#201 pilot finding).
if sys.version_info < (3, 11):  # pragma: no cover - exercised on old interpreters only
    raise RuntimeError(
        "execution_coordinator requires Python 3.11 or newer; "
        f"found {sys.version.split()[0]}"
    )

from .agent import AgentSession, AdapterProtocolError, MutationGateway

__all__ = ["AdapterProtocolError", "AgentSession", "MutationGateway"]
