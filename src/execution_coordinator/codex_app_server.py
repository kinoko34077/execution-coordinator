from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Protocol, Sequence

from .execution_request import DispatchOutcome, ExecutionRequest


ACCESS_TOKEN_ENV = "ACCESS_TOKEN"
DEFAULT_RPC_TIMEOUT_SECONDS = 30.0
DEFAULT_TURN_TIMEOUT_SECONDS = 900.0
CODEX_APP_SERVER_COMMAND = (
    "codex",
    "app-server",
    "--listen",
    "stdio://",
    "-c",
    'model_provider="openai_chatgpt_plan"',
    "-c",
    'model_providers.openai_chatgpt_plan.name="ChatGPT plan"',
    "-c",
    'model_providers.openai_chatgpt_plan.base_url="https://api.openai.com/v1"',
    "-c",
    'model_providers.openai_chatgpt_plan.env_key="ACCESS_TOKEN"',
    "-c",
    'model_providers.openai_chatgpt_plan.wire_api="responses"',
    "-c",
    "model_providers.openai_chatgpt_plan.requires_openai_auth=false",
    "-c",
    "model_providers.openai_chatgpt_plan.supports_websockets=false",
    "-c",
    "shell_environment_policy.ignore_default_excludes=false",
)


class CodexAppServerProtocolError(RuntimeError):
    """The app-server transport returned evidence that cannot be trusted."""


class CodexAppServerRpcError(CodexAppServerProtocolError):
    """The app-server explicitly rejected one RPC request."""


class CodexAppServerTimeoutError(TimeoutError):
    """The app-server produced no trustworthy evidence before the deadline."""


class CodexTurnStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class CodexProviderContext:
    """Typed, non-secret provider context established before runtime ACK."""

    request_id: str
    worker_id: str
    thread_id: str
    resumed: bool


@dataclass(frozen=True, slots=True)
class CodexTurnResult:
    """Terminal or ambiguity evidence for one bounded app-server turn."""

    thread_id: str
    turn_id: str | None
    status: CodexTurnStatus
    reason: str | None = None

    @property
    def completed(self) -> bool:
        return self.status is CodexTurnStatus.COMPLETED


class CodexJsonLineTransport(Protocol):
    def send(self, message: dict[str, object]) -> None: ...

    def receive(self, timeout_seconds: float | None = None) -> dict[str, object] | None: ...

    def close(self) -> None: ...


