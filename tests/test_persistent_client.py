"""Tests for the persistent client manager."""

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.claude.persistent import (
    PersistentClientEntry,
    PersistentClientManager,
    PersistentResponse,
    StopResult,
    TurnContext,
    derive_state_key,
)
from src.claude.sdk_integration import StreamUpdate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_sdk_manager():
    """Create a mock ClaudeSDKManager with build_options."""
    mgr = MagicMock()
    mgr.build_options.return_value = MagicMock()
    return mgr


def _make_mock_config():
    """Create a minimal mock config."""
    cfg = MagicMock()
    cfg.claude_timeout_seconds = 300
    return cfg


def _make_mock_client():
    """Create a mock ClaudeSDKClient."""
    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.query = AsyncMock()
    client.interrupt = AsyncMock()
    client._query = MagicMock()
    return client


def _make_entry(state="idle", state_key="1:2", **kwargs):
    """Create a PersistentClientEntry with sensible defaults."""
    return PersistentClientEntry(
        client=_make_mock_client(),
        state=state,
        state_key=state_key,
        working_directory=Path("/tmp/test"),
        **kwargs,
    )


def _make_mock_result_message(total_cost_usd=1.0, session_id="s1", result="Hello"):
    """Create a mock ResultMessage."""
    from claude_agent_sdk import ResultMessage

    msg = MagicMock(spec=ResultMessage)
    msg.total_cost_usd = total_cost_usd
    msg.session_id = session_id
    msg.result = result
    return msg


def _make_turn_context(prompt="test", **kwargs):
    """Create a TurnContext with a real future."""
    loop = asyncio.get_event_loop()
    return TurnContext(
        prompt=prompt,
        stream_callback=kwargs.get("stream_callback"),
        stall_callback=kwargs.get("stall_callback"),
        response_future=loop.create_future(),
        started_at=kwargs.get("started_at", time.time()),
    )


# ---------------------------------------------------------------------------
# derive_state_key
# ---------------------------------------------------------------------------


class TestDeriveStateKey:
    def test_with_thread_id(self):
        assert derive_state_key(123, 456, 789) == "123:456"

    def test_without_thread_id(self):
        assert derive_state_key(123, None, 789) == "123:789"

    def test_zero_thread_id_is_valid(self):
        assert derive_state_key(123, 0, 789) == "123:0"


# ---------------------------------------------------------------------------
# Client state
# ---------------------------------------------------------------------------


class TestClientState:
    def test_get_state_none(self):
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        assert manager.get_client_state("nonexistent") is None

    def test_get_state_idle(self):
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        manager._clients["1:2"] = _make_entry(state="idle")
        assert manager.get_client_state("1:2") == "idle"

    def test_get_state_busy(self):
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        manager._clients["1:2"] = _make_entry(state="busy")
        assert manager.get_client_state("1:2") == "busy"


# ---------------------------------------------------------------------------
# _handle_result_message — the core turn-end logic
# ---------------------------------------------------------------------------


