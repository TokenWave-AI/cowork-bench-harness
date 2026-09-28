#!/usr/bin/env python3
"""Drive one Codex run through the ``codex app-server`` JSON-RPC protocol.

``/goal`` is a TUI-only slash command, so ``codex exec`` cannot express goal
mode at all.  The app-server protocol can: ``thread/goal/set`` installs an
objective with an optional token budget, the model reports progress back
through ``thread/goal/updated``, and ``thread/resume`` re-enters the same
thread by id.  That gives this driver three things ``codex exec`` never had:

* a machine-readable completion signal (``goal.status == "complete"``) instead
  of "the process exited, hopefully because it finished",
* an observable budget surface (``tokensUsed`` / ``tokenBudget``), and
* a durable resume handle, so a run interrupted at the task timeout can be
  continued rather than restarted.

This module is deliberately self-contained and stdlib-only: it is delivered
into the task container as a plain file and executed by the container's own
``python3``.  It must not import ``frontier_probe``.

Wire format (verified against ``codex-cli 0.150.1`` on 2026-09-02): newline
delimited JSON, one JSON-RPC frame per line, no ``Content-Length`` headers.

The driver never handles credential bytes.  Authentication is whatever Pier
already arranged in ``$CODEX_HOME`` before this process starts.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import urlparse

CLIENT_NAME = "jira2code-goal-driver"
CLIENT_VERSION = "0.1.0"

GOAL_OBJECTIVE_MAX_CHARS = 4000
GOAL_COMPLETION_CONTRACT_MARKER = "[jira2code-goal-completion-contract-v5]"
ALL_UNFINISHED_REQUIREMENTS_MARKER = (
    "[jira2code-all-unfinished-requirements-v1]"
)
OUTCOME_FREEDOM_MARKER = "[jira2code-outcome-not-workflow-v1]"
GOAL_COMPLETION_CONTRACT = f"""\
{GOAL_COMPLETION_CONTRACT_MARKER}
{ALL_UNFINISHED_REQUIREMENTS_MARKER}
{OUTCOME_FREEDOM_MARKER}
Your goal is to complete every explicit, unfinished requirement available in
the task workplace and repository, regardless of assignee, owner, author, or
where the requirement is recorded. Do not limit the goal to the first task in
the initial instruction or to cards assigned to the current workplace user.

Available requirements may be recorded in board cards and epics, card details,
comments and status history, chat channels and direct messages, documents and
their version history, review and CI records, artifacts, repository history,
code, tests, the task README, or the initial instruction. Cover all available
records and result pages. A requirement is unfinished when the current code or
other required deliverable does not yet satisfy it, even if workflow metadata
is stale or says otherwise.

You are free to choose how you investigate, plan, implement, communicate, and
validate the work. Explicit requirements and their observable
acceptance criteria are the standard for correctness. Do not replace them with
guessed intent, and do not invent extra product requirements. When sources
conflict, use the most current and authoritative evidence available; investigate
until the conflict is resolved or record the precise genuine blocker.

Keep the goal active across turns until every discoverable explicit unfinished
requirement has been implemented and verified, or a genuine blocker prevents
further progress. A partial patch, one completed turn, elapsed time, or a subset
of passing tests is not completion. Before marking complete, audit the full
available requirement set and leave a closure record mapping each requirement
and source to its implementation and verification evidence, or to its exact
blocker. Inspect the final diff and working tree and run relevant validation.
The workplace may be read-only: completion does not require changing a board
status or mutating collaboration records unless an explicit requirement says
otherwise.

