"""Persistent Claude SDK client manager.

Manages long-lived ClaudeSDKClient instances per Telegram thread, enabling:
- Follow-up messages while Claude is working (queued as next turn)
- Actual /stop via client.interrupt() (not just Python task cancellation)
- Independent threads (each thread gets its own CLI subprocess)
- Correct per-turn cost tracking via cumulative cost deltas
"""

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    Message,
    ResultMessage,
    ThinkingBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk._errors import MessageParseError
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk.types import StreamEvent

from .exceptions import ClaudeProcessError
from .sdk_integration import ClaudeSDKManager, ClaudeResponse, StreamUpdate

logger = structlog.get_logger()

# How long to wait for a ResultMessage after interrupt() before giving up
_INTERRUPT_TIMEOUT_S = 10.0

# Default idle timeout for cleanup (30 minutes)
_DEFAULT_IDLE_TIMEOUT_S = 1800

# How long to wait for the continuation ResultMessage after injection
# If the second turn doesn't complete within this window, fall back to idle
_INJECTION_DRAIN_TIMEOUT_S = 120.0


@dataclass
class TurnContext:
    """Context for a single Claude turn within a persistent session."""

    prompt: str
    stream_callback: Optional[Callable]
    response_future: "asyncio.Future[PersistentResponse]"
    started_at: float
    messages: List[Message] = field(default_factory=list)
    result_raw_data: Dict[str, Any] = field(default_factory=dict)
    # Diagnostic tracking — per-turn token accounting
    api_call_count: int = 0
    total_input_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_output_tokens: int = 0
    last_message_at: float = 0.0
    last_message_type: str = ""
    last_stream_event_type: str = ""  # Inner event type from most recent StreamEvent


@dataclass
class PersistentResponse:
    """Response from a persistent client turn."""

    content: str
    session_id: str
    cost: float  # DELTA cost for this turn only
    duration_ms: int
    num_turns: int
    is_error: bool = False
    is_interrupted: bool = False
    stop_reason: Optional[str] = None  # SDK stop_reason: "end_turn", "max_tokens", etc.
    tools_used: List[Dict[str, Any]] = field(default_factory=list)
    context_window: Optional[int] = None
    total_input_tokens: Optional[int] = None


@dataclass
class StopResult:
    """Result of stopping a client."""

    was_busy: bool
    discarded_messages: List[str]


@dataclass
class PersistentClientEntry:
    """A persistent Claude client for a single Telegram thread."""

    client: ClaudeSDKClient
    state: str  # "idle", "busy", or "draining"
    state_key: str
    working_directory: Path
    session_id: Optional[str] = None
    last_activity: float = field(default_factory=time.time)
    previous_cumulative_cost: float = 0.0
    pending_turns: int = 0
    collector_task: Optional[asyncio.Task] = None
    current_turn: Optional[TurnContext] = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _interrupted: bool = False
    _watchdog_task: Optional[asyncio.Task] = None
    _injection_count: int = 0  # injections during current turn
    _drain_timeout_task: Optional[asyncio.Task] = None


def derive_state_key(chat_id: int, thread_id: Optional[int], user_id: int) -> str:
    """Derive a unique state key for a Telegram thread.

    Uses chat_id:thread_id if thread_id is present (topic/forum threads),
    otherwise chat_id:user_id (private chats, non-threaded groups).
    """
    if thread_id is not None:
        return f"{chat_id}:{thread_id}"
    return f"{chat_id}:{user_id}"


