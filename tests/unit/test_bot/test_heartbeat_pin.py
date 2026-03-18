"""Tests for HeartbeatPin.

Covers:
- First tool_called sends message + pins
- Subsequent calls edit in place
- Throttle prevents rapid edits
- Pending text flushed on flush()
- cleanup edits to Done, unpins, deletes
- cleanup handles partial failures gracefully
- Send failure disables heartbeat
- Edit failure disables heartbeat
- Pin failure continues unpinned
- has_active_message property
- reset_throttle resets edit timer
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.utils.heartbeat_pin import HeartbeatPin


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.edit_message_text = AsyncMock()
    bot.pin_chat_message = AsyncMock()
    bot.unpin_chat_message = AsyncMock()
    bot.delete_message = AsyncMock()
    return bot


@pytest.fixture
def pin(mock_bot):
    return HeartbeatPin(
        bot=mock_bot,
        chat_id=123,
        message_thread_id=None,
        throttle_interval=5.0,
    )


# ---------------------------------------------------------------------------
# First tool call — send + pin
# ---------------------------------------------------------------------------


class TestFirstToolCall:
    async def test_sends_message_on_first_call(self, pin, mock_bot):
        mock_bot.send_message.return_value = MagicMock(message_id=42)
        await pin.tool_called("Read")
        mock_bot.send_message.assert_called_once()
        kwargs = mock_bot.send_message.call_args[1]
        assert kwargs["chat_id"] == 123
        assert "Read" in kwargs["text"]
        assert "1" in kwargs["text"]

    async def test_pins_message_after_send(self, pin, mock_bot):
        mock_bot.send_message.return_value = MagicMock(message_id=42)
        await pin.tool_called("Read")
        mock_bot.pin_chat_message.assert_called_once_with(
            chat_id=123,
            message_id=42,
            disable_notification=True,
        )

    async def test_pin_failure_continues_unpinned(self, pin, mock_bot):
        mock_bot.send_message.return_value = MagicMock(message_id=42)
        mock_bot.pin_chat_message.side_effect = Exception("no admin")
        await pin.tool_called("Read")
        # Should still be enabled, just not pinned
        assert pin._enabled
        assert pin._message_id == 42
        assert not pin._pinned

    async def test_send_failure_disables(self, pin, mock_bot):
        mock_bot.send_message.side_effect = Exception("network")
        await pin.tool_called("Read")
        assert not pin._enabled

    async def test_thread_id_passed(self, mock_bot):
        pin = HeartbeatPin(bot=mock_bot, chat_id=123, message_thread_id=999)
        mock_bot.send_message.return_value = MagicMock(message_id=42)
        await pin.tool_called("Read")
        kwargs = mock_bot.send_message.call_args[1]
        assert kwargs["message_thread_id"] == 999


# ---------------------------------------------------------------------------
# Subsequent calls — edit in place
# ---------------------------------------------------------------------------


class TestSubsequentCalls:
    async def test_edits_existing_message(self, pin, mock_bot):
        mock_bot.send_message.return_value = MagicMock(message_id=42)
        await pin.tool_called("Read")
        mock_bot.edit_message_text.reset_mock()
        # Force past throttle
        pin._last_update_time = 0.0
        await pin.tool_called("Grep")
        mock_bot.edit_message_text.assert_called_once()
        kwargs = mock_bot.edit_message_text.call_args[1]
        assert kwargs["message_id"] == 42
        assert "Grep" in kwargs["text"]
        assert "2" in kwargs["text"]

    async def test_edit_failure_disables(self, pin, mock_bot):
        mock_bot.send_message.return_value = MagicMock(message_id=42)
        await pin.tool_called("Read")
        pin._last_update_time = 0.0
        mock_bot.edit_message_text.side_effect = Exception("rate limit")
        await pin.tool_called("Grep")
        assert not pin._enabled


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------


class TestThrottle:
    async def test_throttle_buffers_update(self, pin, mock_bot):
        mock_bot.send_message.return_value = MagicMock(message_id=42)
        await pin.tool_called("Read")  # sends
        mock_bot.edit_message_text.reset_mock()
        # Within throttle window — should buffer
        await pin.tool_called("Grep")
        mock_bot.edit_message_text.assert_not_called()
        assert pin._pending_text is not None
        assert "Grep" in pin._pending_text

    async def test_flush_sends_pending(self, pin, mock_bot):
        mock_bot.send_message.return_value = MagicMock(message_id=42)
        await pin.tool_called("Read")
        await pin.tool_called("Grep")  # buffered
        mock_bot.edit_message_text.reset_mock()
        # Force past throttle for flush
        pin._last_update_time = 0.0
        await pin.flush()
        mock_bot.edit_message_text.assert_called_once()

    async def test_flush_noop_when_nothing_pending(self, pin, mock_bot):
        mock_bot.send_message.return_value = MagicMock(message_id=42)
        await pin.tool_called("Read")
        mock_bot.edit_message_text.reset_mock()
        await pin.flush()
        mock_bot.edit_message_text.assert_not_called()

    async def test_flush_noop_when_disabled(self, pin, mock_bot):
        pin._enabled = False
        pin._pending_text = "something"
        await pin.flush()
        mock_bot.edit_message_text.assert_not_called()


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


class TestCleanup:
    async def test_cleanup_edits_unpins_deletes(self, pin, mock_bot):
        mock_bot.send_message.return_value = MagicMock(message_id=42)
        await pin.tool_called("Read")
        pin._pinned = True
        await pin.cleanup()
        # Should edit to Done
        mock_bot.edit_message_text.assert_called()
        edit_kwargs = mock_bot.edit_message_text.call_args[1]
        assert "Done" in edit_kwargs["text"]
        # Should unpin
        mock_bot.unpin_chat_message.assert_called_once_with(
            chat_id=123, message_id=42
        )
        # Should delete
        mock_bot.delete_message.assert_called_once_with(
            chat_id=123, message_id=42
        )
        assert pin._message_id is None

    async def test_cleanup_skips_unpin_when_not_pinned(self, pin, mock_bot):
        mock_bot.send_message.return_value = MagicMock(message_id=42)
        mock_bot.pin_chat_message.side_effect = Exception("no admin")
        await pin.tool_called("Read")
        await pin.cleanup()
        mock_bot.unpin_chat_message.assert_not_called()
        mock_bot.delete_message.assert_called_once()

    async def test_cleanup_noop_when_no_message(self, pin, mock_bot):
        await pin.cleanup()
        mock_bot.edit_message_text.assert_not_called()
        mock_bot.delete_message.assert_not_called()

    async def test_cleanup_survives_edit_failure(self, pin, mock_bot):
        mock_bot.send_message.return_value = MagicMock(message_id=42)
        await pin.tool_called("Read")
        pin._pinned = True
        mock_bot.edit_message_text.side_effect = Exception("edit fail")
        await pin.cleanup()
        # Should still attempt unpin + delete
        mock_bot.unpin_chat_message.assert_called_once()
        mock_bot.delete_message.assert_called_once()

    async def test_cleanup_survives_unpin_failure(self, pin, mock_bot):
        mock_bot.send_message.return_value = MagicMock(message_id=42)
        await pin.tool_called("Read")
        pin._pinned = True
        mock_bot.unpin_chat_message.side_effect = Exception("unpin fail")
        await pin.cleanup()
        mock_bot.delete_message.assert_called_once()

    async def test_cleanup_survives_delete_failure(self, pin, mock_bot):
        mock_bot.send_message.return_value = MagicMock(message_id=42)
        await pin.tool_called("Read")
        pin._pinned = True
        mock_bot.delete_message.side_effect = Exception("delete fail")
        # Should not raise
        await pin.cleanup()


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    def test_has_active_message_false_initially(self, pin):
        assert not pin.has_active_message

    async def test_has_active_message_true_after_send(self, pin, mock_bot):
        mock_bot.send_message.return_value = MagicMock(message_id=42)
        await pin.tool_called("Read")
        assert pin.has_active_message

    async def test_has_active_message_false_when_disabled(self, pin, mock_bot):
        pin._enabled = False
        pin._message_id = 42
        assert not pin.has_active_message

    async def test_has_active_message_false_after_cleanup(self, pin, mock_bot):
        mock_bot.send_message.return_value = MagicMock(message_id=42)
        await pin.tool_called("Read")
        await pin.cleanup()
        assert not pin.has_active_message

    def test_reset_throttle_updates_time(self, pin):
        pin._last_update_time = 0.0
        pin.reset_throttle()
        assert pin._last_update_time > 0.0


# ---------------------------------------------------------------------------
# Disabled state
# ---------------------------------------------------------------------------


class TestDisabledState:
    async def test_tool_called_noop_when_disabled(self, pin, mock_bot):
        pin._enabled = False
        await pin.tool_called("Read")
        mock_bot.send_message.assert_not_called()
        assert pin._call_count == 0