[public-question-closure-v1]
Keep a record of unanswered questions, deferred replies, and requested
clarifications. Follow up on the actual pending topic when appropriate; unrelated
messages do not resolve it. Before completion, check available replies and late
updates, resolve requirements they reveal, and retry recoverable tool failures
within the remaining budget. A temporary colleague delay is not a final blocker.
Distinguish completed requirements from unverified assumptions in the closure
record. Use only information available through this task's authorized surfaces.
"""


def apply_goal_completion_contract(objective: str | None = None) -> str:
    """Build a compact goal objective, optionally extending a custom objective.

    The complete task belongs in the first-turn instruction, not in
    ``thread/goal/set``. Codex currently caps that RPC's objective at 4000
    characters, so the default objective is deliberately task-independent.
    """

    base = (objective or "").strip()
    if GOAL_COMPLETION_CONTRACT_MARKER in base:
        result = base
    elif base:
        result = f"{base}\n\n{GOAL_COMPLETION_CONTRACT.rstrip()}"
    else:
        result = GOAL_COMPLETION_CONTRACT.rstrip()
    if len(result) > GOAL_OBJECTIVE_MAX_CHARS:
        raise ValueError(
            "goal objective exceeds Codex's "
            f"{GOAL_OBJECTIVE_MAX_CHARS}-character limit: {len(result)}"
        )
    return f"{result}\n"

# Terminal goal states: the objective is either met or cannot progress without
# an operator.  Anything else means "keep turning".
GOAL_DONE = "complete"
# How long a turn may keep running after the goal first reports ``complete``.
# Codex marks the goal done before the closing assistant message is emitted, so
# cutting immediately would lose it; draining to the full deadline (the old
# behaviour) wasted the rest of the budget when the stream stalled on a retry.
GOAL_DONE_GRACE_SECONDS = 180.0
GOAL_STUCK = ("blocked", "budgetLimited", "usageLimited")
RATE_LIMIT_RETRY_BASE_SECONDS = 5.0
RATE_LIMIT_RETRY_MAX_SECONDS = 60.0

# approvalPolicy=never + sandbox=danger-full-access should mean the server never
# asks.  If it asks anyway, a driver that ignores the request deadlocks the run,
# so answer permissively and record that it happened.
APPROVAL_REPLIES: dict[str, dict[str, Any]] = {
    "commandExecution/requestApproval": {"decision": "decline"},
    "fileChange/requestApproval": {"decision": "decline"},
    "applyPatchApproval": {"decision": "denied"},
    "execCommandApproval": {"decision": "denied"},
}
APPROVAL_FALLBACK = {"decision": "decline"}

CONTINUATION_PROMPT = (
    "Your goal is still open. Re-read the full objective and its completion "
    "contract. Determine what remains by any effective approach you choose. "
    "The scope is every explicit unfinished requirement available in board "
    "cards, comments, chats, documents, reviews, CI records, artifacts, repository "
    "history, code, tests, the task README, and the initial instruction, regardless "
    "of assignee or owner. Keep working autonomously on everything that remains. "
    "A partial patch, one completed turn, elapsed time, or a subset of passing "
    "tests is not completion. Before completion, audit the full available "
    "requirement set and map every requirement to implementation and verification "
    "evidence or an exact genuine blocker. Keep the goal active until that closure "
    "condition is met."
)

WORKPLACE_SERVICE_SOCKET = "/run/workplace/service.sock"
WORKPLACE_MCP_SERVER = "workplace"
WORKPLACE_REQUIRED_TOOL = "workplace_help"
PROVIDER_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ProtocolError(RuntimeError):
    pass


def is_provider_rate_limit(error: Any) -> bool:
    """Recognize transport 429s, never ordinary task/tool errors or auth failures."""
    if not isinstance(error, dict):
        return False
    detail = " ".join(str(error.get(key) or "") for key in ("message", "additionalDetails"))
    if any(word in detail.lower() for word in ("insufficient_quota", "billing_hard_limit")):
        return False
    info = error.get("codexErrorInfo") or {}
    if isinstance(info, dict):
        for value in info.values():
            if isinstance(value, dict) and value.get("httpStatusCode") == 429:
                return True
    return bool(re.search(r"(?:status[:\s]+429\b|\bHTTP\s+429\b|\b429\s+Too Many Requests)", detail, re.I))


def is_provider_denial(error: Any) -> bool:
    """Only structured provider error slots, never ordinary task/tool prose."""
    if not isinstance(error, dict):
        return False
    text = json.dumps(error)
    return bool(re.search(r'\b(?:401|403)\b|invalid.?api.?key|authentication|unauthorized|'
                          r'forbidden|cyber.?verification|\brefusal\b|\bmodelrefusal\b|'
                          r'content[_ -]?filter|policy[_ -]?(?:violation|denial)|'
                          r'safety[_ -]?(?:refusal|violation|policy)', text, re.I))


def build_app_server_argv(
    codex_bin: str,
    *,
    provider_id: str | None = None,
    provider_base_url: str | None = None,
    provider_env_key: str = "OPENAI_API_KEY",
    provider_supports_websockets: bool | None = None,
    provider_request_max_retries: int | None = None,
    provider_stream_max_retries: int | None = None,
) -> list[str]:
    """Build a Codex app-server command for an optional compatible provider."""

    argv = [codex_bin]
    if provider_id:
        if not PROVIDER_ID_PATTERN.fullmatch(provider_id):
            raise ValueError(f"invalid model provider id: {provider_id!r}")
        if provider_id in {"openai", "ollama", "lmstudio"}:
            raise ValueError(f"reserved model provider id: {provider_id!r}")
        parsed = urlparse(provider_base_url or "")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(
                f"invalid model provider base URL: {provider_base_url!r}"
            )
        if parsed.username or parsed.password:
            raise ValueError("model provider base URL must not contain credentials")
        if not ENV_KEY_PATTERN.fullmatch(provider_env_key):
            raise ValueError(f"invalid provider environment key: {provider_env_key!r}")
        config = {
            "model_provider": provider_id,
            f"model_providers.{provider_id}.name": provider_id,
            f"model_providers.{provider_id}.base_url": provider_base_url,
            f"model_providers.{provider_id}.env_key": provider_env_key,
            f"model_providers.{provider_id}.wire_api": "responses",
        }
        if provider_supports_websockets is not None:
            config[
                f"model_providers.{provider_id}.supports_websockets"
            ] = provider_supports_websockets
        for name, value in (
            ("request_max_retries", provider_request_max_retries),
            ("stream_max_retries", provider_stream_max_retries),
        ):
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"{name} must be a non-negative integer")
                config[f"model_providers.{provider_id}.{name}"] = value
        for key, value in config.items():
            encoded = json.dumps(value, ensure_ascii=True)
            argv.extend(["-c", f"{key}={encoded}"])
    elif (
        provider_base_url
        or provider_supports_websockets is not None
        or provider_request_max_retries is not None
        or provider_stream_max_retries is not None
    ):
        raise ValueError("model provider settings require a provider id")
    argv.extend(["app-server", "--listen", "stdio://"])
    return argv


def validate_workplace_mcp_status(result: Any) -> int:
    """Require the live workplace server and its entry-point tool.

    ``required = true`` makes Codex fail when the configured process cannot
    initialize.  This second check closes the remaining wiring gap: a server
    with the wrong name or stale tool catalog must also fail before the first
    paid model turn, rather than leaving the model to improvise without the
    task's workplace context.
    """

    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, list):
        raise ProtocolError("MCP status response has no server inventory")
    workplace = next(
        (
            item
            for item in data
            if isinstance(item, dict)
            and item.get("name") == WORKPLACE_MCP_SERVER
        ),
        None,
    )
    if workplace is None:
        raise ProtocolError("required workplace MCP server is not registered")
    tools = workplace.get("tools")
    if not isinstance(tools, dict) or WORKPLACE_REQUIRED_TOOL not in tools:
        raise ProtocolError(
            "required workplace MCP tool is unavailable: "
            f"{WORKPLACE_REQUIRED_TOOL}"
        )
    return len(tools)


class AppServerClient:
    """Minimal JSON-RPC client over one ``codex app-server`` stdio process."""

    def __init__(self, argv: list[str], *, events_path: str | None, env=None):
        self._events = open(events_path, "a", buffering=1) if events_path else None
        self._events_lock = threading.Lock()
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
        )
        self._next_id = 0
        self._send_lock = threading.Lock()
        self._inbox: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._responses: dict[int, dict[str, Any]] = {}
        self._responses_cv = threading.Condition()
        self._closed = threading.Event()
        self._stderr_tail: list[str] = []
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._errors = threading.Thread(target=self._read_stderr, daemon=True)
        self._errors.start()

    # -- plumbing ---------------------------------------------------------
    def _mirror(self, direction: str, frame: dict[str, Any]) -> None:
        if self._events is None:
            return
        record = {"t": round(time.time(), 3), "dir": direction, "frame": frame}
        with self._events_lock:
            self._events.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _read_stdout(self) -> None:
        try:
            for line in self._proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    self._mirror("in-unparsed", {"raw": line[:4000]})
                    continue
                self._mirror("in", frame)
                if "id" in frame and "method" not in frame:
                    with self._responses_cv:
                        self._responses[frame["id"]] = frame
                        self._responses_cv.notify_all()
                else:
                    self._inbox.put(frame)
        finally:
            self._closed.set()
            with self._responses_cv:
                self._responses_cv.notify_all()
            self._inbox.put({"__eof__": True})

    def _read_stderr(self) -> None:
        for line in self._proc.stderr:  # type: ignore[union-attr]
            line = line.rstrip("\n")
            key = os.environ.get('TOKENWAVE_API_KEY', '')
            if key:
                line = line.replace(key, '[REDACTED]')
            self._mirror('stderr', {'text': line})
            self._stderr_tail.append(line)
            del self._stderr_tail[:-200]

    def stderr_tail(self, count: int = 20) -> str:
        return "\n".join(self._stderr_tail[-count:])

    def _write(self, frame: dict[str, Any]) -> None:
        payload = json.dumps(frame, ensure_ascii=False)
        with self._send_lock:
            if self._proc.stdin is None or self._proc.poll() is not None:
                raise ProtocolError("app-server stdin is closed")
            self._proc.stdin.write(payload + "\n")
            self._proc.stdin.flush()
        self._mirror("out", frame)

    # -- public API -------------------------------------------------------
    def request(self, method: str, params: dict[str, Any], *, timeout: float = 120.0):
        with self._send_lock:
            self._next_id += 1
            request_id = self._next_id
        self._write(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        deadline = time.monotonic() + timeout
        with self._responses_cv:
            while request_id not in self._responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProtocolError(f"timed out waiting for {method} response")
                if self._closed.is_set() and request_id not in self._responses:
                    raise ProtocolError(
                        f"app-server exited before answering {method}; "
                        f"stderr tail:\n{self.stderr_tail()}"
                    )
                self._responses_cv.wait(min(remaining, 1.0))
            frame = self._responses.pop(request_id)
        if "error" in frame:
            raise ProtocolError(f"{method} failed: {json.dumps(frame['error'])}")
        return frame.get("result")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def next_event(self, timeout: float):
        try:
            return self._inbox.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._events:
            self._events.close()


def log(message: str) -> None:
    """Progress line for Pier's captured agent stdout."""
    sys.stdout.write(f"[goal-driver] {message}\n")
    sys.stdout.flush()


