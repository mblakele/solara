"""Devices pill order: sentinels first, plugs by priority, vehicles last.

Red-phase: _build_load_management_payload currently preserves tracker
insertion order, so the dashboard Devices grid jumps around. The payload
must sort state.devices as sentinel plugs, then regular plugs in
priority order (higher number first, name tie-break), then vehicles.
"""

from unittest.mock import MagicMock

from load_models import PlugConfig


def _make_lm(
    device_names: list[str],
    plugs: dict[str, PlugConfig],
    sentinel_names: set[str] | frozenset[str],
) -> MagicMock:
    """Build a mock LoadManager with devices in scrambled insertion order."""
    lm = MagicMock()
    lm.enabled = True
    lm.dry_run = False
    lm.target_wh = -9
    lm.nbc_device = "test_nbc"
    lm.config_interval_secs = 30
    lm.sentinel_names = sentinel_names
    lm.plugs = plugs
    lm.state.to_dict.return_value = {
        "devices": {name: {"actual_state": False} for name in device_names},
        "pending_effects": [],
    }
    return lm


def test_devices_sorted_sentinel_plugs_vehicle() -> None:
    """Sentinel first, plugs by descending priority, tesla last."""
    from app import _build_load_management_payload

    plugs = {
        "sentinel": PlugConfig(name="sentinel", accessory_id="s1", sentinel=True),
        "ecoflow": PlugConfig(name="ecoflow", accessory_id="e1", power_watts=260.0, priority=1),
        "jackery": PlugConfig(name="jackery", accessory_id="j1", power_watts=260.0, priority=5),
        "water heater": PlugConfig(
            name="water heater", accessory_id="w1", power_watts=4857.0, priority=10
        ),
    }
    lm = _make_lm(
        ["tesla", "ecoflow", "sentinel", "jackery", "water heater"],
        plugs,
        {"sentinel"},
    )
    result = _build_load_management_payload(lm)
    assert list(result["state"]["devices"].keys()) == [
        "sentinel",
        "water heater",
        "jackery",
        "ecoflow",
        "tesla",
    ]


def test_devices_tie_break_by_name() -> None:
    """Equal priorities fall back to alphabetical order."""
    from app import _build_load_management_payload

    plugs = {
        "bravo": PlugConfig(name="bravo", accessory_id="b1", power_watts=100.0),
        "alpha": PlugConfig(name="alpha", accessory_id="a1", power_watts=100.0),
    }
    lm = _make_lm(["tesla", "bravo", "alpha"], plugs, set())
    result = _build_load_management_payload(lm)
    assert list(result["state"]["devices"].keys()) == ["alpha", "bravo", "tesla"]


def test_devices_empty_unchanged() -> None:
    """Empty device dicts pass through without error."""
    from app import _build_load_management_payload

    lm = _make_lm([], {}, set())
    result = _build_load_management_payload(lm)
    assert result["state"]["devices"] == {}
