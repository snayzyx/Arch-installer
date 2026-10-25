"""Disk layout computation.

The installer erases the whole target disk and creates, in this order:

    1. ESP                 1 GiB, FAT32, mounted on /boot (GRUB, kernels, Windows boot files)
    2. Windows             NTFS, formatted by Windows Setup
    3. Arch root           ext4/btrfs/xfs, optionally inside LUKS2
    4. Arch /home          optional, takes all the space left
    5. Shared / staging    last partition. It first holds the Windows Setup files (FAT32,
                           so that UEFI can boot WinPE from it). Afterwards it becomes the
                           shared NTFS data partition, or is removed if none was requested.

Sizes are whole MiB; every partition starts on a 1 MiB boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MIB = 1024 * 1024
GIB = 1024 * MIB

ESP_MIB = 1024
STAGING_GIB = 12
MIN_WINDOWS_GIB = 64
MIN_ROOT_GIB = 20
MIN_HOME_GIB = 5
START_MIB = 1  # room for the GPT header and alignment
END_RESERVE_MIB = 1  # room for the backup GPT

ROLE_ESP = "esp"
ROLE_WINDOWS = "windows"
ROLE_ROOT = "root"
ROLE_HOME = "home"
ROLE_SHARED = "shared"


@dataclass(frozen=True)
class Partition:
    number: int
    role: str
    label: str
    start_sector: int
    end_sector: int  # inclusive
    size_mib: int
    temporary: bool = False  # staging partition that is removed after the install

    @property
    def size_gib(self) -> float:
        return self.size_mib / 1024


@dataclass
class Layout:
    sector_size: int
    partitions: list[Partition] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.partitions) and not self.errors

    def get(self, role: str) -> Partition | None:
        return next((p for p in self.partitions if p.role == role), None)


def partition_path(device: str, number: int) -> str:
    """/dev/sda + 1 -> /dev/sda1, /dev/nvme0n1 + 1 -> /dev/nvme0n1p1."""
    return f"{device}p{number}" if device[-1].isdigit() else f"{device}{number}"


def default_sizes(disk_bytes: int) -> dict[str, int]:
    """Reasonable starting values for the size questions, in GiB."""
    disk_gib = disk_bytes // GIB
    windows = min(max(round(disk_gib * 0.4), MIN_WINDOWS_GIB), 512)
    shared = max(STAGING_GIB, min(64, round(disk_gib * 0.1)))
    return {"windows_gib": windows, "root_gib": 64, "shared_gib": shared}


def compute_layout(
    disk_bytes: int,
    sector_size: int,
    *,
    windows_gib: int,
    root_gib: int,
    home_enabled: bool,
    shared_enabled: bool,
    shared_gib: int,
) -> Layout:
    layout = Layout(sector_size=sector_size)
    errors = layout.errors

    if windows_gib < MIN_WINDOWS_GIB:
        errors.append(f"Windows 11 needs at least {MIN_WINDOWS_GIB} GiB.")
    if shared_enabled and shared_gib < STAGING_GIB:
        errors.append(
            f"The shared partition must be at least {STAGING_GIB} GiB "
            "(it first holds the Windows setup files)."
        )

    usable_mib = disk_bytes // MIB - START_MIB - END_RESERVE_MIB
    tail_mib = max(shared_gib, STAGING_GIB) * 1024 if shared_enabled else STAGING_GIB * 1024
    windows_mib = max(windows_gib, 0) * 1024

    sizes: list[tuple[str, str, int, bool]] = [
        (ROLE_ESP, "ESP", ESP_MIB, False),
        (ROLE_WINDOWS, "Windows", windows_mib, False),
    ]
    fixed = ESP_MIB + windows_mib + tail_mib

    if home_enabled:
        root_mib = max(root_gib, 0) * 1024
        home_mib = usable_mib - fixed - root_mib
        if root_gib < MIN_ROOT_GIB:
            errors.append(f"The Arch root partition needs at least {MIN_ROOT_GIB} GiB.")
        if home_mib < MIN_HOME_GIB * 1024:
            errors.append(
                f"Not enough space left for /home (at least {MIN_HOME_GIB} GiB). "
                "Reduce the Windows, root or shared sizes."
            )
        sizes += [(ROLE_ROOT, "Arch root", root_mib, False), (ROLE_HOME, "Arch home", home_mib, False)]
    else:
        root_mib = usable_mib - fixed
        if root_mib < MIN_ROOT_GIB * 1024:
            errors.append(
                f"Not enough space left for the Arch root partition (at least {MIN_ROOT_GIB} GiB). "
                "Reduce the Windows or shared sizes."
            )
        sizes.append((ROLE_ROOT, "Arch root", root_mib, False))

    sizes.append((ROLE_SHARED, "Shared" if shared_enabled else "WINSETUP", tail_mib, not shared_enabled))

    if any(size <= 0 for _, _, size, _ in sizes):
        return layout  # nothing sensible to show; the errors explain why

    mib_sectors = MIB // sector_size
    cursor = START_MIB * mib_sectors
    for number, (role, label, size_mib, temporary) in enumerate(sizes, start=1):
        end = cursor + size_mib * mib_sectors - 1
        layout.partitions.append(Partition(number, role, label, cursor, end, size_mib, temporary))
        cursor = end + 1
    return layout
