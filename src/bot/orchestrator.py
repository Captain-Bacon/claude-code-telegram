"""Message orchestrator — routing, commands, and session state.

Registers all Telegram handlers, manages message queuing (busy path),
and contains agentic_text (the main conversational path). Response
delivery lives in delivery.py; media handlers (document/photo/voice)
live in media_handlers.py. Commands remain here because they access
orchestrator instance state (_message_queues, _state_key).
"""

import asyncio
import os
import signal
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
from ..projects import (
    PrivateTopicsUnavailableError,
    build_registry,
    discover_active_repos,
    load_pinned_projects,
)
from ..security.audit import AuditLogger
from .delivery import deliver_turn_result, make_stall_callback
from .media_handlers import (
    _get_verbose_level,
    agentic_document,
    agentic_photo,
    agentic_voice,
)
from .stream_handler import (
    flush_stream_callback,
    make_stream_callback,
)
from .utils.draft_streamer import DraftStreamer, generate_draft_id
from .utils.error_format import (
    _format_error_message,
    _update_working_directory_from_claude_response,
)
from .utils.heartbeat_pin import HeartbeatPin
from .utils.html_format import escape_html
from .utils.image_extractor import ImageAttachment

logger = structlog.get_logger()


@dataclass
class QueuedMessage:
    """A message queued while Claude was busy."""

    text: str
    sent_at: float  # time.time() when user sent it
    placeholder_message_id: Optional[int] = None  # Telegram msg id of the placeholder
    images: Optional[List[Dict[str, Any]]] = None  # media content blocks


