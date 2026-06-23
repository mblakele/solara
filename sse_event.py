"""Thread-safe SSE (Server-Sent Events) broadcaster for real-time streaming.

Provides SSEBroadcaster for pub/sub event distribution and event_stream
generator for Flask-compatible SSE output.
"""

from __future__ import annotations

import json
import queue
import threading
from datetime import datetime, timezone
from typing import Any


class SSEBroadcaster:
    """Thread-safe pub/sub for SSE events.

    Each subscriber gets a queue.Queue. The background loop pumps events
    into all subscriber queues; each SSE generator reads from its own queue
    and formats SSE frames.

    Attributes:
        maxsize: Maximum items per subscriber queue (default 64).
            Slow clients that fill their queue are evicted automatically.
    """

    def __init__(self, maxsize: int = 64) -> None:
        """Initialize the broadcaster.

        Args:
            maxsize: Maximum items per subscriber queue.
        """
        self._maxsize = maxsize
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        """Register a new subscriber.

        Returns:
            A queue.Queue that receives published events.
        """
        q: queue.Queue = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """Remove a subscriber queue.

        Args:
            q: The queue to remove.
        """
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, event: str, data: object) -> int:
        """Publish a named SSE event to all subscribers.

        Subscribers whose queues are full are evicted automatically.

        Args:
            event: SSE event type (e.g. "load_cycle", "metrics_update").
            data: JSON-serializable payload.

        Returns:
            Number of subscribers that received the event.
        """
        payload: dict[str, object] = {
            "event": event,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        dead: list[queue.Queue] = []
        with self._lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._subscribers.discard(q)
        return len(self._subscribers)

    def subscriber_count(self) -> int:
        """Return the current number of subscribers."""
        with self._lock:
            return len(self._subscribers)


def event_stream(
    broadcaster: SSEBroadcaster,
    timeout: int = 60,
    initial_events: list[tuple[str, object]] | None = None,
    dumper: Any = json.dumps,
) -> Any:
    """Generator yielding SSE-formatted frames from a broadcaster.

    Args:
        broadcaster: The SSEBroadcaster instance.
        timeout: Seconds before emitting a heartbeat when queue is idle.
            Default 60s — a safety net for proxies that drop idle connections,
            rarely fires in practice since load cycles run every ~15-30s.
        initial_events: List of (event_name, data) tuples to emit on connect
            before subscribing to the queue. Used for bootstrapping new clients
            with the current state.
        dumper: JSON serialization function (default json.dumps). Pass
            app.json.dumps when using Flask's custom JSON encoder.

    Yields:
        SSE-formatted text frames: "event: NAME\\ndata: JSON\\n\\n"
    """
    if initial_events:
        for event_name, data in initial_events:
            yield f"event: {event_name}\ndata: {dumper(data)}\n\n"

    q = broadcaster.subscribe()
    try:
        while True:
            try:
                payload = q.get(timeout=timeout)
                yield (
                    f"event: {payload['event']}\n"
                    f"data: {dumper(payload['data'])}\n\n"
                )
            except queue.Empty:
                yield "event: heartbeat\ndata: {}\n\n"
    except GeneratorExit:
        pass
    finally:
        broadcaster.unsubscribe(q)
