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


@dataclass
class QueuedMessage:
    """A message queued while the client was busy."""

    text: str
    queued_at: float


@dataclass
class TurnContext:
    """Context for a single Claude turn within a persistent session."""

    prompt: str
    stream_callback: Optional[Callable]
    response_future: "asyncio.Future[PersistentResponse]"
    started_at: float
    messages: List[Message] = field(default_factory=list)
    result_raw_data: Dict[str, Any] = field(default_factory=dict)


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
    state: str  # "idle" or "busy"
    state_key: str
    working_directory: Path
    session_id: Optional[str] = None
    last_activity: float = field(default_factory=time.time)
    previous_cumulative_cost: float = 0.0
    pending_turns: int = 0
    queued_messages: List[QueuedMessage] = field(default_factory=list)
    collector_task: Optional[asyncio.Task] = None
    current_turn: Optional[TurnContext] = None
    turn_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    _interrupted: bool = False


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
    ) -> PersistentResponse:
        """Send a message to Claude via persistent client.

        If no client exists for state_key, creates one.
        If client is idle, sends query immediately.
        If client is busy, sends query() to CLI (queued as next turn).

        Returns PersistentResponse when the turn completes.
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

        entry.last_activity = time.time()
        previous_state = entry.state

        # Create turn context with a future for the response
        loop = asyncio.get_event_loop()
        future: asyncio.Future[PersistentResponse] = loop.create_future()
        turn = TurnContext(
            prompt=prompt,
            stream_callback=stream_callback,
            response_future=future,
            started_at=time.time(),
        )

        # IMPORTANT: Set current_turn BEFORE query() so the collector
        # has a turn context when messages start flowing from the CLI.
        if entry.state == "idle":
            entry.state = "busy"
            entry.current_turn = turn
        else:
            # Client is busy — queue for the collector to pick up
            # after the current turn's ResultMessage
            await entry.turn_queue.put(turn)

        # Send query to CLI — it handles queueing internally.
        # Must happen AFTER current_turn is set (idle case) or
        # turn is queued (busy case).
        await entry.client.query(prompt)
        entry.pending_turns += 1

        logger.info(
            "turn.started",
            state_key=state_key,
            transition=f"{previous_state}→busy",
            pending_turns=entry.pending_turns,
            queued=previous_state == "busy",
            prompt_len=len(prompt),
        )

        # Await the turn's completion
        return await future

    async def stop_client(self, state_key: str) -> StopResult:
        """Stop current work and discard queued messages.

        Calls client.interrupt() to stop the current turn.
        Discards any messages queued for subsequent turns.
        Returns the texts of discarded messages so the user can resend.
        """
        entry = self._clients.get(state_key)
        if entry is None or entry.state != "busy":
            return StopResult(was_busy=False, discarded_messages=[])

        # Collect queued message texts before discarding
        discarded = [qm.text for qm in entry.queued_messages]
        entry.queued_messages.clear()

        # Cancel any pending turns beyond the current one
        # Drain the turn queue and cancel their futures
        cancelled_turns = []
        while not entry.turn_queue.empty():
            try:
                turn = entry.turn_queue.get_nowait()
                # Don't cancel the current turn — interrupt handles that
                if turn is not entry.current_turn:
                    cancelled_turns.append(turn)
            except asyncio.QueueEmpty:
                break

        for turn in cancelled_turns:
            if not turn.response_future.done():
                turn.response_future.cancel()

        # Signal interrupt
        entry._interrupted = True
        entry.pending_turns = 1  # Only current turn remains

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
            discarded_count=len(discarded),
            pending_turns=entry.pending_turns,
        )

        return StopResult(was_busy=True, discarded_messages=discarded)

    async def _interrupt_timeout(self, entry: PersistentClientEntry) -> None:
        """Safety net: resolve current turn if no ResultMessage after interrupt.

        interrupt() does NOT produce a ResultMessage, so the response collector
        would wait forever. This fires after _INTERRUPT_TIMEOUT_S and resolves
        the turn as interrupted if it's still pending.
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
        """Create and connect a new persistent client."""
        options = self._sdk_manager.build_options(
            working_directory=working_directory,
            model=model,
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
        logger.info(
            "client.created",
            state_key=state_key,
            working_directory=str(working_directory),
            collector_started=True,
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

        # Cancel any pending turn futures
        if entry.current_turn and not entry.current_turn.response_future.done():
            entry.current_turn.response_future.cancel()
        while not entry.turn_queue.empty():
            try:
                turn = entry.turn_queue.get_nowait()
                if not turn.response_future.done():
                    turn.response_future.cancel()
            except asyncio.QueueEmpty:
                break

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
                try:
                    message = parse_message(raw_data)
                except MessageParseError as e:
                    logger.debug(
                        "sdk.unparseable",
                        state_key=entry.state_key,
                        error=str(e),
                        raw_keys=list(raw_data.keys()) if isinstance(raw_data, dict) else type(raw_data).__name__,
                    )
                    continue

                msg_type = type(message).__name__
                turn = entry.current_turn

                # Log every SDK message at debug level — the trail we need
                # when diagnosing stalls and mystery signals
                logger.debug(
                    "sdk.message",
                    state_key=entry.state_key,
                    msg_type=msg_type,
                    msg_num=msg_count,
                    has_turn=turn is not None,
                    state=entry.state,
                    elapsed_ms=int((time.time() - turn.started_at) * 1000) if turn else None,
                )

                if isinstance(message, ResultMessage):
                    await self._handle_result_message(entry, message, raw_data)
                elif turn and turn.stream_callback:
                    # Stream update to current turn's callback
                    try:
                        update = self._message_to_stream_update(message)
                        if update:
                            result = turn.stream_callback(update)
                            if asyncio.iscoroutine(result):
                                await result
                    except Exception as e:
                        logger.warning(
                            "Stream callback error",
                            error=str(e),
                            state_key=entry.state_key,
                        )
                elif not turn:
                    # SDK sent us something but we have no active turn —
                    # this is exactly the "no response requested" scenario
                    logger.warning(
                        "sdk.orphan_message",
                        state_key=entry.state_key,
                        msg_type=msg_type,
                        msg_num=msg_count,
                        state=entry.state,
                        pending_turns=entry.pending_turns,
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
        """Process a ResultMessage — marks the end of a turn."""
        turn = entry.current_turn
        if turn is None:
            logger.warning(
                "ResultMessage with no active turn",
                state_key=entry.state_key,
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

        # Move to next turn or go idle
        if entry.pending_turns > 0:
            # Another turn is queued — get it from the queue
            try:
                next_turn = entry.turn_queue.get_nowait()
                entry.current_turn = next_turn
            except asyncio.QueueEmpty:
                # Queue empty but pending_turns > 0 — defensive reset
                logger.warning(
                    "pending_turns > 0 but turn_queue empty, resetting",
                    state_key=entry.state_key,
                    pending_turns=entry.pending_turns,
                )
                entry.pending_turns = 0
                entry.state = "idle"
                entry.current_turn = None
        else:
            entry.state = "idle"
            entry.current_turn = None

        logger.info(
            "turn.completed",
            state_key=entry.state_key,
            cost=response.cost,
            duration_ms=response.duration_ms,
            is_interrupted=response.is_interrupted,
            pending_turns=entry.pending_turns,
            transition=f"busy→{entry.state}",
            session_id=entry.session_id,
        )

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

        # Content extraction — use ResultMessage.result if available
        result_content = getattr(result, "result", None)
        if result_content is not None:
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

        return PersistentResponse(
            content=content,
            session_id=session_id,
            cost=max(0.0, turn_cost),
            duration_ms=duration_ms,
            num_turns=num_turns,
            is_interrupted=is_interrupted,
            tools_used=tools_used,
            context_window=context_window,
            total_input_tokens=total_input_tokens,
        )

    async def _handle_client_death(
        self, entry: PersistentClientEntry, error: Exception
    ) -> None:
        """Handle unexpected client death — resolve pending futures with errors."""
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

        # Resolve any queued turns
        while not entry.turn_queue.empty():
            try:
                turn = entry.turn_queue.get_nowait()
                if not turn.response_future.done():
                    turn.response_future.set_result(error_response)
            except asyncio.QueueEmpty:
                break

        # Remove from registry
        self._clients.pop(entry.state_key, None)

    @staticmethod
    def _message_to_stream_update(message: Message) -> Optional[StreamUpdate]:
        """Convert an SDK message to a StreamUpdate for the stream callback."""
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

            # Emit thinking blocks as a separate update
            if thinking_parts:
                return StreamUpdate(
                    type="thinking",
                    content="\n".join(thinking_parts),
                )

            if text_parts or tool_calls:
                return StreamUpdate(
                    type="assistant",
                    content=("\n".join(text_parts) if text_parts else None),
                    tool_calls=tool_calls if tool_calls else None,
                )

        elif isinstance(message, StreamEvent):
            event = getattr(message, "event", None) or {}
            if event.get("type") == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        return StreamUpdate(type="stream_delta", content=text)

        elif isinstance(message, UserMessage):
            content = getattr(message, "content", "")
            if content:
                return StreamUpdate(type="user", content=content)

        return None
