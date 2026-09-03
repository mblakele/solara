"""Tests for EffectStore (extracted from StateTracker)."""

from datetime import datetime, timedelta, timezone

from load_models import PendingEffect
from load_nbc import EffectStore, StateTracker

FIXED_NOW = datetime(2026, 5, 7, 15, 10, 0, tzinfo=timezone.utc)


def _plug(
    name: str = "plug",
    age_secs: int = 0,
    action: str = "turn_on",
    power_watts: float = 1000.0,
) -> PendingEffect:
    """Build a plug PendingEffect aged by age_secs."""
    ts = FIXED_NOW - timedelta(seconds=age_secs)
    return PendingEffect(
        device_name=name,
        action=action,  # type: ignore[arg-type]
        timestamp=ts,
        data_point_at=ts,
        power_watts=power_watts if action == "turn_on" else -power_watts,
    )


def test_add_and_snapshot_isolation():
    """Added effects appear in snapshots; snapshots are copies."""
    store = EffectStore()
    store.add(_plug(age_secs=5))
    snap = store.snapshot()
    assert len(snap) == 1
    snap.clear()
    assert len(store.snapshot()) == 1


def test_has_and_count_since_use_window_buffer():
    """Effects within window_secs before nbc_ts count as pending."""
    store = EffectStore(window_secs=30)
    nbc_ts = FIXED_NOW
    store.add(_plug(age_secs=15))  # 15s before nbc_ts, inside 30s buffer
    assert store.has_since(nbc_ts) is True
    assert store.count_since(nbc_ts) == 1


def test_has_since_false_when_old():
    """Effects older than the window do not count."""
    store = EffectStore(window_secs=30)
    nbc_ts = FIXED_NOW
    store.add(_plug(age_secs=120))
    assert store.has_since(nbc_ts) is False
    assert store.count_since(nbc_ts) == 0


def test_prune_needs_both_ages_old():
    """Prune only when BOTH wall-clock and data-point age exceed window."""
    store = EffectStore(window_secs=30)
    now = FIXED_NOW
    dp = FIXED_NOW
    store.add(_plug(name="old", age_secs=120))  # both ages old -> pruned
    store.add(_plug(name="recent", age_secs=5))  # fresh -> kept
    pruned = store.prune(data_point_at=dp, now=now)
    assert pruned == 1
    assert [e.device_name for e in store.snapshot()] == ["recent"]


def test_latest_for_returns_newest_match():
    """latest_for returns the most recent effect for a device."""
    store = EffectStore()
    store.add(_plug(name="tesla", age_secs=50, action="turn_on"))
    newest = _plug(name="tesla", age_secs=5, action="turn_off")
    store.add(newest)
    store.add(_plug(name="other", age_secs=1))
    assert store.latest_for("tesla") is newest
    assert store.latest_for("missing") is None


def test_clear_tesla_set_amps_preserves_on_off():
    """clear_tesla_set_amps removes only tesla set_amps effects."""
    store = EffectStore()
    store.add(
        PendingEffect(
            device_name="tesla",
            action="set_amps",
            timestamp=FIXED_NOW,
            data_point_at=FIXED_NOW,
            power_watts=0,
            target_amps=16,
        )
    )
    store.add(_plug(name="tesla", action="turn_off"))
    store.add(_plug(name="heater"))
    store.clear_tesla_set_amps()
    remaining = {(e.device_name, e.action) for e in store.snapshot()}
    assert remaining == {("tesla", "turn_off"), ("heater", "turn_on")}


def test_state_tracker_pending_effects_list_compat():
    """StateTracker.pending_effects still supports direct list ops + assignment."""
    tracker = StateTracker()
    tracker.pending_effects.append(_plug(name="a"))
    tracker.pending_effects.extend([_plug(name="b")])
    assert len(tracker.pending_effects) == 2
    tracker.pending_effects.clear()
    assert len(tracker.pending_effects) == 0
    tracker.pending_effects = [_plug(name="c")]
    assert [e.device_name for e in tracker.pending_effects] == ["c"]
    # add_effect path still works and is visible via the property
    tracker.add_effect(_plug(name="d"))
    assert {e.device_name for e in tracker.pending_effects} == {"c", "d"}