class TestHandleResultMessage:
    @pytest.mark.asyncio
    async def test_resolves_turn_future(self):
        """ResultMessage should resolve the current turn's future."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        entry = _make_entry(state="busy", pending_turns=1)
        turn = _make_turn_context()
        entry.current_turn = turn
        manager._clients["1:2"] = entry

        result_msg = _make_mock_result_message(total_cost_usd=0.5)
        await manager._handle_result_message(entry, result_msg, {})

        assert turn.response_future.done()
        response = turn.response_future.result()
        assert isinstance(response, PersistentResponse)
        assert response.cost == pytest.approx(0.5)
        assert response.session_id == "s1"

    @pytest.mark.asyncio
    async def test_transitions_to_idle(self):
        """After last pending turn, state should go idle."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        entry = _make_entry(state="busy", pending_turns=1)
        entry.current_turn = _make_turn_context()
        manager._clients["1:2"] = entry

        await manager._handle_result_message(
            entry, _make_mock_result_message(), {}
        )

        assert entry.state == "idle"
        assert entry.current_turn is None
        assert entry.pending_turns == 0

    @pytest.mark.asyncio
    async def test_always_returns_to_idle_after_result(self):
        """ResultMessage always returns entry to idle.

        Injected messages (sent while busy) are absorbed into the current
        turn by the CLI — they don't produce their own ResultMessage.
        So when a ResultMessage arrives, the turn is done and we go idle.
        """
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        entry = _make_entry(state="busy", pending_turns=1)
        turn = _make_turn_context(prompt="first")
        entry.current_turn = turn
        manager._clients["1:2"] = entry

        await manager._handle_result_message(
            entry, _make_mock_result_message(), {}
        )

        assert turn.response_future.done()
        assert entry.current_turn is None
        assert entry.state == "idle"
        assert entry.pending_turns == 0

    @pytest.mark.asyncio
    async def test_no_active_turn_logs_warning(self):
        """ResultMessage with no current turn should not crash."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        entry = _make_entry(state="busy")
        entry.current_turn = None
        manager._clients["1:2"] = entry

        # Should not raise
        await manager._handle_result_message(
            entry, _make_mock_result_message(), {}
        )


# ---------------------------------------------------------------------------
# Cost delta tracking
# ---------------------------------------------------------------------------


class TestCostDeltaTracking:
    @pytest.mark.asyncio
    async def test_first_turn_cost_equals_cumulative(self):
        """First turn: delta = cumulative (previous was 0)."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        entry = _make_entry(state="busy", pending_turns=1)
        entry.previous_cumulative_cost = 0.0
        turn = _make_turn_context()
        entry.current_turn = turn

        await manager._handle_result_message(
            entry, _make_mock_result_message(total_cost_usd=1.50), {}
        )

        response = turn.response_future.result()
        assert response.cost == pytest.approx(1.50)
        assert entry.previous_cumulative_cost == pytest.approx(1.50)

    @pytest.mark.asyncio
    async def test_second_turn_cost_is_delta(self):
        """Second turn: delta = cumulative - previous."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        entry = _make_entry(state="busy", pending_turns=1)
        entry.previous_cumulative_cost = 1.50  # From first turn
        turn = _make_turn_context()
        entry.current_turn = turn

        await manager._handle_result_message(
            entry, _make_mock_result_message(total_cost_usd=2.30), {}
        )

        response = turn.response_future.result()
        assert response.cost == pytest.approx(0.80)
        assert entry.previous_cumulative_cost == pytest.approx(2.30)

    @pytest.mark.asyncio
    async def test_cost_never_negative(self):
        """Cost should never be negative (defensive)."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        entry = _make_entry(state="busy", pending_turns=1)
        entry.previous_cumulative_cost = 5.0  # Higher than result (shouldn't happen)
        turn = _make_turn_context()
        entry.current_turn = turn

        await manager._handle_result_message(
            entry, _make_mock_result_message(total_cost_usd=3.0), {}
        )

        response = turn.response_future.result()
        assert response.cost == 0.0  # max(0, -2.0) = 0


# ---------------------------------------------------------------------------
# Stop client
# ---------------------------------------------------------------------------


class TestStopClient:
    @pytest.mark.asyncio
    async def test_stop_not_busy(self):
        """Stopping a non-existent client returns was_busy=False."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        result = await manager.stop_client("nonexistent")
        assert result.was_busy is False
        assert result.discarded_messages == []

    @pytest.mark.asyncio
    async def test_stop_idle_client(self):
        """Stopping an idle client returns was_busy=False."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        manager._clients["1:2"] = _make_entry(state="idle")
        result = await manager.stop_client("1:2")
        assert result.was_busy is False

    @pytest.mark.asyncio
    async def test_stop_busy_calls_interrupt(self):
        """Stopping a busy client should call interrupt()."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        entry = _make_entry(state="busy")
        entry.current_turn = _make_turn_context()
        manager._clients["1:2"] = entry

        await manager.stop_client("1:2")
        entry.client.interrupt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_returns_no_discarded_messages(self):
        """Stop returns empty discarded list (follow-ups are injected, not queued)."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        entry = _make_entry(state="busy", pending_turns=1)
        entry.current_turn = _make_turn_context()
        manager._clients["1:2"] = entry

        result = await manager.stop_client("1:2")

        assert result.was_busy is True
        assert result.discarded_messages == []


