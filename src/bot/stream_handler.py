"""Stream callback factory for handling Claude SDK streaming updates.

Provides the stream callback implementation for verbose progress tracking,
draft streaming, MCP image interception, and real-time message delivery.
"""

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog

from ..claude.sdk_integration import StreamUpdate
from ..config.settings import Settings
from .utils.draft_streamer import DraftStreamer
from .utils.heartbeat_pin import HeartbeatPin
from .utils.html_format import escape_html
from .utils.image_extractor import ImageAttachment, validate_image_path

logger = structlog.get_logger()

# Patterns that look like secrets/credentials in CLI arguments
_SECRET_PATTERNS: List[re.Pattern[str]] = [
    # API keys / tokens (sk-ant-..., sk-..., ghp_..., gho_..., github_pat_..., xoxb-...)
    re.compile(
        r"(sk-ant-api\d*-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]*"
        r"|(sk-[A-Za-z0-9_-]{20})[A-Za-z0-9_-]*"
        r"|(ghp_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(gho_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(github_pat_[A-Za-z0-9_]{5})[A-Za-z0-9_]*"
        r"|(xoxb-[A-Za-z0-9]{5})[A-Za-z0-9-]*"
    ),
    # AWS access keys
    re.compile(r"(AKIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    # Generic long hex/base64 tokens after common flags/env patterns
    re.compile(
        r"((?:--token|--secret|--password|--api-key|--apikey|--auth)"
        r"[= ]+)['\"]?[A-Za-z0-9+/_.:-]{8,}['\"]?"
    ),
    # Inline env assignments like KEY=value
    re.compile(
        r"((?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|AUTH_TOKEN|PRIVATE_KEY"
        r"|ACCESS_KEY|CLIENT_SECRET|WEBHOOK_SECRET)"
        r"=)['\"]?[^\s'\"]{8,}['\"]?"
    ),
    # Bearer / Basic auth headers
    re.compile(r"(Bearer )[A-Za-z0-9+/_.:-]{8,}" r"|(Basic )[A-Za-z0-9+/=]{8,}"),
    # Connection strings with credentials  user:pass@host
    re.compile(r"://([^:]+:)[^@]{4,}(@)"),
]


def _redact_secrets(text: str) -> str:
    """Replace likely secrets/credentials with redacted placeholders."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda m: next((g + "***" for g in m.groups() if g is not None), "***"),
            result,
        )
    return result


# Tool name -> friendly emoji mapping for verbose output
_TOOL_ICONS: Dict[str, str] = {
    "Read": "\U0001f4d6",
    "Write": "\u270f\ufe0f",
    "Edit": "\u270f\ufe0f",
    "MultiEdit": "\u270f\ufe0f",
    "Bash": "\U0001f4bb",
    "Glob": "\U0001f50d",
    "Grep": "\U0001f50d",
    "LS": "\U0001f4c2",
    "Task": "\U0001f9e0",
    "TaskOutput": "\U0001f9e0",
    "WebFetch": "\U0001f310",
    "WebSearch": "\U0001f310",
    "NotebookRead": "\U0001f4d3",
    "NotebookEdit": "\U0001f4d3",
    "TodoRead": "\u2611\ufe0f",
    "TodoWrite": "\u2611\ufe0f",
}


def _tool_icon(name: str) -> str:
    """Return emoji for a tool, with a default wrench."""
    return _TOOL_ICONS.get(name, "\U0001f527")


def _format_verbose_progress(
    activity_log: List[Dict[str, Any]],
    verbose_level: int,
    start_time: float,
) -> str:
    """Build the progress message text based on activity so far."""
    if not activity_log:
        return "Working..."

    elapsed = time.time() - start_time
    lines: List[str] = [f"Working... ({elapsed:.0f}s)\n"]

    for entry in activity_log[-15:]:  # Show last 15 entries max
        kind = entry.get("kind", "tool")
        if kind == "text":
            # Claude's intermediate reasoning/commentary
            snippet = entry.get("detail", "")
            if verbose_level >= 2:
                lines.append(f"\U0001f4ac {snippet}")
            else:
                # Level 1: one short line
                lines.append(f"\U0001f4ac {snippet[:80]}")
        else:
            # Tool call
            icon = _tool_icon(entry["name"])
            if verbose_level >= 2 and entry.get("detail"):
                lines.append(f"{icon} {entry['name']}: {entry['detail']}")
            else:
                lines.append(f"{icon} {entry['name']}")

    if len(activity_log) > 15:
        lines.insert(1, f"... ({len(activity_log) - 15} earlier entries)\n")

    return "\n".join(lines)


def _summarize_tool_input(tool_name: str, tool_input: Dict[str, Any]) -> str:
    """Return a short summary of tool input for verbose level 2."""
    if not tool_input:
        return ""
    if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
        path = tool_input.get("file_path") or tool_input.get("path", "")
        if path:
            # Show just the filename, not the full path
            return path.rsplit("/", 1)[-1]
    if tool_name in ("Glob", "Grep"):
        pattern = tool_input.get("pattern", "")
        if pattern:
            return pattern[:60]
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if cmd:
            return _redact_secrets(cmd[:100])[:80]
    if tool_name in ("WebFetch", "WebSearch"):
        return (tool_input.get("url", "") or tool_input.get("query", ""))[:60]
    if tool_name == "Task":
        desc = tool_input.get("description", "")
        if desc:
            return desc[:60]
    # Generic: show first key's value
    for v in tool_input.values():
        if isinstance(v, str) and v:
            return v[:60]
    return ""


def make_stream_callback(
    settings: Settings,
    verbose_level: int,
    progress_msg: Any,
    tool_log: List[Dict[str, Any]],
    start_time: float,
    mcp_images: Optional[List[ImageAttachment]] = None,
    approved_directory: Optional[Path] = None,
    draft_streamer: Optional[DraftStreamer] = None,
    telegram_update: Optional[Any] = None,
    heartbeat_pin: Optional[HeartbeatPin] = None,
) -> Optional[Callable[[StreamUpdate], Any]]:
    """Create a stream callback for verbose progress updates.

    When *mcp_images* is provided, the callback also intercepts
    ``send_image_to_user`` tool calls and collects validated
    :class:`ImageAttachment` objects for later Telegram delivery.

    When *draft_streamer* is provided, tool activity and assistant
    text are streamed to the user in real time via
    ``sendMessageDraft``.

    When *telegram_update* is provided, Claude's text commentary
    between tool calls is sent as persistent Telegram messages
    immediately (not batched until the end).  Thinking blocks are
    sent as ephemeral messages visible during streaming and deleted
    once the final response is delivered.

    Returns None when verbose_level is 0 **and** no MCP image
    collection or draft streaming is requested.
    Typing indicators are handled by a separate heartbeat task.
    """
    import html as html_mod

    need_mcp_intercept = mcp_images is not None and approved_directory is not None

    if (
        verbose_level == 0
        and not need_mcp_intercept
        and draft_streamer is None
        and telegram_update is None
        and heartbeat_pin is None
    ):
        return None

    last_edit_time = [0.0]  # mutable container for closure

    # Batching state for rapid-fire text blocks (rate-limit safety).
    # We accumulate text for up to _TEXT_BATCH_WINDOW seconds before
    # sending a single persistent message.
    _TEXT_BATCH_WINDOW = 1.5  # seconds
    _pending_text: List[str] = []
    _pending_thinking: List[str] = []
    _thinking_message_ids: List[int] = []
    _text_batch_task: List[Optional[asyncio.Task]] = [None]

    # Track whether assistant text was sent as standalone messages
    # during this turn.  If True, the final response is suppressed
    # (the standalone messages already delivered the content).
    _text_was_sent = [False]

    # Track whether the final flush completed without losing messages.
    # If False AND _text_was_sent is True, deliver_turn_result resends
    # as a safety net (partial duplication > lost response).
    _flush_succeeded = [True]

    # Lock protecting the mutable closure state above.  Without this,
    # concurrent async operations (_schedule_flush, _enqueue_text,
    # the external flush_pending entry-point) race on the shared lists
    # and the cancel-and-recreate pattern for _text_batch_task.
    _stream_lock = asyncio.Lock()

    # Serialises Telegram sends so two concurrent flushes don't
    # interleave messages.  Separate from _stream_lock so that
    # network I/O never blocks _enqueue_text / _on_stream.
    _send_lock = asyncio.Lock()

    async def _flush_pending_text() -> None:
        """Send accumulated text/thinking as persistent messages.

        Collects pending data under _stream_lock (microseconds),
        then sends to Telegram under _send_lock (network I/O).
        This prevents slow/failing Telegram sends from blocking
        the SDK message pipeline.
        """
        # --- Phase 1: collect under _stream_lock ---
        t0 = time.time()
        async with _stream_lock:
            lock_wait_ms = (time.time() - t0) * 1000
            if lock_wait_ms > 100:
                logger.warning(
                    "stream_lock.slow_acquire",
                    wait_ms=round(lock_wait_ms, 1),
                    phase="flush_collect",
                )

            if not telegram_update:
                return

            # Snapshot and clear — releases lock quickly
            thinking_snapshot = list(_pending_thinking)
            _pending_thinking.clear()
            text_snapshot = list(_pending_text)
            _pending_text.clear()

        # Nothing to send
        if not thinking_snapshot and not text_snapshot:
            return

        # --- Phase 2: send under _send_lock (network I/O) ---
        async with _send_lock:
            # Send thinking as ephemeral messages
            if thinking_snapshot:
                from src.utils.constants import TELEGRAM_MAX_MESSAGE_LENGTH

                thinking_text = "\n\n".join(thinking_snapshot)
                escaped = html_mod.escape(thinking_text)
                prefix = "\U0001f9e0 "
                max_content = TELEGRAM_MAX_MESSAGE_LENGTH - len(prefix)
                if len(escaped) > max_content:
                    escaped = escaped[: max_content - 1] + "\u2026"
                thinking_msg = f"{prefix}{escaped}"
                try:
                    send_t0 = time.time()
                    sent = await telegram_update.effective_message.reply_text(
                        thinking_msg,
                        parse_mode="HTML",
                        disable_notification=True,
                    )
                    _thinking_message_ids.append(sent.message_id)
                    await asyncio.sleep(0.3)
                except Exception as e:
                    send_duration_ms = (time.time() - send_t0) * 1000
                    logger.warning(
                        "Failed to send thinking message",
                        error=str(e),
                        error_type=type(e).__name__,
                        send_duration_ms=round(send_duration_ms, 1),
                    )

            # Send commentary text
            if text_snapshot:
                combined = "\n\n".join(text_snapshot)
                if not combined.strip():
                    return

                from .utils.formatting import ResponseFormatter

                formatter = ResponseFormatter(settings)
                formatted = formatter.format_claude_response(combined)
                for msg in formatted:
                    if not msg.text or not msg.text.strip():
                        continue
                    try:
                        send_t0 = time.time()
                        await telegram_update.effective_message.reply_text(
                            msg.text,
                            parse_mode=msg.parse_mode,
                            reply_markup=None,
                            disable_notification=True,
                            do_quote=False,
                        )
                        _text_was_sent[0] = True
                        # Content just went out — reset heartbeat throttle
                        # so it doesn't immediately edit redundantly
                        if heartbeat_pin:
                            heartbeat_pin.reset_throttle()
                        await asyncio.sleep(0.3)
                    except Exception as send_err:
                        send_duration_ms = (time.time() - send_t0) * 1000
                        logger.warning(
                            "Failed to send intermediate text",
                            error=str(send_err),
                            error_type=type(send_err).__name__,
                            send_duration_ms=round(send_duration_ms, 1),
                            text_length=len(msg.text),
                        )
                        # Fallback: send as plain text
                        try:
                            await telegram_update.effective_message.reply_text(
                                combined,
                                reply_markup=None,
                                disable_notification=True,
                                do_quote=False,
                            )
                        except Exception:
                            # Both HTML and plain text failed — mark flush as failed
                            _flush_succeeded[0] = False

    async def _schedule_flush() -> None:
        """Wait for the batch window then flush."""
        await asyncio.sleep(_TEXT_BATCH_WINDOW)
        await _flush_pending_text()
        _text_batch_task[0] = None

    async def _enqueue_text(text: str, is_thinking: bool = False) -> None:
        """Add text to the pending batch and schedule a flush."""
        t0 = time.time()
        async with _stream_lock:
            lock_wait_ms = (time.time() - t0) * 1000
            if lock_wait_ms > 100:
                logger.warning(
                    "stream_lock.slow_acquire",
                    wait_ms=round(lock_wait_ms, 1),
                    phase="enqueue",
                )

            if is_thinking:
                _pending_thinking.append(text)
            else:
                _pending_text.append(text)

            # Cancel any existing scheduled flush and restart the timer
            if _text_batch_task[0] is not None:
                _text_batch_task[0].cancel()
            _text_batch_task[0] = asyncio.create_task(_schedule_flush())

    async def _on_stream(update_obj: StreamUpdate) -> None:
        # Intercept send_image_to_user MCP tool calls.
        # The SDK namespaces MCP tools as "mcp__<server>__<tool>",
        # so match both the bare name and the namespaced variant.
        if update_obj.tool_calls and need_mcp_intercept:
            for tc in update_obj.tool_calls:
                tc_name = tc.get("name", "")
                if tc_name == "send_image_to_user" or tc_name.endswith(
                    "__send_image_to_user"
                ):
                    tc_input = tc.get("input", {})
                    file_path = tc_input.get("file_path", "")
                    caption = tc_input.get("caption", "")
                    img = validate_image_path(file_path, approved_directory, caption)
                    if img:
                        mcp_images.append(img)

        # Capture tool calls
        if update_obj.tool_calls:
            for tc in update_obj.tool_calls:
                name = tc.get("name", "unknown")
                detail = _summarize_tool_input(name, tc.get("input", {}))
                if verbose_level >= 1:
                    tool_log.append({"kind": "tool", "name": name, "detail": detail})
                if draft_streamer:
                    icon = _tool_icon(name)
                    line = f"{icon} {name}: {detail}" if detail else f"{icon} {name}"
                    await draft_streamer.append_tool(line)
                if heartbeat_pin:
                    await heartbeat_pin.tool_called(name)

        # Send thinking blocks as ephemeral messages (deleted after response)
        if update_obj.type == "thinking" and update_obj.content:
            text = update_obj.content.strip()
            if text and telegram_update:
                await _enqueue_text(text, is_thinking=True)
            if draft_streamer:
                first_line = text.split("\n", 1)[0].strip() if text else ""
                if first_line:
                    await draft_streamer.append_tool(f"\U0001f914 {first_line[:80]}")

        # Capture assistant text (reasoning / commentary)
        if update_obj.type == "assistant" and update_obj.content:
            text = update_obj.content.strip()
            if text:
                # Send every assistant text as a persistent standalone
                # message.  The _text_was_sent flag is set when the
                # flush actually delivers to Telegram — the final
                # response path checks this and skips the main text
                # to avoid duplication.
                if telegram_update:
                    await _enqueue_text(text)

                first_line = text.split("\n", 1)[0].strip()
                if first_line:
                    if verbose_level >= 1:
                        tool_log.append({"kind": "text", "detail": first_line[:120]})
                    if draft_streamer:
                        await draft_streamer.append_tool(
                            f"\U0001f4ac {first_line[:120]}"
                        )

        # Stream text to user via draft (prefer token deltas;
        # skip full assistant messages to avoid double-appending)
        if draft_streamer and update_obj.content:
            if update_obj.type == "stream_delta":
                await draft_streamer.append_text(update_obj.content)

        # Throttle progress message edits to avoid Telegram rate limits.
        # Skip entirely when heartbeat pin is active — it IS the liveness signal.
        if not draft_streamer and verbose_level >= 1:
            if heartbeat_pin and heartbeat_pin.has_active_message:
                pass  # heartbeat pin handles liveness
            elif (time.time() - last_edit_time[0]) >= 8.0 and tool_log:
                last_edit_time[0] = time.time()
                new_text = _format_verbose_progress(tool_log, verbose_level, start_time)
                try:
                    await progress_msg.edit_text(new_text)
                except Exception:
                    pass

    async def _delete_thinking_messages() -> None:
        """Delete all ephemeral thinking messages sent during streaming."""
        if not telegram_update or not _thinking_message_ids:
            return
        chat = telegram_update.effective_message.chat
        for msg_id in _thinking_message_ids:
            try:
                await chat.delete_message(msg_id)
            except Exception as e:
                logger.debug(
                    "Failed to delete thinking message",
                    message_id=msg_id,
                    error=str(e),
                )
        _thinking_message_ids.clear()

    # Attach helpers so callers can drain pending text and check state
    _on_stream.flush_pending = _flush_pending_text  # type: ignore[attr-defined]
    _on_stream.delete_thinking = _delete_thinking_messages  # type: ignore[attr-defined]
    _on_stream.text_was_sent = _text_was_sent  # type: ignore[attr-defined]
    _on_stream.flush_succeeded = _flush_succeeded  # type: ignore[attr-defined]

    return _on_stream


async def flush_stream_callback(
    on_stream: Optional[Callable],
) -> None:
    """Flush any pending batched text from the stream callback.

    Call this after streaming completes to ensure any buffered
    intermediate text or thinking blocks are sent before the
    final response.
    """
    if on_stream and hasattr(on_stream, "flush_pending"):
        try:
            await on_stream.flush_pending()
        except Exception as e:
            logger.debug("Failed to flush pending stream text", error=str(e))


async def cleanup_thinking_messages(
    on_stream: Optional[Callable],
) -> None:
    """Delete ephemeral thinking messages after the final response is sent."""
    if on_stream and hasattr(on_stream, "delete_thinking"):
        try:
            await on_stream.delete_thinking()
        except Exception as e:
            logger.debug("Failed to delete thinking messages", error=str(e))