def _is_private_chat(update: Update) -> bool:
    """Return True when update is from a private chat."""
    chat = update.effective_chat
    return bool(chat and getattr(chat, "type", "") == "private")


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /restart command - gracefully restart the bot process.

    Sets a restart flag then triggers SIGTERM. main.py performs ordered
    shutdown, then os.execv() replaces the process with a fresh instance.
    Works regardless of how the bot was launched (Toolbox, tmux, direct).
    """
    assert update.message is not None
    assert update.effective_user is not None
    assert update.effective_chat is not None

    audit_logger: AuditLogger = context.bot_data.get("audit_logger")  # type: ignore[assignment]
    user_id = update.effective_user.id

    await update.message.reply_text(
        "🔄 <b>Restarting bot…</b>\n\n"
        "Current Claude session will end. "
        "I'll send a confirmation once I'm back online.",
        parse_mode="HTML",
    )

    if audit_logger:
        await audit_logger.log_command(user_id, "restart", [], True)

    logger.info("Restart requested via /restart command", user_id=user_id)

    # Flag tells main.py to os.execv() after graceful shutdown instead of exiting.
    os.environ["_RESTART_REQUESTED"] = "1"

    # Tell the new process where to send the "back online" confirmation.
    chat_id = update.effective_chat.id
    os.environ["_RESTART_CHAT_ID"] = str(chat_id)
    thread_id = getattr(update.message, "message_thread_id", None)
    if thread_id is not None:
        os.environ["_RESTART_THREAD_ID"] = str(thread_id)
    else:
        os.environ.pop("_RESTART_THREAD_ID", None)

    # SIGTERM triggers the existing graceful-shutdown handler in main.py.
    os.kill(os.getpid(), signal.SIGTERM)


async def sync_topics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Synchronize project topics — refreshes registry from disk then reconciles."""
    assert update.message is not None
    assert update.effective_user is not None
    assert update.effective_chat is not None

    settings: Settings = context.bot_data["settings"]
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")  # type: ignore[assignment]
    user_id = update.effective_user.id

    if not settings.enable_project_threads:
        await update.message.reply_text(
            "ℹ\ufe0f <b>Project thread mode is disabled.</b>", parse_mode="HTML"
        )
        return

    manager = context.bot_data.get("project_threads_manager")
    if not manager:
        await update.message.reply_text(
            "\u274c <b>Project thread manager not initialized.</b>", parse_mode="HTML"
        )
        return

    status_msg = await update.message.reply_text(
        "\U0001f504 <b>Syncing project topics...</b>", parse_mode="HTML"
    )

    if settings.project_threads_mode == "private":
        if not _is_private_chat(update):
            await status_msg.edit_text(
                "\u274c <b>Private Thread Mode</b>\n\n"
                "Run <code>/sync_topics</code> in your private chat with the bot.",
                parse_mode="HTML",
            )
            return
        target_chat_id = update.effective_chat.id
    else:
        if settings.project_threads_chat_id is None:
            await status_msg.edit_text(
                "\u274c <b>Group Thread Mode Misconfigured</b>\n\n"
                "Set <code>PROJECT_THREADS_CHAT_ID</code> first.",
                parse_mode="HTML",
            )
            return
        if (
            not update.effective_chat
            or update.effective_chat.id != settings.project_threads_chat_id
        ):
            await status_msg.edit_text(
                "\u274c <b>Group Thread Mode</b>\n\n"
                "Run <code>/sync_topics</code> in the configured project threads group.",
                parse_mode="HTML",
            )
            return
        target_chat_id = settings.project_threads_chat_id

    try:
        # Refresh registry: pinned from YAML + discovery from filesystem
        pinned = []
        if settings.projects_config_path:
            pinned = load_pinned_projects(
                config_path=settings.projects_config_path,
                approved_directory=settings.approved_directory,
            )

        discovered = []
        if settings.project_threads_discover:
            pinned_slugs = {p.slug for p in pinned}
            discovered = discover_active_repos(
                base_directory=settings.approved_directory,
                max_results=settings.project_threads_discover_limit,
                max_days=settings.project_threads_discover_days,
                exclude_slugs=pinned_slugs,
            )

        all_projects = pinned + discovered
        if not all_projects:
            await status_msg.edit_text(
                "\u274c <b>No projects found</b>\n\n"
                "Check projects config or ensure git repos exist "
                "in the approved directory.",
                parse_mode="HTML",
            )
            if audit_logger:
                await audit_logger.log_command(user_id, "sync_topics", [], False)
            return

        registry = build_registry(pinned=pinned, discovered=discovered)
        manager.registry = registry
        context.bot_data["project_registry"] = registry

        result = await manager.sync_topics(context.bot, chat_id=target_chat_id)
        await status_msg.edit_text(
            "\u2705 <b>Project topic sync complete</b>\n\n"
            f"\u2022 Created: <b>{result.created}</b>\n"
            f"\u2022 Reused: <b>{result.reused}</b>\n"
            f"\u2022 Renamed: <b>{result.renamed}</b>\n"
            f"\u2022 Reopened: <b>{result.reopened}</b>\n"
            f"\u2022 Closed: <b>{result.closed}</b>\n"
            f"\u2022 Deactivated: <b>{result.deactivated}</b>\n"
            f"\u2022 Failed: <b>{result.failed}</b>",
            parse_mode="HTML",
        )
        if audit_logger:
            await audit_logger.log_command(user_id, "sync_topics", [], True)
    except PrivateTopicsUnavailableError:
        await status_msg.edit_text(
            manager.private_topics_unavailable_message(),
            parse_mode="HTML",
        )
        if audit_logger:
            await audit_logger.log_command(user_id, "sync_topics", [], False)
    except Exception as e:
        await status_msg.edit_text(
            f"\u274c <b>Project topic sync failed</b>\n\n{escape_html(str(e))}",
            parse_mode="HTML",
        )
        if audit_logger:
            await audit_logger.log_command(user_id, "sync_topics", [], False)


