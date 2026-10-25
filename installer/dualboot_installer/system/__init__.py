"""Access to the machine the installer runs on (real or simulated)."""

from __future__ import annotations

import sys

from .base import System


def get_system(*, demo: bool, demo_offline: bool = False) -> System:
    if demo or demo_offline:
        from .demo import DEMO_WINDOWS_ISO, DemoSystem

        return DemoSystem(embedded_windows=DEMO_WINDOWS_ISO if demo_offline else None)
    if not sys.platform.startswith("linux"):
        raise SystemExit("The installer only runs on Linux. Use --demo to try the interface anywhere.")
    from .live import LiveSystem

    return LiveSystem()