class PersistentClientManager:
    """Manages persistent ClaudeSDKClient instances per Telegram thread.

    Each thread gets its own CLI subprocess. Clients persist between
    messages, enabling follow-up queries, interrupt support, and
    independent thread processing.
    """

    def __init__(self, sdk_manager: ClaudeSDKManager, config: Any):
        self._clients: Dict[str, PersistentClientEntry] = {}
        self._saved_sessions: Dict[str, str] = {}  # state_key → session_id
        self._sdk_manager = sdk_manager
        self._config = config

    async def send_message(
        self,
        state_key: str,
        prompt: str,
        working_directory: Path,
        stream_callback: Optional[Callable[[StreamUpdate], Any]] = None,
        model: Optional[str] = None,
        force_new: bool = False,
    ) -> Optional[PersistentResponse]:
        """Send a message to Claude via persistent client.

        If no client exists for state_key, creates one.
        If client is idle, sends query and awaits the turn's result.
        If client is busy, injects via query() and returns None immediately.
            The CLI absorbs injected messages into the current turn —
            no separate ResultMessage is produced, so there is nothing
            to await.

        Returns PersistentResponse for normal turns, None for injections.
        """
        entry = self._clients.get(state_key)

        # Force new: disconnect existing client first
        if force_new and entry is not None:
            await self._disconnect_entry(entry)
            entry = None

        # Create client if needed
        if entry is None:
            entry = await self._create_client(
                state_key, working_directory, model=model
            )

        # Lock the state-check → query() section to prevent race
        # between concurrent send_message calls on the same entry.
        async with entry.send_lock:
            entry.last_activity = time.time()
            previous_state = entry.state

            if entry.state in ("busy", "draining"):
                # Client is mid-turn (or draining after injection).
                # query() injects this message.  The CLI processes
                # injected messages as a second internal turn with its
                # own ResultMessage.  We track injections so
                # _handle_result_message enters/stays in "draining".
                await entry.client.query(prompt)
                entry._injection_count += 1
                logger.info(
                    "turn.injected",
                    state_key=state_key,
                    prompt_len=len(prompt),
                    injection_count=entry._injection_count,
                    state=entry.state,
                )
                return None

            # Normal idle → busy transition
            loop = asyncio.get_event_loop()
            future: asyncio.Future[PersistentResponse] = loop.create_future()
            turn = TurnContext(
                prompt=prompt,
                stream_callback=stream_callback,
                response_future=future,
                started_at=time.time(),
            )

            entry.state = "busy"
            entry.current_turn = turn
            entry._injection_count = 0  # reset for new turn
            # Start stall watchdog for this turn
            if entry._watchdog_task and not entry._watchdog_task.done():
                entry._watchdog_task.cancel()
            entry._watchdog_task = asyncio.create_task(
                self._turn_watchdog(entry),
                name=f"watchdog:{state_key}",
            )
            entry.pending_turns += 1

            await entry.client.query(prompt)

            logger.info(
                "turn.started",
                state_key=state_key,
                prompt_len=len(prompt),
            )

        # Await the turn's completion (outside lock — don't block other senders)
        return await future

    async def stop_client(self, state_key: str) -> StopResult:
        """Stop current work.

        Calls client.interrupt() to stop the current turn.
        Follow-up messages are injected into the current turn (not queued),
        so there is nothing to discard.
        """
        entry = self._clients.get(state_key)
        if entry is None or entry.state not in ("busy", "draining"):
            return StopResult(was_busy=False, discarded_messages=[])

        # Signal interrupt
        entry._interrupted = True
        entry.pending_turns = 1  # Only current turn remains
        entry._injection_count = 0  # Cancel any pending drain

        # Cancel drain timeout if active
        if entry._drain_timeout_task and not entry._drain_timeout_task.done():
            entry._drain_timeout_task.cancel()
            entry._drain_timeout_task = None

        try:
            await entry.client.interrupt()
        except Exception as e:
            logger.warning(
                "interrupt() failed",
                state_key=state_key,
                error=str(e),
            )

        # Start interrupt timeout — if CLI doesn't send ResultMessage
        # within _INTERRUPT_TIMEOUT_S, resolve the turn as interrupted
        asyncio.create_task(
            self._interrupt_timeout(entry),
            name=f"interrupt-timeout:{state_key}",
        )

        logger.info(
            "client.stopped",
            state_key=state_key,
            pending_turns=entry.pending_turns,
        )

        return StopResult(was_busy=True, discarded_messages=[])

    async def _interrupt_timeout(self, entry: PersistentClientEntry) -> None:
        """Safety net: resolve current turn if no ResultMessage after interrupt.

        interrupt() does NOT produce a ResultMessage, so the response collector
        would wait forever. This fires after _INTERRUPT_TIMEOUT_S and resolves
        the turn as interrupted if it's still pending.

        Also handles the case where interrupt is called during draining state.
        """
        await asyncio.sleep(_INTERRUPT_TIMEOUT_S)
        turn = entry.current_turn
        if turn and not turn.response_future.done() and entry._interrupted:
            logger.warning(
                "Interrupt timeout — resolving turn without ResultMessage",
                state_key=entry.state_key,
                timeout_s=_INTERRUPT_TIMEOUT_S,
            )
            response = PersistentResponse(
                content="[Interrupted]",
                session_id=entry.session_id or "",
                cost=0.0,
                duration_ms=int((time.time() - turn.started_at) * 1000),
                num_turns=0,
                is_interrupted=True,
            )
            turn.response_future.set_result(response)
            entry.state = "idle"
            entry.current_turn = None
            entry.pending_turns = 0
            entry._interrupted = False
            entry._injection_count = 0
        elif entry.state == "draining" and entry._interrupted:
            # Interrupted during drain — just go idle
            logger.warning(
                "Interrupt timeout during drain — going idle",
                state_key=entry.state_key,
                timeout_s=_INTERRUPT_TIMEOUT_S,
            )
            entry.state = "idle"
            entry.current_turn = None
            entry.pending_turns = 0
            entry._interrupted = False
            entry._injection_count = 0

    async def _turn_watchdog(self, entry: PersistentClientEntry) -> None:
        """Monitor active turns for silence — logs when the CLI goes quiet.

        Fires at 30s, then every 60s. Checks whether the CLI subprocess is
        still alive. This is diagnostic only — it does not intervene.
        """
        thresholds = [30, 60]  # first two alerts
        idx = 0
        try:
            while True:
                wait = thresholds[idx] if idx < len(thresholds) else 60
                # After the first two thresholds, check every 60s
                if idx >= len(thresholds):
                    wait = 60
                await asyncio.sleep(wait)

                turn = entry.current_turn
                if not turn:
                    return  # Turn completed, watchdog done

                now = time.time()
                last_msg = turn.last_message_at or turn.started_at
                silence_s = now - last_msg
                total_elapsed_s = now - turn.started_at

                # Check if CLI subprocess is alive
                cli_alive = True
                try:
                    proc = getattr(entry.client, "_query", None)
                    if proc:
                        transport = getattr(proc, "_transport", None)
                        if transport:
                            subprocess_proc = getattr(transport, "_process", None)
                            if subprocess_proc and subprocess_proc.returncode is not None:
                                cli_alive = False
                except Exception:
                    pass  # Best effort — don't crash the watchdog

                logger.warning(
                    "turn.quiet",
                    state_key=entry.state_key,
                    silence_seconds=round(silence_s, 1),
                    total_elapsed_seconds=round(total_elapsed_s, 1),
                    last_message_type=turn.last_message_type or "none",
                    last_stream_event_type=turn.last_stream_event_type or "none",
                    api_calls_so_far=turn.api_call_count,
                    cli_alive=cli_alive,
                    pending_turns=entry.pending_turns,
                )
                idx += 1

        except asyncio.CancelledError:
            pass

    async def disconnect_client(self, state_key: str) -> None:
        """Fully disconnect and remove client. Next message creates fresh."""
        entry = self._clients.get(state_key)
        if entry is None:
            return
        await self._disconnect_entry(entry)

    async def cleanup_idle_clients(
        self, max_idle_seconds: int = _DEFAULT_IDLE_TIMEOUT_S
    ) -> int:
        """Disconnect clients idle for longer than max_idle_seconds."""
        now = time.time()
        to_remove = [
            key
            for key, entry in self._clients.items()
            if entry.state == "idle"
            and (now - entry.last_activity) > max_idle_seconds
        ]
        for key in to_remove:
            entry = self._clients.get(key)
            if entry:
                # Preserve session_id so we can resume on reconnect
                if entry.session_id:
                    self._saved_sessions[key] = entry.session_id
                    logger.info(
                        "Saved session for future resume",
                        state_key=key,
                        session_id=entry.session_id,
                    )
                await self._disconnect_entry(entry)
                logger.info(
                    "Cleaned up idle client",
                    state_key=key,
                    idle_seconds=now - entry.last_activity,
                )
        return len(to_remove)

    async def shutdown(self) -> None:
        """Disconnect all clients. Called on bot shutdown."""
        keys = list(self._clients.keys())
        for key in keys:
            entry = self._clients.get(key)
            if entry:
                await self._disconnect_entry(entry)
        logger.info("Persistent client manager shut down", clients_closed=len(keys))

    def get_client_state(self, state_key: str) -> Optional[str]:
        """Get client state for a thread. Returns None, 'idle', or 'busy'."""
        entry = self._clients.get(state_key)
        if entry is None:
            return None
        return entry.state

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _create_client(
        self,
        state_key: str,
        working_directory: Path,
        model: Optional[str] = None,
    ) -> PersistentClientEntry:
        """Create and connect a new persistent client.

        If a previous session_id was saved for this state_key (e.g. after
        idle cleanup), resumes that session so conversation context is
        preserved.
        """
        saved_session_id = self._saved_sessions.pop(state_key, None)
        options = self._sdk_manager.build_options(
            working_directory=working_directory,
            model=model,
            session_id=saved_session_id,
            continue_session=bool(saved_session_id),
            include_partial_messages=True,
        )

        client = ClaudeSDKClient(options)
        await client.connect()

        entry = PersistentClientEntry(
            client=client,
            state="idle",
            state_key=state_key,
            working_directory=working_directory,
        )

        # Start the response collector background task
        entry.collector_task = asyncio.create_task(
            self._response_collector(entry),
            name=f"collector:{state_key}",
        )

        self._clients[state_key] = entry

        # Log what we're actually sending to the CLI subprocess
        sys_prompt = getattr(options, "system_prompt", None)
        sys_prompt_type = "preset+append" if isinstance(sys_prompt, dict) else "string" if isinstance(sys_prompt, str) else "none"
        logger.info(
            "client.created",
            state_key=state_key,
            working_directory=str(working_directory),
            collector_started=True,
            model=getattr(options, "model", None),
            max_turns=getattr(options, "max_turns", None),
            system_prompt_type=sys_prompt_type,
            has_mcp=bool(getattr(options, "mcp_servers", None)),
            has_can_use_tool=getattr(options, "can_use_tool", None) is not None,
            resume_session=getattr(options, "resume", None),
        )
        return entry

    async def _disconnect_entry(self, entry: PersistentClientEntry) -> None:
        """Disconnect a client entry and clean up."""
        state_key = entry.state_key

        # Cancel collector task
        if entry.collector_task and not entry.collector_task.done():
            entry.collector_task.cancel()
            try:
                await entry.collector_task
            except (asyncio.CancelledError, Exception):
                pass

        # Cancel any pending turn future
        if entry.current_turn and not entry.current_turn.response_future.done():
            entry.current_turn.response_future.cancel()

        # Cancel drain timeout if active
        if entry._drain_timeout_task and not entry._drain_timeout_task.done():
            entry._drain_timeout_task.cancel()

        # Disconnect the SDK client
        try:
            await entry.client.disconnect()
        except Exception as e:
            logger.warning(
                "Error disconnecting client",
                state_key=state_key,
                error=str(e),
            )

        # Remove from registry
        self._clients.pop(state_key, None)
        logger.info(
            "client.disconnected",
            state_key=state_key,
        )

    async def _response_collector(self, entry: PersistentClientEntry) -> None:
        """Persistent background loop collecting responses from CLI stdout.

        Runs for the lifetime of the client. Each ResultMessage marks a turn end.
        Between ResultMessages, streams updates to the current turn's callback.
        """
        msg_count = 0
        try:
            async for raw_data in entry.client._query.receive_messages():
                msg_count += 1
                now = time.time()

                try:
                    message = parse_message(raw_data)
                except MessageParseError as e:
                    # Promoted to INFO — unparseable messages include
                    # rate_limit_event and other signals we must not ignore
                    raw_keys = list(raw_data.keys()) if isinstance(raw_data, dict) else [type(raw_data).__name__]
                    logger.info(
                        "sdk.unparseable",
                        state_key=entry.state_key,
                        error=str(e),
                        raw_keys=raw_keys,
                        raw_type=raw_data.get("type") if isinstance(raw_data, dict) else None,
                        msg_num=msg_count,
                    )
                    continue

                msg_type = type(message).__name__
                turn = entry.current_turn

                # Update turn diagnostics
                if turn:
                    turn.last_message_at = now
                    turn.last_message_type = msg_type

                # StreamEvent diagnostics: track inner type + extract token usage
                if isinstance(message, StreamEvent) and turn:
                    event = getattr(message, "event", None) or {}
                    turn.last_stream_event_type = event.get("type", "unknown")

                    if event.get("type") == "message_start":
                        usage = (event.get("message") or {}).get("usage", {})
                        input_tok = usage.get("input_tokens", 0)
                        cache_read = usage.get("cache_read_input_tokens", 0)
                        cache_create = usage.get("cache_creation_input_tokens", 0)
                        output_tok = usage.get("output_tokens", 0)

                        turn.api_call_count += 1
                        turn.total_input_tokens += input_tok
                        turn.total_cache_read_tokens += cache_read
                        turn.total_cache_creation_tokens += cache_create
                        turn.total_output_tokens += output_tok

                        # Cache hit rate for THIS api call
                        total_in = input_tok + cache_read + cache_create
                        cache_pct = (cache_read / total_in * 100) if total_in > 0 else 0

                        logger.info(
                            "api_call.usage",
                            state_key=entry.state_key,
                            call_num=turn.api_call_count,
                            input_tokens=input_tok,
                            cache_read=cache_read,
                            cache_creation=cache_create,
                            output_tokens=output_tok,
                            cache_hit_pct=round(cache_pct, 1),
                            total_context=total_in,
                            elapsed_ms=int((now - turn.started_at) * 1000),
                        )

                # Log every SDK message at debug level — the full trail
                logger.debug(
                    "sdk.message",
                    state_key=entry.state_key,
                    msg_type=msg_type,
                    msg_num=msg_count,
                    has_turn=turn is not None,
                    state=entry.state,
                    elapsed_ms=int((now - turn.started_at) * 1000) if turn else None,
                )

                if isinstance(message, ResultMessage):
                    await self._handle_result_message(entry, message, raw_data)
                elif turn and turn.stream_callback:
                    # Stream update(s) to current turn's callback
                    try:
                        updates = self._message_to_stream_update(message)
                        if updates:
                            for update in updates:
                                result = turn.stream_callback(update)
                                if asyncio.iscoroutine(result):
                                    await result
                    except Exception as e:
                        logger.warning(
                            "Stream callback error",
                            error=str(e),
                            state_key=entry.state_key,
                        )
                elif not turn and entry.state == "draining":
                    # We're in draining state — these are the second
                    # turn's messages after injection.  Log them as
                    # expected continuation, not orphans.
                    logger.debug(
                        "sdk.drain_message",
                        state_key=entry.state_key,
                        msg_type=msg_type,
                        msg_num=msg_count,
                        state=entry.state,
                    )
                elif not turn:
                    # SDK sent us something but we have no active turn
                    # and we're not draining — genuine orphan.
                    # Log content summary to help diagnose what was lost.
                    content_summary = self._summarize_message_content(message)
                    logger.warning(
                        "sdk.orphan_message",
                        state_key=entry.state_key,
                        msg_type=msg_type,
                        msg_num=msg_count,
                        state=entry.state,
                        pending_turns=entry.pending_turns,
                        content_summary=content_summary,
                        raw_keys=list(raw_data.keys()) if isinstance(raw_data, dict) else None,
                    )

                # Collect message for response building
                if turn:
                    turn.messages.append(message)
                    if isinstance(message, ResultMessage):
                        turn.result_raw_data.update(raw_data)

        except asyncio.CancelledError:
            logger.info(
                "collector.stopped",
                state_key=entry.state_key,
                messages_processed=msg_count,
            )
        except Exception as e:
            logger.error(
                "collector.died",
                state_key=entry.state_key,
                error=str(e),
                error_type=type(e).__name__,
                messages_processed=msg_count,
                state=entry.state,
                pending_turns=entry.pending_turns,
                has_turn=entry.current_turn is not None,
            )
            # Client is dead — resolve any pending futures with error
            await self._handle_client_death(entry, e)

    async def _handle_result_message(
        self,
        entry: PersistentClientEntry,
        result: ResultMessage,
        raw_data: Dict[str, Any],
    ) -> None:
        """Process a ResultMessage — marks the end of a turn.

        If injections occurred during this turn, the CLI processes the
        injected message as a second internal turn with its own
        ResultMessage.  Instead of going idle immediately, we enter
        "draining" state to collect that second turn's messages.
        """
        turn = entry.current_turn

        # --- Draining state: this is the SECOND ResultMessage ---
        if entry.state == "draining" and turn is None:
            # We were waiting for exactly this.  Cancel drain timeout.
            if entry._drain_timeout_task and not entry._drain_timeout_task.done():
                entry._drain_timeout_task.cancel()
                entry._drain_timeout_task = None

            entry.state = "idle"
            entry.pending_turns = 0
            entry._injection_count = 0

            # Extract session_id if present
            result_session_id = getattr(result, "session_id", None)
            if result_session_id:
                entry.session_id = result_session_id

            logger.info(
                "turn.drain_completed",
                state_key=entry.state_key,
                transition="draining->idle",
                session_id=entry.session_id,
            )
            return

        if turn is None:
            logger.warning(
                "ResultMessage with no active turn",
                state_key=entry.state_key,
                state=entry.state,
            )
            return

        # Build the response
        response = self._build_persistent_response(
            entry, turn, result, raw_data
        )

        # Update entry state
        entry.pending_turns = max(0, entry.pending_turns - 1)
        entry.last_activity = time.time()
        entry._interrupted = False

        # Extract session_id from first result
        result_session_id = getattr(result, "session_id", None)
        if result_session_id:
            entry.session_id = result_session_id

        # Resolve the turn's future
        if not turn.response_future.done():
            turn.response_future.set_result(response)

        # Cancel stall watchdog — turn completed normally
        if entry._watchdog_task and not entry._watchdog_task.done():
            entry._watchdog_task.cancel()

        # Per-turn token summary — the key diagnostic for cost analysis
        total_in = turn.total_input_tokens + turn.total_cache_read_tokens + turn.total_cache_creation_tokens
        cache_pct = (turn.total_cache_read_tokens / total_in * 100) if total_in > 0 else 0

        # Decide next state: if injections occurred, expect continuation
        if entry._injection_count > 0:
            # The CLI will process the injected message(s) as a second
            # internal turn.  Stay in "draining" to collect those messages
            # instead of discarding them as orphans.
            entry.state = "draining"
            entry.current_turn = None  # first turn done, clear it

            # Safety timeout: if the continuation never produces a
            # ResultMessage, fall back to idle
            entry._drain_timeout_task = asyncio.create_task(
                self._drain_timeout(entry),
                name=f"drain-timeout:{entry.state_key}",
            )

            logger.info(
                "turn.completed",
                state_key=entry.state_key,
                cost=response.cost,
                duration_ms=response.duration_ms,
                is_interrupted=response.is_interrupted,
                stop_reason=response.stop_reason,
                num_turns=response.num_turns,
                pending_turns=entry.pending_turns,
                transition="busy->draining",
                injection_count=entry._injection_count,
                session_id=entry.session_id,
                api_calls=turn.api_call_count,
                input_tokens=turn.total_input_tokens,
                cache_read_tokens=turn.total_cache_read_tokens,
                cache_creation_tokens=turn.total_cache_creation_tokens,
                output_tokens=turn.total_output_tokens,
                total_context_tokens=total_in,
                cache_hit_pct=round(cache_pct, 1),
            )
        else:
            # No injections — normal idle transition
            entry.state = "idle"
            entry.current_turn = None
            entry.pending_turns = 0  # defensive reset

            logger.info(
                "turn.completed",
                state_key=entry.state_key,
                cost=response.cost,
                duration_ms=response.duration_ms,
                is_interrupted=response.is_interrupted,
                stop_reason=response.stop_reason,
                num_turns=response.num_turns,
                pending_turns=entry.pending_turns,
                transition=f"busy->idle",
                session_id=entry.session_id,
                api_calls=turn.api_call_count,
                input_tokens=turn.total_input_tokens,
                cache_read_tokens=turn.total_cache_read_tokens,
                cache_creation_tokens=turn.total_cache_creation_tokens,
                output_tokens=turn.total_output_tokens,
                total_context_tokens=total_in,
                cache_hit_pct=round(cache_pct, 1),
            )

    async def _drain_timeout(self, entry: PersistentClientEntry) -> None:
        """Safety net: if draining state doesn't receive a ResultMessage
        within _INJECTION_DRAIN_TIMEOUT_S, fall back to idle.
        """
        await asyncio.sleep(_INJECTION_DRAIN_TIMEOUT_S)
        if entry.state == "draining":
            logger.warning(
                "turn.drain_timeout",
                state_key=entry.state_key,
                timeout_s=_INJECTION_DRAIN_TIMEOUT_S,
                injection_count=entry._injection_count,
            )
            entry.state = "idle"
            entry.current_turn = None
            entry.pending_turns = 0
            entry._injection_count = 0

    def _build_persistent_response(
        self,
        entry: PersistentClientEntry,
        turn: TurnContext,
        result: ResultMessage,
        raw_data: Dict[str, Any],
    ) -> PersistentResponse:
        """Build a PersistentResponse from a completed turn."""
        # Cost delta tracking
        cumulative_cost = getattr(result, "total_cost_usd", 0.0) or 0.0
        turn_cost = cumulative_cost - entry.previous_cumulative_cost
        entry.previous_cumulative_cost = cumulative_cost

        # Duration
        duration_ms = int((time.time() - turn.started_at) * 1000)

        # Content extraction — use ResultMessage.result if non-empty,
        # otherwise fall back to assembling from AssistantMessage blocks.
        # NOTE: The CLI may send result="" for some turns (e.g. conversational
        # replies without tool use). An `is not None` check treated "" as valid
        # content, producing "(No content to display)" in Telegram.
        result_content = getattr(result, "result", None)
        if result_content:
            content = result_content
        else:
            content_parts = []
            for msg in turn.messages:
                if isinstance(msg, AssistantMessage):
                    msg_content = getattr(msg, "content", [])
                    if msg_content and isinstance(msg_content, list):
                        for block in msg_content:
                            if hasattr(block, "text"):
                                content_parts.append(block.text)
                    elif msg_content:
                        content_parts.append(str(msg_content))
            content = "\n".join(content_parts)

        # Tools used
        tools_used: List[Dict[str, Any]] = []
        for msg in turn.messages:
            if isinstance(msg, AssistantMessage):
                msg_content = getattr(msg, "content", [])
                if msg_content and isinstance(msg_content, list):
                    for block in msg_content:
                        if isinstance(block, ToolUseBlock):
                            tools_used.append(
                                {
                                    "name": getattr(block, "name", "unknown"),
                                    "input": getattr(block, "input", {}),
                                }
                            )

        # Session ID
        session_id = getattr(result, "session_id", None) or entry.session_id or ""

        # Context window from modelUsage
        context_window = None
        model_usage = raw_data.get("modelUsage", {})
        if model_usage:
            first_model = next(iter(model_usage.values()), {})
            context_window = first_model.get("contextWindow")

        # Input tokens from last StreamEvent
        total_input_tokens = None
        for msg in reversed(turn.messages):
            if isinstance(msg, StreamEvent):
                event = getattr(msg, "event", None) or {}
                if event.get("type") == "message_start":
                    usage = (event.get("message") or {}).get("usage", {})
                    total_input_tokens = (
                        usage.get("input_tokens", 0)
                        + usage.get("cache_read_input_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0)
                    )
                    break

        # Count turns
        num_turns = len(
            [
                m
                for m in turn.messages
                if isinstance(m, (UserMessage, AssistantMessage))
            ]
        )

        # Interrupted detection
        is_interrupted = entry._interrupted

        # Stop reason from SDK
        stop_reason = getattr(result, "stop_reason", None)

        return PersistentResponse(
            content=content,
            session_id=session_id,
            cost=max(0.0, turn_cost),
            duration_ms=duration_ms,
            num_turns=num_turns,
            is_interrupted=is_interrupted,
            stop_reason=stop_reason,
            tools_used=tools_used,
            context_window=context_window,
            total_input_tokens=total_input_tokens,
        )

    async def _handle_client_death(
        self, entry: PersistentClientEntry, error: Exception
    ) -> None:
        """Handle unexpected client death — resolve pending futures with errors."""
        # Cancel watchdog on death
        if entry._watchdog_task and not entry._watchdog_task.done():
            entry._watchdog_task.cancel()

        # Cancel drain timeout on death
        if entry._drain_timeout_task and not entry._drain_timeout_task.done():
            entry._drain_timeout_task.cancel()

        turn = entry.current_turn
        logger.error(
            "client.death",
            state_key=entry.state_key,
            error=str(error),
            error_type=type(error).__name__,
            had_active_turn=turn is not None,
            last_message_type=turn.last_message_type if turn else None,
            api_calls_at_death=turn.api_call_count if turn else 0,
            elapsed_ms=int((time.time() - turn.started_at) * 1000) if turn else 0,
        )

        error_response = PersistentResponse(
            content=f"Client error: {error}",
            session_id=entry.session_id or "",
            cost=0.0,
            duration_ms=0,
            num_turns=0,
            is_error=True,
        )

        # Resolve current turn
        if entry.current_turn and not entry.current_turn.response_future.done():
            entry.current_turn.response_future.set_result(error_response)

        # Remove from registry
        self._clients.pop(entry.state_key, None)

    @staticmethod
    def _summarize_message_content(message: Message) -> str:
        """Produce a short summary of message content for diagnostic logging.

        Returns a string like "AssistantMessage: 3 blocks (text, tool_use, text)"
        or "UserMessage: 42 chars" — enough to understand what was lost without
        logging the full content.
        """
        msg_type = type(message).__name__
        try:
            if isinstance(message, AssistantMessage):
                content = getattr(message, "content", [])
                if content and isinstance(content, list):
                    block_types = [type(b).__name__ for b in content]
                    return f"{msg_type}: {len(content)} blocks ({', '.join(block_types)})"
                return f"{msg_type}: empty"
            elif isinstance(message, UserMessage):
                content = getattr(message, "content", "")
                length = len(content) if isinstance(content, str) else len(str(content))
                return f"{msg_type}: {length} chars"
            elif isinstance(message, ResultMessage):
                result_text = getattr(message, "result", "")
                return f"{msg_type}: result={len(result_text) if result_text else 0} chars"
            elif isinstance(message, StreamEvent):
                event = getattr(message, "event", None) or {}
                return f"{msg_type}: {event.get('type', 'unknown')}"
            else:
                return msg_type
        except Exception:
            return msg_type

    @staticmethod
    def _message_to_stream_update(
        message: Message,
    ) -> Optional[List[StreamUpdate]]:
        """Convert an SDK message to StreamUpdate(s) for the stream callback.

        Returns a list of updates (possibly empty/None).  When an
        AssistantMessage contains both thinking blocks and tool calls,
        both are emitted as separate updates so neither shadows the other.
        """
        if isinstance(message, AssistantMessage):
            content = getattr(message, "content", [])
            text_parts = []
            tool_calls = []
            thinking_parts = []

            if content and isinstance(content, list):
                for block in content:
                    if isinstance(block, ToolUseBlock):
                        tool_calls.append(
                            {
                                "name": getattr(block, "name", "unknown"),
                                "input": getattr(block, "input", {}),
                                "id": getattr(block, "id", None),
                            }
                        )
                    elif isinstance(block, ThinkingBlock):
                        thinking_text = getattr(block, "thinking", "")
                        if thinking_text and thinking_text.strip():
                            thinking_parts.append(thinking_text)
                    elif hasattr(block, "text"):
                        text_parts.append(block.text)

            updates: List[StreamUpdate] = []

            # Emit thinking blocks as a separate update
            if thinking_parts:
                updates.append(
                    StreamUpdate(
                        type="thinking",
                        content="\n".join(thinking_parts),
                    )
                )

            if text_parts or tool_calls:
                updates.append(
                    StreamUpdate(
                        type="assistant",
                        content=("\n".join(text_parts) if text_parts else None),
                        tool_calls=tool_calls if tool_calls else None,
                    )
                )

            return updates if updates else None

        elif isinstance(message, StreamEvent):
            event = getattr(message, "event", None) or {}
            if event.get("type") == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        return [StreamUpdate(type="stream_delta", content=text)]

        elif isinstance(message, UserMessage):
            content = getattr(message, "content", "")
            if content:
                return [StreamUpdate(type="user", content=content)]

        return None
