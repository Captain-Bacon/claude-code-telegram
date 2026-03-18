"""Tests for the MessageOrchestrator."""

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.orchestrator import MessageOrchestrator
from src.bot.stream_handler import _redact_secrets, _summarize_tool_input
from src.config import create_test_config


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def agentic_settings(tmp_dir):
    return create_test_config(approved_directory=str(tmp_dir), agentic_mode=True, enable_project_threads=False)


@pytest.fixture
def classic_settings(tmp_dir):
    return create_test_config(approved_directory=str(tmp_dir), agentic_mode=False, enable_project_threads=False)


@pytest.fixture
def group_thread_settings(tmp_dir):
    project_dir = tmp_dir / "project_a"
    project_dir.mkdir()
    config_file = tmp_dir / "projects.yaml"
    config_file.write_text(
        "projects:\n"
        "  - slug: project_a\n"
        "    name: Project A\n"
        "    path: project_a\n",
        encoding="utf-8",
    )
    return create_test_config(
        approved_directory=str(tmp_dir),
        agentic_mode=False,
        enable_project_threads=True,
        project_threads_mode="group",
        project_threads_chat_id=-1001234567890,
        projects_config_path=str(config_file),
    )


@pytest.fixture
def private_thread_settings(tmp_dir):
    project_dir = tmp_dir / "project_a"
    project_dir.mkdir()
    config_file = tmp_dir / "projects.yaml"
    config_file.write_text(
        "projects:\n"
        "  - slug: project_a\n"
        "    name: Project A\n"
        "    path: project_a\n",
        encoding="utf-8",
    )
    return create_test_config(
        approved_directory=str(tmp_dir),
        agentic_mode=False,
        enable_project_threads=True,
        project_threads_mode="private",
        projects_config_path=str(config_file),
    )


@pytest.fixture
def deps():
    return {
        "storage": MagicMock(),
        "security_validator": MagicMock(),
        "rate_limiter": MagicMock(),
        "audit_logger": MagicMock(),
    }


def test_agentic_registers_8_commands(agentic_settings, deps):
    """Agentic mode registers start, new, status, verbose, repo, model, restart, stop commands."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    app = MagicMock()
    app.add_handler = MagicMock()

    orchestrator.register_handlers(app)

    # Collect all CommandHandler registrations
    from telegram.ext import CommandHandler

    cmd_handlers = [
        call
        for call in app.add_handler.call_args_list
        if isinstance(call[0][0], CommandHandler)
    ]
    commands = [h[0][0].commands for h in cmd_handlers]

    assert len(cmd_handlers) == 8
    assert frozenset({"start"}) in commands
    assert frozenset({"new"}) in commands
    assert frozenset({"status"}) in commands
    assert frozenset({"verbose"}) in commands
    assert frozenset({"repo"}) in commands
    assert frozenset({"model"}) in commands
    assert frozenset({"restart"}) in commands
    assert frozenset({"stop"}) in commands



def test_agentic_registers_text_document_photo_handlers(agentic_settings, deps):
    """Agentic mode registers text, document, photo, and voice message handlers."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    app = MagicMock()
    app.add_handler = MagicMock()

    orchestrator.register_handlers(app)

    from telegram.ext import CallbackQueryHandler, MessageHandler

    msg_handlers = [
        call
        for call in app.add_handler.call_args_list
        if isinstance(call[0][0], MessageHandler)
    ]
    cb_handlers = [
        call
        for call in app.add_handler.call_args_list
        if isinstance(call[0][0], CallbackQueryHandler)
    ]

    # 4 message handlers (text, document, photo, voice)
    assert len(msg_handlers) == 4
    # 1 callback handler (for cd: only)
    assert len(cb_handlers) == 1


async def test_agentic_bot_commands(agentic_settings, deps):
    """Agentic mode returns 8 bot commands."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    commands = await orchestrator.get_bot_commands()

    assert len(commands) == 8
    cmd_names = [c.command for c in commands]
    assert cmd_names == ["start", "new", "status", "verbose", "repo", "model", "restart", "stop"]



async def test_restart_command_sends_sigterm(deps):
    """restart_command sets restart flag and sends SIGTERM."""
    from unittest.mock import patch

    from src.bot.orchestrator import restart_command

    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"audit_logger": None}

    with patch("src.bot.orchestrator.os.kill") as mock_kill:
        await restart_command(update, context)

    import os
    import signal

    mock_kill.assert_called_once_with(os.getpid(), signal.SIGTERM)
    assert os.environ.pop("_RESTART_REQUESTED", None) == "1"
    # Verify confirmation message was sent
    update.message.reply_text.assert_called_once()
    msg = update.message.reply_text.call_args[0][0]
    assert "Restarting" in msg


async def test_agentic_start_no_keyboard(agentic_settings, deps):
    """Agentic /start sends brief message without inline keyboard."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    update = MagicMock()
    update.effective_user.first_name = "Alice"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {"settings": agentic_settings}
    for k, v in deps.items():
        context.bot_data[k] = v

    await orchestrator.agentic_start(update, context)

    update.message.reply_text.assert_called_once()
    call_kwargs = update.message.reply_text.call_args
    # No reply_markup argument (no keyboard)
    assert (
        "reply_markup" not in call_kwargs.kwargs
        or call_kwargs.kwargs.get("reply_markup") is None
    )
    # Contains user name
    assert "Alice" in call_kwargs.args[0]