class _SubprocessJsonLineTransport:
    """Minimal newline-delimited JSON stdio transport.

    The child inherits the caller environment. This object never receives,
    copies, serializes or logs the OAuth access-token value.
    """

    def __init__(self, command: Sequence[str]) -> None:
        self._process = subprocess.Popen(
            tuple(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        if self._process.stdin is None or self._process.stdout is None:
            self.close()
            raise OSError("codex app-server stdio pipes are unavailable")

        self._incoming: queue.Queue[str | Exception | None] = queue.Queue()
        self._reader_thread = threading.Thread(
            target=self._read_stdout,
            name="codex-app-server-stdout",
            daemon=True,
        )
        self._reader_thread.start()

    def _read_stdout(self) -> None:
        stdout = self._process.stdout
        if stdout is None:
            self._incoming.put(OSError("codex app-server stdout is unavailable"))
            return
        try:
            while True:
                line = stdout.readline()
                if line == "":
                    self._incoming.put(None)
                    return
                self._incoming.put(line)
        except Exception as exc:  # pragma: no cover - platform pipe failure
            self._incoming.put(exc)

    def send(self, message: dict[str, object]) -> None:
        if self._process.stdin is None:
            raise OSError("codex app-server stdin is unavailable")
        self._process.stdin.write(
            json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        self._process.stdin.flush()

    def receive(
        self,
        timeout_seconds: float | None = None,
    ) -> dict[str, object] | None:
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise CodexAppServerTimeoutError(
                "codex app-server receive deadline exceeded"
            )
        try:
            line = self._incoming.get(timeout=timeout_seconds)
        except queue.Empty as exc:
            raise CodexAppServerTimeoutError(
                "codex app-server receive deadline exceeded"
            ) from exc
        if isinstance(line, Exception):
            if isinstance(line, OSError):
                raise line
            raise OSError(str(line) or type(line).__name__) from line
        if line is None:
            return None
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CodexAppServerProtocolError(
                "codex app-server emitted malformed JSON"
            ) from exc
        if not isinstance(value, dict):
            raise CodexAppServerProtocolError(
                "codex app-server message must be a JSON object"
            )
        return value

    def close(self) -> None:
        process = getattr(self, "_process", None)
        if process is None:
            return
        if process.stdin is not None and not process.stdin.closed:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        reader = getattr(self, "_reader_thread", None)
        if reader is not None and reader.is_alive():
            reader.join(timeout=0.2)


TransportFactory = Callable[[Sequence[str]], CodexJsonLineTransport]


@dataclass(slots=True)
class _LiveSession:
    transport: CodexJsonLineTransport
    context: CodexProviderContext
    next_request_id: int
    active_turn_id: str | None = None
    reconciliation_required: bool = False


def _require_nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CodexAppServerProtocolError(f"{field} must be a non-empty string")
    return value


def _require_positive_timeout(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a positive finite number")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{field} must be a positive finite number")
    return timeout


def _bootstrap_parameters(request: ExecutionRequest) -> dict[str, str]:
    return dict(request.bootstrap.parameters)


def _runtime_policy(parameters: dict[str, str]) -> tuple[str, str, str]:
    cwd = parameters.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        raise ValueError("cwd must be a non-empty bootstrap parameter")

    sandbox = parameters.get("codex_sandbox")
    if sandbox != "read-only":
        raise ValueError("codex_sandbox must be exactly read-only for Stage-C transport")

    approval_policy = parameters.get("codex_approval_policy")
    if approval_policy != "never":
        raise ValueError(
            "codex_approval_policy must be exactly never for Stage-C transport"
        )
    return cwd, sandbox, approval_policy


def _rpc_error_reason(message: dict[str, object]) -> str | None:
    if "error" not in message:
        return None
    error = message["error"]
    if isinstance(error, dict):
        message_text = error.get("message")
        if isinstance(message_text, str) and message_text.strip():
            return message_text.strip()[:300]
    if isinstance(error, str) and error.strip():
        return error.strip()[:300]
    return "codex app-server returned an RPC error"


def _response_result(message: dict[str, object], request_id: int) -> dict[str, object]:
    if message.get("id") != request_id:
        raise CodexAppServerProtocolError(
            "codex app-server response id does not match request"
        )
    reason = _rpc_error_reason(message)
    if reason is not None:
        raise CodexAppServerRpcError(reason)
    result = message.get("result")
    if not isinstance(result, dict):
        raise CodexAppServerProtocolError(
            "codex app-server response result must be an object"
        )
    return result


def _thread_id(result: dict[str, object]) -> str:
    thread = result.get("thread")
    if not isinstance(thread, dict):
        raise CodexAppServerProtocolError(
            "codex app-server thread response is missing thread"
        )
    return _require_nonempty(thread.get("id"), "thread.id")


def _turn_id(result: dict[str, object]) -> str:
    turn = result.get("turn")
    if not isinstance(turn, dict):
        raise CodexAppServerProtocolError(
            "codex app-server turn response is missing turn"
        )
    return _require_nonempty(turn.get("id"), "turn.id")


def _terminal_turn_result(
    message: dict[str, object],
    *,
    thread_id: str,
    turn_id: str,
) -> CodexTurnResult | None:
    if "id" in message and "method" in message:
        raise CodexAppServerProtocolError(
            "unexpected app-server request during bounded turn observation"
        )
    if message.get("method") != "turn/completed":
        return None
    params = message.get("params")
    if not isinstance(params, dict):
        raise CodexAppServerProtocolError("turn/completed params must be an object")
    turn = params.get("turn")
    if not isinstance(turn, dict):
        raise CodexAppServerProtocolError("turn/completed is missing turn")
    completed_id = _require_nonempty(turn.get("id"), "turn/completed turn.id")
    if completed_id != turn_id:
        raise CodexAppServerProtocolError(
            "turn/completed turn.id does not match active turn"
        )
    raw_status = turn.get("status")
    try:
        status = CodexTurnStatus(raw_status)
    except (TypeError, ValueError) as exc:
        raise CodexAppServerProtocolError(
            "turn/completed status is unsupported"
        ) from exc
    if status is CodexTurnStatus.AMBIGUOUS:
        raise CodexAppServerProtocolError(
            "provider cannot emit synthetic ambiguous terminal status"
        )
    return CodexTurnResult(
        thread_id=thread_id,
        turn_id=turn_id,
        status=status,
        reason=None if status is CodexTurnStatus.COMPLETED else status.value,
    )


class CodexAppServerAdapter:
    """Bounded Codex app-server transport adapter.

    start() establishes an initialized app-server thread only. It does not
    send turn/start, so an auto-launch caller can preserve the accepted D3
    ordering: CLAIMED -> provider context -> ACK/RUNNING -> actual work.

    OAuth acquisition/refresh/storage is intentionally outside this adapter.
    The caller must supply an already-authorized token in ACCESS_TOKEN; the
    subprocess inherits that environment variable without the value entering
    adapter state or command arguments.
    """

    def __init__(
        self,
        *,
        transport_factory: TransportFactory = _SubprocessJsonLineTransport,
        command: Sequence[str] = CODEX_APP_SERVER_COMMAND,
        client_name: str = "kinotch_execution_coordinator",
        client_title: str = "KiNoTch. execution-coordinator",
        client_version: str = "0.1",
        rpc_timeout_seconds: float = DEFAULT_RPC_TIMEOUT_SECONDS,
        turn_timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS,
    ) -> None:
        self._transport_factory = transport_factory
        self._command = tuple(command)
        self._client_info = {
            "name": _require_nonempty(client_name, "client_name"),
            "title": _require_nonempty(client_title, "client_title"),
            "version": _require_nonempty(client_version, "client_version"),
        }
        self._rpc_timeout_seconds = _require_positive_timeout(
            rpc_timeout_seconds,
            "rpc_timeout_seconds",
        )
        self._turn_timeout_seconds = _require_positive_timeout(
            turn_timeout_seconds,
            "turn_timeout_seconds",
        )
        self._sessions: dict[str, _LiveSession] = {}

    @staticmethod
    def _receive_before_deadline(
        transport: CodexJsonLineTransport,
        deadline: float,
    ) -> dict[str, object] | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CodexAppServerTimeoutError(
                "codex app-server receive deadline exceeded"
            )
        return transport.receive(remaining)

    def _receive_response(
        self,
        transport: CodexJsonLineTransport,
        request_id: int,
        *,
        notifications: list[dict[str, object]] | None = None,
        deadline: float | None = None,
    ) -> dict[str, object]:
        if deadline is None:
            deadline = time.monotonic() + self._rpc_timeout_seconds
        while True:
            message = self._receive_before_deadline(transport, deadline)
            if message is None:
                raise EOFError("codex app-server closed before RPC response")
            if "id" not in message:
                if notifications is not None:
                    notifications.append(message)
                continue
            return _response_result(message, request_id)

    def _initialize(self, transport: CodexJsonLineTransport) -> None:
        transport.send(
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": dict(self._client_info)},
            }
        )
        self._receive_response(transport, 1)
        transport.send({"method": "initialized", "params": {}})

    def start(self, request: ExecutionRequest) -> DispatchOutcome:
        if not isinstance(request, ExecutionRequest):
            raise TypeError("request must be an ExecutionRequest")

        parameters = _bootstrap_parameters(request)
        try:
            cwd, sandbox, approval_policy = _runtime_policy(parameters)
            resume_thread_id = parameters.get("resume_thread_id")
            model = parameters.get("model")
            if resume_thread_id is None:
                if not isinstance(model, str) or not model.strip():
                    raise ValueError("model must be a non-empty bootstrap parameter")
            elif not resume_thread_id.strip():
                raise ValueError("resume_thread_id must not be empty")
        except ValueError as exc:
            return DispatchOutcome.failed(
                request_id=request.request_id,
                reason=str(exc),
                schema_version=request.schema_version,
            )

        if not os.environ.get(ACCESS_TOKEN_ENV):
            return DispatchOutcome.unavailable(
                request_id=request.request_id,
                reason="ACCESS_TOKEN is not configured",
                schema_version=request.schema_version,
            )

        try:
            transport = self._transport_factory(self._command)
        except (OSError, CodexAppServerProtocolError) as exc:
            return DispatchOutcome.unavailable(
                request_id=request.request_id,
                reason=str(exc) or type(exc).__name__,
                schema_version=request.schema_version,
            )

        try:
            self._initialize(transport)
        except (
            OSError,
            EOFError,
            CodexAppServerProtocolError,
            CodexAppServerTimeoutError,
        ) as exc:
            transport.close()
            return DispatchOutcome.failed(
                request_id=request.request_id,
                reason=str(exc) or type(exc).__name__,
                schema_version=request.schema_version,
            )

        rpc_id = 2
        common_params: dict[str, object] = {
            "cwd": cwd,
            "sandbox": sandbox,
            "approvalPolicy": approval_policy,
        }
        if resume_thread_id is None:
            method = "thread/start"
            params: dict[str, object] = {**common_params, "model": model}
        else:
            method = "thread/resume"
            params = {**common_params, "threadId": resume_thread_id}

        try:
            transport.send({"id": rpc_id, "method": method, "params": params})
            result = self._receive_response(transport, rpc_id)
            thread_id = _thread_id(result)
        except CodexAppServerRpcError as exc:
            transport.close()
            return DispatchOutcome.failed(
                request_id=request.request_id,
                reason=str(exc),
                schema_version=request.schema_version,
            )
        except CodexAppServerProtocolError as exc:
            transport.close()
            return DispatchOutcome.ambiguous(
                request_id=request.request_id,
                reason=str(exc),
                schema_version=request.schema_version,
            )
        except (OSError, EOFError, CodexAppServerTimeoutError) as exc:
            transport.close()
            return DispatchOutcome.ambiguous(
                request_id=request.request_id,
                reason=str(exc) or type(exc).__name__,
                schema_version=request.schema_version,
            )

        if resume_thread_id is not None and thread_id != resume_thread_id:
            transport.close()
            return DispatchOutcome.ambiguous(
                request_id=request.request_id,
                reason="thread/resume returned a different thread id",
                schema_version=request.schema_version,
            )
        if thread_id in self._sessions:
            transport.close()
            return DispatchOutcome.ambiguous(
                request_id=request.request_id,
                reason="thread id is already active in this adapter",
                schema_version=request.schema_version,
            )

        context = CodexProviderContext(
            request_id=request.request_id,
            worker_id=request.authority.worker_id,
            thread_id=thread_id,
            resumed=resume_thread_id is not None,
        )
        self._sessions[thread_id] = _LiveSession(
            transport=transport,
            context=context,
            next_request_id=3,
        )
        return DispatchOutcome.accepted(
            request_id=request.request_id,
            worker_id=request.authority.worker_id,
            session_id=thread_id,
            schema_version=request.schema_version,
        )

    def context(self, session_id: str) -> CodexProviderContext:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError("unknown Codex app-server session")
        return session.context

    @staticmethod
    def _abandon_session(session: _LiveSession) -> None:
        session.reconciliation_required = True
        session.transport.close()

    def run_turn(self, session_id: str | None, prompt: str) -> CodexTurnResult:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError("unknown Codex app-server session")
        if session.active_turn_id is not None:
            raise RuntimeError("Codex app-server session already has an active turn")
        if session.reconciliation_required:
            raise RuntimeError(
                "Codex app-server session requires external reconciliation before reuse"
            )

        rpc_id = session.next_request_id
        session.next_request_id += 1
        pending_notifications: list[dict[str, object]] = []
        turn_deadline = time.monotonic() + self._turn_timeout_seconds
        try:
            session.transport.send(
                {
                    "id": rpc_id,
                    "method": "turn/start",
                    "params": {
                        "threadId": session.context.thread_id,
                        "input": [{"type": "text", "text": prompt}],
                    },
                }
            )
            result = self._receive_response(
                session.transport,
                rpc_id,
                notifications=pending_notifications,
                deadline=turn_deadline,
            )
            turn_id = _turn_id(result)
            session.active_turn_id = turn_id
        except CodexAppServerRpcError as exc:
            return CodexTurnResult(
                thread_id=session.context.thread_id,
                turn_id=None,
                status=CodexTurnStatus.FAILED,
                reason=str(exc),
            )
        except CodexAppServerProtocolError as exc:
            self._abandon_session(session)
            return CodexTurnResult(
                thread_id=session.context.thread_id,
                turn_id=None,
                status=CodexTurnStatus.AMBIGUOUS,
                reason=str(exc),
            )
        except (OSError, EOFError, CodexAppServerTimeoutError) as exc:
            self._abandon_session(session)
            return CodexTurnResult(
                thread_id=session.context.thread_id,
                turn_id=None,
                status=CodexTurnStatus.AMBIGUOUS,
                reason=str(exc) or type(exc).__name__,
            )

        try:
            for message in pending_notifications:
                terminal = _terminal_turn_result(
                    message,
                    thread_id=session.context.thread_id,
                    turn_id=turn_id,
                )
                if terminal is not None:
                    return terminal

            while True:
                message = self._receive_before_deadline(
                    session.transport,
                    turn_deadline,
                )
                if message is None:
                    self._abandon_session(session)
                    return CodexTurnResult(
                        thread_id=session.context.thread_id,
                        turn_id=turn_id,
                        status=CodexTurnStatus.AMBIGUOUS,
                        reason="app-server closed before turn/completed",
                    )
                terminal = _terminal_turn_result(
                    message,
                    thread_id=session.context.thread_id,
                    turn_id=turn_id,
                )
                if terminal is not None:
                    return terminal
        except CodexAppServerTimeoutError as exc:
            self._abandon_session(session)
            return CodexTurnResult(
                thread_id=session.context.thread_id,
                turn_id=turn_id,
                status=CodexTurnStatus.AMBIGUOUS,
                reason=str(exc),
            )
        except CodexAppServerProtocolError as exc:
            self._abandon_session(session)
            return CodexTurnResult(
                thread_id=session.context.thread_id,
                turn_id=turn_id,
                status=CodexTurnStatus.AMBIGUOUS,
                reason=str(exc),
            )
        except OSError as exc:
            self._abandon_session(session)
            return CodexTurnResult(
                thread_id=session.context.thread_id,
                turn_id=turn_id,
                status=CodexTurnStatus.AMBIGUOUS,
                reason=str(exc) or type(exc).__name__,
            )
        finally:
            session.active_turn_id = None

    def close_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            session.transport.close()

    def close(self) -> None:
        for session_id in tuple(self._sessions):
            self.close_session(session_id)
