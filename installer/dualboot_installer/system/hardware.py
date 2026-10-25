"""Hardware facts that matter for a laptop: chassis, battery, storage controller mode."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# SMBIOS chassis types: 8 Portable, 9 Laptop, 10 Notebook, 11 Handheld, 14 Sub Notebook,
# 30 Tablet, 31 Convertible, 32 Detachable.
LAPTOP_CHASSIS_TYPES = frozenset({8, 9, 10, 11, 14, 30, 31, 32})

# `lspci` descriptions of a storage controller that hides the disks from Windows Setup, which
# has no driver for it (Intel RST / VMD, or a SATA controller in RAID mode).
_HIDDEN_STORAGE_MARKERS = ("Volume Management Device", "RAID bus controller")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _power_supplies(sys_root: Path) -> list[Path]:
    try:
        return sorted((sys_root / "class" / "power_supply").iterdir())
    except OSError:
        return []


@dataclass(frozen=True)
class PowerState:
    has_battery: bool
    on_ac: bool | None  # None = unknown
    capacity: int | None  # percent of the first battery


def power_state(sys_root: Path = Path("/sys")) -> PowerState:
    has_battery, ac_online, ac_seen, capacity = False, False, False, None
    for supply in _power_supplies(sys_root):
        kind = _read(supply / "type")
        if kind == "Battery" and _read(supply / "scope") != "Device":  # "Device" = a wireless mouse...
            has_battery = True
            if capacity is None and _read(supply / "capacity").isdigit():
                capacity = int(_read(supply / "capacity"))
        elif kind in ("Mains", "USB"):
            ac_seen = True
            ac_online = ac_online or _read(supply / "online") == "1"
    return PowerState(has_battery, ac_online if ac_seen else None, capacity)


def detect_laptop(sys_root: Path = Path("/sys")) -> bool:
    """A battery or a portable chassis type means a laptop (or tablet)."""
    if power_state(sys_root).has_battery:
        return True
    chassis = _read(sys_root / "class" / "dmi" / "id" / "chassis_type")
    return chassis.isdigit() and int(chassis) in LAPTOP_CHASSIS_TYPES


def hidden_storage_controller(lspci_output: str) -> str | None:
    """The lspci line of a controller that Windows Setup cannot see through, if there is one."""
    for line in lspci_output.splitlines():
        if any(marker in line for marker in _HIDDEN_STORAGE_MARKERS):
            return line.split(" ", 1)[-1].strip()
    return None
