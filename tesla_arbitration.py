"""REST arbitration verdict state machine for ambiguous Tesla amps.

Positive but uncorroborated ``ChargeAmps`` with no active command is
ambiguous: it can be the idle pilot ghost or a real externally started
session. Only a REST ``charge_state`` read can tell them apart. Each poll
can wake a sleeping car and costs API quota, so polls are spaced apart and
the last definitive answer is sustained between them.

This module owns that verdict and its poll clock. It reads no clock, no
telemetry, and no config. Callers pass ``now`` and report events. Every
transition has one owner, so a reader does not have to reconstruct the
machine from the call sites.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import logging

from constants import TESLA_ARBITRATION_COOLDOWN_SECS

logger = logging.getLogger(__name__)


class ArbitrationState(StrEnum):
    """State of the REST arbitration verdict for ambiguous Tesla amps."""

    UNKNOWN = "unknown"
    CHARGING = "charging"
    IDLE = "idle"


class RestArbitrationMachine:
    """Verdict and cooldown for the REST arbitration poll.

    The machine holds two facts: the verdict and the time of the last
    definitive answer. The clock is a *poll* clock. Clearing the verdict
    never clears the clock, so a cleared verdict still blocks a new poll
    until the cooldown ends.

    Args:
        cooldown_secs: Minimum seconds between definitive REST polls.
    """

    def __init__(
        self,
        cooldown_secs: int = TESLA_ARBITRATION_COOLDOWN_SECS,
    ) -> None:
        self._cooldown_secs = cooldown_secs
        self._state: ArbitrationState = ArbitrationState.UNKNOWN
        self._last_poll_at: datetime | None = None

    @property
    def state(self) -> ArbitrationState:
        """Current verdict."""
        return self._state

    @property
    def last_poll_at(self) -> datetime | None:
        """Time of the last definitive REST answer, or ``None``."""
        return self._last_poll_at

    def observe_amps(self, reported_amps: int | None, *, now: datetime | None = None) -> None:
        """End a confirmed session when amps are absent or not positive.

        Args:
            reported_amps: Parsed ``ChargeAmps`` value, or ``None`` when the
                field is absent.
            now: Current time, used only for the audit log.
        """
        if reported_amps is None or reported_amps <= 0:
            self._log_transition(
                ArbitrationState.IDLE,
                "amps_zero",
                amps=reported_amps,
                poll_age=self._poll_age_secs(now),
            )

    def note_corroborated_idle(self, *, now: datetime | None = None) -> None:
        """End a confirmed session on authoritative idle telemetry.

        Args:
            now: Current time, used only for the audit log.
        """
        self._log_transition(
            ArbitrationState.IDLE,
            "corroborated_idle",
            poll_age=self._poll_age_secs(now),
        )

    def note_commanded_change(self, *, now: datetime | None = None) -> None:
        """End a confirmed session when we command the car on or off.

        Without this reset, a sustained charging verdict could re-trigger a
        trim and restart the car we just stopped.

        Args:
            now: Current time, used only for the audit log.
        """
        self._log_transition(
            ArbitrationState.IDLE,
            "commanded_change",
            poll_age=self._poll_age_secs(now),
        )

    def sustains(self, now: datetime) -> bool:
        """Return True when a charging verdict is fresh enough to reuse.

        Args:
            now: Current time for the cooldown check.
        """
        if self._state != ArbitrationState.CHARGING:
            return False
        if self._last_poll_at is None:
            return False
        age = (now - self._last_poll_at).total_seconds()
        return age < self._cooldown_secs

    def needs_poll(self, now: datetime) -> bool:
        """Return True when the cooldown allows a new REST poll.

        Args:
            now: Current time for the cooldown check.
        """
        if self._last_poll_at is None:
            return True
        age = (now - self._last_poll_at).total_seconds()
        return age >= self._cooldown_secs

    def record_answer(self, now: datetime, *, charging: bool) -> None:
        """Record one definitive REST answer and start the cooldown.

        Args:
            now: Time of the answer.
            charging: True when REST confirms charging, False when it
                definitively reports idle.
        """
        poll_age = self._poll_age_secs(now)
        self._last_poll_at = now
        self._log_transition(
            ArbitrationState.CHARGING if charging else ArbitrationState.IDLE,
            "rest_answer",
            poll_age=poll_age,
        )

    def record_transport_failure(self) -> None:
        """Record a failed poll. This is a no-op by design.

        A transport failure is not an answer. The clock and verdict stay
        untouched so the next cycle retries instead of serving a latched
        idle while the car is actually charging.
        """
        logger.debug(
            "[tesla_arbitration] transport_failure state=%s last_poll_at=%s",
            self._state.value,
            "none" if self._last_poll_at is None else self._last_poll_at.isoformat(),
        )

    def _poll_age_secs(self, now: datetime | None) -> float | None:
        """Seconds since the last definitive answer, or ``None``."""
        if now is None or self._last_poll_at is None:
            return None
        return (now - self._last_poll_at).total_seconds()

    def _log_transition(
        self,
        new_state: ArbitrationState,
        event: str,
        *,
        amps: int | None = None,
        poll_age: float | None = None,
    ) -> None:
        """Move to ``new_state`` and log only when the verdict changes."""
        if new_state == self._state:
            return
        logger.debug(
            "[tesla_arbitration] %s -> %s event=%s amps=%s poll_age=%s",
            self._state.value,
            new_state.value,
            event,
            "none" if amps is None else amps,
            "none" if poll_age is None else f"{poll_age:.1f}s",
        )
        self._state = new_state