def summarize_item(item: dict[str, Any]) -> str | None:
    """One progress line per completed thread item, for the captured stdout.

    Full fidelity already lives in the mirrored event log and in Codex's own
    rollout session JSONL (which is what Pier converts into the ATIF
    trajectory), so this is intentionally lossy and cheap.
    """

    kind = item.get("type") or "item"
    if kind == "commandExecution":
        command = item.get("command")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        return f"exec[{item.get('exitCode')}] {str(command or '')[:200]}"
    if kind == "fileChange":
        changes = item.get("changes") or []
        paths = []
        for change in changes if isinstance(changes, list) else []:
            if isinstance(change, dict):
                paths.append(str(change.get("path") or change.get("target") or "?"))
        return f"edit[{item.get('status')}] {', '.join(paths[:6])[:200]}"
    if kind == "agentMessage":
        return f"assistant: {str(item.get('text') or '')[:300]}"
    if kind == "plan":
        return f"plan: {str(item.get('text') or '')[:300]}"
    if kind in ("mcpToolCall", "dynamicToolCall"):
        return f"{kind}: {item.get('tool') or item.get('namespace') or ''}"
    if kind == "contextCompaction":
        return "context compacted"
    if kind in ("userMessage", "reasoning"):
        return None
    return kind


class GoalRun:
    """One goal-mode Codex run: start or resume a thread, then turn until done."""

    def __init__(self, client: AppServerClient, args: argparse.Namespace):
        self.client = client
        self.args = args
        self.thread_id: str | None = None
        self.turns = 0
        self.tokens_used = 0
        self.goal: dict[str, Any] | None = None
        self.errors: list[dict[str, Any]] = []
        self.approvals_answered = 0
        self.stop_reason = "unknown"
        self.rate_limit_max_retries = getattr(args, "rate_limit_max_retries", 0)
        if self.rate_limit_max_retries < 0:
            raise ValueError("rate_limit_max_retries must be non-negative")
        self.rate_limit_retries: list[dict[str, Any]] = []
        self._awaiting_automatic_turn = False
        self._pending_rate_limit_retry: dict[str, Any] | None = None
        self.deadline = (
            time.monotonic() + args.deadline_seconds
            if args.deadline_seconds > 0
            else None
        )
        self._interrupted = threading.Event()
        # ``turn/interrupt`` requires the turn id; without it app-server answers
        # -32600 and the turn keeps running until the harness kills the process.
        self._active_turn_id: str | None = None
        # Set when ``thread/goal/updated`` first reports ``complete`` mid-turn.
        # The turn is given a bounded grace window to emit its closing message
        # instead of draining against the full wall-clock deadline.
        self._goal_done_at: float | None = None

    # -- state ------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        return {
            "schema": "jira2code.goal-driver.state/1",
            "threadId": self.thread_id,
            "model": self.args.model,
            "effort": self.args.effort,
            "cwd": self.args.cwd,
            "turns": self.turns,
            "tokensUsed": self.tokens_used,
            "tokenBudget": self.args.token_budget,
            "goal": self.goal,
            "stopReason": self.stop_reason,
            "approvalsAnswered": self.approvals_answered,
            "errors": self.errors[-20:],
            "rateLimitMaxRetries": self.rate_limit_max_retries,
            "rateLimitRetries": self.rate_limit_retries,
            "updatedAt": round(time.time(), 3),
        }

    def save_state(self) -> None:
        """Persist the resume handle atomically; a torn file is unresumable."""
        if not self.args.state_path:
            return
        temporary = f"{self.args.state_path}.tmp"
        with open(temporary, "w") as handle:
            json.dump(self.state(), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.args.state_path)

    def time_left(self) -> float:
        if self.deadline is None:
            return float("inf")
        return self.deadline - time.monotonic()

    # -- protocol ---------------------------------------------------------
    def initialize(self) -> None:
        result = self.client.request(
            "initialize",
            {
                "clientInfo": {
                    "name": CLIENT_NAME,
                    "version": CLIENT_VERSION,
                    "title": "Jira2Code goal-mode probe driver",
                }
            },
            timeout=self.args.rpc_timeout,
        )
        log(f"initialized: codexHome={(result or {}).get('codexHome')}")

    def _thread_params(self) -> dict[str, Any]:
        # Model provider, base URL and auth all come from the config.toml Pier
        # already wrote into CODEX_HOME; do not restate them here.
        return {
            "cwd": self.args.cwd,
            "model": self.args.model,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }

    def start_or_resume(self) -> None:
        existing = self._existing_thread_id()
        if existing:
            params = dict(self._thread_params())
            params["threadId"] = existing
            result = self.client.request(
                "thread/resume", params, timeout=self.args.rpc_timeout
            )
            self.thread_id = ((result or {}).get("thread") or {}).get("id") or existing
            log(f"resumed thread {self.thread_id}")
        else:
            result = self.client.request(
                "thread/start", self._thread_params(), timeout=self.args.rpc_timeout
            )
            self.thread_id = ((result or {}).get("thread") or {}).get("id")
            log(f"started thread {self.thread_id}")
        if not self.thread_id:
            raise ProtocolError(
                f"no threadId in thread start/resume response: {json.dumps(result)[:400]}"
            )
        self.verify_workplace_mcp()

    def verify_workplace_mcp(self) -> None:
        result = self.client.request('mcpServerStatus/list',
            {'threadId': self.thread_id, 'detail': 'toolsAndAuthOnly'}, timeout=self.args.rpc_timeout)
        servers = result.get('data', [])
        server = next((s for s in servers if s.get('name') == 'cowork'), None)
        expected = int(os.environ['COWORK_EXPECTED_TOOLS'])
        if not server or len(server.get('tools', {})) != expected:
            raise ProtocolError('Incomplete isolated task tool registry: ' + str(server))
        names = list(server['tools'])
        if not any(n == 'terminal' or n.endswith('__terminal') for n in names):
            raise ProtocolError('Task terminal unavailable')
        declared = os.environ.get('COWORK_EXPECTED_TOOL_NAMES')
        if declared:
            expected_names = set(json.loads(declared))
            actual_names = {name if name.startswith('mcp__') else 'mcp__cowork__' + name for name in names}
            if (actual_names != expected_names or len(servers) != 1
                    or server.get('toolsError')):
                raise ProtocolError('Incomplete isolated task tool registry: exact names differ')
        with open(os.path.join(os.path.dirname(self.args.state_path), 'MCP_STATUS.json'), 'w') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        log(f'Isolated task MCP ready: {len(names)} tools')

    def _existing_thread_id(self) -> str | None:
        if not self.args.resume or not self.args.state_path:
            return None
        try:
            with open(self.args.state_path) as handle:
                previous = json.load(handle)
        except (OSError, ValueError):
            log("resume requested but no readable prior state; starting fresh")
            return None
        thread_id = previous.get("threadId")
        if isinstance(thread_id, str) and thread_id:
            self.turns = int(previous.get("turns") or 0)
            self.tokens_used = int(previous.get("tokensUsed") or 0)
            self.rate_limit_retries = list(previous.get("rateLimitRetries") or [])
            return thread_id
        log("resume requested but prior state has no threadId; starting fresh")
        return None

    def set_goal(self, objective: str) -> None:
        if not objective.strip() or len(objective) > GOAL_OBJECTIVE_MAX_CHARS:
            raise ProtocolError('Goal objective must contain 1 to 4000 characters')
        params: dict[str, Any] = {
            "threadId": self.thread_id,
            "objective": objective,
            "status": "active",
        }
        if self.args.token_budget:
            params["tokenBudget"] = self.args.token_budget
        result = self.client.request(
            "thread/goal/set", params, timeout=self.args.rpc_timeout
        )
        self.goal = (result or {}).get("goal")
        log(
            "goal set: status="
            f"{(self.goal or {}).get('status')} budget={self.args.token_budget or 'none'}"
        )

    def refresh_goal(self) -> dict[str, Any] | None:
        try:
            result = self.client.request(
                "thread/goal/get",
                {"threadId": self.thread_id},
                timeout=self.args.rpc_timeout,
            )
        except ProtocolError as exc:
            log(f"goal/get failed ({exc}); relying on last notification")
            return self.goal
        goal = (result or {}).get("goal")
        if goal:
            self.goal = goal
        return self.goal

    # -- turn loop --------------------------------------------------------
    def _handle_server_request(self, frame: dict[str, Any]) -> None:
        method = frame.get("method") or ""
        reply = APPROVAL_REPLIES.get(method)
        if reply is None:
            log(f"WARNING unexpected server request {method}; declining")
            reply = APPROVAL_FALLBACK
        self.approvals_answered += 1
        try:
            self.client.respond(frame.get("id"), dict(reply))
        except ProtocolError as exc:
            log(f"failed to answer server request {method}: {exc}")

    def _handle_notification(self, frame: dict[str, Any], turn_id: str | None):
        """Return the completed ``Turn`` when this turn finishes, else ``None``."""

        method = frame.get("method")
        params = frame.get("params") or {}

        if method == "turn/started":
            # Codex 0.147 can acknowledge ``turn/start`` with a provisional
            # turn id and then publish a different authoritative id in the
            # notification stream. ``turn/interrupt`` accepts only the latter.
            # Follow the notification so a bounded Goal run can actually stop
            # at its wall-clock deadline instead of draining the outer Harbor
            # watchdog.
            started = params.get("turn") or {}
            actual_turn_id = started.get("id") or params.get("turnId")
            if isinstance(actual_turn_id, str) and actual_turn_id:
                if self._active_turn_id and self._active_turn_id != actual_turn_id:
                    log(
                        "turn/start response id replaced by authoritative "
                        f"turn/started id {actual_turn_id}"
                    )
                self._active_turn_id = actual_turn_id
            if self._awaiting_automatic_turn and self._active_turn_id:
                self._awaiting_automatic_turn = False
                self.turns += 1
                log(f"automatic turn {self.turns} started (id={self._active_turn_id})")
                if self._pending_rate_limit_retry is not None:
                    self._pending_rate_limit_retry.update(
                        status="resumed", nextTurnId=self._active_turn_id,
                        resumedAt=round(time.time(), 3),
                    )
                    self._pending_rate_limit_retry = None
                self.save_state()
            return None

        if method == "item/completed":
            line = summarize_item(params.get("item") or {})
            if line:
                log(line)
            return None
        if method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") or {}
            usage = usage.get('total') or usage
            total = usage.get("totalTokens") or usage.get("total_tokens")
            if isinstance(total, int):
                self.tokens_used = total
            return None
        if method == "thread/goal/updated":
            self.goal = params.get("goal") or self.goal
            status = (self.goal or {}).get("status")
            used = (self.goal or {}).get("tokensUsed")
            log(f"goal update: status={status} tokensUsed={used}")
            if status == GOAL_DONE and self._goal_done_at is None:
                self._goal_done_at = time.monotonic()
                log(
                    "goal complete mid-turn; allowing "
                    f"{GOAL_DONE_GRACE_SECONDS:.0f}s for the turn to close"
                )
            self.save_state()
            return None
        if method == "thread/goal/cleared":
            log("WARNING goal was cleared by the session")
            self.goal = None
            return None
        if method == "error":
            error = params.get("error") or {}
            self.errors.append(
                {
                    "turnId": params.get("turnId"),
                    "willRetry": params.get("willRetry"),
                    "error": error,
                }
            )
            log(
                f"error (willRetry={params.get('willRetry')}): "
                f"{json.dumps(error)[:400]}"
            )
            if is_provider_denial(error):
                self.stop_reason = 'provider_refusal'
                self._interrupt_turn()
                self.save_state()
                return {'id': self._active_turn_id or turn_id, 'status': 'failed', 'error': error}
            return None
        if method == "thread/compacted":
            log("context compacted by the server")
            return None
        if method == "turn/completed":
            turn = params.get("turn") or {}
            expected_turn_id = self._active_turn_id or turn_id
            if expected_turn_id is None or turn.get("id") == expected_turn_id:
                return turn
            return None
        return None

    def _drain_until_turn_end(self, turn_id: str | None) -> dict[str, Any] | None:
        while True:
            if self._interrupted.is_set():
                return None
            remaining = self.time_left()
            if remaining <= 0:
                log("wall-clock deadline reached; interrupting the active turn")
                self.stop_reason = "deadline"
                self._interrupt_turn()
                return None
            if self._goal_done_at is not None:
                grace = GOAL_DONE_GRACE_SECONDS - (
                    time.monotonic() - self._goal_done_at
                )
                if grace <= 0:
                    log("goal complete and grace window elapsed; interrupting")
                    self.stop_reason = "goal-complete"
                    self._interrupt_turn()
                    return None
                remaining = min(remaining, grace)
            frame = self.client.next_event(timeout=min(remaining, 5.0))
            if frame is None:
                continue
            if frame.get("__eof__"):
                raise ProtocolError(
                    "app-server exited mid-turn; stderr tail:\n"
                    f"{self.client.stderr_tail()}"
                )
            if "id" in frame and "method" in frame:
                self._handle_server_request(frame)
                continue
            completed = self._handle_notification(frame, turn_id)
            if completed is not None:
                return completed
            # A goal may become terminal between automatic turns. There is no
            # active turn to drain in this case; never send a duplicate prompt.
            if self._awaiting_automatic_turn and frame.get("method") == "thread/goal/updated":
                goal_status = (self.goal or {}).get("status")
                if goal_status == GOAL_DONE or goal_status in GOAL_STUCK:
                    self.stop_reason = (
                        "goal-complete" if goal_status == GOAL_DONE else f"goal-{goal_status}"
                    )
                    return None

    def _interrupt_turn(self) -> None:
        params: dict[str, Any] = {"threadId": self.thread_id}
        if self._active_turn_id:
            params["turnId"] = self._active_turn_id
        try:
            self.client.request("turn/interrupt", params, timeout=30.0)
        except ProtocolError as exc:
            log(f"turn/interrupt failed: {exc}")

    def run_turn(self, text: str) -> dict[str, Any] | None:
        params: dict[str, Any] = {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": text}],
        }
        if self.args.effort:
            params["effort"] = self.args.effort
        self._awaiting_automatic_turn = False
        result = self.client.request(
            "turn/start", params, timeout=self.args.rpc_timeout
        )
        turn_id = ((result or {}).get("turn") or {}).get("id")
        self._active_turn_id = turn_id
        self._goal_done_at = None
        self.turns += 1
        log(f"turn {self.turns} started (id={turn_id})")
        turn = self._drain_until_turn_end(turn_id)
        self._active_turn_id = None
        if turn is not None:
            log(
                f"turn {self.turns} {turn.get('status')} "
                f"in {turn.get('durationMs')}ms"
            )
        self.save_state()
        return turn

    def _continue_automatically(self, *, reactivate_goal: bool = False) -> dict[str, Any] | None:
        """Adopt Codex's own continuation without injecting another user turn."""
        self._active_turn_id = None
        self._goal_done_at = None
        self._awaiting_automatic_turn = True
        if self._interrupted.is_set() or self.time_left() <= 0:
            self.stop_reason = "signal" if self._interrupted.is_set() else "deadline"
            return None
        if reactivate_goal:
            self._pending_rate_limit_retry = self.rate_limit_retries[-1]
            # Status-only activation preserves the objective, token budget and
            # all usage counters. Codex 0.150.1 itself starts the next turn, so
            # following this RPC with turn/start would schedule the work twice.
            result = self.client.request(
                "thread/goal/set",
                {"threadId": self.thread_id, "status": "active"},
                timeout=min(self.args.rpc_timeout, max(0.1, self.time_left())),
            )
            self.goal = (result or {}).get("goal") or self.goal
        turn = self._drain_until_turn_end(None)
        self._active_turn_id = None
        if turn is not None:
            log(f"turn {self.turns} {turn.get('status')} in {turn.get('durationMs')}ms")
        self.save_state()
        return turn

    def _prepare_rate_limit_retry(self, turn: dict[str, Any]) -> bool:
        """Back off inside the existing thread, worktree and wall-clock budget."""
        if is_provider_denial(turn.get('error')):
            return False
        if not is_provider_rate_limit(turn.get("error")):
            return False
        if (self.goal or {}).get("status") in (GOAL_DONE, "budgetLimited", "usageLimited"):
            return False
        if len(self.rate_limit_retries) >= self.rate_limit_max_retries:
            return False
        if self.args.max_turns and self.turns >= self.args.max_turns:
            self.stop_reason = "max-turns"
            return False
        if self.time_left() <= 0:
            self.stop_reason = "deadline"
            return False
        ordinal = len(self.rate_limit_retries) + 1
        remaining = self.time_left()
        delay = max(0.0, min(
            RATE_LIMIT_RETRY_MAX_SECONDS,
            RATE_LIMIT_RETRY_BASE_SECONDS * 2 ** min(ordinal - 1, 10),
            remaining,
        ))
        retry = {"retry": ordinal, "failedTurnId": turn.get("id"),
                 "category": "provider-http-429", "delaySeconds": delay,
                 "startedAt": round(time.time(), 3), "status": "backoff"}
        self.rate_limit_retries.append(retry)
        self.save_state()
        log(f"provider 429; same-thread retry {ordinal}/{self.rate_limit_max_retries} after {delay:.1f}s")
        wait_started = time.monotonic()
        interrupted = self._interrupted.wait(delay)
        retry["waitedSeconds"] = round(time.monotonic() - wait_started, 3)
        retry["backoffEndedAt"] = round(time.time(), 3)
        if interrupted:
            retry["status"] = "interrupted"
            self.stop_reason = "signal"
            self.save_state()
            return False
        if self.time_left() <= 0:
            retry["status"] = "deadline"
            self.stop_reason = "deadline"
            self.save_state()
            return False
        return True

    def loop(self, instruction: str) -> int:
        turn = self.run_turn(instruction)
        while True:
            if self._interrupted.is_set():
                self.stop_reason = self.stop_reason if self.stop_reason != "unknown" else "signal"
                break
            if turn is None:
                # Deadline, signal, or interrupt: state is already persisted and
                # the thread is resumable, so this is a stop, not a failure.
                if self.stop_reason == "unknown":
                    self.stop_reason = "interrupted"
                break
            status = turn.get("status")
            if status == "failed":
                self.errors.append({"turn": turn.get("id"), "error": turn.get("error")})
                if self._prepare_rate_limit_retry(turn):
                    turn = self._continue_automatically(reactivate_goal=True)
                    continue
                if self.stop_reason == "unknown":
                    self.stop_reason = "turn-failed"
                log(f"turn failed: {json.dumps(turn.get('error'))[:400]}")
                break
            # Use the goal notifications consumed through this completed turn.
            # A goal/get response may already reflect a later automatic turn's
            # failure whose events are still queued, hiding its recoverable 429.
            goal_status = (self.goal or {}).get("status")
            if goal_status == GOAL_DONE:
                self.stop_reason = "goal-complete"
                log("goal reported complete")
                break
            if goal_status in GOAL_STUCK:
                self.stop_reason = f"goal-{goal_status}"
                log(f"goal cannot progress: {goal_status}")
                break
            if self.args.max_turns and self.turns >= self.args.max_turns:
                self.stop_reason = "max-turns"
                log(f"reached --max-turns {self.args.max_turns}")
                break
            if self.time_left() <= 0:
                self.stop_reason = "deadline"
                break
            log(f"goal still {goal_status}; continuing")
            turn = self._continue_automatically()

        self.save_state()
        log(
            f"finished: reason={self.stop_reason} turns={self.turns} "
            f"tokensUsed={self.tokens_used} threadId={self.thread_id}"
        )
        # A run that stopped at a budget or wall-clock boundary produced real
        # work and a resumable thread; only a protocol/turn failure is an error.
        return 1 if self.stop_reason in ("turn-failed", "provider_refusal") else 0

    def request_stop(self) -> None:
        self.stop_reason = "signal"
        self._interrupted.set()
        self._interrupt_turn()


