"""Adaptive prediction/settle window state machine.

The settle window is derived from quantization data and committed only after
two consecutive confirmations outside a dead-band. This module owns the
transition logic as an explicit state machine.

The machine takes an already-filtered integer from ``LoadManager`` and returns
a ``SettleWindowDecision`` so callers can apply store updates and emit audit
lines. It reads no clock, config, cache, or store.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from constants import (
    DEFAULT_PREDICTION_WINDOW_SECS,
    SETTLE_WINDOW_DEADBAND_SECS,
)


class SettleWindowState(StrEnum):
    """State of the adaptive prediction/settle window.

    COMMITTED is both the initial fallback and any confirmed detection; the
    value itself does not record which case it is.
    """

    COMMITTED = "COMMITTED"
    CANDIDATE = "CANDIDATE"


@dataclass(frozen=True)
class SettleWindowDecision:
    """One observation, before the caller applies the commit."""

    state: SettleWindowState
    committed_secs: int
    pending_secs: int | None
    changed: bool
    reason: str


class SettleWindowMachine:
    """Committed window plus one pending candidate.

    Reads no clock, no cache, no config, and no store. The caller passes the
    candidate and applies a commit to its own consumers.

    Invariants:
        - ``state is COMMITTED`` implies ``pending_secs is None``.
        - ``state is CANDIDATE`` implies ``pending_secs is not None``,
          ``pending_secs != committed_secs``, and
          ``abs(pending_secs - committed_secs) >= deadband_secs``.
        - ``committed_secs`` changes only on row 3 (confirmed).
        - ``changed`` is true only on row 3.
    """

    def __init__(
        self,
        initial_secs: int = DEFAULT_PREDICTION_WINDOW_SECS,
        deadband_secs: int = SETTLE_WINDOW_DEADBAND_SECS,
    ) -> None:
        self._committed: int = initial_secs
        self._pending: int | None = None
        self._deadband: int = deadband_secs

    @property
    def state(self) -> SettleWindowState:
        """Current state of the machine."""
        return (
            SettleWindowState.CANDIDATE
            if self._pending is not None
            else SettleWindowState.COMMITTED
        )

    @property
    def committed_secs(self) -> int:
        """The committed window in seconds."""
        return self._committed

    @property
    def pending_secs(self) -> int | None:
        """The pending candidate, or ``None`` when state is COMMITTED."""
        return self._pending

    def observe(self, candidate_secs: int) -> SettleWindowDecision:
        """Record one observation and return the resulting decision.

        Rows are evaluated in order:

        1. If ``candidate_secs == committed``, reset the candidate.
        2. If ``abs(candidate_secs - committed) < deadband``, reset it.
        3. If the machine is already at this candidate, confirm the commit.
        4. Otherwise, replace any old candidate with this value.

        Args:
            candidate_secs: The prediction window observed this cycle.

        Returns:
            A ``SettleWindowDecision`` describing the outcome.
        """
        if candidate_secs == self._committed:
            self._pending = None
            return SettleWindowDecision(
                state=SettleWindowState.COMMITTED,
                committed_secs=self._committed,
                pending_secs=None,
                changed=False,
                reason="same",
            )

        if abs(candidate_secs - self._committed) < self._deadband:
            self._pending = None
            return SettleWindowDecision(
                state=SettleWindowState.COMMITTED,
                committed_secs=self._committed,
                pending_secs=None,
                changed=False,
                reason="deadband",
            )

        if self._pending == candidate_secs:
            self._committed = candidate_secs
            self._pending = None
            return SettleWindowDecision(
                state=SettleWindowState.COMMITTED,
                committed_secs=self._committed,
                pending_secs=None,
                changed=True,
                reason="confirmed",
            )

        self._pending = candidate_secs
        return SettleWindowDecision(
            state=SettleWindowState.CANDIDATE,
            committed_secs=self._committed,
            pending_secs=self._pending,
            changed=False,
            reason="candidate",
        )
