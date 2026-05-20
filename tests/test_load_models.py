"""Tests for load_models data models."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone

from load_models import PendingEffect


class TestPendingEffect:
    """Tests for PendingEffect dataclass."""

    def test_power_watts_required(self) -> None:
        """PendingEffect.power_watts must always be provided — never optional."""
        now = datetime.now(timezone.utc)

        # Creating a PendingEffect without power_watts should be a type error.
        # This test verifies the invariant at runtime: power_watts has no default.
        with pytest.raises(TypeError):
            PendingEffect(
                device_name="water_heater",
                action="turn_on",
                timestamp=now,
                data_point_at=now,
            )
