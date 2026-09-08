"""Device pill colors: on = solid gen green, pending-on = faded green."""

from pathlib import Path


def _block(css: str, selector: str) -> str:
    """Return the declaration block for a CSS selector."""
    return css.split(selector + " {", 1)[1].split("}", 1)[0]


def test_device_on_uses_solid_gen_green() -> None:
    """Active devices match the forecast/sparkline green."""
    css = Path("static/style.css").read_text()
    block = _block(css, ".device-pill--on")
    assert "var(--color-gen)" in block
    assert "color-mix" not in block


def test_device_pending_on_uses_faded_green() -> None:
    """Pending-on matches the faded 'recent' pills (history__pill--gen)."""
    css = Path("static/style.css").read_text()
    pending = _block(css, ".device-pill--pending-on")
    recent = _block(css, ".history__pill--gen")
    assert "color-mix(in srgb, var(--color-sunken) 78%, var(--color-gen))" in pending
    assert "color-mix(in srgb, var(--color-sunken) 78%, var(--color-gen))" in recent
