"""Gunicorn configuration for Solara.

Wires cooperative shutdown into worker lifecycle hooks so a single
SIGINT/SIGTERM stops the server promptly:

- ``post_worker_init`` chains a cooperative-shutdown handler over the
  worker's installed signal handlers (runs after ``init_signals()``, so
  chaining sees gunicorn's handlers). The wrapper calls
  :func:`app.request_shutdown` first — waking background loops and SSE
  streams — then delegates, preserving gunicorn's graceful-SIGTERM /
  fast-SIGINT semantics.
- ``worker_int`` / ``worker_exit`` are belt-and-braces passes through
  :func:`app.request_shutdown` for exit paths where handlers were replaced.

Without these hooks an open SSE stream keeps a gthread pool thread parked in
a queue get; concurrent.futures joins that thread at interpreter exit and
process shutdown hangs until a second Ctrl-C.
"""

def post_worker_init(_worker):
    """Install shutdown-chaining signal handlers in the freshly booted worker."""
    from app import install_shutdown_signal_hooks

    install_shutdown_signal_hooks()


def worker_int(_worker):
    """Cooperative shutdown when the worker received SIGINT/SIGQUIT."""
    from app import request_shutdown

    request_shutdown("gunicorn:worker_int")


def worker_exit(_server, _worker):
    """Final cooperative-shutdown pass as the worker exits."""
    from app import request_shutdown

    request_shutdown("gunicorn:worker_exit")


# Arbiter watchdog ("--timeout"): a hung-worker guard, NOT a request limit.
# The gthread worker's main loop heartbeats every ~1s regardless of what
# its pool threads are doing, so slow requests (even a full 30s Emporia
# fetch) never trip it. Pinned here rather than per-deploy CLI flags so
# Render, Replit, and local runs share one value; kept at double the app's
# fetch bound (EnergyCache fetch_timeout_secs = 30s) so watchdog and
# application timeout can never converge if either changes.
timeout = 60
