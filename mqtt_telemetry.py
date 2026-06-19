"""MQTT-based Tesla fleet telemetry subscriber.

Subscribes to a local mosquitto broker fed by the fleet-telemetry container
and maintains thread-safe in-process state for the current vehicle snapshot.

Topics follow the pattern ``{topic_base}/{field}``, where field is one of:
- ``Location``              → ``{"latitude": float, "longitude": float}``
- ``ChargeState``           → string enum (e.g. ``"Charging"``, ``"Disconnected"``)
- ``ChargeAmps``            → float (amps currently drawn)
- ``DetailedChargeState``   → ``{"battery_level": float, "charge_limit_soc": int}``

Single-vehicle assumed — no per-VIN storage.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt

from load_controllers import _haversine_distance
from load_models import TeslaState

# Deferred import to avoid circular import with config_loader → device_config
import config


logger = logging.getLogger(__name__)

# === Module-level state ===

_telemetry_lock: threading.Lock = threading.Lock()
# Normalised fields received so far.
# Keys written by on_message: "Location", "ChargeState", "ChargeAmps",
# "DetailedChargeState"
_telemetry_state: dict[str, Any] = {}
_telemetry_warned_empty = False
# Wall-clock timestamp of the last update to each field.
_field_update_at: dict[str, datetime] = {}

# === Public API ===


def get_telemetry_snapshot() -> dict[str, Any]:
    """Return a thread-safe shallow copy of the current MQTT field state.

    Returns:
        Dict with any subset of ``Location``, ``ChargeState``, ``ChargeAmps``
        keys populated by the most recent MQTT messages.
    """
    with _telemetry_lock:
        return dict(_telemetry_state)


def has_telemetry() -> bool:
    """Return True once at least one MQTT message has been received.

    Returns:
        True if ``_telemetry_state`` is non-empty (i.e. at least one field
        has been received from the broker), False otherwise.
    """
    global _telemetry_warned_empty
    with _telemetry_lock:
        result = bool(_telemetry_state)
    if not result and not _telemetry_warned_empty:
        logger.warning("mqtt_telemetry: has_telemetry() is False — no MQTT messages received yet")
        _telemetry_warned_empty = True
    return result

def get_field_update_at(field: str) -> datetime | None:
    """Return the wall-clock timestamp when *field* was last updated.

    Args:
        field: The telemetry field name (e.g. ``"ChargeAmps"``).

    Returns:
        Datetime when the field was last received, or ``None`` if never.
    """
    with _telemetry_lock:
        return _field_update_at.get(field)


def on_message(client: Any, userdata: Any, msg: Any) -> None:  # noqa: ARG001
    """paho callback: parse incoming MQTT message and update state.

    Extracts the field name from the last topic segment and stores a
    normalised value in ``_telemetry_state`` under that key.

    Fleet-telemetry publishes per-field payloads in one of two formats:

    * Wrapped: ``{"value": <raw>, "createdAt": "..."}``
    * Raw: the value directly (string, number, or dict)

    For ``Location`` the raw value is ``{"latitude": float, "longitude": float}``.
    For ``ChargeState`` the raw value is a string enum.
    For ``ChargeAmps`` the raw value is a float.
    """
    try:
        topic: str = msg.topic
        field = topic.split("/")[-1]

        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("mqtt_telemetry: non-JSON payload on %s", topic)
            return

        # Unwrap fleet-telemetry's {"value": ..., "createdAt": ...} envelope.
        if isinstance(payload, dict) and "value" in payload:
            value = payload["value"]
        else:
            value = payload

        with _telemetry_lock:
            is_new = field not in _telemetry_state
            _telemetry_state[field] = value
            _field_update_at[field] = datetime.now(timezone.utc)

        if is_new:
            logger.info("mqtt_telemetry: first value for field %s = %r", field, value)
        else:
            logger.debug("mqtt_telemetry: %s = %r", field, value)

    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("mqtt_telemetry.on_message: unexpected error")


def check_fleet_telemetry_dotfile() -> None:
    """Warn if fleet-telemetry has never been provisioned.

    The dotfile is written by provisioning on success. Its absence is a
    strong signal that telemetry messages will never arrive regardless of
    whether the broker is reachable.
    """
    if _FLEET_TELEMETRY_DOTFILE.exists():
        import datetime
        mtime = datetime.datetime.fromtimestamp(
            _FLEET_TELEMETRY_DOTFILE.stat().st_mtime,
            tz=datetime.timezone.utc,
        )
        logger.info(
            "mqtt_telemetry: fleet-telemetry provisioned at %s (%s)",
            mtime.isoformat(), _FLEET_TELEMETRY_DOTFILE,
        )
    else:
        logger.warning(
            "mqtt_telemetry: fleet-telemetry dotfile not found (%s) — "
            "vehicle may not be provisioned; run --provision-fleet-telemetry",
            _FLEET_TELEMETRY_DOTFILE,
        )


def start_mqtt_subscriber(cfg: Any) -> None:
    """Connect to the MQTT broker and start a non-blocking subscriber loop.

    Reads broker connection details from ``cfg`` (a ``Config`` instance).
    Subscribes to ``{cfg.mqtt_topic_base}/#`` so all field sub-topics are
    received.  Runs the network loop in a daemon thread via ``loop_start()``.

    If the broker is unreachable the error is logged and the background
    thread keeps retrying (paho auto-reconnect disabled — caller can restart).

    Args:
        cfg: Application ``Config`` instance (must expose ``mqtt_host``,
            ``mqtt_port``, ``mqtt_topic_base``).
    """
    host = cfg.mqtt_host
    port = cfg.mqtt_port
    topic_base = cfg.mqtt_topic_base

    def _run() -> None:
        client = mqtt.Client()
        client.on_message = on_message

        def _on_connect(c: Any, userdata: Any, flags: Any, rc: int) -> None:  # noqa: ARG001
            if rc == 0:
                logger.info(
                    "mqtt_telemetry: connected to %s:%d, subscribing to %s/#",
                    host, port, topic_base,
                )
                c.subscribe(f"{topic_base}/#")
            else:
                logger.error(
                    "mqtt_telemetry: connection failed rc=%d host=%s port=%d"
                    " — check mqtt_host/mqtt_port config",
                    rc, host, port,
                )

        client.on_connect = _on_connect
        try:
            client.connect(host, port, keepalive=60)
            client.loop_forever()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception(
                "mqtt_telemetry: subscriber thread terminated host=%s port=%d",
                host, port,
            )

    check_fleet_telemetry_dotfile()
    t = threading.Thread(target=_run, name="mqtt-subscriber", daemon=True)
    t.start()
    logger.info(
        "mqtt_telemetry: subscriber thread started host=%s port=%d topic=%s/#",
        host, port, topic_base,
    )


def tesla_state_from_snapshot(
    snapshot: dict[str, Any],
) -> TeslaState | None:
    """Build a ``TeslaState`` from the current normalised MQTT snapshot.

    Returns ``None`` when neither ``DetailedChargeState`` nor a positive
    ``ChargeAmps`` value is present.

    When ``DetailedChargeState`` is available, the standard mapping applies:
    - ``"DetailedChargeStateCharging"``     → ``is_charging=True``,  ``plugged_in=True``
    - ``"DetailedChargeStateComplete"``     → ``is_charging=False``, ``plugged_in=True``
    - ``"DetailedChargeStateDisconnected"`` → ``is_charging=False``, ``plugged_in=False``

    When ``DetailedChargeState`` has not yet arrived via MQTT but
    ``ChargeAmps > 0`` is present, the charging state is inferred from
    the amps value (amps > 0 means charging is active).  This avoids
    returning stale cached state from an earlier REST fallback.

    ``at_home`` is computed via haversine from the ``Location`` field.
    ``current_amps`` comes from ``ChargeAmps`` (rounded to int).

    Args:
        snapshot: Dict from ``get_telemetry_snapshot()``.

    Returns:
        Populated ``TeslaState``, or ``None`` if insufficient data.
    """

    logger.debug(
        "mqtt_telemetry: snapshot fields present: %s",
        sorted(snapshot.keys()) if snapshot else "(empty)",
    )


    # ── Parse DetailedChargeState (if available) ─────────────────────────
    detailed_charge_state_raw = snapshot.get("DetailedChargeState")
    if detailed_charge_state_raw is not None:
        detailed_charge_state_str: str = (
            str(detailed_charge_state_raw)
            if not isinstance(detailed_charge_state_raw, dict)
            else str(detailed_charge_state_raw.get("value", ""))
        )
        is_charging = detailed_charge_state_str == "DetailedChargeStateCharging"
        plugged_in = (
            is_charging or
            detailed_charge_state_str == "DetailedChargeStateComplete" or
            detailed_charge_state_str != "DetailedChargeStateDisconnected"
        )
    else:
        # DetailedChargeState not yet received — try to infer from ChargeAmps.
        charge_amps_raw = snapshot.get("ChargeAmps")
        if charge_amps_raw is not None:
            try:
                raw_val = (
                    charge_amps_raw.get("value", charge_amps_raw)
                    if isinstance(charge_amps_raw, dict)
                    else charge_amps_raw
                )
                charge_val = round(float(raw_val))
            except (ValueError, TypeError):
                charge_val = None
            if charge_val is not None and charge_val > 0:
                # Amps > 0 means charging is active — infer state from
                # partial snapshot and skip remaining parsing.
                logger.info(
                    "mqtt_telemetry: inferred charging from ChargeAmps=%s "
                    "(DetailedChargeState not yet received)",
                    charge_val,
                )
                return TeslaState(
                    is_charging=True,
                    current_amps=charge_val,
                    plugged_in=True,
                    at_home=_compute_at_home_from_location(snapshot),
                )
        logger.warning(
            "mqtt_telemetry: tesla_state_from_snapshot returning None — "
            "DetailedChargeState not yet received (snapshot keys: %s)",
            sorted(snapshot.keys()),
        )
        return None

    # ChargeAmps
    current_amps: int | None = None
    charge_amps_raw = snapshot.get("ChargeAmps")
    if charge_amps_raw is not None:
        raw_val = (
            charge_amps_raw.get("value", None)
            if isinstance(charge_amps_raw, dict)
            else charge_amps_raw
        )
        if raw_val is not None:
            try:
                current_amps = round(float(raw_val))
            except (ValueError, TypeError):
                pass

    at_home = _compute_at_home_from_location(snapshot)
    return TeslaState(
        is_charging=is_charging,
        current_amps=current_amps,
        plugged_in=plugged_in,
        at_home=at_home,
    )


def _compute_at_home_from_location(snapshot: dict[str, Any]) -> bool:
    """Extract Location from snapshot and compute at_home.

    Args:
        snapshot: Telemetry snapshot dict.

    Returns:
        True if Location is present and within home radius, False otherwise.
    """
    location_raw = snapshot.get("Location")
    if location_raw is None:
        return False
    loc = (
        location_raw.get("value", location_raw)
        if isinstance(location_raw, dict)
        else None
    )
    if not isinstance(loc, dict):
        return False
    lat_raw = loc.get("latitude")
    lon_raw = loc.get("longitude")
    if lat_raw is None or lon_raw is None:
        return False
    try:
        return _compute_at_home(float(lat_raw), float(lon_raw))
    except (ValueError, TypeError, AttributeError):
        return False


def _compute_at_home(
    vehicle_lat: float,
    vehicle_lon: float,
) -> bool:
    """Compute whether the vehicle is at home based on GPS coordinates.

    Home coordinates are read from Config (env vars). The radius is read
    from devices.json for backwards compatibility (default 500 m).

    Args:
        vehicle_lat: Vehicle latitude.
        vehicle_lon: Vehicle longitude.

    Returns:
        True if the vehicle is within home_radius_m of the configured home.
    """
    import device_config as _dc

    cfg = config.Config()
    home_lat = cfg.tesla_home_lat
    home_lon = cfg.tesla_home_lon
    if home_lat is None or home_lon is None:
        return False
    home_radius_m: float = 500.0
    dc = _dc.get_tesla_config()
    if dc is not None and "home_radius_m" in dc:
        home_radius_m = float(dc["home_radius_m"])
    dist_m = _haversine_distance(vehicle_lat, vehicle_lon, home_lat, home_lon)
    return dist_m <= home_radius_m


# === Fleet telemetry provisioning dotfile ===

_FLEET_TELEMETRY_DOTFILE = Path.home() / ".solara-fleet-telemetry"
