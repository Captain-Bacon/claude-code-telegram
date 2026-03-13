"""Tests for context window token extraction in persistent client."""

from dataclasses import field
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from src.claude.persistent import PersistentClientEntry, PersistentClientManager, TurnContext


def _make_turn(event_loop: Any = None, **kwargs: Any) -> TurnContext:
    """Create a TurnContext with defaults."""
    import asyncio

    future: asyncio.Future = asyncio.get_event_loop().create_future()
    defaults = dict(
        prompt="test",
        stream_callback=None,
        response_future=future,
        started_at=1000.0,
        messages=[],
        result_raw_data={},
    )
    defaults.update(kwargs)
    return TurnContext(**defaults)


def _make_stream_event(event_type: str, usage: Optional[Dict[str, int]] = None) -> MagicMock:
    """Create a mock StreamEvent with the given event type and usage."""
    from claude_agent_sdk.types import StreamEvent

    msg = MagicMock(spec=StreamEvent)
    event: Dict[str, Any] = {"type": event_type}
    if event_type == "message_start" and usage is not None:
        event["message"] = {"usage": usage}
    msg.event = event
    return msg


class TestLastInputTokensTracking:
    """Tests that TurnContext.last_input_tokens tracks the most recent API call."""

    def test_last_input_tokens_default(self) -> None:
        turn = _make_turn()
        assert turn.last_input_tokens == 0

    def test_last_input_tokens_set_from_collector_logic(self) -> None:
        """Simulates what the collector does: overwrite last_input_tokens per API call."""
        turn = _make_turn()

        # First API call: 10k input + 5k cache read
        turn.last_input_tokens = 10_000 + 5_000
        assert turn.last_input_tokens == 15_000

        # Second API call: 12k input + 5k cache read (context grew)
        turn.last_input_tokens = 12_000 + 5_000
        assert turn.last_input_tokens == 17_000  # Overwritten, not accumulated


class TestBuildPersistentResponseContextWindow:
    """Tests for context window extraction in _build_persistent_response."""

    @pytest.fixture
    def manager(self) -> PersistentClientManager:
        sdk_manager = MagicMock()
        config = MagicMock()
        return PersistentClientManager(sdk_manager, config)

    @pytest.fixture
    def entry(self) -> PersistentClientEntry:
        return PersistentClientEntry(
            client=MagicMock(),
            state="busy",
            state_key="test:1",
            working_directory=MagicMock(),
        )

    def test_context_window_from_model_usage(self, manager: PersistentClientManager, entry: PersistentClientEntry) -> None:
        """Context window extracted from modelUsage in raw_data."""
        turn = _make_turn(last_input_tokens=50_000)
        result = MagicMock()
        result.result = "test response"
        result.total_cost_usd = 0.01
        result.session_id = "sess-1"
        result.stop_reason = "end_turn"

        raw_data = {
            "modelUsage": {
                "claude-opus-4-20250514": {"contextWindow": 200_000}
            }
        }

        resp = manager._build_persistent_response(entry, turn, result, raw_data)
        assert resp.context_window == 200_000
        assert resp.total_input_tokens == 50_000

    def test_context_window_fallback_when_none(self, manager: PersistentClientManager, entry: PersistentClientEntry) -> None:
        """Falls back to 200k when modelUsage is missing."""
        turn = _make_turn(last_input_tokens=50_000)
        result = MagicMock()
        result.result = "test response"
        result.total_cost_usd = 0.01
        result.session_id = "sess-1"
        result.stop_reason = "end_turn"

        resp = manager._build_persistent_response(entry, turn, result, {})
        assert resp.context_window == 200_000

    def test_tokens_from_last_input_tokens_field(self, manager: PersistentClientManager, entry: PersistentClientEntry) -> None:
        """Uses turn.last_input_tokens (collector-tracked) as primary source."""
        turn = _make_turn(last_input_tokens=85_000)
        result = MagicMock()
        result.result = "test"
        result.total_cost_usd = 0.0
        result.session_id = "sess-1"
        result.stop_reason = "end_turn"

        resp = manager._build_persistent_response(entry, turn, result, {})
        assert resp.total_input_tokens == 85_000

    def test_tokens_fallback_to_message_walk(self, manager: PersistentClientManager, entry: PersistentClientEntry) -> None:
        """Falls back to walking messages when last_input_tokens is 0."""
        event = _make_stream_event("message_start", {
            "input_tokens": 30_000,
            "cache_read_input_tokens": 10_000,
            "cache_creation_input_tokens": 5_000,
        })
        turn = _make_turn(last_input_tokens=0, messages=[event])
        result = MagicMock()
        result.result = "test"
        result.total_cost_usd = 0.0
        result.session_id = "sess-1"
        result.stop_reason = "end_turn"

        resp = manager._build_persistent_response(entry, turn, result, {})
        assert resp.total_input_tokens == 45_000

    def test_no_tokens_available(self, manager: PersistentClientManager, entry: PersistentClientEntry) -> None:
        """Returns None when no token data is available."""
        turn = _make_turn(last_input_tokens=0, messages=[])
        result = MagicMock()
        result.result = "test"
        result.total_cost_usd = 0.0
        result.session_id = "sess-1"
        result.stop_reason = "end_turn"

        resp = manager._build_persistent_response(entry, turn, result, {})
        assert resp.total_input_tokens is None