async def test_agentic_new_resets_session(agentic_settings, deps):
    """Agentic /new clears session and sends brief confirmation."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    persistent_manager = AsyncMock()
    persistent_manager.disconnect_client = AsyncMock()

    update = MagicMock()
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {"claude_session_id": "old-session-123"}
    context.bot_data = {"persistent_manager": persistent_manager}

    await orchestrator.agentic_new(update, context)

    assert context.user_data["claude_session_id"] is None
    persistent_manager.disconnect_client.assert_awaited_once()
    update.message.reply_text.assert_called_once_with("Session reset. What's next?")


async def test_agentic_status_compact(agentic_settings, deps):
    """Agentic /status returns compact one-line status."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {"rate_limiter": None}

    await orchestrator.agentic_status(update, context)

    call_args = update.message.reply_text.call_args
    text = call_args.args[0]
    assert "Session: none" in text


async def test_agentic_text_calls_claude(agentic_settings, deps):
    """Agentic text handler calls Claude via persistent manager."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    # Mock persistent response
    mock_response = MagicMock()
    mock_response.session_id = "session-abc"
    mock_response.content = "Hello, I can help with that!"
    mock_response.tools_used = []
    mock_response.is_interrupted = False
    mock_response.context_window = None
    mock_response.total_input_tokens = None

    persistent_manager = MagicMock()
    persistent_manager.send_message = AsyncMock(return_value=mock_response)
    persistent_manager.get_client_state = MagicMock(return_value=None)

    update = MagicMock()
    update.effective_user.id = 123
    update.effective_chat.id = 456
    update.message.text = "Help me with this code"
    update.message.message_id = 1
    update.message.message_thread_id = None
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock()

    # Progress message mock
    progress_msg = AsyncMock()
    progress_msg.delete = AsyncMock()
    update.message.reply_text.return_value = progress_msg

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": agentic_settings,
        "persistent_manager": persistent_manager,
        "storage": None,
        "rate_limiter": None,
        "audit_logger": None,
    }

    await orchestrator.agentic_text(update, context)

    # Claude was called via persistent manager
    persistent_manager.send_message.assert_awaited_once()

    # Session ID updated
    assert context.user_data["claude_session_id"] == "session-abc"

    # Progress message edited to final state (not deleted)
    progress_msg.edit_text.assert_called()
    final_text = progress_msg.edit_text.call_args[0][0]
    assert "\u2705" in final_text  # ✅ Done

    # Response sent without keyboard (reply_markup=None)
    response_calls = [
        c
        for c in update.message.reply_text.call_args_list
        if c != update.message.reply_text.call_args_list[0]
    ]
    for call in response_calls:
        assert call.kwargs.get("reply_markup") is None


async def test_agentic_callback_scoped_to_cd_pattern(agentic_settings, deps):
    """Agentic callback handler is registered with cd: pattern filter."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)
    app = MagicMock()
    app.add_handler = MagicMock()

    orchestrator.register_handlers(app)

    from telegram.ext import CallbackQueryHandler

    cb_handlers = [
        call[0][0]
        for call in app.add_handler.call_args_list
        if isinstance(call[0][0], CallbackQueryHandler)
    ]

    assert len(cb_handlers) == 1
    # The pattern attribute should match cd: prefixed data
    assert cb_handlers[0].pattern is not None
    assert cb_handlers[0].pattern.match("cd:my_project")


async def test_agentic_document_rejects_large_files(agentic_settings, deps):
    """Agentic document handler rejects files over 10MB."""
    from src.bot.media_handlers import agentic_document

    update = MagicMock()
    update.effective_user.id = 123
    update.message.document.file_name = "big.bin"
    update.message.document.file_size = 20 * 1024 * 1024  # 20MB
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"security_validator": None}

    await agentic_document(agentic_settings, update, context)

    call_args = update.message.reply_text.call_args
    assert "too large" in call_args.args[0].lower()


