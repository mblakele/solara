"""Tests for MQTT subscriber resilience (plan subtask 2.5, fixes R4).

The subscriber must survive a broker that is unreachable at boot: retry
forever with capped exponential backoff, track live connection state,
and expose it via ``is_connected()`` for /health and /api/v1/load/status.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import mqtt_telemetry

pytestmark = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning"
)


@pytest.fixture(autouse=True)
def reset_connection_state():
    """Reset module connection state around each test."""
    with mqtt_telemetry._telemetry_lock:
        mqtt_telemetry._connection_ok = False
    yield
    with mqtt_telemetry._telemetry_lock:
        mqtt_telemetry._connection_ok = False


def _cfg():
    return SimpleNamespace(
        mqtt_host="broker.local",
        mqtt_port=1883,
        mqtt_topic_base="tesla",
    )


class FakeClient:
    """Scriptable stand-in for paho.mqtt.client.Client."""

    connect_behavior = staticmethod(lambda n: None)
    kill_switch = threading.Event()
    loop_blocker = threading.Event()
    instances: list["FakeClient"] = []

    def __init__(self):
        self.on_message = None
        self.on_connect = None
        self.on_disconnect = None
        self.connect_calls = 0
        type(self).instances.append(self)

    def connect(self, host, port, keepalive=60):
        if FakeClient.kill_switch.is_set():
            raise SystemExit("test teardown")
        self.connect_calls += 1
        outcome = FakeClient.connect_behavior(self.connect_calls)
        if isinstance(outcome, BaseException):
            raise outcome
        if self.on_connect is not None:
            self.on_connect(self, None, None, 0)

    def loop_forever(self):
        FakeClient.loop_blocker.wait(timeout=30)

    def subscribe(self, topic):
        pass


@pytest.fixture()
def harness(monkeypatch):
    """Patch paho client, dotfile check, and sleep; collect artifacts."""
    FakeClient.instances = []
    FakeClient.kill_switch = threading.Event()
    FakeClient.loop_blocker = threading.Event()

    monkeypatch.setattr(
        mqtt_telemetry, "mqtt", SimpleNamespace(Client=FakeClient)
    )
    monkeypatch.setattr(
        mqtt_telemetry, "check_fleet_telemetry_dotfile", lambda: None
    )

    recorded_delays: list[float] = []
    finished = threading.Event()

    def fake_sleep(secs):
        if finished.is_set():
            # Teardown requested: park until process/kill ends the thread.
            FakeClient.kill_switch.wait()
            return
        recorded_delays.append(secs)

    monkeypatch.setattr(
        mqtt_telemetry,
        "_time_mod",
        SimpleNamespace(sleep=fake_sleep),
        raising=False,
    )

    threads: list[threading.Thread] = []

    def start():
        t = threading.Thread(
            target=mqtt_telemetry.start_mqtt_subscriber,
            args=(_cfg(),),
            daemon=True,
        )
        t.start()
        threads.append(t)
        return t

    yield SimpleNamespace(
        start=start,
        delays=recorded_delays,
        finished=finished,
        threads=threads,
    )

    finished.set()
    FakeClient.kill_switch.set()
    FakeClient.loop_blocker.set()
    for t in threads:
        t.join(timeout=5)


def test_connect_failure_retries_with_backoff(harness):
    """Broker down at boot: retries forever with capped exponential delay."""
    def behavior(attempt):
        if harness.finished.is_set():
            raise SystemExit("teardown")
        return ConnectionRefusedError("refused")

    FakeClient.connect_behavior = staticmethod(behavior)

    harness.start()

    # Wait until the subscriber has recorded its second backoff, then a
    # beat for the third attempt to instantiate.
    deadline = threading.Event()
    reached_second_backoff = False
    for _ in range(200):
        if len(harness.delays) >= 2:
            reached_second_backoff = True
            break
        deadline.wait(timeout=0.02)
    assert reached_second_backoff, (
        "subscriber gave up after initial connect failure — no retry loop; "
        f"delays={harness.delays}"
    )

    for _ in range(100):
        if len(FakeClient.instances) >= 3:
            break
        deadline.wait(timeout=0.02)

    assert len(FakeClient.instances) >= 3
    assert harness.delays[:2] == [2.0, 4.0], (
        f"expected capped exponential backoff [2, 4], got {harness.delays}"
    )
    harness.finished.set()


def test_recovery_flips_connection_state(harness):
    """Failed first connect retries; success flips is_connected() True."""
    attempts = {"n": 0}

    def behavior(_attempt):
        attempts["n"] += 1
        return (
            ConnectionRefusedError("refused") if attempts["n"] == 1 else None
        )

    FakeClient.connect_behavior = staticmethod(behavior)
    harness.start()

    connected = False
    for _ in range(100):
        if mqtt_telemetry.is_connected():
            connected = True
            break
        waiter = threading.Event()
        waiter.wait(timeout=0.05)
    assert connected, (
        "is_connected() never became True after successful reconnect"
    )

    latest = FakeClient.instances[-1]
    latest.on_disconnect(latest, None, 1)
    assert mqtt_telemetry.is_connected() is False, (
        "on_disconnect must clear the connection flag"
    )

    FakeClient.loop_blocker.set()


def test_is_connected_false_before_any_connection(harness):
    """Fresh module state reports not-connected without a broker."""
    FakeClient.connect_behavior = staticmethod(
        lambda attempt: ConnectionRefusedError("refused")
    )
    harness.start()
    # Give the first attempt a moment to fail.
    waiter = threading.Event()
    waiter.wait(timeout=0.2)
    assert mqtt_telemetry.is_connected() is False