class MessageOrchestrator:
    """Routes messages based on mode. Single entry point for all Telegram updates."""

    def __init__(self, settings: Settings, deps: Dict[str, Any]):
        self.settings = settings
        self.deps = deps
        # Message queue per state_key — messages received while Claude is busy
        self._message_queues: Dict[str, List[QueuedMessage]] = {}

    @staticmethod
    def _state_key(update: Update) -> str:
        """Derive a persistent client state key from a Telegram update."""
        assert update.effective_chat is not None
        assert update.message is not None
        assert update.effective_user is not None

        chat_id = update.effective_chat.id
        thread_id = getattr(update.message, "message_thread_id", None)
        user_id = update.effective_user.id
        return derive_state_key(chat_id, thread_id, user_id)

    def _inject_deps(self, handler: Callable) -> Callable:  # type: ignore[type-arg]
        """Wrap handler to inject dependencies into context.bot_data."""

        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            for key, value in self.deps.items():
                context.bot_data[key] = value  # type: ignore[index]
            context.bot_data["settings"] = self.settings  # type: ignore[index]
            context.user_data.pop("_thread_context", None)  # type: ignore[union-attr]

            is_sync_bypass = handler.__name__ == "sync_topics"
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
        mapping = await manager.get_mapping(chat.id, message_thread_id)

        # Determine project root and metadata
        if project:
            # Mapped to a known, enabled project
            project_root = project.absolute_path
            project_slug = project.slug
            project_name = project.name
        elif mapping:
            # Mapping exists but no active project (scratchpad or stale)
            project_root = self.settings.approved_directory
            project_slug = mapping.project_slug
            project_name = mapping.topic_name
        else:
            # Unmapped topic — auto-adopt as scratchpad so /repo can aim it
            topic_name = "Untitled"
            if update.effective_message:
                # Try to get the topic name from the message
                topic = getattr(update.effective_message, "forum_topic_created", None)
                if topic and hasattr(topic, "name"):
                    topic_name = topic.name
            await manager.adopt_topic(
                chat_id=chat.id,
                message_thread_id=message_thread_id,
                topic_name=topic_name,
            )
            project_root = self.settings.approved_directory
            project_slug = None
            project_name = topic_name

        state_key = f"{chat.id}:{message_thread_id}"
        thread_states = context.user_data.setdefault("thread_state", {})  # type: ignore[union-attr]
        state = thread_states.get(state_key, {})

        current_dir_raw = state.get("current_directory")
        current_dir = (
            Path(current_dir_raw).resolve() if current_dir_raw else project_root
        )
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        context.user_data["current_directory"] = current_dir  # type: ignore[index]
        context.user_data["claude_session_id"] = state.get("claude_session_id")  # type: ignore[index]
        context.user_data["_thread_context"] = {  # type: ignore[index]
            "chat_id": chat.id,
            "message_thread_id": message_thread_id,
            "state_key": state_key,
            "project_slug": project_slug,
            "project_root": str(project_root),
            "project_name": project_name,
        }
        return True

    def _persist_thread_state(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Persist compatibility keys back into per-thread state."""
        thread_context = context.user_data.get("_thread_context")  # type: ignore[union-attr]
        if not thread_context:
            return

        project_root = Path(thread_context["project_root"])
        current_dir = context.user_data.get("current_directory", project_root)  # type: ignore[union-attr]
        if not isinstance(current_dir, Path):
            current_dir = Path(str(current_dir))
        current_dir = current_dir.resolve()
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        thread_states = context.user_data.setdefault("thread_state", {})  # type: ignore[union-attr]
        thread_states[thread_context["state_key"]] = {
            "current_directory": str(current_dir),
            "claude_session_id": context.user_data.get("claude_session_id"),  # type: ignore[union-attr]
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
                await query.message.reply_text(message, parse_mode="HTML")  # type: ignore[attr-defined]
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
            ("speak", self.agentic_speak),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_topics", sync_topics))

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

        # Media handlers live in media_handlers.py as standalone functions
        # that take (settings, update, context, on_busy). These wrappers
        # bind settings and create the on_busy callback so _inject_deps
        # can call them as (update, context). Follow this pattern when
        # adding a new media handler.
        async def _document_handler(
            update: Update, context: ContextTypes.DEFAULT_TYPE
        ) -> None:
            on_busy = self._make_media_busy_callback(update)
            await agentic_document(self.settings, update, context, on_busy=on_busy)

        app.add_handler(
            MessageHandler(filters.Document.ALL, self._inject_deps(_document_handler)),
            group=10,
        )

        # Photo uploads -> Claude
        async def _photo_handler(
            update: Update, context: ContextTypes.DEFAULT_TYPE
        ) -> None:
            on_busy = self._make_media_busy_callback(update)
            await agentic_photo(self.settings, update, context, on_busy=on_busy)

        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(_photo_handler)),
            group=10,
        )

        # Voice messages -> transcribe -> Claude
        async def _voice_handler(
            update: Update, context: ContextTypes.DEFAULT_TYPE
        ) -> None:
            on_busy = self._make_media_busy_callback(update)
            await agentic_voice(self.settings, update, context, on_busy=on_busy)

        app.add_handler(
            MessageHandler(filters.VOICE, self._inject_deps(_voice_handler)),
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
            BotCommand("speak", "Read last response aloud"),
        ]
        if self.settings.enable_project_threads:
            commands.append(BotCommand("sync_topics", "Sync project topics"))
        return commands

    # --- Agentic handlers ---

    async def agentic_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Brief welcome, no buttons."""
        assert update.message is not None
        assert update.effective_user is not None

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
            manager = context.bot_data.get("project_threads_manager")  # type: ignore[union-attr]
            if manager:
                try:
                    result = await manager.sync_topics(
                        context.bot,  # type: ignore[union-attr]
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
                    sync_line = "\n\n🧵 Topic sync failed. Run /sync_topics to retry."
        current_dir = context.user_data.get(  # type: ignore[union-attr]
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
        assert update.message is not None

        persistent_manager: Optional[PersistentClientManager] = context.bot_data.get(  # type: ignore[union-attr]
            "persistent_manager"
        )
        if persistent_manager:
            state_key = self._state_key(update)
            await persistent_manager.disconnect_client(state_key)

        context.user_data["claude_session_id"] = None  # type: ignore[index]
        context.user_data["session_started"] = True  # type: ignore[index]
        context.user_data["force_new_session"] = True  # type: ignore[index]
        context.user_data.pop("_context_last_warned", None)  # type: ignore[union-attr]

        await update.message.reply_text("Session reset. What's next?")

    async def agentic_model(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Switch Claude model at runtime."""
        assert update.message is not None

        aliases = {
            "opus": "claude-opus-4-6",
            "sonnet": "claude-sonnet-4-6",
            "haiku": "claude-haiku-4-5-20251001",
        }
        args = update.message.text.split()[1:] if update.message.text else []
        if not args:
            current = context.user_data.get("claude_model")  # type: ignore[union-attr]
            display = current or "default (CLI decides)"
            await update.message.reply_text(
                f"Model: <b>{escape_html(display)}</b>\n\n"
                "Usage: <code>/model opus|sonnet|haiku|default</code>",
                parse_mode="HTML",
            )
            return
        choice = args[0].lower()
        if choice == "default":
            context.user_data.pop("claude_model", None)  # type: ignore[union-attr]
            await update.message.reply_text(
                "Model reset to <b>default</b> (CLI decides)",
                parse_mode="HTML",
            )
            return
        model_id = aliases.get(choice, choice)
        context.user_data["claude_model"] = model_id  # type: ignore[index]
        await update.message.reply_text(
            f"Model set to <b>{escape_html(model_id)}</b>",
            parse_mode="HTML",
        )

    async def agentic_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Compact one-line status, no buttons."""
        assert update.message is not None
        assert update.effective_user is not None

        from src import get_build_info

        current_dir = context.user_data.get(  # type: ignore[union-attr]
            "current_directory", self.settings.approved_directory
        )
        dir_display = str(current_dir)

        session_id = context.user_data.get("claude_session_id")  # type: ignore[union-attr]
        session_status = "active" if session_id else "none"

        # Cost info
        cost_str = ""
        rate_limiter = context.bot_data.get("rate_limiter")  # type: ignore[union-attr]
        if rate_limiter:
            try:
                user_status = rate_limiter.get_user_status(update.effective_user.id)
                cost_usage = user_status.get("cost_usage", {})
                current_cost = cost_usage.get("current", 0.0)
                cost_str = f" · Cost: ${current_cost:.2f}"
            except Exception:
                pass

        model_str = ""
        current_model = context.user_data.get("claude_model")  # type: ignore[union-attr]
        if current_model:
            model_str = f" · Model: {current_model}"

        build = get_build_info()

        await update.message.reply_text(
            f"📂 {dir_display} · Session: {session_status}{cost_str}{model_str}\n"
            f"Build: <code>{escape_html(build)}</code>",
            parse_mode="HTML",
        )

    async def agentic_verbose(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Set output verbosity: /verbose [0|1|2]."""
        assert update.message is not None

        args = update.message.text.split()[1:] if update.message.text else []
        if not args:
            current = _get_verbose_level(self.settings, context)
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

        context.user_data["verbose_level"] = level  # type: ignore[index]
        labels = {0: "quiet", 1: "normal", 2: "detailed"}
        await update.message.reply_text(
            f"Verbosity set to <b>{level}</b> ({labels[level]})",
            parse_mode="HTML",
        )

    async def agentic_stop(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Stop the active Claude work for this thread."""
        assert update.message is not None
        assert update.effective_user is not None

        persistent_manager: Optional[PersistentClientManager] = context.bot_data.get(  # type: ignore[union-attr]
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

    async def agentic_speak(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Convert the last bot response to speech and send as voice message."""
        assert update.message is not None

        from ..config.features import FeatureFlags

        if not FeatureFlags(self.settings).tts_enabled:
            await update.message.reply_text(
                "Text-to-speech is not available. "
                "Set ENABLE_TTS=true and install mlx-audio: "
                'pip install "claude-code-telegram[tts]"'
            )
            return

        last_response = context.user_data.get("_last_bot_response")
        if not last_response:
            await update.message.reply_text(
                "No recent response to speak. Send a message first, then /speak."
            )
            return

        from .media.text_adapter import adapt_for_speech
        from .media.tts_handler import TTSHandler

        chat = update.message.chat
        await chat.send_action("record_voice")
        progress_msg = await update.message.reply_text("Generating speech...")

        try:
            adapted_text = adapt_for_speech(
                last_response,
                max_length=self.settings.tts_max_text_length,
            )

            if not adapted_text.strip():
                await progress_msg.edit_text(
                    "Nothing to speak \u2014 the response was all code/tables."
                )
                return

            tts = TTSHandler(config=self.settings)
            result = await tts.synthesise(adapted_text)

            await progress_msg.edit_text("Sending voice message...")

            await chat.send_voice(
                voice=result.audio_bytes,
                duration=int(result.duration_seconds),
                reply_to_message_id=update.message.message_id,
            )

            await progress_msg.delete()

        except Exception as e:
            logger.error(
                "TTS synthesis failed",
                error=str(e),
                user_id=update.effective_user.id,
            )
            await progress_msg.edit_text(
                f"Speech generation failed: {escape_html(str(e)[:200])}",
                parse_mode="HTML",
            )

    # ------------------------------------------------------------------
    # Message queuing (ceq) — queue messages while Claude is busy
    # ------------------------------------------------------------------

    def _make_media_busy_callback(
        self, update: Update
    ) -> "Callable[[str, str, Optional[List[Dict[str, Any]]], Optional[int]], Any]":
        """Create a callback for media handlers to queue when Claude is busy.

        Returns an async callable: (state_key, prompt, images, placeholder_id) -> None
        """

        async def _on_busy(
            state_key: str,
            prompt: str,
            images: Optional[List[Dict[str, Any]]] = None,
            placeholder_message_id: Optional[int] = None,
        ) -> None:
            await self._enqueue_message(
                state_key=state_key,
                message_text=prompt,
                update=update,
                images=images,
                existing_placeholder_id=placeholder_message_id,
            )

        return _on_busy

    async def _enqueue_message(
        self,
        state_key: str,
        message_text: str,
        update: Update,
        images: Optional[List[Dict[str, Any]]] = None,
        existing_placeholder_id: Optional[int] = None,
    ) -> None:
        """Queue a message for delivery after the current Claude turn finishes."""
        assert update.message is not None

        if existing_placeholder_id:
            placeholder_id = existing_placeholder_id
        else:
            placeholder = await update.message.reply_text(
                "\U0001f554 Queued \u2014 Claude will see this when the current task finishes"
            )
            placeholder_id = placeholder.message_id
        qm = QueuedMessage(
            text=message_text,
            sent_at=time.time(),
            placeholder_message_id=placeholder_id,
            images=images,
        )
        self._message_queues.setdefault(state_key, []).append(qm)
        logger.info(
            "message.queued",
            state_key=state_key,
            queue_depth=len(self._message_queues[state_key]),
            preview=message_text[:80],
        )

    @staticmethod
    def _combine_queued_messages(queued: List[QueuedMessage]) -> str:
        """Combine queued messages into a single prompt with temporal context."""
        if len(queued) == 1:
            qm = queued[0]
            ts = datetime.fromtimestamp(qm.sent_at, tz=UTC).strftime("%H:%M:%S UTC")
            return (
                f"[This message was queued during your previous turn, "
                f"originally sent at {ts}]\n\n{qm.text}"
            )
        parts = [
            f"[The following {len(queued)} messages were queued "
            f"during your previous turn]\n"
        ]
        for qm in queued:
            ts = datetime.fromtimestamp(qm.sent_at, tz=UTC).strftime("%H:%M:%S UTC")
            parts.append(f"[{ts}] {qm.text}")
        return "\n\n".join(parts)

    async def _drain_queue(
        self,
        state_key: str,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Send any queued messages to Claude as a normal turn.

        Loops until the queue is empty — if the user sends more messages
        while the drain turn is running, they get queued and drained next
        iteration.
        """
        assert update.message is not None
        assert update.effective_chat is not None

        while True:
            queued = self._message_queues.pop(state_key, [])
            if not queued:
                return

            chat = update.effective_chat

            # If any queued message has images, process one at a time
            # to preserve media content. Re-queue the rest for the next
            # iteration. Text-only queues combine as before.
            has_images = any(qm.images for qm in queued)
            if has_images:
                batch = [queued[0]]
                if len(queued) > 1:
                    self._message_queues[state_key] = queued[1:]
            else:
                batch = queued

            # Delete placeholder messages for this batch
            for qm in batch:
                if qm.placeholder_message_id and chat:
                    try:
                        await chat.delete_message(qm.placeholder_message_id)
                    except Exception:
                        pass

            if has_images:
                combined = batch[0].text
                drain_images = batch[0].images
            else:
                combined = self._combine_queued_messages(batch)
                drain_images = None

            logger.info(
                "queue.draining",
                state_key=state_key,
                message_count=len(batch),
                has_images=has_images,
            )

            persistent_manager: Optional[PersistentClientManager] = (
                context.bot_data.get("persistent_manager")  # type: ignore[union-attr]
            )
            if not persistent_manager:
                return

            progress_msg = await update.message.reply_text("Working (queued)...")
            verbose_level = _get_verbose_level(self.settings, context)
            tool_log: List[Dict[str, Any]] = []
            start_time = time.time()
            mcp_images: List[ImageAttachment] = []

            heartbeat_pin = (
                HeartbeatPin(
                    bot=context.bot,  # type: ignore[union-attr]
                    chat_id=chat.id,
                    message_thread_id=(
                        update.message.message_thread_id if update.message else None
                    ),
                )
                if self.settings.enable_heartbeat_pin
                else None
            )

            on_stream = make_stream_callback(
                self.settings,
                verbose_level,
                progress_msg,
                tool_log,
                start_time,
                mcp_images=mcp_images,
                approved_directory=self.settings.approved_directory,
                draft_streamer=None,
                telegram_update=update,
                heartbeat_pin=heartbeat_pin,
            )

            on_stall = make_stall_callback(progress_msg)

            error_messages = None
            claude_response = None
            success = True
            try:
                current_dir = context.user_data.get(  # type: ignore[union-attr]
                    "current_directory", self.settings.approved_directory
                )
                claude_response = await persistent_manager.send_message(
                    state_key=state_key,
                    prompt=combined,
                    working_directory=current_dir,
                    stream_callback=on_stream,
                    stall_callback=on_stall,
                    model=context.user_data.get("claude_model"),  # type: ignore[union-attr]
                    images=drain_images,
                )

                if claude_response is None:
                    re_qm = QueuedMessage(
                        text=combined, sent_at=time.time(), images=drain_images
                    )
                    self._message_queues.setdefault(state_key, []).append(re_qm)
                    logger.warning("queue.drain_raced", state_key=state_key)
                    try:
                        await progress_msg.delete()
                    except Exception:
                        pass
                    return

            except asyncio.CancelledError:
                success = False
                from .utils.formatting import FormattedMessage

                error_messages = [FormattedMessage("Stopped.", parse_mode="HTML")]
            except Exception as e:
                success = False
                logger.error("Queue drain failed", error=str(e), state_key=state_key)
                from .utils.formatting import FormattedMessage

                error_messages = [
                    FormattedMessage(_format_error_message(e), parse_mode="HTML")
                ]
            finally:
                if heartbeat_pin:
                    try:
                        await heartbeat_pin.cleanup()
                    except Exception:
                        pass
                try:
                    await flush_stream_callback(on_stream)
                except Exception:
                    pass

            await deliver_turn_result(
                settings=self.settings,
                update=update,
                context=context,
                claude_response=claude_response,
                on_stream=on_stream,
                progress_msg=progress_msg,
                start_time=start_time,
                mcp_images=mcp_images,
                success=success,
                error_messages=error_messages,
            )
            # Loop back to check if more messages were queued during this turn

    async def agentic_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Direct Claude passthrough. Simple progress. No suggestions."""
        assert update.message is not None
        assert update.effective_user is not None

        user_id = update.effective_user.id
        message_text = update.message.text
        assert message_text is not None

        logger.info(
            "Agentic text message",
            user_id=user_id,
            message_length=len(message_text),
        )

        # Rate limit check
        rate_limiter = context.bot_data.get("rate_limiter")  # type: ignore[union-attr]
        if rate_limiter:
            allowed, limit_message = await rate_limiter.check_rate_limit(user_id, 0.001)
            if not allowed:
                await update.message.reply_text(f"⏱️ {limit_message}")
                return

        chat = update.message.chat
        await chat.send_action("typing")

        persistent_manager: Optional[PersistentClientManager] = context.bot_data.get(  # type: ignore[union-attr]
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

        verbose_level = _get_verbose_level(self.settings, context)
        progress_msg = await update.message.reply_text("Working...")

        current_dir = context.user_data.get(  # type: ignore[union-attr]
            "current_directory", self.settings.approved_directory
        )

        # Check if /new was used — skip auto-resume for this first message.
        force_new = bool(context.user_data.get("force_new_session"))  # type: ignore[union-attr]

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

        # Pinned heartbeat showing live tool activity
        heartbeat_pin = (
            HeartbeatPin(
                bot=context.bot,
                chat_id=chat.id,
                message_thread_id=update.message.message_thread_id,
            )
            if self.settings.enable_heartbeat_pin
            else None
        )

        on_stream = make_stream_callback(
            self.settings,
            verbose_level,
            progress_msg,
            tool_log,
            start_time,
            mcp_images=mcp_images,
            approved_directory=self.settings.approved_directory,
            draft_streamer=draft_streamer,
            telegram_update=update,
            heartbeat_pin=heartbeat_pin,
        )

        on_stall = make_stall_callback(progress_msg)

        error_messages = None
        claude_response = None
        success = True
        try:
            claude_response = await persistent_manager.send_message(
                state_key=state_key,
                prompt=message_text,
                working_directory=current_dir,
                stream_callback=on_stream,
                stall_callback=on_stall,
                model=context.user_data.get("claude_model"),  # type: ignore[union-attr]
                force_new=force_new,
            )

            # None means race: another handler claimed the turn
            if claude_response is None:
                if heartbeat_pin:
                    try:
                        await heartbeat_pin.cleanup()
                    except Exception:
                        pass
                try:
                    await update.message.set_reaction("\U0001f440")  # 👀
                except Exception:
                    pass
                try:
                    await progress_msg.delete()
                except Exception:
                    pass
                return

            if force_new:
                context.user_data["force_new_session"] = False  # type: ignore[index]

            context.user_data["claude_session_id"] = claude_response.session_id  # type: ignore[index]

            _update_working_directory_from_claude_response(
                claude_response, context, self.settings, user_id
            )

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

        except asyncio.CancelledError:
            success = False
            logger.info("Claude request cancelled", user_id=user_id)
            from .utils.formatting import FormattedMessage

            error_messages = [FormattedMessage("Stopped.", parse_mode="HTML")]
        except Exception as e:
            success = False
            logger.error("Claude integration failed", error=str(e), user_id=user_id)
            from .utils.formatting import FormattedMessage

            error_messages = [
                FormattedMessage(_format_error_message(e), parse_mode="HTML")
            ]
        finally:
            if draft_streamer:
                try:
                    await draft_streamer.flush()
                except Exception:
                    pass
            if heartbeat_pin:
                try:
                    await heartbeat_pin.cleanup()
                except Exception:
                    pass
            try:
                await flush_stream_callback(on_stream)
            except Exception:
                pass

        await deliver_turn_result(
            settings=self.settings,
            update=update,
            context=context,
            claude_response=claude_response,
            on_stream=on_stream,
            progress_msg=progress_msg,
            start_time=start_time,
            mcp_images=mcp_images,
            success=success,
            error_messages=error_messages,
        )

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[message_text[:100]],
                success=success,
            )

        # Drain any messages that were queued while this turn was running.
        await self._drain_queue(state_key, update, context)

    async def agentic_repo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List repos in workspace or switch to one.

        /repo          — list subdirectories with git indicators
        /repo <name>   — switch to that directory, resume session if available
        """
        assert update.message is not None

        args = update.message.text.split()[1:] if update.message.text else []
        base = self.settings.approved_directory
        current_dir = context.user_data.get("current_directory", base)  # type: ignore[union-attr]

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

            context.user_data["current_directory"] = target_path  # type: ignore[index]

            # In thread mode, update the topic mapping to point at this repo
            thread_context = context.user_data.get("_thread_context")  # type: ignore[union-attr]
            if thread_context:
                manager = context.bot_data.get("project_threads_manager")
                if manager:
                    from ..projects.discovery import slugify

                    slug = slugify(target_name)
                    await manager.adopt_topic(
                        chat_id=thread_context["chat_id"],
                        message_thread_id=thread_context["message_thread_id"],
                        topic_name=target_name,
                        project_slug=slug,
                    )
                    # Update thread context for persist
                    thread_context["project_slug"] = slug
                    thread_context["project_root"] = str(target_path)
                    thread_context["project_name"] = target_name

            is_git = (target_path / ".git").is_dir()
            git_badge = " (git)" if is_git else ""

            await update.message.reply_text(
                f"Switched to <code>{escape_html(target_name)}/</code>" f"{git_badge}",
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
        assert update.callback_query is not None

        query = update.callback_query
        await query.answer()

        data = query.data
        assert data is not None

        _, project_name = data.split(":", 1)

        base = self.settings.approved_directory
        new_path = base / project_name

        if not new_path.is_dir():
            await query.edit_message_text(
                f"Directory not found: <code>{escape_html(project_name)}</code>",
                parse_mode="HTML",
            )
            return

        context.user_data["current_directory"] = new_path  # type: ignore[index]

        # In thread mode, update the topic mapping to point at this repo
        thread_context = context.user_data.get("_thread_context")  # type: ignore[union-attr]
        if thread_context:
            manager = context.bot_data.get("project_threads_manager")
            if manager:
                from ..projects.discovery import slugify

                slug = slugify(project_name)
                await manager.adopt_topic(
                    chat_id=thread_context["chat_id"],
                    message_thread_id=thread_context["message_thread_id"],
                    topic_name=project_name,
                    project_slug=slug,
                )
                thread_context["project_slug"] = slug
                thread_context["project_root"] = str(new_path)
                thread_context["project_name"] = project_name

        is_git = (new_path / ".git").is_dir()
        git_badge = " (git)" if is_git else ""

        await query.edit_message_text(
            f"Switched to <code>{escape_html(project_name)}/</code>" f"{git_badge}",
            parse_mode="HTML",
        )

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")  # type: ignore[union-attr]
        if audit_logger:
            await audit_logger.log_command(
                user_id=query.from_user.id,
                command="cd",
                args=[project_name],
                success=True,
            )
