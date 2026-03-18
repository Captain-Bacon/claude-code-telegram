"""Telegram media message handlers — document, photo, voice.

Each handler validates input, then delegates to _handle_media_message
which runs the prompt through Claude via PersistentClientManager.
Response delivery uses deliver_turn_result from delivery.py.

Registered by orchestrator._register_agentic_handlers via wrapper
functions that bind settings (handlers take settings as first arg,
PTB expects update+context only).

"""

import asyncio
import time
from typing import Any, Dict, List, Optional

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from ..claude.persistent import PersistentClientManager, derive_state_key
from ..config.features import FeatureFlags
from ..config.settings import Settings
from .delivery import deliver_turn_result, start_typing_heartbeat
from .stream_handler import flush_stream_callback, make_stream_callback
from .utils.error_format import (
    _format_error_message,
    _update_working_directory_from_claude_response,
)
from .utils.html_format import escape_html
from .utils.image_extractor import ImageAttachment

logger = structlog.get_logger()


def _get_verbose_level(
    settings: Settings, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Return effective verbose level: per-user override or global default.

    Shared — imported by orchestrator.py for agentic_text and _drain_queue.
    """
    user_override = context.user_data.get("verbose_level")
    if user_override is not None:
        return int(user_override)
    return settings.verbose_level


def _voice_unavailable_message(settings: Settings) -> str:
    """Return provider-aware guidance when voice feature is unavailable."""
    api_key_env = settings.voice_provider_api_key_env
    if api_key_env:
        return (
            "Voice processing is not available. "
            f"Set {api_key_env} "
            f"for {settings.voice_provider_display_name} and install "
            'voice extras with: pip install "claude-code-telegram[voice]"'
        )
    # Local provider (Parakeet) -- no API key, different install instructions
    return (
        "Voice processing is not available. "
        f"Install {settings.voice_provider_display_name} with: "
        'pip install "claude-code-telegram[voice-local]" '
        "and ensure ffmpeg is installed."
    )


async def agentic_document(
    settings: Settings,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Process file upload -> Claude, minimal chrome."""
    user_id = update.effective_user.id
    document = update.message.document

    logger.info(
        "Agentic document upload",
        user_id=user_id,
        filename=document.file_name,
    )

    # Security validation
    security_validator = context.bot_data.get("security_validator")
    if security_validator:
        valid, error = security_validator.validate_filename(document.file_name)
        if not valid:
            await update.message.reply_text(f"File rejected: {error}")
            return

    # Size check
    max_size = 10 * 1024 * 1024
    if document.file_size > max_size:
        await update.message.reply_text(
            f"File too large ({document.file_size / 1024 / 1024:.1f}MB). Max: 10MB."
        )
        return

    chat = update.message.chat
    await chat.send_action("typing")
    progress_msg = await update.message.reply_text("Working...")

    prompt: Optional[str] = None

    file = await document.get_file()
    file_bytes = await file.download_as_bytearray()
    try:
        content = file_bytes.decode("utf-8")
        if len(content) > 50000:
            content = content[:50000] + "\n... (truncated)"
        caption = update.message.caption or "Please review this file:"
        prompt = (
            f"{caption}\n\n**File:** `{document.file_name}`\n\n"
            f"```\n{content}\n```"
        )
    except UnicodeDecodeError:
        await progress_msg.edit_text(
            "Unsupported file format. Must be text-based (UTF-8)."
        )
        return

    # Process with Claude via persistent client
    await _handle_media_message(
        settings=settings,
        update=update,
        context=context,
        prompt=prompt,
        progress_msg=progress_msg,
        user_id=user_id,
        chat=chat,
    )


async def agentic_photo(
    settings: Settings,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Process photo -> Claude, minimal chrome."""
    user_id = update.effective_user.id

    from .media.image_handler import ImageHandler

    image_handler = ImageHandler(config=settings)

    chat = update.message.chat
    await chat.send_action("typing")
    progress_msg = await update.message.reply_text("Working...")

    try:
        photo = update.message.photo[-1]
        processed_image = await image_handler.process_image(
            photo, update.message.caption
        )
        await _handle_media_message(
            settings=settings,
            update=update,
            context=context,
            prompt=processed_image.prompt,
            progress_msg=progress_msg,
            user_id=user_id,
            chat=chat,
            images=[
                {
                    "base64_data": processed_image.base64_data,
                    "media_type": processed_image.media_type,
                }
            ],
        )

    except Exception as e:
        await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
        logger.error(
            "Claude photo processing failed", error=str(e), user_id=user_id
        )


async def agentic_voice(
    settings: Settings,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Transcribe voice message -> Claude, minimal chrome."""
    user_id = update.effective_user.id

    from .media.voice_handler import VoiceHandler

    if not FeatureFlags(settings).voice_messages_enabled:
        await update.message.reply_text(_voice_unavailable_message(settings))
        return

    voice_handler = VoiceHandler(config=settings)

    chat = update.message.chat
    await chat.send_action("typing")
    progress_msg = await update.message.reply_text("Transcribing...")

    try:
        voice = update.message.voice
        processed_voice = await voice_handler.process_voice_message(
            voice, update.message.caption
        )

        # Show transcription so the user can see what was heard
        transcription_text = escape_html(processed_voice.transcription)
        await update.message.reply_text(
            f"\U0001f3a4 <b>Transcription:</b>\n{transcription_text}",
            parse_mode="HTML",
        )

        await progress_msg.edit_text("Working...")
        await _handle_media_message(
            settings=settings,
            update=update,
            context=context,
            prompt=processed_voice.prompt,
            progress_msg=progress_msg,
            user_id=user_id,
            chat=chat,
        )

    except Exception as e:
        await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
        logger.error(
            "Claude voice processing failed", error=str(e), user_id=user_id
        )


async def _handle_media_message(
    *,
    settings: Settings,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    prompt: str,
    progress_msg: Any,
    user_id: int,
    chat: Any,
    images: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Run a media-derived prompt through Claude and send responses."""
    persistent_manager: Optional[PersistentClientManager] = context.bot_data.get(
        "persistent_manager"
    )
    if not persistent_manager:
        await progress_msg.edit_text(
            "Claude integration not available. Check configuration."
        )
        return

    chat_id = update.effective_chat.id
    thread_id = getattr(update.message, "message_thread_id", None)
    user_id_for_key = update.effective_user.id
    state_key = derive_state_key(chat_id, thread_id, user_id_for_key)

    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    force_new = bool(context.user_data.get("force_new_session"))

    verbose_level = _get_verbose_level(settings, context)
    tool_log: List[Dict[str, Any]] = []
    start_time = time.time()
    mcp_images_media: List[ImageAttachment] = []
    on_stream = make_stream_callback(
        settings,
        verbose_level,
        progress_msg,
        tool_log,
        start_time,
        mcp_images=mcp_images_media,
        approved_directory=settings.approved_directory,
        telegram_update=update,
    )

    heartbeat = start_typing_heartbeat(chat)

    error_messages = None
    claude_response = None
    success = True
    try:
        claude_response = await persistent_manager.send_message(
            state_key=state_key,
            prompt=prompt,
            working_directory=current_dir,
            stream_callback=on_stream,
            model=context.user_data.get("claude_model"),
            force_new=force_new,
            images=images,
        )

        if claude_response is None:
            heartbeat.cancel()
            try:
                await progress_msg.delete()
            except Exception:
                pass
            return

        if force_new:
            context.user_data["force_new_session"] = False

        context.user_data["claude_session_id"] = claude_response.session_id

        _update_working_directory_from_claude_response(
            claude_response, context, settings, user_id
        )

    except asyncio.CancelledError:
        success = False
        logger.info("Claude media request cancelled", user_id=user_id)
        from .utils.formatting import FormattedMessage

        error_messages = [FormattedMessage("Stopped.", parse_mode=None)]
    except Exception as e:
        success = False
        logger.error(
            "Claude media processing failed", error=str(e), user_id=user_id
        )
        from .utils.formatting import FormattedMessage

        error_messages = [
            FormattedMessage(_format_error_message(e), parse_mode="HTML")
        ]
    finally:
        heartbeat.cancel()
        try:
            await flush_stream_callback(on_stream)
        except Exception:
            pass

    await deliver_turn_result(
        settings=settings,
        update=update,
        context=context,
        claude_response=claude_response,
        on_stream=on_stream,
        progress_msg=progress_msg,
        start_time=start_time,
        mcp_images=mcp_images_media,
        success=success,
        error_messages=error_messages,
    )