async def test_agentic_voice_calls_claude(agentic_settings, deps):
    """Agentic voice handler transcribes and routes prompt via persistent manager."""
    from src.bot.media_handlers import agentic_voice as _agentic_voice

    agentic_settings.enable_voice_messages = True
    agentic_settings.voice_provider = "mistral"
    agentic_settings.mistral_api_key = "test-key"

    mock_response = MagicMock()
    mock_response.session_id = "voice-session-123"
    mock_response.content = "Voice response from Claude"
    mock_response.tools_used = []
    mock_response.is_interrupted = False
    mock_response.context_window = None
    mock_response.total_input_tokens = None

    persistent_manager = MagicMock()
    persistent_manager.send_message = AsyncMock(return_value=mock_response)
    persistent_manager.get_client_state = MagicMock(return_value=None)

    processed_voice = MagicMock()
    processed_voice.prompt = "Voice prompt text"
    processed_voice.transcription = "Voice prompt text"

    mock_voice_handler = MagicMock()
    mock_voice_handler.process_voice_message = AsyncMock(return_value=processed_voice)

    update = MagicMock()
    update.effective_user.id = 123
    update.effective_chat.id = 456
    update.message.voice = MagicMock()
    update.message.caption = "please summarize"
    update.message.message_id = 1
    update.message.message_thread_id = None
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock()

    progress_msg = AsyncMock()
    progress_msg.edit_text = AsyncMock()
    progress_msg.delete = AsyncMock()
    update.message.reply_text.return_value = progress_msg

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": agentic_settings,
        "persistent_manager": persistent_manager,
    }

    with patch("src.bot.media.voice_handler.VoiceHandler", return_value=mock_voice_handler):
        await _agentic_voice(agentic_settings, update, context)

    mock_voice_handler.process_voice_message.assert_awaited_once_with(
        update.message.voice, "please summarize"
    )
    persistent_manager.send_message.assert_awaited_once()
    assert context.user_data["claude_session_id"] == "voice-session-123"


async def test_agentic_voice_missing_handler_is_provider_aware(tmp_path, deps):
    """Missing voice handler guidance references the configured provider key."""
    from src.bot.media_handlers import agentic_voice as _agentic_voice

    settings = create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        voice_provider="openai",
    )

    features = MagicMock()
    features.get_voice_handler.return_value = None

    update = MagicMock()
    update.effective_user.id = 123
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"features": features}
    context.user_data = {}

    await _agentic_voice(settings, update, context)

    call_args = update.message.reply_text.call_args
    assert "OPENAI_API_KEY" in call_args.args[0]


async def test_agentic_voice_transcription_failure_surfaces_user_error(
    agentic_settings, deps
):
    """Transcription failures are shown to users and do not call Claude."""
    from src.bot.media_handlers import agentic_voice as _agentic_voice

    agentic_settings.enable_voice_messages = True
    agentic_settings.voice_provider = "mistral"
    agentic_settings.mistral_api_key = "test-key"

    mock_voice_handler = MagicMock()
    mock_voice_handler.process_voice_message = AsyncMock(
        side_effect=RuntimeError("Mistral transcription request failed: boom")
    )

    persistent_manager = MagicMock()
    persistent_manager.send_message = AsyncMock()

    update = MagicMock()
    update.effective_user.id = 123
    update.message.voice = MagicMock()
    update.message.caption = None
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock()

    progress_msg = AsyncMock()
    progress_msg.edit_text = AsyncMock()
    update.message.reply_text.return_value = progress_msg

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": agentic_settings,
        "persistent_manager": persistent_manager,
    }

    with patch("src.bot.media.voice_handler.VoiceHandler", return_value=mock_voice_handler):
        await _agentic_voice(agentic_settings, update, context)

    progress_msg.edit_text.assert_awaited_once()
    error_text = progress_msg.edit_text.call_args.args[0]
    assert "Mistral transcription request failed" in error_text
    assert progress_msg.edit_text.call_args.kwargs["parse_mode"] == "HTML"
    persistent_manager.send_message.assert_not_awaited()


async def test_agentic_start_escapes_html_in_name(agentic_settings, deps):
    """Names with HTML-special characters are escaped safely."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    update = MagicMock()
    update.effective_user.first_name = "A<B>&C"
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {}

    await orchestrator.agentic_start(update, context)

    call_kwargs = update.message.reply_text.call_args
    text = call_kwargs.args[0]
    # HTML-special characters should be escaped
    assert "A&lt;B&gt;&amp;C" in text
    # parse_mode is HTML
    assert call_kwargs.kwargs.get("parse_mode") == "HTML"


async def test_agentic_text_logs_failure_on_error(agentic_settings, deps):
    """Failed Claude runs are logged with success=False."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    persistent_manager = MagicMock()
    persistent_manager.send_message = AsyncMock(side_effect=Exception("Claude broke"))
    persistent_manager.get_client_state = MagicMock(return_value=None)

    audit_logger = AsyncMock()
    audit_logger.log_command = AsyncMock()

    update = MagicMock()
    update.effective_user.id = 123
    update.effective_chat.id = 456
    update.message.text = "do something"
    update.message.message_id = 1
    update.message.message_thread_id = None
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock()

    progress_msg = AsyncMock()
    progress_msg.delete = AsyncMock()
    update.message.reply_text.return_value = progress_msg

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": agentic_settings,
        "persistent_manager": persistent_manager,
        "storage": None,
        "rate_limiter": None,
        "audit_logger": audit_logger,
    }

    await orchestrator.agentic_text(update, context)

    # Audit logged with success=False
    audit_logger.log_command.assert_called_once()
    call_kwargs = audit_logger.log_command.call_args
    assert call_kwargs.kwargs["success"] is False