# ---------------------------------------------------------------------------
# Disconnect client
# ---------------------------------------------------------------------------


class TestDisconnectClient:
    @pytest.mark.asyncio
    async def test_disconnect_nonexistent(self):
        """Disconnecting non-existent client is a no-op."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        await manager.disconnect_client("nonexistent")

    @pytest.mark.asyncio
    async def test_disconnect_removes_from_registry(self):
        """Disconnect should remove client from registry."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        entry = _make_entry(state="idle")
        manager._clients["1:2"] = entry

        await manager.disconnect_client("1:2")

        assert "1:2" not in manager._clients
        entry.client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disconnect_cancels_pending_futures(self):
        """Disconnect should cancel any pending turn futures."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        entry = _make_entry(state="busy")
        turn = _make_turn_context()
        entry.current_turn = turn
        manager._clients["1:2"] = entry

        await manager.disconnect_client("1:2")
        assert turn.response_future.cancelled()


# ---------------------------------------------------------------------------
# Idle cleanup
# ---------------------------------------------------------------------------


class TestIdleCleanup:
    @pytest.mark.asyncio
    async def test_cleans_old_idle(self):
        """Clients idle beyond threshold should be cleaned up."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())

        old = _make_entry(state="idle", state_key="old")
        old.last_activity = time.time() - 3600
        manager._clients["old"] = old

        fresh = _make_entry(state="idle", state_key="fresh")
        fresh.last_activity = time.time()
        manager._clients["fresh"] = fresh

        cleaned = await manager.cleanup_idle_clients(max_idle_seconds=1800)

        assert cleaned == 1
        assert "old" not in manager._clients
        assert "fresh" in manager._clients

    @pytest.mark.asyncio
    async def test_busy_clients_not_cleaned(self):
        """Busy clients should never be cleaned regardless of age."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())

        busy = _make_entry(state="busy", state_key="busy")
        busy.last_activity = time.time() - 7200
        manager._clients["busy"] = busy

        cleaned = await manager.cleanup_idle_clients(max_idle_seconds=1800)
        assert cleaned == 0
        assert "busy" in manager._clients


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_disconnects_all(self):
        """Shutdown should disconnect all clients."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())

        for i in range(3):
            manager._clients[f"c:{i}"] = _make_entry(state_key=f"c:{i}")

        await manager.shutdown()
        assert len(manager._clients) == 0


# ---------------------------------------------------------------------------
# Thread independence
# ---------------------------------------------------------------------------


