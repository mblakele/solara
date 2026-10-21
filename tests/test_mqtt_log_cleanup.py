"""Log hygiene for mqtt_telemetry: no per-call snapshot spam, no module prefix.

Regression test for the duplicated log lines observed in production::

    [DEBUG] mqtt_telemetry mqtt_telemetry: snapshot fields present: [...]
    [DEBUG] mqtt_telemetry mqtt_telemetry: snapshot fields present: [...]

Two problems: ``tesla_state_from_snapshot()`` logged every call (and it is
called twice per load-management cycle — pending-check display refresh plus
async-phase fetch — so one snapshot produced two lines), and every message
repeated the module name even though the formatter already emits
``%(name)s`` (``mqtt_telemetry mqtt_telemetry: ...``).
"""

from __future__ import annotations

import logging


def _snapshot() -> dict:
    return {
        "ChargeAmps": 5.0,
        "ChargeState": "Charging",
        "DetailedChargeState": "DetailedChargeStateCharging",
        "Location": {"latitude": 37.0, "longitude": -122.0},
    }


def test_snapshot_parse_emits_no_per_call_debug(caplog):
    """Parsing one snapshot must not log per-call (the twice-per-cycle spam)."""
    from mqtt_telemetry import tesla_state_from_snapshot

    with caplog.at_level(logging.DEBUG, logger="mqtt_telemetry"):
        caplog.clear()
        state = tesla_state_from_snapshot(_snapshot())
    assert state is not None
    assert not [
        r for r in caplog.records if "snapshot fields present" in r.getMessage()
    ], "tesla_state_from_snapshot logged per-call snapshot spam"


def test_no_module_prefix_in_messages(caplog):
    """Log messages must not repeat the logger name (formatter emits it)."""
    import mqtt_telemetry as mt
    from mqtt_telemetry import tesla_state_from_snapshot

    with mt._telemetry_lock:
        mt._telemetry_state.clear()
        mt._field_update_at.clear()
    mt._telemetry_warned_empty = False

    class _Msg:
        topic = "telemetry/ChargeState"
        payload = b'"Charging"'

    with caplog.at_level(logging.DEBUG, logger="mqtt_telemetry"):
        caplog.clear()
        mt.on_message(None, None, _Msg())
        # Exercise the None-returning warning path (no DetailedChargeState,
        # uncorroborated amps) plus a successful parse.
        tesla_state_from_snapshot({"ChargeAmps": 3.0})
        tesla_state_from_snapshot(_snapshot())
    prefixed = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("mqtt_telemetry:")
        or r.getMessage().startswith("mqtt_telemetry.")
    ]
    assert not prefixed, f"messages repeat logger name: {prefixed}"
