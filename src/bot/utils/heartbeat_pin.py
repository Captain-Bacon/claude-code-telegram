"""Pinned heartbeat message showing live tool activity during a Claude turn.

Sends a short pinned message (e.g. "Read 3") that updates on each tool call,
giving the user a visible indicator of activity without scrolling. The message
is unpinned and deleted when the turn ends.

Runs alongside the existing DraftStreamer — this is a supplement, not a
replacement.
"""

import time
from typing import Optional

import structlog
import telegram

logger = structlog.get_logger()


class HeartbeatPin:
    """Manages a pinned message that shows the current tool call and count."""

    def __init__(
        self,
        bot: telegram.Bot,
        chat_id: int,
        message_thread_id: Optional[int] = None,
        throttle_interval: float = 5.0,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.message_thread_id = message_thread_id
        self.throttle_interval = throttle_interval

        self._message_id: Optional[int] = None
        self._call_count = 0
        self._last_update_time = 0.0
        self._enabled = True
        self._pinned = False
        self._pending_text: Optional[str] = None

    async def tool_called(self, tool_name: str) -> None:
        """Record a tool call and update the pinned message."""
        if not self._enabled:
            return

        self._call_count += 1
        text = f"\u2699\ufe0f {tool_name} {self._call_count}"
        now = time.time()

        if (now - self._last_update_time) >= self.throttle_interval:
            await self._update(text)
        else:
            # Buffer it — flush() will pick it up if the turn ends soon
            self._pending_text = text

    async def flush(self) -> None:
        """Send any pending update that was throttled."""
        if not self._enabled or not self._pending_text:
            return
        await self._update(self._pending_text)
        self._pending_text = None

    async def cleanup(self) -> None:
        """Unpin and delete the heartbeat message.

        Edits to a benign final state first so that if deletion fails
        (no admin rights in groups), the remnant is informative rather
        than a cryptic tool count.
        """
        if not self._message_id:
            return

        # Best-effort edit to final state before deletion
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self._message_id,
                text="\u2705 Done",
            )
        except Exception:
            pass

        if self._pinned:
            try:
                await self.bot.unpin_chat_message(
                    chat_id=self.chat_id,
                    message_id=self._message_id,
                )
            except Exception:
                logger.debug(
                    "Failed to unpin heartbeat",
                    chat_id=self.chat_id,
                    message_id=self._message_id,
                )

        try:
            await self.bot.delete_message(
                chat_id=self.chat_id,
                message_id=self._message_id,
            )
        except Exception:
            logger.debug(
                "Failed to delete heartbeat",
                chat_id=self.chat_id,
                message_id=self._message_id,
            )

        self._message_id = None

    @property
    def has_active_message(self) -> bool:
        """Whether the heartbeat has a live message in Telegram."""
        return self._enabled and self._message_id is not None

    def reset_throttle(self) -> None:
        """Reset the edit timer — call after sending content to avoid redundant edits."""
        self._last_update_time = time.time()

    async def _update(self, text: str) -> None:
        """Create or edit the pinned heartbeat message."""
        if self._message_id is None:
            # First call — send message, then try to pin (pin failure is OK)
            try:
                msg = await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                    disable_notification=True,
                    message_thread_id=self.message_thread_id,
                )
                self._message_id = msg.message_id
            except Exception:
                logger.debug(
                    "Heartbeat send failed, disabling",
                    chat_id=self.chat_id,
                )
                self._enabled = False
                return

            try:
                await self.bot.pin_chat_message(
                    chat_id=self.chat_id,
                    message_id=self._message_id,
                    disable_notification=True,
                )
                self._pinned = True
            except Exception:
                logger.debug(
                    "Pin failed (no admin rights?), continuing unpinned",
                    chat_id=self.chat_id,
                )
        else:
            # Subsequent calls — edit in place
            try:
                await self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self._message_id,
                    text=text,
                )
            except Exception:
                logger.debug(
                    "Heartbeat edit failed, disabling",
                    chat_id=self.chat_id,
                )
                self._enabled = False
                return

        self._last_update_time = time.time()
        self._pending_text = None
