"""Linux console keymap -> X11/Wayland (XKB) layout."""

from __future__ import annotations

# Only the keymaps whose XKB name differs from the console name need an entry.
_DIFFERENT = {
    "uk": "gb",
    "de-latin1": "de",
    "de_CH-latin1": "ch",
    "fr_CH": "ch",
    "be-latin1": "be",
    "cf": "ca",
    "la-latin1": "latam",
    "pt-latin9": "pt",
    "br-abnt2": "br",
    "sv-latin1": "se",
    "pl2": "pl",
    "trq": "tr",
    "jp106": "jp",
    "fr-latin9": "fr",
}


def xkb_layout(keymap: str) -> str:
    """XKB layout for a console keymap; unknown keymaps fall back to ``us``."""
    if keymap in _DIFFERENT:
        return _DIFFERENT[keymap]
    base = keymap.split("-")[0].split("_")[0]
    return base if base.isalpha() and 2 <= len(base) <= 3 else "us"