# --- _redact_secrets / _summarize_tool_input tests ---


class TestRedactSecrets:
    """Ensure sensitive substrings are redacted from Bash command summaries."""

    def test_safe_command_unchanged(self):
        assert (
            _redact_secrets("poetry run pytest tests/ -v")
            == "poetry run pytest tests/ -v"
        )

    def test_anthropic_api_key_redacted(self):
        key = "sk-ant-api03-abc123def456ghi789jkl012mno345"
        cmd = f"ANTHROPIC_API_KEY={key}"
        result = _redact_secrets(cmd)
        assert key not in result
        assert "***" in result

    def test_sk_key_redacted(self):
        cmd = "curl -H 'Authorization: Bearer sk-1234567890abcdefghijklmnop'"
        result = _redact_secrets(cmd)
        assert "sk-1234567890abcdefghijklmnop" not in result
        assert "***" in result

    def test_github_pat_redacted(self):
        cmd = "git clone https://ghp_abcdefghijklmnop1234@github.com/user/repo"
        result = _redact_secrets(cmd)
        assert "ghp_abcdefghijklmnop1234" not in result
        assert "***" in result

    def test_aws_key_redacted(self):
        cmd = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        result = _redact_secrets(cmd)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "***" in result

    def test_flag_token_redacted(self):
        cmd = "mycli --token=supersecretvalue123"
        result = _redact_secrets(cmd)
        assert "supersecretvalue123" not in result
        assert "--token=" in result or "--token" in result

    def test_password_env_redacted(self):
        cmd = "PASSWORD=MyS3cretP@ss! ./run.sh"
        result = _redact_secrets(cmd)
        assert "MyS3cretP@ss!" not in result
        assert "***" in result

    def test_bearer_token_redacted(self):
        cmd = "curl -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig'"
        result = _redact_secrets(cmd)
        assert "eyJhbGciOiJIUzI1NiJ9.payload.sig" not in result

    def test_connection_string_redacted(self):
        cmd = "psql postgresql://admin:secret_password@db.host:5432/mydb"
        result = _redact_secrets(cmd)
        assert "secret_password" not in result

    def test_summarize_tool_input_bash_redacts(self, agentic_settings, deps):
        """_summarize_tool_input applies redaction to Bash commands."""
        result = _summarize_tool_input(
            "Bash",
            {"command": "curl --token=mysupersecrettoken123 https://api.example.com"},
        )
        assert "mysupersecrettoken123" not in result
        assert "***" in result

    def test_summarize_tool_input_non_bash_unchanged(self, agentic_settings, deps):
        """Non-Bash tools don't go through redaction."""
        result = _summarize_tool_input(
            "Read", {"file_path": "/home/user/.env"}
        )
        assert result == ".env"


# --- Typing heartbeat tests ---


class TestTypingHeartbeat:
    """Verify typing indicator stays alive independently of stream events."""

    async def test_heartbeat_sends_typing_action(self, agentic_settings, deps):
        """Heartbeat sends typing actions at the configured interval."""
        from src.bot.delivery import start_typing_heartbeat

        chat = AsyncMock()
        chat.send_action = AsyncMock()

        heartbeat = start_typing_heartbeat(chat, interval=0.05)

        # Let the heartbeat fire a few times
        await asyncio.sleep(0.2)
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass

        # Should have been called multiple times
        assert chat.send_action.call_count >= 2
        chat.send_action.assert_called_with("typing")

    async def test_heartbeat_cancels_cleanly(self, agentic_settings, deps):
        """Cancelling the heartbeat task does not raise."""
        from src.bot.delivery import start_typing_heartbeat

        chat = AsyncMock()
        heartbeat = start_typing_heartbeat(chat, interval=0.05)

        heartbeat.cancel()
        # Should not raise
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass

        assert heartbeat.cancelled() or heartbeat.done()

    async def test_heartbeat_survives_send_action_errors(self, agentic_settings, deps):
        """Heartbeat keeps running even if send_action raises."""
        chat = AsyncMock()
        call_count = [0]

        async def flaky_send_action(action: str) -> None:
            call_count[0] += 1
            if call_count[0] <= 2:
                raise Exception("Network error")

        chat.send_action = flaky_send_action

        from src.bot.delivery import start_typing_heartbeat

        heartbeat = start_typing_heartbeat(chat, interval=0.05)

        await asyncio.sleep(0.3)
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass

        # Should have called send_action more than 2 times (survived errors)
        assert call_count[0] >= 3

    async def test_stream_callback_independent_of_typing(self, agentic_settings, deps):
        """Stream callback no longer sends typing — that's the heartbeat's job."""
        from src.bot.stream_handler import make_stream_callback

        progress_msg = AsyncMock()
        tool_log: list = []  # type: ignore[type-arg]
        callback = make_stream_callback(
            agentic_settings,
            verbose_level=1,
            progress_msg=progress_msg,
            tool_log=tool_log,
            start_time=0.0,
        )
        assert callback is not None

        # Verify the callback signature doesn't accept a 'chat' parameter
        # (typing is no longer handled by the stream callback)
        import inspect

        sig = inspect.signature(make_stream_callback)
        assert "chat" not in sig.parameters


