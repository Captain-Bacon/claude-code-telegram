"""Message orchestrator — single entry point for all Telegram updates.

Provides a minimal agentic conversational interface (commands + text/file/photo).
"""

import asyncio
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..claude.persistent import PersistentClientManager, derive_state_key
from ..config.settings import Settings
from ..projects import PrivateTopicsUnavailableError, load_project_registry
from ..security.audit import AuditLogger
from .stream_handler import (
    cleanup_thinking_messages,
    flush_stream_callback,
    make_stream_callback,
)
from .utils.draft_streamer import DraftStreamer, generate_draft_id
from .utils.error_format import _format_error_message, _update_working_directory_from_claude_response
from .utils.html_format import escape_html
from .utils.image_extractor import (
    ImageAttachment,
    should_send_as_photo,
    validate_image_path,
)

logger = structlog.get_logger()


@dataclass
class QueuedMessage:
    """A message queued while Claude was busy."""

    text: str
    sent_at: float  # time.time() when user sent it
    placeholder_message_id: Optional[int] = None  # Telegram msg id of the placeholder



def _is_private_chat(update: Update) -> bool:
    """Return True when update is from a private chat."""
    chat = update.effective_chat
    return bool(chat and getattr(chat, "type", "") == "private")


async def restart_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /restart command - gracefully restart the bot process.

    Sends a confirmation message then triggers SIGTERM so systemd
    (or any process manager with restart-on-exit) brings the bot back up.

    Auth: protected by the auth middleware (group -2) which raises
    ``ApplicationHandlerStop`` for unauthenticated users before any
    handler in group 10 runs.  No per-handler check is needed.
    """
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    user_id = update.effective_user.id

    await update.message.reply_text(
        "🔄 <b>Restarting bot…</b>\n\nBack shortly.",
        parse_mode="HTML",
    )

    if audit_logger:
        await audit_logger.log_command(user_id, "restart", [], True)

    logger.info("Restart requested via /restart command", user_id=user_id)

    # SIGTERM triggers the existing graceful-shutdown handler in main.py;
    # systemd Restart=always will bring the process back up.
    os.kill(os.getpid(), signal.SIGTERM)


async def sync_threads(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Synchronize project topics in the configured forum chat."""
    settings: Settings = context.bot_data["settings"]
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    user_id = update.effective_user.id

    if not settings.enable_project_threads:
        await update.message.reply_text(
            "ℹ️ <b>Project thread mode is disabled.</b>", parse_mode="HTML"
        )
        return

    manager = context.bot_data.get("project_threads_manager")
    if not manager:
        await update.message.reply_text(
            "❌ <b>Project thread manager not initialized.</b>", parse_mode="HTML"
        )
        return

    status_msg = await update.message.reply_text(
        "🔄 <b>Syncing project topics...</b>", parse_mode="HTML"
    )

    if settings.project_threads_mode == "private":
        if not _is_private_chat(update):
            await status_msg.edit_text(
                "❌ <b>Private Thread Mode</b>\n\n"
                "Run <code>/sync_threads</code> in your private chat with the bot.",
                parse_mode="HTML",
            )
            return
        target_chat_id = update.effective_chat.id
    else:
        if settings.project_threads_chat_id is None:
            await status_msg.edit_text(
                "❌ <b>Group Thread Mode Misconfigured</b>\n\n"
                "Set <code>PROJECT_THREADS_CHAT_ID</code> first.",
                parse_mode="HTML",
            )
            return
        if (
            not update.effective_chat
            or update.effective_chat.id != settings.project_threads_chat_id
        ):
            await status_msg.edit_text(
                "❌ <b>Group Thread Mode</b>\n\n"
                "Run <code>/sync_threads</code> in the configured project threads group.",
                parse_mode="HTML",
            )
            return
        target_chat_id = settings.project_threads_chat_id

    try:
        if not settings.projects_config_path:
            await status_msg.edit_text(
                "❌ <b>Project thread mode is misconfigured</b>\n\n"
                "Set <code>PROJECTS_CONFIG_PATH</code> to a valid YAML file.",
                parse_mode="HTML",
            )
            if audit_logger:
                await audit_logger.log_command(user_id, "sync_threads", [], False)
            return

        registry = load_project_registry(
            config_path=settings.projects_config_path,
            approved_directory=settings.approved_directory,
        )
        manager.registry = registry
        context.bot_data["project_registry"] = registry

        result = await manager.sync_topics(context.bot, chat_id=target_chat_id)
        await status_msg.edit_text(
            "✅ <b>Project topic sync complete</b>\n\n"
            f"• Created: <b>{result.created}</b>\n"
            f"• Reused: <b>{result.reused}</b>\n"
            f"• Renamed: <b>{result.renamed}</b>\n"
            f"• Reopened: <b>{result.reopened}</b>\n"
            f"• Closed: <b>{result.closed}</b>\n"
            f"• Deactivated: <b>{result.deactivated}</b>\n"
            f"• Failed: <b>{result.failed}</b>",
            parse_mode="HTML",
        )
        if audit_logger:
            await audit_logger.log_command(user_id, "sync_threads", [], True)
    except PrivateTopicsUnavailableError:
        await status_msg.edit_text(
            manager.private_topics_unavailable_message(),
            parse_mode="HTML",
        )
        if audit_logger:
            await audit_logger.log_command(user_id, "sync_threads", [], False)
    except Exception as e:
        await status_msg.edit_text(
            f"❌ <b>Project topic sync failed</b>\n\n{escape_html(str(e))}",
            parse_mode="HTML",
        )
        if audit_logger:
            await audit_logger.log_command(user_id, "sync_threads", [], False)


