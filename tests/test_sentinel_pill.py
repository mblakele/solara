"""Sentinel device pill coloring.

Sentinel is special: red when active, grey when off, and never shows
pending-effect colors.
"""
from __future__ import annotations


def _render(devices: dict, effects: list, sentinel_names: list | None = None) -> str:
    """Render _metrics.html with custom devices, effects, and sentinels."""
    from flask import render_template
    import app as app_mod

    from tests.test_app import realistic_metrics

    metrics = realistic_metrics()
    nbc = metrics["devices"][0]["nbc"]
    nbc = dict(nbc, QH1=dict(nbc["QH1"], predicted_wh=-800.0))
    metrics = {"devices": [dict(metrics["devices"][0], nbc=nbc)]}
    load_management = {
        "last_cycle_result": {"diagnostics": {"hysteresis_wh": 50.0}},
        "state": {"devices": devices, "pending_effects": effects},
    }
    if sentinel_names is not None:
        load_management["sentinel_names"] = sentinel_names
    with app_mod.app.test_request_context():
        return render_template(
            "_metrics.html",
            metrics=metrics,
            load_management=load_management,
            freshness=None,
        )


def test_sentinel_on_renders_red_not_green() -> None:
    """Active sentinel pill uses the red sentinel class, not green on."""
    html = _render(
        devices={"guard": {"actual_state": True, "current_amps": None}},
        effects=[],
        sentinel_names=["guard"],
    )
    assert "device-pill--sentinel-on" in html
    assert ">guard</li>" in html


def test_sentinel_off_renders_grey() -> None:
    """Inactive sentinel pill stays grey like any off device."""
    html = _render(
        devices={"guard": {"actual_state": False, "current_amps": None}},
        effects=[],
        sentinel_names=["guard"],
    )
    assert "device-pill--off" in html
    assert "device-pill--sentinel-on" not in html


def test_sentinel_ignores_pending_effects() -> None:
    """A pending effect for a sentinel never changes its pill color."""
    html = _render(
        devices={"guard": {"actual_state": True, "current_amps": None}},
        effects=[{"device_name": "guard", "action": "turn_off", "target_amps": None}],
        sentinel_names=["guard"],
    )
    assert "device-pill--sentinel-on" in html
    assert "device-pill--pending-off" not in html
    assert "device-pill--pending-on" not in html


def test_sentinel_css_is_red() -> None:
    """style.css defines the sentinel-on pill with the error (red) color."""
    from pathlib import Path

    css = Path("static/style.css").read_text()
    assert ".device-pill--sentinel-on" in css


def test_sentinel_filtered_from_payload_pending_effects() -> None:
    """_build_load_management_payload strips sentinel pending effects."""
    from types import SimpleNamespace

    import app as app_mod

    class FakeState:
        """Minimal StateTracker stub exposing to_dict."""

        def to_dict(self) -> dict:
            return {
                "devices": {"guard": {"actual_state": True}},
                "pending_effects": [
                    {"device_name": "guard", "action": "turn_off"},
                    {"device_name": "pool_pump", "action": "turn_on"},
                ],
            }

    lm = SimpleNamespace(
        enabled=True,
        dry_run=True,
        target_wh=-50,
        nbc_device="test",
        state=FakeState(),
        sentinel_names=frozenset({"guard"}),
        config_interval_secs=30,
    )
    payload = app_mod._build_load_management_payload(lm=lm)
    assert payload["sentinel_names"] == ["guard"]
    remaining = [e["device_name"] for e in payload["state"]["pending_effects"]]
    assert "guard" not in remaining
    assert "pool_pump" in remaining