async def test_group_thread_mode_rejects_non_forum_chat(group_thread_settings, deps):
    """Strict thread mode rejects updates outside configured forum chat."""
    orchestrator = MessageOrchestrator(group_thread_settings, deps)

    project_threads_manager = MagicMock()
    project_threads_manager.guidance_message.return_value = "Use project thread"
    deps["project_threads_manager"] = project_threads_manager

    called = {"value": False}

    async def dummy_handler(update, context):
        called["value"] = True

    wrapped = orchestrator._inject_deps(dummy_handler)

    update = MagicMock()
    update.effective_chat.id = -1002222222
    update.effective_message.reply_text = AsyncMock()
    update.callback_query = None

    context = MagicMock()
    context.bot_data = {}
    context.user_data = {}

    await wrapped(update, context)

    assert called["value"] is False
    update.effective_message.reply_text.assert_called_once()


async def test_thread_mode_loads_and_persists_thread_state(group_thread_settings, deps):
    """Thread mode loads per-thread context and writes updates back."""
    orchestrator = MessageOrchestrator(group_thread_settings, deps)

    project_path = group_thread_settings.approved_directory / "project_a"
    project = SimpleNamespace(
        slug="project_a",
        name="Project A",
        absolute_path=project_path,
    )

    project_threads_manager = MagicMock()
    project_threads_manager.resolve_project = AsyncMock(return_value=project)
    project_threads_manager.guidance_message.return_value = "Use project thread"
    deps["project_threads_manager"] = project_threads_manager

    async def dummy_handler(update, context):
        assert context.user_data["claude_session_id"] == "old-session"
        context.user_data["claude_session_id"] = "new-session"

    wrapped = orchestrator._inject_deps(dummy_handler)

    update = MagicMock()
    update.effective_chat.id = -1001234567890
    update.effective_message.message_thread_id = 777
    update.effective_message.reply_text = AsyncMock()
    update.callback_query = None

    context = MagicMock()
    context.bot_data = {}
    context.user_data = {
        "thread_state": {
            "-1001234567890:777": {
                "current_directory": str(project_path),
                "claude_session_id": "old-session",
            }
        }
    }

    await wrapped(update, context)

    assert (
        context.user_data["thread_state"]["-1001234567890:777"]["claude_session_id"]
        == "new-session"
    )


async def test_sync_threads_bypasses_thread_gate(group_thread_settings, deps):
    """sync_threads command bypasses strict thread routing gate."""
    orchestrator = MessageOrchestrator(group_thread_settings, deps)

    called = {"value": False}

    async def sync_threads(update, context):
        called["value"] = True

    project_threads_manager = MagicMock()
    project_threads_manager.guidance_message.return_value = "Use project thread"
    deps["project_threads_manager"] = project_threads_manager

    wrapped = orchestrator._inject_deps(sync_threads)

    update = MagicMock()
    update.effective_chat.id = -1002222222
    update.effective_message.reply_text = AsyncMock()
    update.callback_query = None

    context = MagicMock()
    context.bot_data = {}
    context.user_data = {}

    await wrapped(update, context)

    assert called["value"] is True


async def test_private_mode_start_bypasses_thread_gate(private_thread_settings, deps):
    """Private mode allows /start outside topics."""
    orchestrator = MessageOrchestrator(private_thread_settings, deps)
    called = {"value": False}

    async def start_command(update, context):
        called["value"] = True

    project_threads_manager = MagicMock()
    project_threads_manager.guidance_message.return_value = "Use project topic"
    deps["project_threads_manager"] = project_threads_manager

    wrapped = orchestrator._inject_deps(start_command)

    update = MagicMock()
    update.effective_chat.type = "private"
    update.effective_chat.id = 12345
    update.effective_chat.is_forum = False
    update.effective_message.reply_text = AsyncMock()
    update.callback_query = None

    context = MagicMock()
    context.bot_data = {}
    context.user_data = {}

    await wrapped(update, context)

    assert called["value"] is True
    project_threads_manager.resolve_project.assert_not_called()