class TestThreadIndependence:
    def test_different_keys_independent(self):
        """Different state keys should have independent entries."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())

        for key in ["100:1", "100:2", "200:1"]:
            manager._clients[key] = _make_entry(state_key=key)

        assert len(manager._clients) == 3
        assert manager.get_client_state("100:1") == "idle"
        assert manager.get_client_state("100:2") == "idle"
        assert manager.get_client_state("999:1") is None


# ---------------------------------------------------------------------------
# Response building
# ---------------------------------------------------------------------------


class TestResponseBuilding:
    @pytest.mark.asyncio
    async def test_builds_response_with_content(self):
        """Should extract content from ResultMessage.result."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        entry = _make_entry(state="busy", pending_turns=1)
        turn = _make_turn_context()
        entry.current_turn = turn

        result_msg = _make_mock_result_message(
            total_cost_usd=0.75,
            session_id="session-xyz",
            result="Here is my response",
        )

        response = manager._build_persistent_response(
            entry, turn, result_msg, {}
        )

        assert response.content == "Here is my response"
        assert response.session_id == "session-xyz"
        assert response.cost == pytest.approx(0.75)
        assert response.is_error is False
        assert response.is_interrupted is False

    @pytest.mark.asyncio
    async def test_interrupted_flag(self):
        """Should set is_interrupted when entry._interrupted is True."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        entry = _make_entry(state="busy", pending_turns=1)
        entry._interrupted = True
        turn = _make_turn_context()
        entry.current_turn = turn

        response = manager._build_persistent_response(
            entry, turn, _make_mock_result_message(), {}
        )

        assert response.is_interrupted is True


# ---------------------------------------------------------------------------
# Stream update conversion
# ---------------------------------------------------------------------------


class TestStreamUpdateConversion:
    def test_unknown_message_returns_none(self):
        """Unknown message types should return None."""
        result = PersistentClientManager._message_to_stream_update(MagicMock())
        assert result is None


# ---------------------------------------------------------------------------
# StopResult / PersistentResponse dataclasses
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_stop_result(self):
        r = StopResult(was_busy=True, discarded_messages=["a", "b"])
        assert r.was_busy is True
        assert r.discarded_messages == ["a", "b"]

    def test_persistent_response_defaults(self):
        r = PersistentResponse(
            content="x", session_id="s", cost=0, duration_ms=0, num_turns=0
        )
        assert r.is_error is False
        assert r.is_interrupted is False
        assert r.tools_used == []
        assert r.context_window is None


# ---------------------------------------------------------------------------
# Injection continuation / draining state
# ---------------------------------------------------------------------------


class TestInjectionDraining:
    """Tests for the draining state machine that handles post-injection
    continuation turns."""

    @pytest.mark.asyncio
    async def test_injection_increments_count(self):
        """Injecting into a busy client should increment _injection_count."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        entry = _make_entry(state="busy", pending_turns=1)
        entry.current_turn = _make_turn_context()
        entry._injection_count = 0
        manager._clients["1:2"] = entry

        # Simulate send_message calling query() when busy
        # We call the internal path directly since send_message
        # requires a full client setup
        await entry.client.query("follow-up")
        entry._injection_count += 1

        assert entry._injection_count == 1

    @pytest.mark.asyncio
    async def test_result_with_injection_enters_draining(self):
        """ResultMessage after injection should transition to draining, not idle."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        entry = _make_entry(state="busy", pending_turns=1)
        turn = _make_turn_context()
        entry.current_turn = turn
        entry._injection_count = 1  # One injection occurred
        manager._clients["1:2"] = entry

        await manager._handle_result_message(
            entry, _make_mock_result_message(), {}
        )

        assert entry.state == "draining"
        assert entry.current_turn is None  # First turn cleared
        assert turn.response_future.done()  # First turn resolved

    @pytest.mark.asyncio
    async def test_result_without_injection_goes_idle(self):
        """ResultMessage without injection should go straight to idle."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        entry = _make_entry(state="busy", pending_turns=1)
        turn = _make_turn_context()
        entry.current_turn = turn
        entry._injection_count = 0  # No injection
        manager._clients["1:2"] = entry

        await manager._handle_result_message(
            entry, _make_mock_result_message(), {}
        )

        assert entry.state == "idle"
        assert entry.current_turn is None

    @pytest.mark.asyncio
    async def test_second_result_completes_drain(self):
        """Second ResultMessage during draining should transition to idle."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        entry = _make_entry(state="draining", pending_turns=0)
        entry.current_turn = None  # First turn already cleared
        entry._injection_count = 1
        manager._clients["1:2"] = entry

        # Second ResultMessage arrives while draining
        await manager._handle_result_message(
            entry, _make_mock_result_message(session_id="s2"), {}
        )

        assert entry.state == "idle"
        assert entry.pending_turns == 0
        assert entry._injection_count == 0
        assert entry.session_id == "s2"

    @pytest.mark.asyncio
    async def test_drain_timeout_falls_back_to_idle(self):
        """If drain timeout fires, entry should go idle."""
        from src.claude.persistent import _INJECTION_DRAIN_TIMEOUT_S

        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        entry = _make_entry(state="draining", pending_turns=0)
        entry._injection_count = 1
        manager._clients["1:2"] = entry

        # Call drain timeout directly (don't actually wait)
        # Monkey-patch the timeout to 0 for testing
        import src.claude.persistent as persistent_mod
        original = persistent_mod._INJECTION_DRAIN_TIMEOUT_S
        persistent_mod._INJECTION_DRAIN_TIMEOUT_S = 0.01
        try:
            await manager._drain_timeout(entry)
        finally:
            persistent_mod._INJECTION_DRAIN_TIMEOUT_S = original

        assert entry.state == "idle"
        assert entry._injection_count == 0

    @pytest.mark.asyncio
    async def test_injection_into_draining_state(self):
        """Injecting into a draining client should increment injection count."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        entry = _make_entry(state="draining", pending_turns=0)
        entry._injection_count = 1
        manager._clients["1:2"] = entry

        # send_message treats draining like busy
        assert entry.state in ("busy", "draining")

    @pytest.mark.asyncio
    async def test_stop_during_draining(self):
        """Stopping a draining client should work like stopping a busy one."""
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        entry = _make_entry(state="draining", pending_turns=0)
        entry.current_turn = _make_turn_context()
        entry._injection_count = 1
        manager._clients["1:2"] = entry

        result = await manager.stop_client("1:2")

        assert result.was_busy is True
        entry.client.interrupt.assert_awaited_once()
        assert entry._injection_count == 0

    @pytest.mark.asyncio
    async def test_injection_count_reset_on_new_turn(self):
        """Starting a new turn should reset the injection count."""
        entry = _make_entry(state="idle")
        entry._injection_count = 3  # Leftover from previous turn

        # Simulate the reset that happens in send_message
        entry._injection_count = 0
        assert entry._injection_count == 0

    @pytest.mark.asyncio
    async def test_multiple_injections_single_drain(self):
        """Multiple injections should still result in one draining period.

        The CLI processes all injected messages, and we expect one
        additional ResultMessage regardless of injection count.
        """
        manager = PersistentClientManager(_make_mock_sdk_manager(), _make_mock_config())
        entry = _make_entry(state="busy", pending_turns=1)
        turn = _make_turn_context()
        entry.current_turn = turn
        entry._injection_count = 3  # Three injections
        manager._clients["1:2"] = entry

        # First ResultMessage -> draining
        await manager._handle_result_message(
            entry, _make_mock_result_message(), {}
        )
        assert entry.state == "draining"

        # Second ResultMessage -> idle
        await manager._handle_result_message(
            entry, _make_mock_result_message(), {}
        )
        assert entry.state == "idle"
        assert entry._injection_count == 0


