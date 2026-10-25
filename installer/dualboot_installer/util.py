"""Small helpers shared across the installer."""

from __future__ import annotations

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def human_size(num_bytes: int) -> str:
    """Format a byte count with binary units, e.g. ``476.9 GiB``."""
    size = float(num_bytes)
    for unit in _UNITS:
        if size < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(size)} B"
            text = f"{size:.1f}"
            return f"{text[:-2] if text.endswith('.0') else text} {unit}"
        size /= 1024
    raise AssertionError("unreachable")
