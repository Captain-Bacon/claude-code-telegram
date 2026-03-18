"""Response delivery — getting Claude's output to Telegram.

Handles turn result formatting, image sending, context warnings,
abnormal stop notices, and typing indicator heartbeat.

deliver_turn_result is the shared pipeline called from three places:
orchestrator.agentic_text, orchestrator._drain_queue, and
media_handlers._handle_media_message. Changes here affect all
response paths.
"""

import asyncio
import time
from typing import Any, Callable, Dict, List, Optional

import structlog
from telegram import InputMediaPhoto, Update
from telegram.ext import ContextTypes

from ..config.settings import Settings
from .stream_handler import cleanup_thinking_messages, flush_stream_callback
from .utils.image_extractor import ImageAttachment, should_send_as_photo

logger = structlog.get_logger()

# Context window thresholds (remaining %) at which to warn the user
_CONTEXT_THRESHOLDS = [70, 60, 50, 40, 35, 30, 25, 20, 15, 10, 5]

_STOP_REASON_LABELS = {
    "max_tokens": "reached token limit",
    "max_turns": "reached tool use limit",
    "budget_exceeded": "reached cost limit",
    "stop_sequence": "hit a stop condition",
}


def context_warning(
    response: Any,
    user_data: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Return a context-window warning if a NEW threshold is crossed."""
    if not getattr(response, "context_window", None) or not getattr(
        response, "total_input_tokens", None
    ):
        return None
    used_pct = (response.total_input_tokens / response.context_window) * 100
    remaining_pct = 100 - used_pct
    if remaining_pct > _CONTEXT_THRESHOLDS[0]:
        return None
    level = None
    for threshold in _CONTEXT_THRESHOLDS:
        if remaining_pct <= threshold:
            level = threshold
        else:
            break
    if level is None:
        return None
    if user_data is not None:
        last_warned = user_data.get("_context_last_warned")
        if last_warned is not None and last_warned <= level:
            return None
        user_data["_context_last_warned"] = level
    if level <= 15:
        icon = "❗"
    elif level <= 35:
        icon = "⚠️"
    else:
        icon = "ℹ️"
    return f"\n\n{icon} {level}% context remaining"


def abnormal_stop_notice(response: Any) -> Optional[Any]:
    """Return a user-facing notice if the turn ended abnormally."""
    from .utils.formatting import FormattedMessage

    stop_reason = getattr(response, "stop_reason", None)
    if not stop_reason or stop_reason == "end_turn":
        return None
    label = _STOP_REASON_LABELS.get(stop_reason, stop_reason)
    return FormattedMessage(
        f"\n⚠️ Claude was cut short ({label}). " f"Send a follow-up to continue.",
        parse_mode=None,
    )


async def send_images(
    update: Update,
    images: List[ImageAttachment],
    reply_to_message_id: Optional[int] = None,
    caption: Optional[str] = None,
    caption_parse_mode: Optional[str] = None,
) -> bool:
    """Send extracted images as a media group (album) or documents.

    If *caption* is provided and fits (≤1024 chars), it is attached to the
    photo / first album item so text + images appear as one message.

    Returns True if the caption was successfully embedded in the photo message.
    """
    photos: List[ImageAttachment] = []
    documents: List[ImageAttachment] = []
    for img in images:
        if should_send_as_photo(img.path):
            photos.append(img)
        else:
            documents.append(img)

    # Telegram caption limit
    use_caption = bool(caption and len(caption) <= 1024 and photos and not documents)
    caption_sent = False

    # Send raster photos as a single album (Telegram groups 2-10 items)
    if photos:
        try:
            if len(photos) == 1:
                with open(photos[0].path, "rb") as f:
                    await update.message.reply_photo(
                        photo=f,
                        reply_to_message_id=reply_to_message_id,
                        caption=caption if use_caption else None,
                        parse_mode=caption_parse_mode if use_caption else None,
                    )
                caption_sent = use_caption
            else:
                media = []
                file_handles = []
                for idx, img in enumerate(photos[:10]):
                    fh = open(img.path, "rb")  # noqa: SIM115
                    file_handles.append(fh)
                    media.append(
                        InputMediaPhoto(
                            media=fh,
                            caption=caption if use_caption and idx == 0 else None,
                            parse_mode=(
                                caption_parse_mode if use_caption and idx == 0 else None
                            ),
                        )
                    )
                try:
                    await update.message.chat.send_media_group(
                        media=media,
                        reply_to_message_id=reply_to_message_id,
                    )
                    caption_sent = use_caption
                finally:
                    for fh in file_handles:
                        fh.close()
        except Exception as e:
            logger.warning("Failed to send photo album", error=str(e))

    # Send SVGs / large files as documents (one by one — can't mix in album)
    for img in documents:
        try:
            with open(img.path, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename=img.path.name,
                    reply_to_message_id=reply_to_message_id,
                )
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning(
                "Failed to send document image",
                path=str(img.path),
                error=str(e),
            )

    return caption_sent


async def deliver_turn_result(
    *,
    settings: Settings,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    claude_response: Any,
    on_stream: Optional[Callable],
    progress_msg: Any,
    start_time: float,
    mcp_images: List[ImageAttachment],
    success: bool = True,
    error_messages: Optional[List[Any]] = None,
) -> None:
    """Format, finalize progress, and deliver a turn's result.

    Shared pipeline for agentic_text, _drain_queue, and media handlers.
    On success (error_messages is None): flushes stream, formats the
    claude_response, appends notices.  On error: uses the pre-built
    error_messages list directly.
    """
    from .utils.formatting import FormattedMessage, ResponseFormatter

    if error_messages is not None:
        formatted_messages = error_messages
    else:
        await flush_stream_callback(on_stream)

        text_already_sent = (
            on_stream
            and hasattr(on_stream, "text_was_sent")
            and on_stream.text_was_sent
            and hasattr(on_stream, "flush_succeeded")
            and on_stream.flush_succeeded
        )

        if text_already_sent:
            formatted_messages: List[FormattedMessage] = []
        else:
            formatter = ResponseFormatter(settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

        if claude_response.is_interrupted:
            formatted_messages.append(
                FormattedMessage("[Interrupted]", parse_mode=None)
            )

        stop_notice = abnormal_stop_notice(claude_response)
        if stop_notice:
            formatted_messages.append(stop_notice)
            logger.info(
                "turn.abnormal_stop",
                stop_reason=claude_response.stop_reason,
                num_turns=claude_response.num_turns,
            )

        ctx_warn = context_warning(claude_response, context.user_data)
        if ctx_warn:
            if formatted_messages:
                formatted_messages[-1].text += ctx_warn
            else:
                formatted_messages.append(FormattedMessage(ctx_warn, parse_mode="HTML"))

    # Finalize progress message — always edit to final state
    elapsed = int(time.time() - start_time)
    try:
        if success:
            await progress_msg.edit_text(f"\u2705 Done ({elapsed}s)")
        else:
            await progress_msg.edit_text(f"\u274c Failed ({elapsed}s)")
    except Exception:
        try:
            await progress_msg.delete()
        except Exception:
            pass

    # Send images + text with caption optimisation
    images = mcp_images
    caption_sent = False
    if images and len(formatted_messages) == 1:
        msg = formatted_messages[0]
        if msg.text and len(msg.text) <= 1024:
            try:
                caption_sent = await send_images(
                    update,
                    images,
                    reply_to_message_id=update.message.message_id,
                    caption=msg.text,
                    caption_parse_mode=msg.parse_mode,
                )
            except Exception as img_err:
                logger.warning("Image+caption send failed", error=str(img_err))

    if not caption_sent:
        for i, message in enumerate(formatted_messages):
            if not message.text or not message.text.strip():
                continue
            try:
                await update.message.reply_text(
                    message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=None,
                )
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)
            except Exception as send_err:
                logger.warning(
                    "Failed to send HTML response, retrying as plain text",
                    error=str(send_err),
                )
                try:
                    await update.message.reply_text(
                        message.text,
                        reply_markup=None,
                    )
                except Exception as plain_err:
                    await update.message.reply_text(
                        f"Failed to deliver response "
                        f"(Telegram error: {str(plain_err)[:150]})."
                    )

        if images:
            try:
                await send_images(
                    update,
                    images,
                    reply_to_message_id=update.message.message_id,
                )
            except Exception as img_err:
                logger.warning("Image send failed", error=str(img_err))

    await cleanup_thinking_messages(on_stream)