async def test_private_mode_start_inside_topic_uses_thread_context(
    private_thread_settings, deps
):
    """/start in private topic should load mapped thread context."""
    orchestrator = MessageOrchestrator(private_thread_settings, deps)
    project_path = private_thread_settings.approved_directory / "project_a"
    project = SimpleNamespace(
        slug="project_a",
        name="Project A",
        absolute_path=project_path,
    )
    project_threads_manager = MagicMock()
    project_threads_manager.resolve_project = AsyncMock(return_value=project)
    project_threads_manager.guidance_message.return_value = "Use project topic"
    deps["project_threads_manager"] = project_threads_manager

    captured = {"dir": None}

    async def start_command(update, context):
        captured["dir"] = context.user_data.get("current_directory")

    wrapped = orchestrator._inject_deps(start_command)

    update = MagicMock()
    update.effective_chat.type = "private"
    update.effective_chat.id = 12345
    update.effective_message.message_thread_id = 777
    update.effective_message.reply_text = AsyncMock()
    update.callback_query = None

    context = MagicMock()
    context.bot_data = {}
    context.user_data = {
        "thread_state": {
            "12345:777": {
                "current_directory": str(project_path),
                "claude_session_id": "old",
            }
        }
    }

    await wrapped(update, context)

    project_threads_manager.resolve_project.assert_awaited_once_with(12345, 777)
    assert captured["dir"] == project_path


async def test_private_mode_rejects_help_outside_topics(private_thread_settings, deps):
    """Private mode rejects non-allowed commands outside mapped topics."""
    orchestrator = MessageOrchestrator(private_thread_settings, deps)
    called = {"value": False}

    async def help_command(update, context):
        called["value"] = True

    project_threads_manager = MagicMock()
    project_threads_manager.guidance_message.return_value = "Use project topic"
    deps["project_threads_manager"] = project_threads_manager

    wrapped = orchestrator._inject_deps(help_command)

    update = MagicMock()
    update.effective_chat.type = "private"
    update.effective_chat.id = 12345
    update.effective_chat.is_forum = False
    update.effective_message.message_thread_id = None
    update.effective_message.direct_messages_topic = None
    update.effective_message.reply_text = AsyncMock()
    update.callback_query = None

    context = MagicMock()
    context.bot_data = {}
    context.user_data = {}

    await wrapped(update, context)

    assert called["value"] is False
    update.effective_message.reply_text.assert_called_once()


# --- Message queuing (ceq) tests ---


class TestEnqueueMessage:
    """Verify _enqueue_message queues and sends placeholder."""

    async def test_enqueue_sends_placeholder_and_stores(self, agentic_settings, deps):
        orchestrator = MessageOrchestrator(agentic_settings, deps)

        placeholder_msg = MagicMock()
        placeholder_msg.message_id = 999

        update = MagicMock()
        update.message.reply_text = AsyncMock(return_value=placeholder_msg)

        await orchestrator._enqueue_message("key1", "hello", update)

        # Placeholder sent
        update.message.reply_text.assert_awaited_once()
        text = update.message.reply_text.call_args[0][0]
        assert "Queued" in text

        # Stored in queue
        assert len(orchestrator._message_queues["key1"]) == 1
        qm = orchestrator._message_queues["key1"][0]
        assert qm.text == "hello"
        assert qm.placeholder_message_id == 999

    async def test_enqueue_appends_multiple(self, agentic_settings, deps):
        orchestrator = MessageOrchestrator(agentic_settings, deps)

        placeholder = MagicMock()
        placeholder.message_id = 100

        update = MagicMock()
        update.message.reply_text = AsyncMock(return_value=placeholder)

        await orchestrator._enqueue_message("key1", "first", update)
        await orchestrator._enqueue_message("key1", "second", update)

        assert len(orchestrator._message_queues["key1"]) == 2
        assert orchestrator._message_queues["key1"][0].text == "first"
        assert orchestrator._message_queues["key1"][1].text == "second"