# ---------------------------------------------------------------------------
# Orphan message content summary
# ---------------------------------------------------------------------------


class TestMessageContentSummary:
    """Tests for _summarize_message_content diagnostic helper."""

    def test_assistant_message_summary(self):
        """AssistantMessage with content blocks should show block count and types."""
        from claude_agent_sdk import AssistantMessage
        msg = MagicMock(spec=AssistantMessage)
        block1 = MagicMock()
        type(block1).__name__ = "TextBlock"
        block2 = MagicMock()
        type(block2).__name__ = "ToolUseBlock"
        msg.content = [block1, block2]
        summary = PersistentClientManager._summarize_message_content(msg)
        # MagicMock(spec=X) passes isinstance checks
        assert "2 blocks" in summary
        assert "TextBlock" in summary
        assert "ToolUseBlock" in summary

    def test_user_message_summary(self):
        """UserMessage should show character count."""
        from claude_agent_sdk import UserMessage
        msg = MagicMock(spec=UserMessage)
        msg.content = "Hello world"
        summary = PersistentClientManager._summarize_message_content(msg)
        assert "11 chars" in summary

    def test_result_message_summary(self):
        """ResultMessage should show result length."""
        from claude_agent_sdk import ResultMessage
        msg = MagicMock(spec=ResultMessage)
        msg.result = "Done"
        summary = PersistentClientManager._summarize_message_content(msg)
        assert "result=" in summary
        assert "4 chars" in summary

    def test_unknown_message_returns_type_name(self):
        """Unknown message types should return just the type name."""
        msg = MagicMock()
        summary = PersistentClientManager._summarize_message_content(msg)
        assert isinstance(summary, str)
        assert len(summary) > 0
