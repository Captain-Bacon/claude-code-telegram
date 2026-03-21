"""Tests for /checkin command.

Covers:
- agentic_checkin: basic checkin sends PA-mode prompt to Claude
- agentic_checkin: sync mode runs pa-sync before sending prompt
- agentic_checkin: sync failure still proceeds with checkin
- agentic_checkin: sync timeout still proceeds with checkin
- agentic_checkin: pa-sync not on PATH still proceeds with checkin
- agentic_checkin: busy client queues the checkin prompt
- agentic_checkin: no persistent_manager returns error
- agentic_checkin: audit logging records command and args
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.orchestrator import MessageOrchestrator, _build_checkin_prompt


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_orchestrator():
    settings = MagicMock()
    settings.approved_directory = Path("/test")
    settings.enable_heartbeat_pin = False
    settings.verbose_level = 1
    return MessageOrchestrator(settings=settings, deps={})


def _make_update(text="/checkin"):
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.message_id = 1
    update.message.message_thread_id = None
    update.message.reply_text = AsyncMock(return_value=MagicMock(
        edit_text=AsyncMock(),
        delete=AsyncMock(),
    ))
    update.message.chat = MagicMock()
    update.message.chat.id = 123
    update.message.chat.send_action = AsyncMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 42
    update.effective_chat = MagicMock()
    update.effective_chat.id = 123
    return update


def _make_context():
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.bot_data = {}
    ctx.bot = MagicMock()
    return ctx


def _make_persistent_manager(state="idle"):
    pm = MagicMock()
    pm.get_client_state.return_value = state
    resp = MagicMock()
    resp.session_id = "test-session"
    resp.content = "Here's your status..."
    resp.is_interrupted = False
    resp.stop_reason = "end_turn"
    resp.num_turns = 1
    resp.context_window = None
    resp.total_input_tokens = None
    pm.send_message = AsyncMock(return_value=resp)
    return pm


# ---------------------------------------------------------------------------
# _build_checkin_prompt
# ---------------------------------------------------------------------------


def test_build_checkin_prompt_no_sync():
    prompt = _build_checkin_prompt("")
    assert "PA mode" in prompt
    assert "check-in" in prompt
    assert "[" not in prompt  # no sync context bracket


def test_build_checkin_prompt_with_sync():
    prompt = _build_checkin_prompt("[Data sync completed successfully]")
    assert "PA mode" in prompt
    assert "[Data sync completed successfully]" in prompt


# ---------------------------------------------------------------------------
# agentic_checkin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("src.bot.orchestrator.deliver_turn_result", new_callable=AsyncMock)
@patch("src.bot.orchestrator.flush_stream_callback", new_callable=AsyncMock)
@patch("src.bot.orchestrator.make_stream_callback")
@patch("src.bot.orchestrator.make_stall_callback")
async def test_basic_checkin_sends_prompt(
    mock_stall, mock_stream, mock_flush, mock_deliver
):
    orch = _make_orchestrator()
    update = _make_update()
    ctx = _make_context()
    pm = _make_persistent_manager()
    ctx.bot_data["persistent_manager"] = pm

    await orch.agentic_checkin(update, ctx)

    pm.send_message.assert_called_once()
    call_kwargs = pm.send_message.call_args.kwargs
    assert "PA mode" in call_kwargs["prompt"]
    assert "check-in" in call_kwargs["prompt"]
    mock_deliver.assert_called_once()


@pytest.mark.asyncio
@patch("src.bot.orchestrator.deliver_turn_result", new_callable=AsyncMock)
@patch("src.bot.orchestrator.flush_stream_callback", new_callable=AsyncMock)
@patch("src.bot.orchestrator.make_stream_callback")
@patch("src.bot.orchestrator.make_stall_callback")
@patch("shutil.which", return_value="/usr/local/bin/pa-sync")
@patch("asyncio.create_subprocess_exec")
async def test_checkin_sync_runs_pa_sync(
    mock_subprocess, mock_which,
    mock_stall, mock_stream, mock_flush, mock_deliver,
):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"ok", b""))
    proc.returncode = 0
    mock_subprocess.return_value = proc

    orch = _make_orchestrator()
    update = _make_update(text="/checkin sync")
    ctx = _make_context()
    pm = _make_persistent_manager()
    ctx.bot_data["persistent_manager"] = pm

    await orch.agentic_checkin(update, ctx)

    mock_subprocess.assert_called_once_with(
        "pa-sync",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    call_kwargs = pm.send_message.call_args.kwargs
    assert "[Data sync completed successfully]" in call_kwargs["prompt"]


@pytest.mark.asyncio
@patch("src.bot.orchestrator.deliver_turn_result", new_callable=AsyncMock)
@patch("src.bot.orchestrator.flush_stream_callback", new_callable=AsyncMock)
@patch("src.bot.orchestrator.make_stream_callback")
@patch("src.bot.orchestrator.make_stall_callback")
@patch("shutil.which", return_value="/usr/local/bin/pa-sync")
@patch("asyncio.create_subprocess_exec")
async def test_checkin_sync_failure_still_proceeds(
    mock_subprocess, mock_which,
    mock_stall, mock_stream, mock_flush, mock_deliver,
):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"", b"connection refused"))
    proc.returncode = 1
    mock_subprocess.return_value = proc

    orch = _make_orchestrator()
    update = _make_update(text="/checkin sync")
    ctx = _make_context()
    pm = _make_persistent_manager()
    ctx.bot_data["persistent_manager"] = pm

    await orch.agentic_checkin(update, ctx)

    pm.send_message.assert_called_once()
    call_kwargs = pm.send_message.call_args.kwargs
    assert "exited with code 1" in call_kwargs["prompt"]
    assert "connection refused" in call_kwargs["prompt"]


@pytest.mark.asyncio
@patch("src.bot.orchestrator.deliver_turn_result", new_callable=AsyncMock)
@patch("src.bot.orchestrator.flush_stream_callback", new_callable=AsyncMock)
@patch("src.bot.orchestrator.make_stream_callback")
@patch("src.bot.orchestrator.make_stall_callback")
@patch("shutil.which", return_value="/usr/local/bin/pa-sync")
@patch("asyncio.create_subprocess_exec")
async def test_checkin_sync_timeout_still_proceeds(
    mock_subprocess, mock_which,
    mock_stall, mock_stream, mock_flush, mock_deliver,
):
    proc = AsyncMock()
    proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_subprocess.return_value = proc

    orch = _make_orchestrator()
    update = _make_update(text="/checkin sync")
    ctx = _make_context()
    pm = _make_persistent_manager()
    ctx.bot_data["persistent_manager"] = pm

    await orch.agentic_checkin(update, ctx)

    pm.send_message.assert_called_once()
    call_kwargs = pm.send_message.call_args.kwargs
    assert "timed out" in call_kwargs["prompt"]


@pytest.mark.asyncio
@patch("src.bot.orchestrator.deliver_turn_result", new_callable=AsyncMock)
@patch("src.bot.orchestrator.flush_stream_callback", new_callable=AsyncMock)
@patch("src.bot.orchestrator.make_stream_callback")
@patch("src.bot.orchestrator.make_stall_callback")
@patch("shutil.which", return_value=None)
async def test_checkin_sync_no_pa_sync_on_path(
    mock_which,
    mock_stall, mock_stream, mock_flush, mock_deliver,
):
    orch = _make_orchestrator()
    update = _make_update(text="/checkin sync")
    ctx = _make_context()
    pm = _make_persistent_manager()
    ctx.bot_data["persistent_manager"] = pm

    await orch.agentic_checkin(update, ctx)

    pm.send_message.assert_called_once()
    call_kwargs = pm.send_message.call_args.kwargs
    assert "not found on PATH" in call_kwargs["prompt"]


@pytest.mark.asyncio
async def test_checkin_busy_queues_prompt():
    orch = _make_orchestrator()
    update = _make_update()
    ctx = _make_context()
    pm = _make_persistent_manager(state="busy")
    ctx.bot_data["persistent_manager"] = pm

    with patch.object(orch, "_enqueue_message", new_callable=AsyncMock) as mock_enqueue:
        await orch.agentic_checkin(update, ctx)

        mock_enqueue.assert_called_once()
        call_kwargs = mock_enqueue.call_args.kwargs
        assert "PA mode" in call_kwargs["message_text"]

    pm.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_checkin_no_persistent_manager():
    orch = _make_orchestrator()
    update = _make_update()
    ctx = _make_context()
    # No persistent_manager in bot_data

    await orch.agentic_checkin(update, ctx)

    update.message.reply_text.assert_any_call(
        "Claude integration not available. Check configuration."
    )


@pytest.mark.asyncio
@patch("src.bot.orchestrator.deliver_turn_result", new_callable=AsyncMock)
@patch("src.bot.orchestrator.flush_stream_callback", new_callable=AsyncMock)
@patch("src.bot.orchestrator.make_stream_callback")
@patch("src.bot.orchestrator.make_stall_callback")
async def test_checkin_audit_logging(
    mock_stall, mock_stream, mock_flush, mock_deliver
):
    orch = _make_orchestrator()
    update = _make_update()
    ctx = _make_context()
    pm = _make_persistent_manager()
    ctx.bot_data["persistent_manager"] = pm
    audit = MagicMock()
    audit.log_command = AsyncMock()
    ctx.bot_data["audit_logger"] = audit

    await orch.agentic_checkin(update, ctx)

    audit.log_command.assert_called_once_with(
        user_id=42,
        command="checkin",
        args=[],
        success=True,
    )


@pytest.mark.asyncio
@patch("src.bot.orchestrator.deliver_turn_result", new_callable=AsyncMock)
@patch("src.bot.orchestrator.flush_stream_callback", new_callable=AsyncMock)
@patch("src.bot.orchestrator.make_stream_callback")
@patch("src.bot.orchestrator.make_stall_callback")
@patch("shutil.which", return_value="/usr/local/bin/pa-sync")
@patch("asyncio.create_subprocess_exec")
async def test_checkin_audit_logging_sync(
    mock_subprocess, mock_which,
    mock_stall, mock_stream, mock_flush, mock_deliver,
):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"ok", b""))
    proc.returncode = 0
    mock_subprocess.return_value = proc

    orch = _make_orchestrator()
    update = _make_update(text="/checkin sync")
    ctx = _make_context()
    pm = _make_persistent_manager()
    ctx.bot_data["persistent_manager"] = pm
    audit = MagicMock()
    audit.log_command = AsyncMock()
    ctx.bot_data["audit_logger"] = audit

    await orch.agentic_checkin(update, ctx)

    audit.log_command.assert_called_once_with(
        user_id=42,
        command="checkin",
        args=["sync"],
        success=True,
    )
