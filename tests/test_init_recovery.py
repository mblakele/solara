"""Init-failure recovery for _get_load_manager (plan subtask 2.6, fixes R6).

A single failed LoadManager init (e.g. devices.json observed mid-write
during deploy) must not disable load management until process restart:
retries use exponential backoff instead of latching forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import app as app_mod


@pytest.fixture(autouse=True)
def reset_init_state():
    """Reset init-retry state around each test."""
    app_mod._state.load_manager = None
    app_mod._state.load_manager_init_failed = False
    app_mod._state.load_manager_init_attempts = 0
    app_mod._state.load_manager_next_init_retry_at = None
    yield
    app_mod._state.load_manager = None
    app_mod._state.load_manager_init_failed = False
    app_mod._state.load_manager_init_attempts = 0
    app_mod._state.load_manager_next_init_retry_at = None


class TestInitBackoffCurve:
    """_lm_init_backoff_secs: exponential, capped."""

    def test_doubles_from_base(self):
        assert app_mod._lm_init_backoff_secs(1) == 30.0
        assert app_mod._lm_init_backoff_secs(2) == 60.0
        assert app_mod._lm_init_backoff_secs(3) == 120.0

    def test_caps_at_max(self):
        assert app_mod._lm_init_backoff_secs(5) == 480.0
        assert app_mod._lm_init_backoff_secs(6) == 600.0
        assert app_mod._lm_init_backoff_secs(20) == 600.0


class TestInitRecovery:
    def test_transient_failure_retries_and_recovers(
        self, monkeypatch
    ):
        """First init fails, a later call retries and succeeds."""
        monkeypatch.setattr(app_mod, "_LM_INIT_RETRY_BASE_SECS", 0.0)
        calls: list[int] = []
        manager = MagicMock()

        def factory(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("devices.json read mid-write")
            return manager

        with patch("load_manager.LoadManager", side_effect=factory):
            first = app_mod._get_load_manager()
            assert first is None
            assert app_mod._state.load_manager_init_failed is True

            second = app_mod._get_load_manager()

        assert second is manager
        assert len(calls) == 2
        assert app_mod._state.load_manager_init_failed is False
        assert app_mod._state.load_manager_init_attempts == 0
        assert app_mod._state.load_manager_next_init_retry_at is None

    def test_backoff_blocks_immediate_retry(self, monkeypatch):
        """Inside the backoff window, calls return None without retrying."""
        monkeypatch.setattr(app_mod, "_LM_INIT_RETRY_BASE_SECS", 30.0)
        calls: list[int] = []

        def factory(*args, **kwargs):
            calls.append(1)
            raise RuntimeError("boom")

        with patch("load_manager.LoadManager", side_effect=factory):
            assert app_mod._get_load_manager() is None
            assert app_mod._get_load_manager() is None
        assert len(calls) == 1, "retry attempted before backoff elapsed"

        # Expire the backoff manually -> the next call retries.
        app_mod._state.load_manager_next_init_retry_at = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        with patch("load_manager.LoadManager", side_effect=factory):
            assert app_mod._get_load_manager() is None
        assert len(calls) == 2

    def test_failure_schedules_future_retry(self):
        """A failure records an attempt count and a future retry time."""
        with patch(
            "load_manager.LoadManager", side_effect=RuntimeError("boom")
        ):
            assert app_mod._get_load_manager() is None

        assert app_mod._state.load_manager_init_attempts == 1
        next_at = app_mod._state.load_manager_next_init_retry_at
        assert next_at is not None
        assert next_at > datetime.now(timezone.utc)

    def test_success_resets_attempt_counter(self, monkeypatch):
        """After recovery, the attempt counter is back to zero."""
        monkeypatch.setattr(app_mod, "_LM_INIT_RETRY_BASE_SECS", 0.0)
        calls: list[int] = []
        manager = MagicMock()

        def factory(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return manager

        with patch("load_manager.LoadManager", side_effect=factory):
            app_mod._get_load_manager()
            app_mod._get_load_manager()

        assert app_mod._state.load_manager_init_attempts == 0
