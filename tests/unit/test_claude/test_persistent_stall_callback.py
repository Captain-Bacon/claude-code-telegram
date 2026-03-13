"""Tests for stall_callback support in PersistentClientManager."""

import asyncio
import time
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Capture real sleep before any patching
_real_sleep = asyncio.sleep

from src.claude.persistent import (
    PersistentClientEntry,
    PersistentClientManager,
    PersistentResponse,
    TurnContext,
)
from src.claude.sdk_integration import ClaudeSDKManager


def _make_turn(
    stall_callback: Optional[Any] = None, **kwargs: Any
) -> TurnContext:
    """Create a TurnContext with defaults, including stall_callback support."""
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    defaults = dict(
        prompt="test",
        stream_callback=None,
        stall_callback=stall_callback,
        response_future=future,
        started_at=time.time(),
        messages=[],
        result_raw_data={},
    )
    defaults.update(kwargs)
    return TurnContext(**defaults)


class TestStallCallbackParameter:
    """Test that stall_callback parameter is accepted and stored."""

    @pytest.mark.asyncio
    async def test_send_message_accepts_stall_callback(self) -> None:
        """Test that send_message accepts stall_callback parameter."""
        manager = MagicMock(spec=ClaudeSDKManager)
        config = MagicMock()
        persistent_mgr = PersistentClientManager(manager, config)

        # Mock the client creation
        with patch.object(
            persistent_mgr, "_create_client", new_callable=AsyncMock
        ) as mock_create:
            mock_client = MagicMock()
            mock_entry = PersistentClientEntry(
                client=mock_client,
                state="idle",
                state_key="test:1",
                working_directory=Path("/tmp"),
            )
            mock_create.return_value = mock_entry

            with patch.object(mock_client, "query", new_callable=AsyncMock):
                with patch.object(mock_client, "read_stream", new_callable=AsyncMock) as mock_read:
                    mock_read.side_effect = StopAsyncIteration

                    stall_callback = MagicMock()

                    # Should not raise
                    try:
                        task = asyncio.create_task(
                            persistent_mgr.send_message(
                                state_key="test:1",
                                prompt="hello",
                                working_directory=Path("/tmp"),
                                stall_callback=stall_callback,
                            )
                        )
                        # Let it process the lock and create the turn
                        await asyncio.sleep(0.01)
                        # Cancel to avoid hanging
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                    except TypeError as e:
                        pytest.fail(f"send_message should accept stall_callback: {e}")

    def test_stall_callback_stored_in_turn_context(self) -> None:
        """Test that stall_callback is stored in TurnContext."""
        stall_callback = MagicMock()
        turn = _make_turn(stall_callback=stall_callback)
        assert turn.stall_callback is stall_callback

    def test_none_stall_callback_default(self) -> None:
        """Test that stall_callback defaults to None."""
        turn = _make_turn()
        assert turn.stall_callback is None


