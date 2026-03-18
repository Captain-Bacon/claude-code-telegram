"""Tests for StreamSession.

Covers:
- __call__ with tool_calls updates tool_log and heartbeat
- __call__ with assistant text enqueues for delivery
- __call__ with thinking enqueues as thinking
- __call__ with stream_delta goes to draft_streamer
- __call__ is noop when _active is False
- MCP image interception
- flush_pending sends accumulated text and thinking
- flush_pending is noop with no telegram_update
- flush_pending is noop with empty buffers
- delete_thinking removes ephemeral messages
- text_was_sent and flush_succeeded properties
- flush_succeeded goes False on send failure
- Progress edit throttle (skipped when heartbeat pin active)
- make_stream_callback factory returns StreamSession
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.stream_handler import StreamSession, make_stream_callback
from src.claude.sdk_integration import StreamUpdate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_settings():
    """Minimal Settings mock with what StreamSession reads."""
    s = MagicMock()
    return s


def _make_telegram_update():
    """Mock Telegram update with reply_text and chat.delete_message."""
    update = MagicMock()
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    msg.chat = MagicMock()
    msg.chat.delete_message = AsyncMock()
    update.effective_message = msg
    return update


def _make_progress_msg():
    msg = MagicMock()
    msg.edit_text = AsyncMock()
    return msg


def _make_heartbeat():
    hb = MagicMock()
    hb.tool_called = AsyncMock()
    hb.has_active_message = True
    hb.reset_throttle = MagicMock()
    return hb


@pytest.fixture
def tool_log():
    return []


@pytest.fixture
def progress_msg():
    return _make_progress_msg()


@pytest.fixture
def telegram_update():
    return _make_telegram_update()


@pytest.fixture
def heartbeat():
    return _make_heartbeat()


@pytest.fixture
def session(tool_log, progress_msg, telegram_update, heartbeat):
    return StreamSession(
        settings=_make_settings(),
        verbose_level=1,
        progress_msg=progress_msg,
        tool_log=tool_log,
        start_time=time.time(),
        telegram_update=telegram_update,
        heartbeat_pin=heartbeat,
    )


# ---------------------------------------------------------------------------
# __call__ — tool calls
# ---------------------------------------------------------------------------


class TestToolCalls:
    async def test_tool_call_logged(self, session, tool_log):
        update = StreamUpdate(
            type="assistant",
            tool_calls=[{"name": "Read", "input": {"file_path": "/foo/bar.py"}}],
        )
        await session(update)
        assert len(tool_log) == 1
        assert tool_log[0]["name"] == "Read"

    async def test_tool_call_notifies_heartbeat(self, session, heartbeat):
        update = StreamUpdate(
            type="assistant",
            tool_calls=[{"name": "Grep", "input": {}}],
        )
        await session(update)
        heartbeat.tool_called.assert_called_once_with("Grep")

    async def test_multiple_tool_calls_in_single_update(self, session, tool_log):
        update = StreamUpdate(
            type="assistant",
            tool_calls=[
                {"name": "Read", "input": {}},
                {"name": "Write", "input": {}},
            ],
        )
        await session(update)
        assert len(tool_log) == 2

    async def test_tool_call_not_logged_at_verbose_0(
        self, tool_log, progress_msg, telegram_update, heartbeat
    ):
        session = StreamSession(
            settings=_make_settings(),
            verbose_level=0,
            progress_msg=progress_msg,
            tool_log=tool_log,
            start_time=time.time(),
            telegram_update=telegram_update,
            heartbeat_pin=heartbeat,
        )
        update = StreamUpdate(
            type="assistant",
            tool_calls=[{"name": "Read", "input": {}}],
        )
        await session(update)
        assert len(tool_log) == 0
        # But heartbeat still notified
        heartbeat.tool_called.assert_called_once()


# ---------------------------------------------------------------------------
# __call__ — assistant text
# ---------------------------------------------------------------------------


class TestAssistantText:
    async def test_assistant_text_enqueued(self, session):
        update = StreamUpdate(type="assistant", content="Hello world")
        await session(update)
        # Text should be in pending (or flushed via scheduled task)
        # Just verify it didn't error
        assert session._active

    async def test_assistant_text_logged_at_verbose_1(self, session, tool_log):
        update = StreamUpdate(type="assistant", content="Thinking about it")
        await session(update)
        text_entries = [e for e in tool_log if e.get("kind") == "text"]
        assert len(text_entries) == 1

    async def test_empty_content_ignored(self, session, tool_log):
        update = StreamUpdate(type="assistant", content="")
        await session(update)
        text_entries = [e for e in tool_log if e.get("kind") == "text"]
        assert len(text_entries) == 0


# ---------------------------------------------------------------------------
# __call__ — thinking
# ---------------------------------------------------------------------------


class TestThinking:
    async def test_thinking_enqueued(self, session):
        update = StreamUpdate(type="thinking", content="Let me consider...")
        await session(update)
        # Should have been enqueued as thinking
        assert session._active

    async def test_thinking_empty_ignored(self, session):
        update = StreamUpdate(type="thinking", content="   ")
        await session(update)


# ---------------------------------------------------------------------------
# __call__ — stream_delta to draft_streamer
# ---------------------------------------------------------------------------


class TestStreamDelta:
    async def test_stream_delta_to_draft_streamer(
        self, tool_log, progress_msg, telegram_update, heartbeat
    ):
        draft = MagicMock()
        draft.append_text = AsyncMock()
        draft.append_tool = AsyncMock()
        session = StreamSession(
            settings=_make_settings(),
            verbose_level=1,
            progress_msg=progress_msg,
            tool_log=tool_log,
            start_time=time.time(),
            telegram_update=telegram_update,
            heartbeat_pin=heartbeat,
            draft_streamer=draft,
        )
        update = StreamUpdate(type="stream_delta", content="partial")
        await session(update)
        draft.append_text.assert_called_once_with("partial")


# ---------------------------------------------------------------------------
# __call__ — inactive session
# ---------------------------------------------------------------------------


class TestInactiveSession:
    async def test_noop_when_inactive(self, tool_log, progress_msg):
        """Session with verbose=0 and no heartbeat/telegram/mcp/draft is inactive."""
        session = StreamSession(
            settings=_make_settings(),
            verbose_level=0,
            progress_msg=progress_msg,
            tool_log=tool_log,
            start_time=time.time(),
        )
        assert not session._active
        update = StreamUpdate(
            type="assistant",
            tool_calls=[{"name": "Read", "input": {}}],
            content="test",
        )
        await session(update)
        assert len(tool_log) == 0


# ---------------------------------------------------------------------------
# MCP image interception
# ---------------------------------------------------------------------------


class TestMCPImageInterception:
    async def test_intercepts_send_image_tool(
        self, tool_log, progress_msg, telegram_update, heartbeat, tmp_path
    ):
        # Create a valid image file
        img_file = tmp_path / "test.png"
        img_file.write_bytes(b"\x89PNG" + b"\x00" * 100)

        mcp_images = []
        session = StreamSession(
            settings=_make_settings(),
            verbose_level=1,
            progress_msg=progress_msg,
            tool_log=tool_log,
            start_time=time.time(),
            mcp_images=mcp_images,
            approved_directory=tmp_path,
            telegram_update=telegram_update,
            heartbeat_pin=heartbeat,
        )
        update = StreamUpdate(
            type="assistant",
            tool_calls=[
                {
                    "name": "send_image_to_user",
                    "input": {
                        "file_path": str(img_file),
                        "caption": "test image",
                    },
                }
            ],
        )
        await session(update)
        assert len(mcp_images) == 1

    async def test_no_intercept_without_mcp_images_list(
        self, tool_log, progress_msg, telegram_update, heartbeat
    ):
        """Without mcp_images list, image tool calls pass through."""
        session = StreamSession(
            settings=_make_settings(),
            verbose_level=1,
            progress_msg=progress_msg,
            tool_log=tool_log,
            start_time=time.time(),
            mcp_images=None,
            telegram_update=telegram_update,
            heartbeat_pin=heartbeat,
        )
        update = StreamUpdate(
            type="assistant",
            tool_calls=[
                {
                    "name": "send_image_to_user",
                    "input": {"file_path": "/tmp/x.png", "caption": ""},
                }
            ],
        )
        await session(update)
        # Should not error — just no interception


# ---------------------------------------------------------------------------
# flush_pending
# ---------------------------------------------------------------------------


class TestFlushPending:
    async def test_flush_noop_without_telegram_update(
        self, tool_log, progress_msg
    ):
        session = StreamSession(
            settings=_make_settings(),
            verbose_level=1,
            progress_msg=progress_msg,
            tool_log=tool_log,
            start_time=time.time(),
            telegram_update=None,
        )
        session._pending_text = ["some text"]
        await session.flush_pending()
        # No send attempted — no telegram_update

    async def test_flush_noop_when_empty(self, session, telegram_update):
        await session.flush_pending()
        telegram_update.effective_message.reply_text.assert_not_called()

    async def test_flush_sends_text(self, session, telegram_update):
        session._pending_text = ["Hello from stream"]
        # Patch ResponseFormatter to return simple messages
        with patch(
            "src.bot.utils.formatting.ResponseFormatter"
        ) as MockFormatter:
            mock_fmt = MagicMock()
            mock_msg = MagicMock()
            mock_msg.text = "Hello from stream"
            mock_msg.parse_mode = "HTML"
            mock_fmt.format_claude_response.return_value = [mock_msg]
            MockFormatter.return_value = mock_fmt

            await session.flush_pending()
            telegram_update.effective_message.reply_text.assert_called()
            assert session.text_was_sent

    async def test_flush_sends_thinking(self, session, telegram_update):
        session._pending_thinking = ["Considering options..."]
        sent_msg = MagicMock()
        sent_msg.message_id = 99
        telegram_update.effective_message.reply_text.return_value = sent_msg

        await session.flush_pending()
        telegram_update.effective_message.reply_text.assert_called()
        assert 99 in session._thinking_message_ids


# ---------------------------------------------------------------------------
# delete_thinking
# ---------------------------------------------------------------------------


class TestDeleteThinking:
    async def test_deletes_thinking_messages(self, session, telegram_update):
        session._thinking_message_ids = [10, 11]
        await session.delete_thinking()
        assert telegram_update.effective_message.chat.delete_message.call_count == 2
        assert session._thinking_message_ids == []

    async def test_noop_without_telegram_update(self, tool_log, progress_msg):
        session = StreamSession(
            settings=_make_settings(),
            verbose_level=1,
            progress_msg=progress_msg,
            tool_log=tool_log,
            start_time=time.time(),
            telegram_update=None,
        )
        session._thinking_message_ids = [10]
        await session.delete_thinking()  # No error

    async def test_delete_failure_logged_not_raised(
        self, session, telegram_update
    ):
        session._thinking_message_ids = [10]
        telegram_update.effective_message.chat.delete_message.side_effect = (
            Exception("not found")
        )
        await session.delete_thinking()  # Should not raise


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    def test_text_was_sent_initially_false(self, session):
        assert not session.text_was_sent

    def test_flush_succeeded_initially_true(self, session):
        assert session.flush_succeeded

    async def test_flush_succeeded_false_on_total_failure(
        self, session, telegram_update
    ):
        """If both HTML and plain text sends fail, flush_succeeded goes False."""
        session._pending_text = ["test"]
        telegram_update.effective_message.reply_text.side_effect = Exception(
            "fail"
        )
        with patch(
            "src.bot.utils.formatting.ResponseFormatter"
        ) as MockFormatter:
            mock_fmt = MagicMock()
            mock_msg = MagicMock()
            mock_msg.text = "test"
            mock_msg.parse_mode = "HTML"
            mock_fmt.format_claude_response.return_value = [mock_msg]
            MockFormatter.return_value = mock_fmt

            await session.flush_pending()
            assert not session.flush_succeeded


# ---------------------------------------------------------------------------
# Progress edit throttle — skips when heartbeat active
# ---------------------------------------------------------------------------


class TestProgressEditThrottle:
    async def test_skips_edit_when_heartbeat_active(
        self, session, progress_msg, heartbeat
    ):
        """When heartbeat pin has active message, progress edits are skipped."""
        session._last_edit_time = 0.0  # long ago
        session._tool_log.append({"kind": "tool", "name": "Read", "detail": ""})
        heartbeat.has_active_message = True

        update = StreamUpdate(type="assistant", content=None)
        await session(update)
        progress_msg.edit_text.assert_not_called()

    async def test_edits_when_heartbeat_inactive(
        self, tool_log, progress_msg, telegram_update
    ):
        """Without heartbeat, progress msg is edited after throttle."""
        session = StreamSession(
            settings=_make_settings(),
            verbose_level=1,
            progress_msg=progress_msg,
            tool_log=tool_log,
            start_time=time.time(),
            telegram_update=telegram_update,
            heartbeat_pin=None,
        )
        session._last_edit_time = 0.0
        tool_log.append({"kind": "tool", "name": "Read", "detail": ""})

        update = StreamUpdate(type="assistant", content=None)
        await session(update)
        progress_msg.edit_text.assert_called_once()

    async def test_no_edit_within_throttle(
        self, tool_log, progress_msg, telegram_update
    ):
        """Progress msg is not edited if within 8s throttle."""
        session = StreamSession(
            settings=_make_settings(),
            verbose_level=1,
            progress_msg=progress_msg,
            tool_log=tool_log,
            start_time=time.time(),
            telegram_update=telegram_update,
            heartbeat_pin=None,
        )
        session._last_edit_time = time.time()
        tool_log.append({"kind": "tool", "name": "Read", "detail": ""})

        update = StreamUpdate(type="assistant", content=None)
        await session(update)
        progress_msg.edit_text.assert_not_called()


# ---------------------------------------------------------------------------
# make_stream_callback factory
# ---------------------------------------------------------------------------


class TestFactory:
    def test_returns_stream_session(self, progress_msg, tool_log):
        result = make_stream_callback(
            settings=_make_settings(),
            verbose_level=1,
            progress_msg=progress_msg,
            tool_log=tool_log,
            start_time=time.time(),
        )
        assert isinstance(result, StreamSession)
