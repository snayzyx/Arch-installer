"""The real machine: reads /sys, runs lsblk, iwctl, localectl, ..."""

from __future__ import annotations

import json
import platform
import re
import socket
import subprocess
from pathlib import Path

from .. import secureboot
from ..data.countries import COUNTRY_DEFAULTS
from ..windows_iso import EMBEDDED_DIR, WindowsIsoInfo, describe_embedded, load_manifest
from .hardware import hidden_storage_controller, power_state
from .models import Check, Country, DiskInfo, GpuInfo, SecureBootState, WifiNetwork
from .regional import parse_iso3166, parse_supported_locales, parse_zone_tab

ZONEINFO = Path("/usr/share/zoneinfo")
LIVE_MEDIUM_MOUNT = "/run/archiso/bootmnt"
LIVE_MEDIUM_LABEL = "ARCHINSTALL"  # the volume label of our ISO (docker/build-iso.sh)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_WIFI_ROW_RE = re.compile(r"^\s*(?:>\s*)?(?P<ssid>.+?)\s{2,}(?P<sec>open|psk|8021x|wep)\s+(?P<sig>\*{1,4})\s*$")
_PCI_GPU_RE = re.compile(r"(VGA compatible controller|3D controller|Display controller)[^:]*:\s*(?P<name>.+?)\s*\[(?P<vendor>[0-9a-f]{4}):[0-9a-f]{4}\]")

_FALLBACK_KEYMAPS = ["us", "uk", "fr", "de-latin1", "es", "it", "pt-latin9", "br-abnt2", "be-latin1",
                     "cf", "de_CH-latin1", "fr_CH", "nl", "sv-latin1", "no", "dk", "fi", "pl2", "cz",
                     "ru", "trq", "jp106"]
_FALLBACK_LOCALES = ["en_US.UTF-8", "en_GB.UTF-8", "fr_FR.UTF-8", "de_DE.UTF-8", "es_ES.UTF-8",
                     "it_IT.UTF-8", "pt_BR.UTF-8", "nl_NL.UTF-8"]


def _run(argv: list[str], timeout: int = 15) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


