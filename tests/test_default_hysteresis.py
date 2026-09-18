"""Red-phase: GapMinder default hysteresis suits residential systems."""

from constants import DEFAULT_HYSTERESIS_WH
from load_nbc import GapMinder


def test_default_hysteresis_is_residential() -> None:
    """Default hysteresis is 20 Wh, not the old 1000 Wh grid-scale value."""
    assert DEFAULT_HYSTERESIS_WH == 20
    assert GapMinder().HYSTERESIS_WH == 20
