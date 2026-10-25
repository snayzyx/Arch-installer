"""Simulated hardware, to try and test the interface on any machine."""

from __future__ import annotations

from ..windows_iso import EMBEDDED_DIR, WimImage, WindowsIsoInfo, describe_embedded
from .models import Check, Country, DiskInfo, GpuInfo, SecureBootState, WifiNetwork

GIB = 1024**3

# What an offline build would bundle: a multi-edition English ISO.
DEMO_WINDOWS_ISO = WindowsIsoInfo(
    path=EMBEDDED_DIR / "Windows11.iso",
    size=9_047_330_816,
    sha256="0" * 64,
    images=(
        WimImage(1, "Windows 11 Home", "Core", ("en-US",), "en-US", "26300", "x86_64"),
        WimImage(2, "Windows 11 Pro", "Professional", ("en-US",), "en-US", "26300", "x86_64"),
    ),
)

_COUNTRIES = {
    "AU": ["Australia/Sydney"],
    "BE": ["Europe/Brussels"],
    "BR": ["America/Sao_Paulo"],
    "CA": ["America/Toronto", "America/Vancouver"],
    "CH": ["Europe/Zurich"],
    "DE": ["Europe/Berlin"],
    "ES": ["Europe/Madrid"],
    "FR": ["Europe/Paris"],
    "GB": ["Europe/London"],
    "IT": ["Europe/Rome"],
    "JP": ["Asia/Tokyo"],
    "NL": ["Europe/Amsterdam"],
    "US": ["America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"],
}
_NAMES = {
    "AU": "Australia", "BE": "Belgium", "BR": "Brazil", "CA": "Canada", "CH": "Switzerland",
    "DE": "Germany", "ES": "Spain", "FR": "France", "GB": "United Kingdom", "IT": "Italy",
    "JP": "Japan", "NL": "Netherlands", "US": "United States",
}


class DemoSystem:
    def __init__(self, embedded_windows: WindowsIsoInfo | None = None) -> None:
        self._embedded_windows = embedded_windows

    def embedded_windows(self) -> WindowsIsoInfo | None:
        return self._embedded_windows

    def checks(self) -> list[Check]:
        checks = [
            Check("UEFI firmware", "ok", "Simulated.", blocking=True),
            Check("Architecture", "ok", "x86_64 (simulated)", blocking=True),
            Check("Memory", "ok", "16.0 GiB (simulated)"),
            Check("TPM 2.0", "warn", "Not found (simulated); the Windows 11 TPM check will be bypassed."),
            Check("Secure Boot", "ok", "Currently disabled (simulated)."),
        ]
        if self._embedded_windows:
            checks.append(Check("Windows 11 image", "ok", describe_embedded(self._embedded_windows)))
        return checks

    def disks(self) -> list[DiskInfo]:
        return [
            DiskInfo("/dev/nvme0n1", 512 * GIB, "Samsung SSD 980 PRO", "nvme", False, 512, "gpt"),
            DiskInfo("/dev/sda", 2000 * GIB, "WDC WD20EZAZ", "sata", False, 512, "dos"),
            DiskInfo("/dev/sdb", 32 * GIB, "SanDisk Ultra", "usb", True, 512, "", is_live_medium=True),
            DiskInfo("/dev/vda", 40 * GIB, "", "", False, 512, ""),
        ]

    def gpus(self) -> list[GpuInfo]:
        return [GpuInfo("8086", "Intel Corporation Iris Xe Graphics"), GpuInfo("10de", "NVIDIA GeForce RTX 3060")]

    def countries(self) -> list[Country]:
        return sorted((Country(c, _NAMES[c]) for c in _COUNTRIES if c in _NAMES), key=lambda c: c.name)

    def timezones(self) -> list[str]:
        return sorted({z for zones in _COUNTRIES.values() for z in zones}) + ["UTC"]

    def country_timezones(self, code: str) -> list[str]:
        return list(_COUNTRIES.get(code, []))

    def locales(self) -> list[str]:
        return ["de_DE.UTF-8", "en_GB.UTF-8", "en_US.UTF-8", "es_ES.UTF-8", "fr_FR.UTF-8", "it_IT.UTF-8"]

    def keymaps(self) -> list[str]:
        return ["de-latin1", "es", "fr", "it", "uk", "us"]

    def apply_keymap(self, keymap: str) -> None:
        pass

    def is_online(self) -> bool:
        return True

    def wifi_scan(self) -> tuple[str, list[WifiNetwork]]:
        return "wlan0", [WifiNetwork("HomeNetwork", "psk", 4), WifiNetwork("Guest [open]", "open", 2)]

    def wifi_connect(self, device: str, ssid: str, passphrase: str) -> tuple[bool, str]:
        return True, f"Connected to {ssid} (simulated)."

    def secure_boot_state(self) -> SecureBootState:
        return SecureBootState(enabled=False, setup_mode=False)
