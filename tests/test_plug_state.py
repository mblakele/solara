"""Plug lifecycle state derivation.

Pins the truth table that the device pills use. The state is derived from
stored facts only: desired state, actual state, and the newest pending
effect. Pending wins over the stored pair.
"""

from __future__ import annotations

import pytest

from load_models import PlugState, derive_plug_state


@pytest.mark.parametrize(
    ("desired", "actual", "expected"),
    [
        (True, True, PlugState.ON),
        (True, False, PlugState.PENDING_ON),
        (True, None, PlugState.PENDING_ON),
        (False, True, PlugState.PENDING_OFF),
        (False, False, PlugState.OFF),
        (False, None, PlugState.OFF),
        (None, True, PlugState.ON),
        (None, False, PlugState.OFF),
        (None, None, PlugState.UNKNOWN),
    ],
)
def test_no_pending_effect(
    desired: bool | None, actual: bool | None, expected: PlugState
) -> None:
    """The stored pair decides the state when no effect is pending."""
    assert derive_plug_state(desired=desired, actual=actual) is expected


@pytest.mark.parametrize(
    ("desired", "actual"),
    [
        (True, True),
        (True, False),
        (False, True),
        (False, False),
        (None, None),
    ],
)
@pytest.mark.parametrize(
    ("pending_action", "expected"),
    [
        ("turn_on", PlugState.PENDING_ON),
        ("turn_off", PlugState.PENDING_OFF),
    ],
)
def test_pending_effect_wins(
    pending_action: str,
    expected: PlugState,
    desired: bool | None,
    actual: bool | None,
) -> None:
    """The newest effect overrides every stored pair."""
    assert derive_plug_state(
        desired=desired,
        actual=actual,
        pending_action=pending_action,
    ) is expected


@pytest.mark.parametrize(
    ("pending_amps", "expected"),
    [
        (0, PlugState.PENDING_OFF),
        (1, PlugState.PENDING_ON),
        (16, PlugState.PENDING_ON),
        (None, PlugState.PENDING_ON),
    ],
)
def test_set_amps_effect(
    pending_amps: int | None, expected: PlugState
) -> None:
    """A zero amp command shows pending-off; all other amp commands pending-on."""
    assert derive_plug_state(
        desired=True,
        actual=True,
        pending_action="set_amps",
        pending_amps=pending_amps,
    ) is expected


def test_plug_state_values_serialize_as_strings() -> None:
    """The enum values are the JSON/template strings."""
    assert PlugState.UNKNOWN == "unknown"
    assert PlugState.OFF == "off"
    assert PlugState.ON == "on"
    assert PlugState.PENDING_ON == "pending_on"
    assert PlugState.PENDING_OFF == "pending_off"
    assert isinstance(PlugState.PENDING_ON, str)


def test_unknown_is_the_empty_observation_state() -> None:
    """No observation and no command is the explicit unknown state."""
    assert derive_plug_state(desired=None, actual=None) is PlugState.UNKNOWN