class TestStallCallbackInvocation:
    """Test that stall_callback is invoked at the right times."""

    @pytest.mark.asyncio
    async def test_stall_callback_invoked_on_watchdog_timeout(self) -> None:
        """Test that stall_callback is invoked when watchdog detects silence."""
        manager = MagicMock(spec=ClaudeSDKManager)
        config = MagicMock()
        persistent_mgr = PersistentClientManager(manager, config)

        callback_event = asyncio.Event()
        captured_kwargs: Dict[str, Any] = {}

        async def tracking_callback(**kwargs: Any) -> None:
            captured_kwargs.update(kwargs)
            callback_event.set()

        turn = _make_turn(stall_callback=tracking_callback)
        turn.started_at = time.time() - 35
        turn.last_message_at = time.time() - 35

        mock_client = MagicMock()
        entry = PersistentClientEntry(
            client=mock_client,
            state="busy",
            state_key="test:1",
            working_directory=Path("/tmp"),
            current_turn=turn,
        )

        async def yielding_sleep(_: float) -> None:
            await _real_sleep(0)

        with patch("src.claude.persistent.asyncio.sleep", side_effect=yielding_sleep):
            watchdog_task = asyncio.create_task(
                persistent_mgr._turn_watchdog(entry)
            )
            try:
                await asyncio.wait_for(callback_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pytest.fail("Stall callback was not invoked")
            finally:
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass

        assert "silence_seconds" in captured_kwargs
        assert "total_elapsed_seconds" in captured_kwargs
        assert "cli_alive" in captured_kwargs
        assert "is_dead" in captured_kwargs

    @pytest.mark.asyncio
    async def test_stall_callback_receives_correct_kwargs(self) -> None:
        """Test that stall_callback receives correct keyword arguments."""
        manager = MagicMock(spec=ClaudeSDKManager)
        config = MagicMock()
        persistent_mgr = PersistentClientManager(manager, config)

        callback_event = asyncio.Event()
        captured_kwargs: Dict[str, Any] = {}

        async def tracking_callback(**kwargs: Any) -> None:
            captured_kwargs.update(kwargs)
            callback_event.set()

        turn = _make_turn(stall_callback=tracking_callback)
        now = time.time()
        turn.started_at = now - 40
        turn.last_message_at = now - 35

        mock_client = MagicMock()
        entry = PersistentClientEntry(
            client=mock_client,
            state="busy",
            state_key="test:1",
            working_directory=Path("/tmp"),
            current_turn=turn,
        )

        async def yielding_sleep(_: float) -> None:
            await _real_sleep(0)

        with patch("src.claude.persistent.asyncio.sleep", side_effect=yielding_sleep):
            watchdog_task = asyncio.create_task(
                persistent_mgr._turn_watchdog(entry)
            )
            try:
                await asyncio.wait_for(callback_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pytest.fail("Stall callback was not invoked")
            finally:
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass

        assert isinstance(captured_kwargs["silence_seconds"], float)
        assert isinstance(captured_kwargs["total_elapsed_seconds"], float)
        assert isinstance(captured_kwargs["cli_alive"], bool)
        assert captured_kwargs["is_dead"] is False
        assert captured_kwargs["silence_seconds"] >= 5.0
        assert captured_kwargs["total_elapsed_seconds"] >= 40.0

    @pytest.mark.asyncio
    async def test_async_stall_callback_is_awaited(self) -> None:
        """Test that async stall_callback is properly awaited."""
        manager = MagicMock(spec=ClaudeSDKManager)
        config = MagicMock()
        persistent_mgr = PersistentClientManager(manager, config)

        callback_event = asyncio.Event()

        async def async_callback(**kwargs: Any) -> None:
            callback_event.set()

        turn = _make_turn(stall_callback=async_callback)
        turn.started_at = time.time() - 35
        turn.last_message_at = time.time() - 35

        mock_client = MagicMock()
        entry = PersistentClientEntry(
            client=mock_client,
            state="busy",
            state_key="test:1",
            working_directory=Path("/tmp"),
            current_turn=turn,
        )

        async def yielding_sleep(_: float) -> None:
            await _real_sleep(0)

        with patch("src.claude.persistent.asyncio.sleep", side_effect=yielding_sleep):
            watchdog_task = asyncio.create_task(
                persistent_mgr._turn_watchdog(entry)
            )
            try:
                await asyncio.wait_for(callback_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pytest.fail("Async stall callback was not awaited")
            finally:
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass

    @pytest.mark.asyncio
    async def test_stall_callback_error_does_not_crash_watchdog(self) -> None:
        """Test that stall_callback errors don't crash the watchdog."""
        manager = MagicMock(spec=ClaudeSDKManager)
        config = MagicMock()
        persistent_mgr = PersistentClientManager(manager, config)

        call_count = 0

        def broken_callback(**kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("Callback broken")

        turn = _make_turn(stall_callback=broken_callback)
        turn.started_at = time.time() - 35
        turn.last_message_at = time.time() - 35

        mock_client = MagicMock()
        entry = PersistentClientEntry(
            client=mock_client,
            state="busy",
            state_key="test:1",
            working_directory=Path("/tmp"),
            current_turn=turn,
        )

        async def yielding_sleep(_: float) -> None:
            await _real_sleep(0)

        with patch("src.claude.persistent.asyncio.sleep", side_effect=yielding_sleep):
            watchdog_task = asyncio.create_task(
                persistent_mgr._turn_watchdog(entry)
            )
            # Let it run a couple of iterations
            await _real_sleep(0.05)
            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass

        # Callback was invoked (and raised), but watchdog survived
        assert call_count >= 1


class TestStallCallbackOnClientDeath:
    """Test stall_callback invocation when client dies."""

    @pytest.mark.asyncio
    async def test_stall_callback_invoked_on_client_death(self) -> None:
        """Test that stall_callback is invoked when client dies."""
        manager = MagicMock(spec=ClaudeSDKManager)
        config = MagicMock()
        persistent_mgr = PersistentClientManager(manager, config)

        stall_callback = AsyncMock()
        turn = _make_turn(stall_callback=stall_callback)

        mock_client = MagicMock()
        entry = PersistentClientEntry(
            client=mock_client,
            state="busy",
            state_key="test:1",
            working_directory=Path("/tmp"),
            current_turn=turn,
        )

        persistent_mgr._clients["test:1"] = entry

        # Trigger client death handler
        error = RuntimeError("Client process exited")
        await persistent_mgr._handle_client_death(entry, error)

        # Verify callback was invoked
        stall_callback.assert_called_once()
        call_kwargs = stall_callback.call_args[1]
        assert call_kwargs["silence_seconds"] == 0
        assert "total_elapsed_seconds" in call_kwargs
        assert call_kwargs["cli_alive"] is False
        assert call_kwargs.get("is_dead") is True

    @pytest.mark.asyncio
    async def test_stall_callback_receives_is_dead_flag(self) -> None:
        """Test that stall_callback receives is_dead=True on client death."""
        manager = MagicMock(spec=ClaudeSDKManager)
        config = MagicMock()
        persistent_mgr = PersistentClientManager(manager, config)

        stall_callback = AsyncMock()
        turn = _make_turn(stall_callback=stall_callback)

        mock_client = MagicMock()
        entry = PersistentClientEntry(
            client=mock_client,
            state="busy",
            state_key="test:1",
            working_directory=Path("/tmp"),
            current_turn=turn,
        )

        persistent_mgr._clients["test:1"] = entry

        error = RuntimeError("Process died")
        await persistent_mgr._handle_client_death(entry, error)

        stall_callback.assert_called_once()
        call_kwargs = stall_callback.call_args[1]
        assert call_kwargs["is_dead"] is True

    @pytest.mark.asyncio
    async def test_client_death_without_stall_callback(self) -> None:
        """Test that client death works fine with no stall_callback."""
        manager = MagicMock(spec=ClaudeSDKManager)
        config = MagicMock()
        persistent_mgr = PersistentClientManager(manager, config)

        turn = _make_turn(stall_callback=None)

        mock_client = MagicMock()
        entry = PersistentClientEntry(
            client=mock_client,
            state="busy",
            state_key="test:1",
            working_directory=Path("/tmp"),
            current_turn=turn,
        )

        persistent_mgr._clients["test:1"] = entry

        # Should not raise
        error = RuntimeError("Process died")
        await persistent_mgr._handle_client_death(entry, error)

        # Verify response was set
        assert entry.current_turn is not None
        assert entry.current_turn.response_future.done()
        result = entry.current_turn.response_future.result()
        assert result.is_error is True


class TestStallCallbackWithoutTurn:
    """Test that operations work when no turn exists."""

    @pytest.mark.asyncio
    async def test_watchdog_handles_no_current_turn(self) -> None:
        """Test that watchdog gracefully exits when turn completes."""
        manager = MagicMock(spec=ClaudeSDKManager)
        config = MagicMock()
        persistent_mgr = PersistentClientManager(manager, config)

        mock_client = MagicMock()
        entry = PersistentClientEntry(
            client=mock_client,
            state="busy",
            state_key="test:1",
            working_directory=Path("/tmp"),
            current_turn=None,
        )

        # Watchdog should immediately return if no turn
        watchdog_task = asyncio.create_task(persistent_mgr._turn_watchdog(entry))
        await asyncio.sleep(0.01)

        # Should complete without hanging
        try:
            await asyncio.wait_for(watchdog_task, timeout=1.0)
        except asyncio.TimeoutError:
            pytest.fail("Watchdog should exit when current_turn is None")