class TestCombineQueuedMessages:
    """Verify _combine_queued_messages formats correctly."""

    def test_single_message(self):
        from src.bot.orchestrator import QueuedMessage

        qm = QueuedMessage(text="do the thing", sent_at=1710300000.0)
        result = MessageOrchestrator._combine_queued_messages([qm])

        assert "queued during your previous turn" in result
        assert "do the thing" in result
        assert "UTC" in result

    def test_multiple_messages(self):
        from src.bot.orchestrator import QueuedMessage

        msgs = [
            QueuedMessage(text="first thought", sent_at=1710300000.0),
            QueuedMessage(text="second thought", sent_at=1710300005.0),
        ]
        result = MessageOrchestrator._combine_queued_messages(msgs)

        assert "2 messages were queued" in result
        assert "first thought" in result
        assert "second thought" in result


class TestDrainQueue:
    """Verify _drain_queue sends combined messages to Claude."""

    async def test_drain_empty_queue_is_noop(self, agentic_settings, deps):
        orchestrator = MessageOrchestrator(agentic_settings, deps)

        update = MagicMock()
        context = MagicMock()
        context.bot_data = {"persistent_manager": MagicMock()}

        # No queue entries — should return immediately
        await orchestrator._drain_queue("key1", update, context)

    async def test_drain_sends_combined_to_claude(self, agentic_settings, deps):
        orchestrator = MessageOrchestrator(agentic_settings, deps)

        from src.bot.orchestrator import QueuedMessage

        orchestrator._message_queues["key1"] = [
            QueuedMessage(
                text="queued msg",
                sent_at=1710300000.0,
                placeholder_message_id=50,
            ),
        ]

        mock_response = MagicMock()
        mock_response.session_id = "drain-session"
        mock_response.content = "Got your queued message"
        mock_response.tools_used = []
        mock_response.is_interrupted = False
        mock_response.context_window = None
        mock_response.total_input_tokens = None

        persistent_manager = MagicMock()
        persistent_manager.send_message = AsyncMock(return_value=mock_response)

        progress_msg = AsyncMock()
        progress_msg.delete = AsyncMock()

        update = MagicMock()
        update.effective_chat.id = 456
        update.effective_chat.send_action = AsyncMock()
        update.effective_chat.delete_message = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=progress_msg)

        context = MagicMock()
        context.user_data = {}
        context.bot_data = {"persistent_manager": persistent_manager}

        await orchestrator._drain_queue("key1", update, context)

        # Placeholder deleted
        update.effective_chat.delete_message.assert_awaited_once_with(50)

        # Claude was called with the combined prompt
        persistent_manager.send_message.assert_awaited_once()
        prompt = persistent_manager.send_message.call_args.kwargs["prompt"]
        assert "queued msg" in prompt
        assert "queued during your previous turn" in prompt

        # Progress message edited to final state
        progress_msg.edit_text.assert_called()
        final_text = progress_msg.edit_text.call_args[0][0]
        assert "\u2705" in final_text

        # Queue is now empty
        assert "key1" not in orchestrator._message_queues

    async def test_drain_requeues_on_race(self, agentic_settings, deps):
        """If send_message returns None (race), re-queue the combined prompt."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)

        from src.bot.orchestrator import QueuedMessage

        orchestrator._message_queues["key1"] = [
            QueuedMessage(text="lost msg", sent_at=1710300000.0),
        ]

        persistent_manager = MagicMock()
        persistent_manager.send_message = AsyncMock(return_value=None)

        progress_msg = AsyncMock()
        progress_msg.delete = AsyncMock()

        update = MagicMock()
        update.effective_chat.delete_message = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=progress_msg)

        context = MagicMock()
        context.user_data = {}
        context.bot_data = {"persistent_manager": persistent_manager}

        await orchestrator._drain_queue("key1", update, context)

        # Message was re-queued
        assert len(orchestrator._message_queues["key1"]) == 1
        assert "lost msg" in orchestrator._message_queues["key1"][0].text


class TestAgenticTextQueuesWhenBusy:
    """Verify agentic_text queues instead of injecting when client is busy."""

    async def test_busy_client_queues_message(self, agentic_settings, deps):
        orchestrator = MessageOrchestrator(agentic_settings, deps)

        persistent_manager = MagicMock()
        persistent_manager.get_client_state = MagicMock(return_value="busy")

        placeholder_msg = MagicMock()
        placeholder_msg.message_id = 42

        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 456
        update.message.text = "follow up thought"
        update.message.message_id = 2
        update.message.message_thread_id = None
        update.message.chat.send_action = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=placeholder_msg)

        context = MagicMock()
        context.user_data = {}
        context.bot_data = {
            "settings": agentic_settings,
            "persistent_manager": persistent_manager,
            "storage": None,
            "rate_limiter": None,
            "audit_logger": None,
        }

        await orchestrator.agentic_text(update, context)

        # Should NOT have called send_message (queued instead)
        persistent_manager.send_message.assert_not_called()

        # Should have queued
        state_key = orchestrator._state_key(update)
        assert len(orchestrator._message_queues[state_key]) == 1
        assert orchestrator._message_queues[state_key][0].text == "follow up thought"

    async def test_draining_client_queues_message(self, agentic_settings, deps):
        orchestrator = MessageOrchestrator(agentic_settings, deps)

        persistent_manager = MagicMock()
        persistent_manager.get_client_state = MagicMock(return_value="draining")

        placeholder_msg = MagicMock()
        placeholder_msg.message_id = 43

        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 456
        update.message.text = "another thought"
        update.message.message_id = 3
        update.message.message_thread_id = None
        update.message.chat.send_action = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=placeholder_msg)

        context = MagicMock()
        context.user_data = {}
        context.bot_data = {
            "settings": agentic_settings,
            "persistent_manager": persistent_manager,
            "storage": None,
            "rate_limiter": None,
            "audit_logger": None,
        }

        await orchestrator.agentic_text(update, context)

        persistent_manager.send_message.assert_not_called()
        state_key = orchestrator._state_key(update)
        assert len(orchestrator._message_queues[state_key]) == 1


class TestActivityLifecycle:
    """Verify status message lifecycle: Working -> Done/Failed/Stalled."""

    async def test_error_shows_failed_status(self, agentic_settings, deps):
        """When Claude errors, progress message shows Failed."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)

        persistent_manager = MagicMock()
        persistent_manager.send_message = AsyncMock(
            side_effect=Exception("Claude broke")
        )
        persistent_manager.get_client_state = MagicMock(return_value=None)

        progress_msg = AsyncMock()
        progress_msg.edit_text = AsyncMock()

        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 456
        update.message.text = "do something"
        update.message.message_id = 1
        update.message.message_thread_id = None
        update.message.chat.send_action = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=progress_msg)

        context = MagicMock()
        context.user_data = {}
        context.bot_data = {
            "settings": agentic_settings,
            "persistent_manager": persistent_manager,
            "storage": None,
            "rate_limiter": None,
            "audit_logger": None,
        }

        await orchestrator.agentic_text(update, context)

        # Progress message should show failed, not be deleted
        progress_msg.edit_text.assert_called()
        final_text = progress_msg.edit_text.call_args[0][0]
        assert "\u274c" in final_text  # ❌ Failed

    async def test_stall_callback_passed_to_persistent_manager(
        self, agentic_settings, deps
    ):
        """send_message receives a stall_callback."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)

        mock_response = MagicMock()
        mock_response.session_id = "session-abc"
        mock_response.content = "response"
        mock_response.tools_used = []
        mock_response.is_interrupted = False
        mock_response.context_window = None
        mock_response.total_input_tokens = None

        persistent_manager = MagicMock()
        persistent_manager.send_message = AsyncMock(return_value=mock_response)
        persistent_manager.get_client_state = MagicMock(return_value=None)

        progress_msg = AsyncMock()
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 456
        update.message.text = "test"
        update.message.message_id = 1
        update.message.message_thread_id = None
        update.message.chat.send_action = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=progress_msg)

        context = MagicMock()
        context.user_data = {}
        context.bot_data = {
            "settings": agentic_settings,
            "persistent_manager": persistent_manager,
            "storage": None,
            "rate_limiter": None,
            "audit_logger": None,
        }

        await orchestrator.agentic_text(update, context)

        # stall_callback should have been passed
        call_kwargs = persistent_manager.send_message.call_args.kwargs
        assert "stall_callback" in call_kwargs
        assert callable(call_kwargs["stall_callback"])

    async def test_no_timeout_on_send_message(self, agentic_settings, deps):
        """send_message is NOT wrapped in wait_for — turns can run indefinitely."""
        orchestrator = MessageOrchestrator(agentic_settings, deps)

        mock_response = MagicMock()
        mock_response.session_id = "long-session"
        mock_response.content = "done after a long time"
        mock_response.tools_used = []
        mock_response.is_interrupted = False
        mock_response.context_window = None
        mock_response.total_input_tokens = None

        persistent_manager = MagicMock()
        persistent_manager.send_message = AsyncMock(return_value=mock_response)
        persistent_manager.get_client_state = MagicMock(return_value=None)

        progress_msg = AsyncMock()
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 456
        update.message.text = "do a big task"
        update.message.message_id = 1
        update.message.message_thread_id = None
        update.message.chat.send_action = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=progress_msg)

        context = MagicMock()
        context.user_data = {}
        context.bot_data = {
            "settings": agentic_settings,
            "persistent_manager": persistent_manager,
            "storage": None,
            "rate_limiter": None,
            "audit_logger": None,
        }

        await orchestrator.agentic_text(update, context)

        # send_message called directly (not via wait_for)
        persistent_manager.send_message.assert_awaited_once()
        # Stall detection is via callback, not timeout
        call_kwargs = persistent_manager.send_message.call_args.kwargs
        assert callable(call_kwargs["stall_callback"])
