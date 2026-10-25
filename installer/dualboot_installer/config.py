"""The answers collected by the wizard."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from typing import ClassVar

from .data.windows import default_keyboard, windows_timezone
from .extras import DEFAULT_APPS, DEFAULT_DEV_GROUPS
from .layout import Layout, compute_layout

FILESYSTEMS = ("ext4", "btrfs", "xfs")
KERNELS = ("linux", "linux-lts", "linux-zen")
AUR_HELPERS = ("none", "paru", "yay")
DESKTOPS = ("hyprland", "minimal")
# embedded = bundled by the offline build; download = a link the user pastes; local = a file
ISO_SOURCES = ("embedded", "download", "local")
SECURE_BOOT_CHOICES = ("auto", "sbctl", "shim")


def _new_disk_guid() -> str:
    return str(uuid.uuid4()).upper()


@dataclass
class DiskConfig:
    device: str = ""
    # GPT disk GUID, set at partitioning time. Windows Setup numbers disks differently from
    # Linux, so the WinPE launcher finds the target disk by this GUID instead of by number.
    guid: str = field(default_factory=_new_disk_guid)
    model: str = ""
    size_bytes: int = 0
    sector_size: int = 512
    sizes_for_device: str = ""  # the device the size fields below were initialised for
    windows_gib: int = 0
    root_gib: int = 64  # only asked when /home is separate
    home_enabled: bool = False
    shared_enabled: bool = False
    shared_gib: int = 64


@dataclass
class ArchConfig:
    country: str = "US"
    timezone: str = "UTC"
    locale: str = "en_US.UTF-8"
    keymap: str = "us"
    hostname: str = "archlinux"
    username: str = ""
    user_password: str = ""
    root_same_password: bool = True
    root_password: str = ""
    filesystem: str = "ext4"
    luks_enabled: bool = False
    luks_passphrase: str = ""
    kernel: str = "linux"
    gpu_profiles: list[str] = field(default_factory=list)
    aur_helper: str = "none"
    desktop: str = "hyprland"
    extra_packages: list[str] = field(default_factory=list)
    # Developer tool groups, desktop applications and a desktop shell preset (see extras.py).
    dev_groups: list[str] = field(default_factory=lambda: list(DEFAULT_DEV_GROUPS))
    apps: list[str] = field(default_factory=lambda: list(DEFAULT_APPS))
    dotfiles: str = "none"
    # Laptop extras (power profiles, Bluetooth, SOF audio firmware...). None = decide from the
    # hardware (a battery or a portable chassis), True/False = forced by an answers file.
    laptop: bool | None = None

    @property
    def effective_root_password(self) -> str:
        return self.user_password if self.root_same_password else self.root_password


@dataclass
class NetworkConfig:
    """The Wi-Fi network used during the installation, kept for the installed system."""

    wifi_ssid: str = ""
    wifi_security: str = ""  # open | psk
    wifi_passphrase: str = ""


@dataclass
class WindowsConfig:
    iso_source: str = "embedded"
    iso_path: str = ""
    iso_url: str = ""
    language_name: str = "English (United States)"
    language_tag: str = "en-US"
    edition: str = "Pro"
    computer_name: str = ""
    username: str = ""
    password: str = ""
    same_password_as_arch: bool = False
    custom_regional: bool = False
    timezone: str = ""  # IANA name, only used when custom_regional
    keyboard: str = ""  # Windows language tag, only used when custom_regional

    # Applied to every installation.
    TWEAKS: ClassVar[tuple[str, ...]] = (
        "Fix the dual boot clock (RealTimeIsUniversal)",
        "Turn off Fast Startup and hibernation (they leave the Windows disk unusable from Linux)",
        "Disable automatic BitLocker / device encryption",
        "Remove preinstalled apps and reduce telemetry",
        "Bypass the TPM, RAM and CPU requirement checks",
    )


@dataclass
class SecureBootConfig:
    strategy: str = "auto"  # auto | sbctl | shim
    resolved: str = ""  # the strategy actually used, set by the Secure Boot screen
    mok_password: str = ""
    # False skips everything that writes to the firmware (key enrollment, boot entry, MOK).
    # Only for tests in a container, where there is no firmware to talk to.
    touch_firmware: bool = True


@dataclass
class InstallConfig:
    disk: DiskConfig = field(default_factory=DiskConfig)
    arch: ArchConfig = field(default_factory=ArchConfig)
    windows: WindowsConfig = field(default_factory=WindowsConfig)
    secureboot: SecureBootConfig = field(default_factory=SecureBootConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    dry_run: bool = False
    # Run the steps that exist and skip the rest (Windows): Arch Linux and GRUB only.
    partial: bool = False

    def layout(self) -> Layout:
        d = self.disk
        return compute_layout(
            d.size_bytes,
            d.sector_size,
            windows_gib=d.windows_gib,
            root_gib=d.root_gib,
            home_enabled=d.home_enabled,
            shared_enabled=d.shared_enabled,
            shared_gib=d.shared_gib,
        )

    def windows_timezone_id(self) -> str:
        w = self.windows
        iana = w.timezone if w.custom_regional and w.timezone else self.arch.timezone
        return windows_timezone(iana)

    def windows_keyboard_tag(self) -> str:
        w = self.windows
        if w.custom_regional and w.keyboard:
            return w.keyboard
        return default_keyboard(self.arch.keymap, self.arch.country)

    def windows_password(self) -> str:
        return self.arch.user_password if self.windows.same_password_as_arch else self.windows.password

    def update_from(self, data: dict) -> None:
        """Fill the configuration from a plain dict (an answers file). Unknown keys are errors."""
        for section, values in data.items():
            if section in ("dry_run", "partial"):
                setattr(self, section, bool(values))
                continue
            target = getattr(self, section, None)
            if not is_dataclass(target) or not isinstance(values, dict):
                raise ValueError(f"Unknown section in the answers: {section!r}")
            known = {f.name for f in fields(target)}
            for key, value in values.items():
                if key not in known:
                    raise ValueError(f"Unknown answer: {section}.{key}")
                setattr(target, key, value)

    def redacted(self) -> dict:
        """The configuration with every secret masked, safe to log."""
        def mask(node):
            if isinstance(node, dict):
                return {
                    key: ("***" if value and any(s in key for s in ("password", "passphrase")) else mask(value))
                    for key, value in node.items()
                }
            return node

        return mask(asdict(self))
