"""Tests for delivery.py.

Covers:
- make_stall_callback: edits progress msg with silence warning
- make_stall_callback: edits progress msg with death warning
- make_stall_callback: edit failure is swallowed
- deliver_turn_result: skips response when text_was_sent + flush_succeeded
- deliver_turn_result: sends response when text_was_sent but flush failed
- deliver_turn_result: sends response normally when no text was streamed
- deliver_turn_result: finalizes progress msg to Done/Failed
- context_warning: crosses thresholds
- context_warning: deduplicates warnings via user_data
- abnormal_stop_notice: returns notice for non-end_turn stops
- abnormal_stop_notice: returns None for end_turn
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

from src.bot.delivery import (
    abnormal_stop_notice,
    context_warning,
    deliver_turn_result,
    make_stall_callback,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_settings():
    return MagicMock()


def _make_update():
    update = MagicMock()
    update.message = MagicMock()
    update.message.message_id = 1
    update.message.reply_text = AsyncMock()
    update.message.reply_photo = AsyncMock()
    update.message.chat = MagicMock()
    update.message.chat.send_media_group = AsyncMock()
    return update


def _make_context():
    ctx = MagicMock()
    ctx.user_data = {}
    return ctx


def _make_progress_msg():
    msg = MagicMock()
    msg.edit_text = AsyncMock()
    msg.delete = AsyncMock()
    return msg


def _make_response(content="Hello", stop_reason="end_turn"):
    resp = MagicMock()
    resp.content = content
    resp.is_interrupted = False
    resp.stop_reason = stop_reason
    resp.num_turns = 3
    resp.context_window = None
    resp.total_input_tokens = None
    return resp


def _make_on_stream(text_was_sent=False, flush_succeeded=True):
    stream = MagicMock()
    stream.text_was_sent = text_was_sent
    stream.flush_succeeded = flush_succeeded
    stream.flush_pending = AsyncMock()
    stream.delete_thinking = AsyncMock()
    return stream


# ---------------------------------------------------------------------------
# make_stall_callback
# ---------------------------------------------------------------------------


class TestMakeStallCallback:
    async def test_silence_warning(self):
        progress = _make_progress_msg()
        cb = make_stall_callback(progress)
        await cb(
            silence_seconds=45.0,
            total_elapsed_seconds=120.0,
            cli_alive=True,
            is_dead=False,
        )
        progress.edit_text.assert_called_once()
        text = progress.edit_text.call_args[0][0]
        assert "45" in text
        assert "120" in text
        assert "still checking" in text

    async def test_death_warning(self):
        progress = _make_progress_msg()
        cb = make_stall_callback(progress)
        await cb(
            silence_seconds=60.0,
            total_elapsed_seconds=180.0,
            cli_alive=False,
            is_dead=True,
        )
        text = progress.edit_text.call_args[0][0]
        assert "died" in text

    async def test_edit_failure_swallowed(self):
        progress = _make_progress_msg()
        progress.edit_text.side_effect = Exception("rate limit")
        cb = make_stall_callback(progress)
        # Should not raise
        await cb(
            silence_seconds=30.0,
            total_elapsed_seconds=60.0,
            cli_alive=True,
            is_dead=False,
        )


# ---------------------------------------------------------------------------
# context_warning
# ---------------------------------------------------------------------------


class TestContextWarning:
    def test_no_warning_when_plenty_of_context(self):
        resp = MagicMock()
        resp.context_window = 200000
        resp.total_input_tokens = 20000  # 10% used = 90% remaining
        assert context_warning(resp) is None

    def test_warning_at_60_percent_remaining(self):
        resp = MagicMock()
        resp.context_window = 200000
        resp.total_input_tokens = 90000  # 45% used = 55% remaining
        result = context_warning(resp)
        assert result is not None
        assert "60%" in result

    def test_warning_dedup_via_user_data(self):
        resp = MagicMock()
        resp.context_window = 200000
        resp.total_input_tokens = 90000
        user_data = {}
        w1 = context_warning(resp, user_data)
        assert w1 is not None
        w2 = context_warning(resp, user_data)
        assert w2 is None  # deduplicated

    def test_no_warning_without_context_window(self):
        resp = MagicMock()
        resp.context_window = None
        resp.total_input_tokens = None
        assert context_warning(resp) is None

    def test_critical_warning_icon(self):
        resp = MagicMock()
        resp.context_window = 200000
        resp.total_input_tokens = 180000  # 90% used = 10% remaining
        result = context_warning(resp)
        assert "10%" in result


# ---------------------------------------------------------------------------
# abnormal_stop_notice
# ---------------------------------------------------------------------------


class TestAbnormalStopNotice:
    def test_returns_none_for_end_turn(self):
        resp = MagicMock()
        resp.stop_reason = "end_turn"
        assert abnormal_stop_notice(resp) is None

    def test_returns_none_for_no_stop_reason(self):
        resp = MagicMock()
        resp.stop_reason = None
        assert abnormal_stop_notice(resp) is None

    def test_returns_notice_for_max_tokens(self):
        resp = MagicMock()
        resp.stop_reason = "max_tokens"
        result = abnormal_stop_notice(resp)
        assert result is not None
        assert "token limit" in result.text

    def test_returns_notice_for_max_turns(self):
        resp = MagicMock()
        resp.stop_reason = "max_turns"
        result = abnormal_stop_notice(resp)
        assert "tool use limit" in result.text


# ---------------------------------------------------------------------------
# deliver_turn_result — flush_succeeded safety net
# ---------------------------------------------------------------------------


class TestDeliverTurnResult:
    async def test_skips_response_when_already_streamed(self):
        """When text_was_sent and flush_succeeded, don't re-send."""
        update = _make_update()
        on_stream = _make_on_stream(text_was_sent=True, flush_succeeded=True)
        response = _make_response()
        progress = _make_progress_msg()

        with patch("src.bot.delivery.flush_stream_callback", new_callable=AsyncMock):
            with patch(
                "src.bot.delivery.cleanup_thinking_messages",
                new_callable=AsyncMock,
            ):
                await deliver_turn_result(
                    settings=_make_settings(),
                    update=update,
                    context=_make_context(),
                    claude_response=response,
                    on_stream=on_stream,
                    progress_msg=progress,
                    start_time=time.time(),
                    mcp_images=[],
                )
        # No reply_text for the main response
        update.message.reply_text.assert_not_called()

    async def test_sends_response_when_flush_failed(self):
        """Safety net: when flush_succeeded is False, deliver full response."""
        update = _make_update()
        on_stream = _make_on_stream(text_was_sent=True, flush_succeeded=False)
        response = _make_response(content="Full response")
        progress = _make_progress_msg()

        with patch("src.bot.delivery.flush_stream_callback", new_callable=AsyncMock):
            with patch(
                "src.bot.delivery.cleanup_thinking_messages",
                new_callable=AsyncMock,
            ):
                with patch(
                    "src.bot.utils.formatting.ResponseFormatter"
                ) as MockFmt:
                    mock_fmt = MagicMock()
                    mock_msg = MagicMock()
                    mock_msg.text = "Full response"
                    mock_msg.parse_mode = "HTML"
                    mock_fmt.format_claude_response.return_value = [mock_msg]
                    MockFmt.return_value = mock_fmt

                    await deliver_turn_result(
                        settings=_make_settings(),
                        update=update,
                        context=_make_context(),
                        claude_response=response,
                        on_stream=on_stream,
                        progress_msg=progress,
                        start_time=time.time(),
                        mcp_images=[],
                    )
        update.message.reply_text.assert_called()

    async def test_sends_response_when_nothing_streamed(self):
        """Normal path: no streaming happened, send full response."""
        update = _make_update()
        on_stream = _make_on_stream(text_was_sent=False, flush_succeeded=True)
        response = _make_response(content="Normal response")
        progress = _make_progress_msg()

        with patch("src.bot.delivery.flush_stream_callback", new_callable=AsyncMock):
            with patch(
                "src.bot.delivery.cleanup_thinking_messages",
                new_callable=AsyncMock,
            ):
                with patch(
                    "src.bot.utils.formatting.ResponseFormatter"
                ) as MockFmt:
                    mock_fmt = MagicMock()
                    mock_msg = MagicMock()
                    mock_msg.text = "Normal response"
                    mock_msg.parse_mode = "HTML"
                    mock_fmt.format_claude_response.return_value = [mock_msg]
                    MockFmt.return_value = mock_fmt

                    await deliver_turn_result(
                        settings=_make_settings(),
                        update=update,
                        context=_make_context(),
                        claude_response=response,
                        on_stream=on_stream,
                        progress_msg=progress,
                        start_time=time.time(),
                        mcp_images=[],
                    )
        update.message.reply_text.assert_called()

    async def test_progress_msg_done_on_success(self):
        update = _make_update()
        on_stream = _make_on_stream(text_was_sent=True, flush_succeeded=True)
        response = _make_response()
        progress = _make_progress_msg()

        with patch("src.bot.delivery.flush_stream_callback", new_callable=AsyncMock):
            with patch(
                "src.bot.delivery.cleanup_thinking_messages",
                new_callable=AsyncMock,
            ):
                await deliver_turn_result(
                    settings=_make_settings(),
                    update=update,
                    context=_make_context(),
                    claude_response=response,
                    on_stream=on_stream,
                    progress_msg=progress,
                    start_time=time.time(),
                    mcp_images=[],
                    success=True,
                )
        progress.edit_text.assert_called()
        text = progress.edit_text.call_args[0][0]
        assert "Done" in text or "\u2705" in text

    async def test_progress_msg_failed_on_error(self):
        update = _make_update()
        progress = _make_progress_msg()

        with patch(
            "src.bot.delivery.cleanup_thinking_messages",
            new_callable=AsyncMock,
        ):
            await deliver_turn_result(
                settings=_make_settings(),
                update=update,
                context=_make_context(),
                claude_response=_make_response(),
                on_stream=None,
                progress_msg=progress,
                start_time=time.time(),
                mcp_images=[],
                success=False,
                error_messages=[],
            )
        progress.edit_text.assert_called()
        text = progress.edit_text.call_args[0][0]
        assert "Failed" in text or "\u274c" in text

    async def test_uses_error_messages_when_provided(self):
        """When error_messages is provided, sends those instead of formatting response."""
        update = _make_update()
        progress = _make_progress_msg()
        error_msg = MagicMock()
        error_msg.text = "Something went wrong"
        error_msg.parse_mode = None

        with patch(
            "src.bot.delivery.cleanup_thinking_messages",
            new_callable=AsyncMock,
        ):
            await deliver_turn_result(
                settings=_make_settings(),
                update=update,
                context=_make_context(),
                claude_response=_make_response(),
                on_stream=None,
                progress_msg=progress,
                start_time=time.time(),
                mcp_images=[],
                error_messages=[error_msg],
            )
        update.message.reply_text.assert_called_once()
        assert update.message.reply_text.call_args[0][0] == "Something went wrong"