def read_text_argument(inline: str | None, path: str | None, label: str) -> str:
    """Prefer the file form: instructions are long and must not sit in argv."""
    if path:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    if inline is not None:
        return inline
    raise SystemExit(f"one of --{label} / --{label}-file is required")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one goal-mode Codex session over the app-server protocol"
    )
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider-id", default=None)
    parser.add_argument("--provider-base-url", default=None)
    parser.add_argument("--provider-env-key", default="OPENAI_API_KEY")
    parser.add_argument(
        "--provider-supports-websockets",
        choices=("true", "false"),
        default=None,
    )
    parser.add_argument("--provider-request-max-retries", type=int, default=None)
    parser.add_argument("--provider-stream-max-retries", type=int, default=None)
    parser.add_argument("--rate-limit-max-retries", type=int, default=0)
    parser.add_argument("--effort", default=None)
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--instruction", default=None)
    parser.add_argument("--instruction-file", default=None)
    parser.add_argument(
        "--objective",
        default=None,
        help="Goal objective; defaults to the instruction text.",
    )
    parser.add_argument("--objective-file", default=None)
    parser.add_argument("--events-path", default=None)
    parser.add_argument("--state-path", default=None)
    parser.add_argument("--token-budget", type=int, default=0)
    parser.add_argument("--max-turns", type=int, default=0)
    parser.add_argument(
        "--deadline-seconds",
        type=float,
        default=0.0,
        help="Stop and leave the thread resumable after this much wall clock.",
    )
    parser.add_argument("--rpc-timeout", type=float, default=300.0)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume the threadId recorded in --state-path when one is present.",
    )
    args = parser.parse_args(argv)
    if not args.cwd:
        args.cwd = os.getcwd()
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    instruction = read_text_argument(
        args.instruction, args.instruction_file, "instruction"
    )
    objective = (
        read_text_argument(args.objective, args.objective_file, "objective")
        if (args.objective is not None or args.objective_file)
        else instruction
    )
    if not instruction.strip():
        raise SystemExit("refusing to start a turn with an empty instruction")

    for path in (args.events_path, args.state_path):
        if path:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)

    supports_websockets = (
        args.provider_supports_websockets == "true"
        if args.provider_supports_websockets is not None
        else None
    )
    client = AppServerClient(
        build_app_server_argv(
            args.codex_bin,
            provider_id=args.provider_id,
            provider_base_url=args.provider_base_url,
            provider_env_key=args.provider_env_key,
            provider_supports_websockets=supports_websockets,
            provider_request_max_retries=args.provider_request_max_retries,
            provider_stream_max_retries=args.provider_stream_max_retries,
        ),
        events_path=args.events_path,
    )
    run = GoalRun(client, args)

    def on_signal(signum, _frame):
        log(f"received signal {signum}; interrupting and saving resume state")
        run.request_stop()

    for signal_name in ("SIGTERM", "SIGINT"):
        handled = getattr(signal, signal_name, None)
        if handled is not None:
            signal.signal(handled, on_signal)

    try:
        run.initialize()
        run.start_or_resume()
        run.set_goal(objective)
        run.save_state()
        return run.loop(instruction)
    except ProtocolError as exc:
        run.stop_reason = "protocol-error"
        run.errors.append({"protocol": str(exc)})
        run.save_state()
        log(f"FATAL {exc}")
        return 2
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