class LiveSystem:
    # ----------------------------------------------------------------- checks

    def checks(self) -> list[Check]:
        checks: list[Check] = []

        uefi = Path("/sys/firmware/efi").is_dir()
        checks.append(Check("UEFI firmware", "ok" if uefi else "fail",
                            "Booted in UEFI mode." if uefi else "Booted in legacy BIOS mode; UEFI is required.",
                            blocking=True))

        arch = platform.machine()
        checks.append(Check("Architecture", "ok" if arch == "x86_64" else "fail", arch, blocking=True))

        ram_gib = self._ram_gib()
        checks.append(Check("Memory", "ok" if ram_gib >= 4 else "warn",
                            f"{ram_gib:.1f} GiB" + ("" if ram_gib >= 4 else " (Windows 11 asks for 4 GiB; the check is bypassed)")))

        tpm_major = _read(Path("/sys/class/tpm/tpm0/tpm_version_major")).strip()
        if tpm_major == "2":
            checks.append(Check("TPM 2.0", "ok", "Present."))
        else:
            checks.append(Check("TPM 2.0", "warn", "Not found; the Windows 11 TPM check will be bypassed."))

        state = self.secure_boot_state()
        if state.enabled:
            detail = "Enabled."
            if state.setup_mode:
                detail += " The firmware is in Setup Mode."
            checks.append(Check("Secure Boot", "ok", detail))
        elif state.enabled is False:
            checks.append(Check("Secure Boot", "ok", "Currently disabled (expected: this live ISO is not signed). "
                                                     "It will be enabled after the installation."))
        else:
            checks.append(Check("Secure Boot", "warn", "State unknown."))

        embedded = self.embedded_windows()
        if embedded:
            checks.append(Check("Windows 11 image", "ok", describe_embedded(embedded)))

        controller = hidden_storage_controller(_run(["lspci"]))
        if controller:
            checks.append(Check("Storage controller", "warn",
                                f"{controller}. Windows Setup has no driver for this mode and may not see the disk: "
                                "in the firmware setup, switch the storage mode to AHCI (or turn Intel VMD off), "
                                "then boot this ISO again."))

        power = power_state()
        if power.has_battery and power.on_ac is False:
            level = f" ({power.capacity}%)" if power.capacity is not None else ""
            checks.append(Check("Power", "warn",
                                f"Running on battery{level}. The installation takes a long time: plug in the charger."))
        return checks

    @staticmethod
    def _ram_gib() -> float:
        for line in _read(Path("/proc/meminfo")).splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1024 / 1024
        return 0.0

    # ------------------------------------------------------------------ disks

    def disks(self) -> list[DiskInfo]:
        out = _run(["lsblk", "--json", "--bytes", "--output",
                    "NAME,PATH,SIZE,MODEL,TRAN,TYPE,RM,RO,LOG-SEC,PTTYPE"])
        try:
            devices = json.loads(out).get("blockdevices", [])
        except json.JSONDecodeError:
            return []

        live_disk = self._live_medium_disk()
        disks: list[DiskInfo] = []
        for dev in devices:
            if dev.get("type") != "disk" or dev.get("ro") in (True, "1", 1):
                continue
            if not dev.get("size") or int(dev["size"]) == 0:
                continue
            disks.append(DiskInfo(
                path=dev["path"],
                size_bytes=int(dev["size"]),
                model=(dev.get("model") or "").strip(),
                transport=(dev.get("tran") or "").strip(),
                removable=dev.get("rm") in (True, "1", 1),
                sector_size=int(dev.get("log-sec") or 512),
                partition_table=(dev.get("pttype") or "").strip(),
                is_live_medium=dev["name"] == live_disk,
            ))
        return disks

    @staticmethod
    def _live_medium_disk() -> str | None:
        """Name of the physical disk the live ISO was booted from (e.g. ``sdb``)."""
        source = _run(["findmnt", "-no", "SOURCE", LIVE_MEDIUM_MOUNT]).strip()
        if not source:
            # Not mounted: archiso unmounts the medium after copying itself to RAM (copytoram), which
            # our boot entries disable but a user can re-enable. The volume label still finds it.
            source = _run(["blkid", "-L", LIVE_MEDIUM_LABEL]).strip()
        if not source:
            return None
        # `lsblk -s` lists the device and all its ancestors, so this also works when
        # the medium sits behind device-mapper (Ventoy) rather than a plain partition.
        for line in _run(["lsblk", "-s", "-l", "-n", "-o", "NAME,TYPE", source]).splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == "disk":
                return parts[0]
        return None

    def gpus(self) -> list[GpuInfo]:
        gpus = []
        for line in _run(["lspci", "-nn"]).splitlines():
            match = _PCI_GPU_RE.search(line)
            if match:
                gpus.append(GpuInfo(match.group("vendor"), match.group("name")))
        return gpus

    # --------------------------------------------------------------- regional

    def countries(self) -> list[Country]:
        names = parse_iso3166(_read(ZONEINFO / "iso3166.tab"))
        zones = parse_zone_tab(_read(ZONEINFO / "zone.tab"))
        if not names:
            names = {code: code for code in COUNTRY_DEFAULTS}
        return sorted((Country(code, name) for code, name in names.items() if not zones or code in zones),
                      key=lambda c: c.name)

    def timezones(self) -> list[str]:
        zones = parse_zone_tab(_read(ZONEINFO / "zone.tab"))
        flat = sorted({zone for items in zones.values() for zone in items})
        return flat + ["UTC"] if flat else ["UTC"]

    def country_timezones(self, code: str) -> list[str]:
        return parse_zone_tab(_read(ZONEINFO / "zone.tab")).get(code, [])

    def locales(self) -> list[str]:
        for path in (Path("/usr/share/i18n/SUPPORTED"), Path("/etc/locale.gen")):
            found = parse_supported_locales(_read(path))
            if found:
                return found
        return list(_FALLBACK_LOCALES)

    def keymaps(self) -> list[str]:
        found = [k for k in _run(["localectl", "list-keymaps", "--no-pager"]).splitlines() if k.strip()]
        return found or list(_FALLBACK_KEYMAPS)

    def apply_keymap(self, keymap: str) -> None:
        """Switch the live console to the chosen layout so passwords are typed correctly."""
        _run(["loadkeys", keymap])

    # ---------------------------------------------------------------- network

    def is_online(self) -> bool:
        try:
            with socket.create_connection(("1.1.1.1", 443), timeout=4):
                pass
            socket.getaddrinfo("archlinux.org", 443)
        except OSError:
            return False
        return True

    def wifi_scan(self) -> tuple[str, list[WifiNetwork]]:
        device = self._wifi_device()
        if not device:
            return "", []
        _run(["iwctl", "station", device, "scan"])
        out = _ANSI_RE.sub("", _run(["iwctl", "station", device, "get-networks"]))
        networks: list[WifiNetwork] = []
        for line in out.splitlines():
            match = _WIFI_ROW_RE.match(line)
            if match:
                networks.append(WifiNetwork(match.group("ssid").strip(), match.group("sec"), len(match.group("sig"))))
        networks.sort(key=lambda n: -n.signal)
        return device, networks

    @staticmethod
    def _wifi_device() -> str:
        out = _ANSI_RE.sub("", _run(["iwctl", "device", "list"]))
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[-1] == "station":
                return parts[0]
        return ""

    def wifi_connect(self, device: str, ssid: str, passphrase: str) -> tuple[bool, str]:
        argv = ["iwctl"]
        if passphrase:
            argv.append(f"--passphrase={passphrase}")
        argv += ["station", device, "connect", ssid]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=45, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        message = _ANSI_RE.sub("", (result.stdout + result.stderr)).strip()
        return result.returncode == 0, message or ("Connected." if result.returncode == 0 else "Connection failed.")

    # ------------------------------------------------------------ secure boot

    def secure_boot_state(self) -> SecureBootState:
        return secureboot.detect()

    # ---------------------------------------------------------------- windows

    def embedded_windows(self) -> WindowsIsoInfo | None:
        return load_manifest(EMBEDDED_DIR)