class MessageOrchestrator:
    """Routes messages based on mode. Single entry point for all Telegram updates."""

    _CONTEXT_THRESHOLDS = [70, 60, 50, 40, 35, 30, 25, 20, 15, 10, 5]

    def __init__(self, settings: Settings, deps: Dict[str, Any]):
        self.settings = settings
        self.deps = deps
        # Track active Claude tasks per user so /stop can cancel them (legacy)
        self._active_tasks: Dict[int, asyncio.Task] = {}  # type: ignore[type-arg]
        # Message queue per state_key — messages received while Claude is busy
        self._message_queues: Dict[str, List[QueuedMessage]] = {}

    @staticmethod
    def _state_key(update: Update) -> str:
        """Derive a persistent client state key from a Telegram update."""
        chat_id = update.effective_chat.id
        thread_id = getattr(update.message, "message_thread_id", None)
        user_id = update.effective_user.id
        return derive_state_key(chat_id, thread_id, user_id)

    @staticmethod
    def _context_warning(
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
        if remaining_pct > MessageOrchestrator._CONTEXT_THRESHOLDS[0]:
            return None
        level = None
        for threshold in MessageOrchestrator._CONTEXT_THRESHOLDS:
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

    _STOP_REASON_LABELS = {
        "max_tokens": "reached token limit",
        "max_turns": "reached tool use limit",
        "budget_exceeded": "reached cost limit",
        "stop_sequence": "hit a stop condition",
    }

    @staticmethod
    def _abnormal_stop_notice(response: Any) -> Optional["FormattedMessage"]:
        """Return a user-facing notice if the turn ended abnormally."""
        from .utils.formatting import FormattedMessage

        stop_reason = getattr(response, "stop_reason", None)
        if not stop_reason or stop_reason == "end_turn":
            return None
        label = MessageOrchestrator._STOP_REASON_LABELS.get(
            stop_reason, stop_reason
        )
        return FormattedMessage(
            f"\n⚠️ Claude was cut short ({label}). "
            f"Send a follow-up to continue.",
            parse_mode=None,
        )

    def _inject_deps(self, handler: Callable) -> Callable:  # type: ignore[type-arg]
        """Wrap handler to inject dependencies into context.bot_data."""

        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            for key, value in self.deps.items():
                context.bot_data[key] = value
            context.bot_data["settings"] = self.settings
            context.user_data.pop("_thread_context", None)

            is_sync_bypass = handler.__name__ == "sync_threads"
            is_start_bypass = handler.__name__ in {"start_command", "agentic_start"}
            message_thread_id = self._extract_message_thread_id(update)
            should_enforce = self.settings.enable_project_threads

            if should_enforce:
                if self.settings.project_threads_mode == "private":
                    should_enforce = not is_sync_bypass and not (
                        is_start_bypass and message_thread_id is None
                    )
                else:
                    should_enforce = not is_sync_bypass

            if should_enforce:
                allowed = await self._apply_thread_routing_context(update, context)
                if not allowed:
                    return

            try:
                await handler(update, context)
            finally:
                if should_enforce:
                    self._persist_thread_state(context)

        return wrapped

    async def _apply_thread_routing_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        """Enforce strict project-thread routing and load thread-local state."""
        manager = context.bot_data.get("project_threads_manager")
        if manager is None:
            await self._reject_for_thread_mode(
                update,
                "❌ <b>Project Thread Mode Misconfigured</b>\n\n"
                "Thread manager is not initialized.",
            )
            return False

        chat = update.effective_chat
        message = update.effective_message
        if not chat or not message:
            return False

        if self.settings.project_threads_mode == "group":
            if chat.id != self.settings.project_threads_chat_id:
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False
        else:
            if getattr(chat, "type", "") != "private":
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False

        message_thread_id = self._extract_message_thread_id(update)
        if not message_thread_id:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        project = await manager.resolve_project(chat.id, message_thread_id)
        if not project:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        state_key = f"{chat.id}:{message_thread_id}"
        thread_states = context.user_data.setdefault("thread_state", {})
        state = thread_states.get(state_key, {})

        project_root = project.absolute_path
        current_dir_raw = state.get("current_directory")
        current_dir = (
            Path(current_dir_raw).resolve() if current_dir_raw else project_root
        )
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        context.user_data["current_directory"] = current_dir
        context.user_data["claude_session_id"] = state.get("claude_session_id")
        context.user_data["_thread_context"] = {
            "chat_id": chat.id,
            "message_thread_id": message_thread_id,
            "state_key": state_key,
            "project_slug": project.slug,
            "project_root": str(project_root),
            "project_name": project.name,
        }
        return True

    def _persist_thread_state(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Persist compatibility keys back into per-thread state."""
        thread_context = context.user_data.get("_thread_context")
        if not thread_context:
            return

        project_root = Path(thread_context["project_root"])
        current_dir = context.user_data.get("current_directory", project_root)
        if not isinstance(current_dir, Path):
            current_dir = Path(str(current_dir))
        current_dir = current_dir.resolve()
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        thread_states = context.user_data.setdefault("thread_state", {})
        thread_states[thread_context["state_key"]] = {
            "current_directory": str(current_dir),
            "claude_session_id": context.user_data.get("claude_session_id"),
            "project_slug": thread_context["project_slug"],
        }

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        """Return True if path is within root."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _extract_message_thread_id(update: Update) -> Optional[int]:
        """Extract topic/thread id from update message for forum/direct topics."""
        message = update.effective_message
        if not message:
            return None
        message_thread_id = getattr(message, "message_thread_id", None)
        if isinstance(message_thread_id, int) and message_thread_id > 0:
            return message_thread_id
        dm_topic = getattr(message, "direct_messages_topic", None)
        topic_id = getattr(dm_topic, "topic_id", None) if dm_topic else None
        if isinstance(topic_id, int) and topic_id > 0:
            return topic_id
        # Telegram omits message_thread_id for the General topic in forum
        # supergroups; its canonical thread ID is 1.
        chat = update.effective_chat
        if chat and getattr(chat, "is_forum", False):
            return 1
        return None

    async def _reject_for_thread_mode(self, update: Update, message: str) -> None:
        """Send a guidance response when strict thread routing rejects an update."""
        query = update.callback_query
        if query:
            try:
                await query.answer()
            except Exception:
                pass
            if query.message:
                await query.message.reply_text(message, parse_mode="HTML")
            return

        if update.effective_message:
            await update.effective_message.reply_text(message, parse_mode="HTML")

    def register_handlers(self, app: Application) -> None:
        """Register agentic handlers."""
        self._register_agentic_handlers(app)

    def _register_agentic_handlers(self, app: Application) -> None:
        """Register agentic handlers: commands + text/file/photo."""
        # Commands
        handlers: list[tuple[str, Callable]] = [
            ("start", self.agentic_start),
            ("new", self.agentic_new),
            ("status", self.agentic_status),
            ("verbose", self.agentic_verbose),
            ("repo", self.agentic_repo),
            ("model", self.agentic_model),
            ("restart", restart_command),
            ("stop", self.agentic_stop),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", sync_threads))

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        # Text messages -> Claude
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(self.agentic_text),
            ),
            group=10,
        )

        # File uploads -> Claude
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(self.agentic_document)
            ),
            group=10,
        )

        # Photo uploads -> Claude
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(self.agentic_photo)),
            group=10,
        )

        # Voice messages -> transcribe -> Claude
        app.add_handler(
            MessageHandler(filters.VOICE, self._inject_deps(self.agentic_voice)),
            group=10,
        )

        # Only cd: callbacks (for project selection), scoped by pattern
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._agentic_callback),
                pattern=r"^cd:",
            )
        )

        logger.info("Agentic handlers registered")

    async def get_bot_commands(self) -> list:  # type: ignore[type-arg]
        """Return bot commands for the Telegram command menu."""
        commands = [
            BotCommand("start", "Start the bot"),
            BotCommand("new", "Start a fresh session"),
            BotCommand("status", "Show session status"),
            BotCommand("verbose", "Set output verbosity (0/1/2)"),
            BotCommand("repo", "List repos / switch workspace"),
            BotCommand("model", "Switch Claude model"),
            BotCommand("restart", "Restart the bot"),
            BotCommand("stop", "Cancel the current Claude request"),
        ]
        if self.settings.enable_project_threads:
            commands.append(BotCommand("sync_threads", "Sync project topics"))
        return commands

    # --- Agentic handlers ---

    async def agentic_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Brief welcome, no buttons."""
        user = update.effective_user
        sync_line = ""
        if (
            self.settings.enable_project_threads
            and self.settings.project_threads_mode == "private"
        ):
            if (
                not update.effective_chat
                or getattr(update.effective_chat, "type", "") != "private"
            ):
                await update.message.reply_text(
                    "🚫 <b>Private Topics Mode</b>\n\n"
                    "Use this bot in a private chat and run <code>/start</code> there.",
                    parse_mode="HTML",
                )
                return
            manager = context.bot_data.get("project_threads_manager")
            if manager:
                try:
                    result = await manager.sync_topics(
                        context.bot,
                        chat_id=update.effective_chat.id,
                    )
                    sync_line = (
                        "\n\n🧵 Topics synced"
                        f" (created {result.created}, reused {result.reused})."
                    )
                except PrivateTopicsUnavailableError:
                    await update.message.reply_text(
                        manager.private_topics_unavailable_message(),
                        parse_mode="HTML",
                    )
                    return
                except Exception:
                    sync_line = "\n\n🧵 Topic sync failed. Run /sync_threads to retry."
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        dir_display = f"<code>{current_dir}/</code>"

        safe_name = escape_html(user.first_name)
        await update.message.reply_text(
            f"Hi {safe_name}! I'm your AI coding assistant.\n"
            f"Just tell me what you need — I can read, write, and run code.\n\n"
            f"Working in: {dir_display}\n"
            f"Commands: /new (reset) · /status"
            f"{sync_line}",
            parse_mode="HTML",
        )

    async def agentic_new(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Reset session — disconnect persistent client, clear state."""
        persistent_manager: Optional[PersistentClientManager] = context.bot_data.get(
            "persistent_manager"
        )
        if persistent_manager:
            state_key = self._state_key(update)
            await persistent_manager.disconnect_client(state_key)

        context.user_data["claude_session_id"] = None
        context.user_data["session_started"] = True
        context.user_data["force_new_session"] = True
        context.user_data.pop("_context_last_warned", None)

        await update.message.reply_text("Session reset. What's next?")

    async def agentic_model(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Switch Claude model at runtime."""
        aliases = {
            "opus": "claude-opus-4-6",
            "sonnet": "claude-sonnet-4-6",
            "haiku": "claude-haiku-4-5-20251001",
        }
        args = update.message.text.split()[1:] if update.message.text else []
        if not args:
            current = context.user_data.get("claude_model")
            display = current or "default (CLI decides)"
            await update.message.reply_text(
                f"Model: <b>{escape_html(display)}</b>\n\n"
                "Usage: <code>/model opus|sonnet|haiku|default</code>",
                parse_mode="HTML",
            )
            return
        choice = args[0].lower()
        if choice == "default":
            context.user_data.pop("claude_model", None)
            await update.message.reply_text(
                "Model reset to <b>default</b> (CLI decides)",
                parse_mode="HTML",
            )
            return
        model_id = aliases.get(choice, choice)
        context.user_data["claude_model"] = model_id
        await update.message.reply_text(
            f"Model set to <b>{escape_html(model_id)}</b>",
            parse_mode="HTML",
        )

    async def agentic_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Compact one-line status, no buttons."""
        from src import get_build_info

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        dir_display = str(current_dir)

        session_id = context.user_data.get("claude_session_id")
        session_status = "active" if session_id else "none"

        # Cost info
        cost_str = ""
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            try:
                user_status = rate_limiter.get_user_status(update.effective_user.id)
                cost_usage = user_status.get("cost_usage", {})
                current_cost = cost_usage.get("current", 0.0)
                cost_str = f" · Cost: ${current_cost:.2f}"
            except Exception:
                pass

        model_str = ""
        current_model = context.user_data.get("claude_model")
        if current_model:
            model_str = f" · Model: {current_model}"

        build = get_build_info()

        await update.message.reply_text(
            f"📂 {dir_display} · Session: {session_status}{cost_str}{model_str}\n"
            f"Build: <code>{escape_html(build)}</code>",
            parse_mode="HTML",
        )

    def _get_verbose_level(self, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Return effective verbose level: per-user override or global default."""
        user_override = context.user_data.get("verbose_level")
        if user_override is not None:
            return int(user_override)
        return self.settings.verbose_level

    async def agentic_verbose(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Set output verbosity: /verbose [0|1|2]."""
        args = update.message.text.split()[1:] if update.message.text else []
        if not args:
            current = self._get_verbose_level(context)
            labels = {0: "quiet", 1: "normal", 2: "detailed"}
            await update.message.reply_text(
                f"Verbosity: <b>{current}</b> ({labels.get(current, '?')})\n\n"
                "Usage: <code>/verbose 0|1|2</code>\n"
                "  0 = quiet (final response only)\n"
                "  1 = normal (tools + reasoning)\n"
                "  2 = detailed (tools with inputs + reasoning)",
                parse_mode="HTML",
            )
            return

        try:
            level = int(args[0])
            if level not in (0, 1, 2):
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Please use: /verbose 0, /verbose 1, or /verbose 2"
            )
            return

        context.user_data["verbose_level"] = level
        labels = {0: "quiet", 1: "normal", 2: "detailed"}
        await update.message.reply_text(
            f"Verbosity set to <b>{level}</b> ({labels[level]})",
            parse_mode="HTML",
        )

    async def agentic_stop(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Stop the active Claude work for this thread."""
        persistent_manager: Optional[PersistentClientManager] = context.bot_data.get(
            "persistent_manager"
        )
        if not persistent_manager:
            await update.message.reply_text("Nothing running to stop.")
            return

        state_key = self._state_key(update)
        result = await persistent_manager.stop_client(state_key)

        # Clear queued messages
        queued = self._message_queues.pop(state_key, [])

        if not result.was_busy and not queued:
            await update.message.reply_text("Nothing running to stop.")
            return

        # Delete queue placeholder messages from Telegram
        chat = update.effective_chat
        for qm in queued:
            if qm.placeholder_message_id and chat:
                try:
                    await chat.delete_message(qm.placeholder_message_id)
                except Exception:
                    pass

        # Report what happened
        parts: List[str] = []
        if result.was_busy:
            parts.append("Stopped.")
        if queued:
            parts.append(f"{len(queued)} queued message(s) discarded:")
            for qm in queued:
                preview = qm.text[:100] + ("..." if len(qm.text) > 100 else "")
                parts.append(f"  \u2022 {escape_html(preview)}")
        await update.message.reply_text("\n".join(parts), parse_mode="HTML")

        logger.info(
            "User stopped client",
            user_id=update.effective_user.id,
            state_key=state_key,
            discarded_queued=len(queued),
        )

    @staticmethod
    def _start_typing_heartbeat(
        chat: Any,
        interval: float = 2.0,
    ) -> "asyncio.Task[None]":
        """Start a background typing indicator task.

        Sends typing every *interval* seconds, independently of
        stream events. Cancel the returned task in a ``finally``
        block.
        """

        async def _heartbeat() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        await chat.send_action("typing")
                    except Exception:
                        pass
            except asyncio.CancelledError:
                pass

        return asyncio.create_task(_heartbeat())

    async def _send_images(
        self,
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
        use_caption = bool(
            caption and len(caption) <= 1024 and photos and not documents
        )
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
                                    caption_parse_mode
                                    if use_caption and idx == 0
                                    else None
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

    async def agentic_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Direct Claude passthrough. Simple progress. No suggestions."""
        user_id = update.effective_user.id
        message_text = update.message.text

        logger.info(
            "Agentic text message",
            user_id=user_id,
            message_length=len(message_text),
        )

        # Rate limit check
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            allowed, limit_message = await rate_limiter.check_rate_limit(user_id, 0.001)
            if not allowed:
                await update.message.reply_text(f"⏱️ {limit_message}")
                return

        chat = update.message.chat
        await chat.send_action("typing")

        persistent_manager: Optional[PersistentClientManager] = context.bot_data.get(
            "persistent_manager"
        )
        if not persistent_manager:
            await update.message.reply_text(
                "Claude integration not available. Check configuration."
            )
            return

        state_key = self._state_key(update)
        client_state = persistent_manager.get_client_state(state_key)

        # If client is busy, queue the message for delivery after the
        # current turn finishes.  No injection — queued messages are
        # sent as a normal turn once Claude is idle.
        if client_state in ("busy", "draining"):
            await self._enqueue_message(
                state_key=state_key,
                message_text=message_text,
                update=update,
            )
            return

        verbose_level = self._get_verbose_level(context)
        progress_msg = await update.message.reply_text("Working...")

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )

        # Check if /new was used — skip auto-resume for this first message.
        force_new = bool(context.user_data.get("force_new_session"))

        # --- Verbose progress tracking via stream callback ---
        tool_log: List[Dict[str, Any]] = []
        start_time = time.time()
        mcp_images: List[ImageAttachment] = []

        # Stream drafts (private chats only)
        draft_streamer: Optional[DraftStreamer] = None
        if self.settings.enable_stream_drafts and chat.type == "private":
            draft_streamer = DraftStreamer(
                bot=context.bot,
                chat_id=chat.id,
                draft_id=generate_draft_id(),
                message_thread_id=update.message.message_thread_id,
                throttle_interval=self.settings.stream_draft_interval,
            )

        on_stream = make_stream_callback(self.settings,
            verbose_level,
            progress_msg,
            tool_log,
            start_time,
            mcp_images=mcp_images,
            approved_directory=self.settings.approved_directory,
            draft_streamer=draft_streamer,
            telegram_update=update,
        )

        # Independent typing heartbeat — stays alive even with no stream events
        heartbeat = self._start_typing_heartbeat(chat)

        success = True
        try:
            claude_response = await persistent_manager.send_message(
                state_key=state_key,
                prompt=message_text,
                working_directory=current_dir,
                stream_callback=on_stream,
                model=context.user_data.get("claude_model"),
                force_new=force_new,
            )

            # None means the message was injected into a busy turn
            # (race: state was idle at the orchestrator check but another
            # concurrent handler claimed the turn before we acquired the
            # send_lock inside send_message).
            if claude_response is None:
                heartbeat.cancel()
                try:
                    await update.message.set_reaction("\U0001f440")  # 👀
                except Exception:
                    pass
                try:
                    await progress_msg.delete()
                except Exception:
                    pass
                return

            # New session created successfully — clear the one-shot flag
            if force_new:
                context.user_data["force_new_session"] = False

            context.user_data["claude_session_id"] = claude_response.session_id

            # Track directory changes


            _update_working_directory_from_claude_response(
                claude_response, context, self.settings, user_id
            )

            # Store interaction
            storage = context.bot_data.get("storage")
            if storage:
                try:
                    await storage.save_claude_interaction(
                        user_id=user_id,
                        session_id=claude_response.session_id,
                        prompt=message_text,
                        response=claude_response,
                        ip_address=None,
                    )
                except Exception as e:
                    logger.warning("Failed to log interaction", error=str(e))

            # Drain any pending batched text BEFORE checking the flag.
            # The 1.5s batch window means text can be enqueued but not yet
            # flushed when the turn completes — checking the flag first
            # would miss it and cause the final response to duplicate.
            await flush_stream_callback(on_stream)

            text_already_sent = (
                on_stream
                and hasattr(on_stream, "text_was_sent")
                and on_stream.text_was_sent[0]
            )

            from .utils.formatting import FormattedMessage, ResponseFormatter

            if text_already_sent:
                formatted_messages: List[FormattedMessage] = []
            else:
                formatter = ResponseFormatter(self.settings)
                formatted_messages = formatter.format_claude_response(
                    claude_response.content
                )

            # Show [Interrupted] if the response was stopped
            if claude_response.is_interrupted:
                formatted_messages.append(
                    FormattedMessage("[Interrupted]", parse_mode=None)
                )

            # Warn if the turn ended for a non-normal reason
            stop_notice = self._abnormal_stop_notice(claude_response)
            if stop_notice:
                formatted_messages.append(stop_notice)
                logger.warning(
                    "turn.abnormal_stop",
                    state_key=self._state_key(update),
                    stop_reason=claude_response.stop_reason,
                    num_turns=claude_response.num_turns,
                )

            # Append context window warning if threshold crossed
            ctx_warn = self._context_warning(claude_response, context.user_data)
            if ctx_warn:
                if formatted_messages:
                    formatted_messages[-1].text += ctx_warn
                else:
                    # No messages to append to — send as standalone
                    formatted_messages.append(
                        FormattedMessage(ctx_warn, parse_mode="HTML")
                    )

        except asyncio.CancelledError:
            success = False
            logger.info("Claude request cancelled", user_id=user_id)
            from .utils.formatting import FormattedMessage

            formatted_messages = [FormattedMessage("Stopped.", parse_mode=None)]
        except Exception as e:
            success = False
            logger.error("Claude integration failed", error=str(e), user_id=user_id)

            from .utils.formatting import FormattedMessage

            formatted_messages = [
                FormattedMessage(_format_error_message(e), parse_mode="HTML")
            ]
        finally:
            heartbeat.cancel()
            if draft_streamer:
                try:
                    await draft_streamer.flush()
                except Exception:
                    logger.debug("Draft flush failed in finally block", user_id=user_id)
            # Flush any pending batched intermediate text before final response.
            # This runs in the finally block so buffered text is delivered even
            # on error paths (previously only flushed on success).
            try:
                await flush_stream_callback(on_stream)
            except Exception:
                logger.debug("Stream callback flush failed in finally block", user_id=user_id)

        try:
            await progress_msg.delete()
        except Exception:
            logger.debug("Failed to delete progress message, ignoring")

        # Use MCP-collected images (from send_image_to_user tool calls)
        images: List[ImageAttachment] = mcp_images

        # Try to combine text + images in one message when possible
        caption_sent = False
        if images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                try:
                    caption_sent = await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                        caption=msg.text,
                        caption_parse_mode=msg.parse_mode,
                    )
                except Exception as img_err:
                    logger.warning("Image+caption send failed", error=str(img_err))

        # Send text messages (skip if caption was already embedded in photos)
        if not caption_sent:
            for i, message in enumerate(formatted_messages):
                if not message.text or not message.text.strip():
                    continue
                try:
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=None,  # No keyboards in agentic mode
                    )
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)
                except Exception as send_err:
                    logger.warning(
                        "Failed to send HTML response, retrying as plain text",
                        error=str(send_err),
                        message_index=i,
                    )
                    try:
                        await update.message.reply_text(
                            message.text,
                            reply_markup=None,
                        )
                    except Exception as plain_err:
                        await update.message.reply_text(
                            f"Failed to deliver response "
                            f"(Telegram error: {str(plain_err)[:150]}). "
                            f"Please try again.",
                        )

            # Send images separately if caption wasn't used
            if images:
                try:
                    await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as img_err:
                    logger.warning("Image send failed", error=str(img_err))

        # Delete ephemeral thinking messages now that the final response is sent
        await cleanup_thinking_messages(on_stream)

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[message_text[:100]],
                success=success,
            )

    async def agentic_document(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
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
        await self._handle_agentic_media_message(
            update=update,
            context=context,
            prompt=prompt,
            progress_msg=progress_msg,
            user_id=user_id,
            chat=chat,
        )

    async def agentic_photo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process photo -> Claude, minimal chrome."""
        user_id = update.effective_user.id

        from .media.image_handler import ImageHandler

        image_handler = ImageHandler(config=self.settings)

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        try:
            photo = update.message.photo[-1]
            processed_image = await image_handler.process_image(
                photo, update.message.caption
            )
            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=processed_image.prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
            )

        except Exception as e:


            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "Claude photo processing failed", error=str(e), user_id=user_id
            )

    async def agentic_voice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Transcribe voice message -> Claude, minimal chrome."""
        user_id = update.effective_user.id

        from .media.voice_handler import VoiceHandler

        voice_key_available = (
            self.settings.voice_provider == "openai"
            and self.settings.openai_api_key
        ) or (
            self.settings.voice_provider == "mistral"
            and self.settings.mistral_api_key
        )
        if not (self.settings.enable_voice_messages and voice_key_available):
            await update.message.reply_text(self._voice_unavailable_message())
            return

        voice_handler = VoiceHandler(config=self.settings)

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
            await self._handle_agentic_media_message(
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

    async def _handle_agentic_media_message(
        self,
        *,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        prompt: str,
        progress_msg: Any,
        user_id: int,
        chat: Any,
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

        state_key = self._state_key(update)
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        force_new = bool(context.user_data.get("force_new_session"))

        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        mcp_images_media: List[ImageAttachment] = []
        on_stream = make_stream_callback(self.settings,
            verbose_level,
            progress_msg,
            tool_log,
            time.time(),
            mcp_images=mcp_images_media,
            approved_directory=self.settings.approved_directory,
            telegram_update=update,
        )

        heartbeat = self._start_typing_heartbeat(chat)
        try:
            claude_response = await persistent_manager.send_message(
                state_key=state_key,
                prompt=prompt,
                working_directory=current_dir,
                stream_callback=on_stream,
                model=context.user_data.get("claude_model"),
                force_new=force_new,
            )
            # None means the message was injected into a busy turn
            if claude_response is None:
                heartbeat.cancel()
                try:
                    await progress_msg.delete()
                except Exception:
                    pass
                return
        except asyncio.CancelledError:
            logger.info("Claude media request cancelled", user_id=user_id)
            try:
                await progress_msg.delete()
            except Exception:
                pass
            await update.message.reply_text("Stopped.")
            return
        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "Claude media processing failed", error=str(e), user_id=user_id
            )
            return
        finally:
            heartbeat.cancel()

        if force_new:
            context.user_data["force_new_session"] = False

        context.user_data["claude_session_id"] = claude_response.session_id

        _update_working_directory_from_claude_response(
            claude_response, context, self.settings, user_id
        )

        from .utils.formatting import FormattedMessage, ResponseFormatter

        # Drain any pending batched text BEFORE checking the flag.
        await flush_stream_callback(on_stream)

        text_already_sent = (
            on_stream
            and hasattr(on_stream, "text_was_sent")
            and on_stream.text_was_sent[0]
        )

        if text_already_sent:
            formatted_messages: List[FormattedMessage] = []
        else:
            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(claude_response.content)

        # Warn if the turn ended for a non-normal reason
        stop_notice = self._abnormal_stop_notice(claude_response)
        if stop_notice:
            formatted_messages.append(stop_notice)

        # Append context window warning if threshold crossed
        ctx_warn = self._context_warning(claude_response, context.user_data)
        if ctx_warn:
            if formatted_messages:
                formatted_messages[-1].text += ctx_warn
            else:
                formatted_messages.append(
                    FormattedMessage(ctx_warn, parse_mode="HTML")
                )

        try:
            await progress_msg.delete()
        except Exception:
            logger.debug("Failed to delete progress message, ignoring")

        # Use MCP-collected images (from send_image_to_user tool calls).
        images: List[ImageAttachment] = mcp_images_media

        caption_sent = False
        if images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                try:
                    caption_sent = await self._send_images(
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
                await update.message.reply_text(
                    message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=None,
                )
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

            if images:
                try:
                    await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as img_err:
                    logger.warning("Image send failed", error=str(img_err))

        # Delete ephemeral thinking messages now that the final response is sent
        await cleanup_thinking_messages(on_stream)

    def _voice_unavailable_message(self) -> str:
        """Return provider-aware guidance when voice feature is unavailable."""
        api_key_env = self.settings.voice_provider_api_key_env
        if api_key_env:
            return (
                "Voice processing is not available. "
                f"Set {api_key_env} "
                f"for {self.settings.voice_provider_display_name} and install "
                'voice extras with: pip install "claude-code-telegram[voice]"'
            )
        # Local provider (Parakeet) -- no API key, different install instructions
        return (
            "Voice processing is not available. "
            f"Install {self.settings.voice_provider_display_name} with: "
            'pip install "claude-code-telegram[voice-local]" '
            "and ensure ffmpeg is installed."
        )

    async def agentic_repo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List repos in workspace or switch to one.

        /repo          — list subdirectories with git indicators
        /repo <name>   — switch to that directory, resume session if available
        """
        args = update.message.text.split()[1:] if update.message.text else []
        base = self.settings.approved_directory
        current_dir = context.user_data.get("current_directory", base)

        if args:
            # Switch to named repo
            target_name = args[0]
            target_path = base / target_name
            if not target_path.is_dir():
                await update.message.reply_text(
                    f"Directory not found: <code>{escape_html(target_name)}</code>",
                    parse_mode="HTML",
                )
                return

            context.user_data["current_directory"] = target_path

            # Try to find a resumable session
            claude_integration = context.bot_data.get("claude_integration")
            session_id = None
            if claude_integration:
                existing = await claude_integration._find_resumable_session(
                    update.effective_user.id, target_path
                )
                if existing:
                    session_id = existing.session_id
            context.user_data["claude_session_id"] = session_id

            is_git = (target_path / ".git").is_dir()
            git_badge = " (git)" if is_git else ""
            session_badge = " · session resumed" if session_id else ""

            await update.message.reply_text(
                f"Switched to <code>{escape_html(target_name)}/</code>"
                f"{git_badge}{session_badge}",
                parse_mode="HTML",
            )
            return

        # No args — list repos
        try:
            entries = sorted(
                [
                    d
                    for d in base.iterdir()
                    if d.is_dir() and not d.name.startswith(".")
                ],
                key=lambda d: d.name,
            )
        except OSError as e:
            await update.message.reply_text(f"Error reading workspace: {e}")
            return

        if not entries:
            await update.message.reply_text(
                f"No repos in <code>{escape_html(str(base))}</code>.\n"
                'Clone one by telling me, e.g. <i>"clone org/repo"</i>.',
                parse_mode="HTML",
            )
            return

        lines: List[str] = []
        keyboard_rows: List[list] = []  # type: ignore[type-arg]
        current_name = current_dir.name if current_dir != base else None

        for d in entries:
            is_git = (d / ".git").is_dir()
            icon = "\U0001f4e6" if is_git else "\U0001f4c1"
            marker = " \u25c0" if d.name == current_name else ""
            lines.append(f"{icon} <code>{escape_html(d.name)}/</code>{marker}")

        # Build inline keyboard (2 per row)
        for i in range(0, len(entries), 2):
            row = []
            for j in range(2):
                if i + j < len(entries):
                    name = entries[i + j].name
                    row.append(InlineKeyboardButton(name, callback_data=f"cd:{name}"))
            keyboard_rows.append(row)

        reply_markup = InlineKeyboardMarkup(keyboard_rows)

        await update.message.reply_text(
            "<b>Repos</b>\n\n" + "\n".join(lines),
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    async def _agentic_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle cd: callbacks — switch directory and resume session if available."""
        query = update.callback_query
        await query.answer()

        data = query.data
        _, project_name = data.split(":", 1)

        base = self.settings.approved_directory
        new_path = base / project_name

        if not new_path.is_dir():
            await query.edit_message_text(
                f"Directory not found: <code>{escape_html(project_name)}</code>",
                parse_mode="HTML",
            )
            return

        context.user_data["current_directory"] = new_path

        # Look for a resumable session instead of always clearing
        claude_integration = context.bot_data.get("claude_integration")
        session_id = None
        if claude_integration:
            existing = await claude_integration._find_resumable_session(
                query.from_user.id, new_path
            )
            if existing:
                session_id = existing.session_id
        context.user_data["claude_session_id"] = session_id

        is_git = (new_path / ".git").is_dir()
        git_badge = " (git)" if is_git else ""
        session_badge = " · session resumed" if session_id else ""

        await query.edit_message_text(
            f"Switched to <code>{escape_html(project_name)}/</code>"
            f"{git_badge}{session_badge}",
            parse_mode="HTML",
        )

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=query.from_user.id,
                command="cd",
                args=[project_name],
                success=True,
            )
