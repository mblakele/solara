"""D.2: notification policy lives in one Notifier with one queue."""

from __future__ import annotations

from datetime import datetime, time, timezone
from unittest.mock import MagicMock

import pytest

from clock import FakeClock
from load_models import PendingEffect
from notifier import Notifier

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class _Owner:
    """Minimal LoadManager surface the Notifier reads at call time."""

    def __init__(self, sender, devices=None, alert_on_auth=True):
        self.telegram_sender = sender
        self._telegram_devices = devices
        self._telegram_alert_on_auth_error = alert_on_auth
        self._clock = FakeClock(NOW)


def _sender(configured: bool = True) -> MagicMock:
    sender = MagicMock()
    sender.is_configured = configured
    return sender


def _turn_on(device: str = "pool_pump") -> PendingEffect:
    return PendingEffect(
        device_name=device,
        action="turn_on",
        timestamp=NOW,
        data_point_at=NOW,
        power_watts=1500.0,
        target_amps=None,
    )


class TestSurplusPolicy:
    def test_queues_when_sender_and_whitelist_match(self):
        owner = _Owner(_sender(), devices={"pool_pump": {"turn_on"}})
        notifier = Notifier(owner)

        assert notifier.surplus(
            [_turn_on()], -2000.0, -500.0, dry_run=False, now=NOW
        )
        assert len(notifier.events) == 1

    def test_dry_run_skips_surplus(self):
        owner = _Owner(_sender(), devices={"pool_pump": {"turn_on"}})
        notifier = Notifier(owner)
        assert not notifier.surplus(
            [_turn_on()], -2000.0, -500.0, dry_run=True, now=NOW
        )
        assert notifier.events == []

    def test_missing_whitelist_skips_surplus(self):
        notifier = Notifier(_Owner(_sender(), devices=None))
        assert not notifier.surplus(
            [_turn_on()], -2000.0, -500.0, dry_run=False, now=NOW
        )
        assert notifier.events == []

    def test_non_matching_action_skips_surplus(self):
        notifier = Notifier(_Owner(_sender(), devices={"other": {"turn_on"}}))
        assert not notifier.surplus(
            [_turn_on()], -2000.0, -500.0, dry_run=False, now=NOW
        )
        assert notifier.events == []

    def test_no_actions_skips_surplus(self):
        notifier = Notifier(_Owner(_sender(), devices={"pool_pump": {"turn_on"}}))
        assert not notifier.surplus([], -2000.0, -500.0, dry_run=False, now=NOW)


class TestBypassPolicy:
    def test_auth_error_bypasses_whitelist_and_dry_run(self):
        notifier = Notifier(_Owner(_sender(), devices=None))
        assert notifier.auth_error("auth failed", login_url=None, now=NOW)
        assert len(notifier.events) == 1

    def test_auth_error_respects_alert_switch(self):
        notifier = Notifier(_Owner(_sender(), alert_on_auth=False))
        assert not notifier.auth_error("auth failed", login_url=None, now=NOW)
        assert notifier.events == []

    def test_stale_fetch_and_out_of_range_bypass_gates(self):
        notifier = Notifier(_Owner(_sender(), devices=None))
        assert notifier.stale_data("stale for 400s", now=NOW)
        assert notifier.fetch_fatal("VueAuthenticationError", now=NOW)
        assert notifier.pipeline_stall("pipeline stalled", now=NOW)

        class _Alert:
            channel_num = 1
            chart_start = NOW
            data_start = NOW
            count = 5

        assert notifier.drift(_Alert(), now=NOW)
        assert notifier.out_of_range(
            "heater", (time(6, 0), time(8, 0)), now=NOW
        )
        assert len(notifier.events) == 5

    def test_unconfigured_sender_skips_everything(self):
        notifier = Notifier(_Owner(_sender(configured=False), devices=None))
        assert not notifier.auth_error("auth failed", login_url=None, now=NOW)
        assert not notifier.stale_data("stale", now=NOW)
        assert notifier.events == []


class TestQueue:
    def test_drain_returns_and_clears(self):
        notifier = Notifier(_Owner(_sender(), devices=None))
        notifier.stale_data("stale", now=NOW)
        events = notifier.drain()
        assert len(events) == 1
        assert notifier.events == []
