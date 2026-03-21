"""Tests for /checkin command.

Covers:
- _build_checkin_prompt: with and without sync data
- _read_sync_summary: reads summary file, handles missing
- agentic_checkin: basic checkin sends PA-mode prompt to Claude
- agentic_checkin: includes existing sync summary in prompt
- agentic_checkin: sync mode runs session-sync.sh then includes summary
- agentic_checkin: sync failure still proceeds with existing data
- agentic_checkin: sync timeout still proceeds with existing data
- agentic_checkin: sync not configured reports clearly
- agentic_checkin: busy client queues the checkin prompt
- agentic_checkin: no persistent_manager returns error
- agentic_checkin: audit logging records command and args
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.orchestrator import (
    MessageOrchestrator,
    _build_checkin_prompt,
    _read_sync_summary,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SYNC_DIR = "/tmp/test-sync-engine"


def _make_orchestrator(sync_engine_dir=None):
    settings = MagicMock()
    settings.approved_directory = Path("/test")
    settings.enable_heartbeat_pin = False
    settings.verbose_level = 1
    settings.sync_engine_dir = sync_engine_dir
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


def test_build_checkin_prompt_with_sync_data():
    prompt = _build_checkin_prompt("## gmail\n3 new emails")
    assert "PA mode" in prompt
    assert "3 new emails" in prompt


# ---------------------------------------------------------------------------
# _read_sync_summary
# ---------------------------------------------------------------------------


def test_read_sync_summary_exists(tmp_path):
    deltas = tmp_path / "deltas"
    deltas.mkdir()
    summary = deltas / ".session-summary.md"
    summary.write_text("## gmail\nNo changes.")

    result = _read_sync_summary(str(tmp_path))
    assert "gmail" in result
    assert "No changes" in result


def test_read_sync_summary_missing(tmp_path):
    result = _read_sync_summary(str(tmp_path))
    assert result == ""


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
@patch("src.bot.orchestrator._read_sync_summary", return_value="## gmail\n2 new emails")
async def test_checkin_includes_existing_summary(
    mock_summary,
    mock_stall, mock_stream, mock_flush, mock_deliver,
):
    orch = _make_orchestrator(sync_engine_dir="/some/dir")
    update = _make_update()
    ctx = _make_context()
    pm = _make_persistent_manager()
    ctx.bot_data["persistent_manager"] = pm

    await orch.agentic_checkin(update, ctx)

    call_kwargs = pm.send_message.call_args.kwargs
    assert "2 new emails" in call_kwargs["prompt"]


@pytest.mark.asyncio
@patch("src.bot.orchestrator.deliver_turn_result", new_callable=AsyncMock)
@patch("src.bot.orchestrator.flush_stream_callback", new_callable=AsyncMock)
@patch("src.bot.orchestrator.make_stream_callback")
@patch("src.bot.orchestrator.make_stall_callback")
@patch("src.bot.orchestrator._read_sync_summary", return_value="## gmail\nFresh data")
@patch("asyncio.create_subprocess_exec")
async def test_checkin_sync_runs_session_sync(
    mock_subprocess, mock_summary,
    mock_stall, mock_stream, mock_flush, mock_deliver,
):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"ok", b""))
    proc.returncode = 0
    mock_subprocess.return_value = proc

    orch = _make_orchestrator(sync_engine_dir="/some/dir")
    # Patch script existence check
    with patch("pathlib.Path.exists", return_value=True):
        update = _make_update(text="/checkin sync")
        ctx = _make_context()
        pm = _make_persistent_manager()
        ctx.bot_data["persistent_manager"] = pm

        await orch.agentic_checkin(update, ctx)

    mock_subprocess.assert_called_once()
    call_kwargs = pm.send_message.call_args.kwargs
    assert "synced successfully" in call_kwargs["prompt"]
    assert "Fresh data" in call_kwargs["prompt"]


@pytest.mark.asyncio
@patch("src.bot.orchestrator.deliver_turn_result", new_callable=AsyncMock)
@patch("src.bot.orchestrator.flush_stream_callback", new_callable=AsyncMock)
@patch("src.bot.orchestrator.make_stream_callback")
@patch("src.bot.orchestrator.make_stall_callback")
@patch("src.bot.orchestrator._read_sync_summary", return_value="## gmail\nStale data")
@patch("asyncio.create_subprocess_exec")
async def test_checkin_sync_failure_still_proceeds(
    mock_subprocess, mock_summary,
    mock_stall, mock_stream, mock_flush, mock_deliver,
):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"", b"connection refused"))
    proc.returncode = 1
    mock_subprocess.return_value = proc

    orch = _make_orchestrator(sync_engine_dir="/some/dir")
    with patch("pathlib.Path.exists", return_value=True):
        update = _make_update(text="/checkin sync")
        ctx = _make_context()
        pm = _make_persistent_manager()
        ctx.bot_data["persistent_manager"] = pm

        await orch.agentic_checkin(update, ctx)

    pm.send_message.assert_called_once()
    call_kwargs = pm.send_message.call_args.kwargs
    assert "Sync failed" in call_kwargs["prompt"]
    assert "Stale data" in call_kwargs["prompt"]


@pytest.mark.asyncio
@patch("src.bot.orchestrator.deliver_turn_result", new_callable=AsyncMock)
@patch("src.bot.orchestrator.flush_stream_callback", new_callable=AsyncMock)
@patch("src.bot.orchestrator.make_stream_callback")
@patch("src.bot.orchestrator.make_stall_callback")
@patch("src.bot.orchestrator._read_sync_summary", return_value="")
@patch("asyncio.create_subprocess_exec")
async def test_checkin_sync_timeout_still_proceeds(
    mock_subprocess, mock_summary,
    mock_stall, mock_stream, mock_flush, mock_deliver,
):
    proc = AsyncMock()
    proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_subprocess.return_value = proc

    orch = _make_orchestrator(sync_engine_dir="/some/dir")
    with patch("pathlib.Path.exists", return_value=True):
        update = _make_update(text="/checkin sync")
        ctx = _make_context()
        pm = _make_persistent_manager()
        ctx.bot_data["persistent_manager"] = pm

        await orch.agentic_checkin(update, ctx)

    pm.send_message.assert_called_once()
    call_kwargs = pm.send_message.call_args.kwargs
    assert "Sync failed" in call_kwargs["prompt"]


@pytest.mark.asyncio
@patch("src.bot.orchestrator.deliver_turn_result", new_callable=AsyncMock)
@patch("src.bot.orchestrator.flush_stream_callback", new_callable=AsyncMock)
@patch("src.bot.orchestrator.make_stream_callback")
@patch("src.bot.orchestrator.make_stall_callback")
async def test_checkin_sync_not_configured(
    mock_stall, mock_stream, mock_flush, mock_deliver,
):
    orch = _make_orchestrator(sync_engine_dir=None)
    update = _make_update(text="/checkin sync")
    ctx = _make_context()
    pm = _make_persistent_manager()
    ctx.bot_data["persistent_manager"] = pm

    await orch.agentic_checkin(update, ctx)

    pm.send_message.assert_called_once()
    call_kwargs = pm.send_message.call_args.kwargs
    assert "SYNC_ENGINE_DIR" in call_kwargs["prompt"]


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
@patch("src.bot.orchestrator._read_sync_summary", return_value="")
@patch("asyncio.create_subprocess_exec")
async def test_checkin_audit_logging_sync(
    mock_subprocess, mock_summary,
    mock_stall, mock_stream, mock_flush, mock_deliver,
):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"ok", b""))
    proc.returncode = 0
    mock_subprocess.return_value = proc

    orch = _make_orchestrator(sync_engine_dir="/some/dir")
    with patch("pathlib.Path.exists", return_value=True):
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
