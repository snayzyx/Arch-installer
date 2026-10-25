"""Network, Wi-Fi and Bluetooth drivers for the new Windows.

A stock Windows ISO does not carry every Wi-Fi driver, and without a network Windows cannot download the
missing one: the laptop ends up with no Wi-Fi at all. Two sources fix that, both optional:

* the drivers of the Windows that is ALREADY on the disk (the factory one, which has the drivers of this
  exact machine): `harvest_drivers` reads its driver store BEFORE the disk is erased, keeps the packages that
  match the hardware IDs of this computer's network and Bluetooth adapters, and holds them in memory;
* a folder of drivers bundled into the ISO at build time (`build.ps1 -DriversDir`), found on the boot
  medium under windows/drivers/.

`stage_drivers` copies both into the Setup partition (sources/$OEM$/$1/Drivers, which Windows Setup puts on
C:\\Drivers) and the answer file lists C:\\Drivers as a driver path. Nothing here is ever fatal: the old
Windows may be missing, encrypted with BitLocker or hibernated, and the installation goes on without drivers.
The volumes of the old Windows are only ever mounted read-only.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from ..windows_iso import EMBEDDED_DIR
from .context import Context

HARVEST_DIR = Path("/tmp/archinstall-windows-drivers")  # on the live system's RAM disk
BUNDLED_DIR = EMBEDDED_DIR / "drivers"  # /run/archiso/bootmnt/windows/drivers
OLD_WINDOWS_MOUNT = "/tmp/archinstall-old-windows"
DRIVER_STORE = "Windows/System32/DriverStore/FileRepository"
SETUP_DRIVERS_DIR = "sources/$OEM$/$1/Drivers"  # Windows Setup copies $1 to C:\ : C:\Drivers
SIZE_CAP_BYTES = 600 * 1024 * 1024
MAX_INF_BYTES = 16 * 1024 * 1024

# PCI base classes: 02 network controllers (Ethernet 0200, Wi-Fi and the rest 0280), 0d wireless controllers
# (Bluetooth 0d11).
PCI_CLASSES = ("02", "0d")
# USB interface classes: e0 wireless controller (Bluetooth), 02 and 0a CDC (Ethernet adapters), ff vendor
# specific (Wi-Fi dongles). Keyboards, mice, hubs, storage and cameras are left out.
USB_CLASSES = frozenset({"e0", "02", "0a", "ff"})

_LSPCI_LINE = re.compile(r"\[(?P<cls>[0-9a-f]{4})\]:\s.*\[(?P<ven>[0-9a-f]{4}):(?P<dev>[0-9a-f]{4})\](?:\s+\(rev [0-9a-f]+\))?\s*$",
                         re.IGNORECASE)


@dataclass
class Package:
    """A driver package (a folder of the driver store) that was kept, or skipped with a reason."""

    folder: str
    inf: str
    matched: str  # the hardware ID found in the INF
    size: int
    skipped: str = ""


# -- what hardware this computer has ---------------------------------------------------------------------------


def pci_ids_from_lspci(text: str) -> list[str]:
    """`PCI\\VEN_8086&DEV_A0F0` strings of the network and wireless controllers in `lspci -nn` output."""
    found: list[str] = []
    for line in text.splitlines():
        match = _LSPCI_LINE.search(line)
        if match and match.group("cls")[:2].lower() in PCI_CLASSES:
            found.append(f"PCI\\VEN_{match.group('ven').upper()}&DEV_{match.group('dev').upper()}")
    return list(dict.fromkeys(found))


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip().lower()
    except OSError:
        return ""


def usb_ids_from_sysfs(sys_root: Path = Path("/sys")) -> list[str]:
    """`USB\\VID_8087&PID_0026` strings of the USB devices that are Bluetooth, Ethernet or vendor specific."""
    found: list[str] = []
    try:
        devices = sorted((sys_root / "bus" / "usb" / "devices").iterdir())
    except OSError:
        return []
    for device in devices:
        if ":" in device.name:  # an interface (1-7:1.0), its device is handled on its own
            continue
        vendor, product = _read(device / "idVendor"), _read(device / "idProduct")
        if not (vendor and product):
            continue
        classes = {_read(device / "bDeviceClass")}
        try:
            # the interfaces are sub-folders of the device (1-5/1-5:1.0/bInterfaceClass): any folder that has one
            classes |= {_read(interface / "bInterfaceClass") for interface in device.iterdir() if interface.is_dir()}
        except OSError:
            pass
        if classes & USB_CLASSES:
            found.append(f"USB\\VID_{vendor.upper()}&PID_{product.upper()}")
    return list(dict.fromkeys(found))


# -- reading the driver store of an old Windows ----------------------------------------------------------------


def read_inf(path: Path) -> str:
    """The text of an INF file: UTF-16 (Windows' own drivers), UTF-8 with a BOM, or ANSI."""
    data = path.read_bytes()[:MAX_INF_BYTES]
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", errors="replace")
    head = data[:400]
    if head and head.count(b"\x00") / len(head) > 0.25:  # UTF-16 without a byte order mark: every other byte is 0
        return data.decode("utf-16-le", errors="replace")
    return data.decode("cp1252", errors="replace")


def _folder_size(folder: Path) -> int:
    total = 0
    for item in folder.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def match_package(folder: Path, ids: list[str]) -> tuple[str, str] | None:
    """(INF name, hardware ID) when an INF of this package lists one of the IDs, else None."""
    wanted = [(hardware_id, hardware_id.upper()) for hardware_id in ids]
    for inf in sorted(folder.glob("*.[iI][nN][fF]")):
        try:
            text = read_inf(inf).upper()
        except OSError:
            continue
        for hardware_id, upper in wanted:
            if upper in text:
                return inf.name, hardware_id
    return None


def harvest_from_store(store: Path, ids: list[str], destination: Path, *,
                       cap: int = SIZE_CAP_BYTES) -> list[Package]:
    """Copy the packages of `store` that match `ids` into `destination`, within `cap` bytes in total."""
    packages: list[Package] = []
    used = 0
    for folder in sorted(store.iterdir()):
        if not folder.is_dir():
            continue
        matched = match_package(folder, ids)
        if matched is None:
            continue
        inf, hardware_id = matched
        size = _folder_size(folder)
        package = Package(folder.name, inf, hardware_id, size)
        if used + size > cap:
            package.skipped = f"over the {cap // 1024 // 1024} MiB limit"
        else:
            try:
                shutil.copytree(folder, destination / folder.name, dirs_exist_ok=True)
                used += size
            except (OSError, shutil.Error) as exc:
                package.skipped = f"cannot be copied ({exc})"
        packages.append(package)
    return packages


# -- the steps -------------------------------------------------------------------------------------------------


def _old_windows_partitions(ctx: Context) -> list[tuple[str, str]]:
    """(device, file system) of the NTFS and BitLocker partitions of the target disk."""
    # lsblk's FSTYPE column comes from the udev database, which is empty when udev is not running (found by the
    # first real run): blkid probes the partition itself.
    rows = ctx.capture("lsblk", "-nrpo", "NAME,TYPE", ctx.cfg.disk.device)
    found = []
    for line in rows.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "part":
            fstype = ctx.capture("blkid", "-o", "value", "-s", "TYPE", parts[0]).strip().lower()
            if fstype in ("ntfs", "bitlocker"):
                found.append((parts[0], fstype))
    return found


def _mount_read_only(ctx: Context, device: str, mountpoint: str) -> bool:
    ctx.run("mkdir", "-p", mountpoint, check=False)
    for driver, options in (("ntfs-3g", "ro,nodev,nosuid,noexec"), ("ntfs3", "ro,nodev,nosuid,noexec")):
        if ctx.run("mount", "-t", driver, "-o", options, device, mountpoint, check=False, quiet=True) == 0:
            return True
    return False


def harvest_drivers(ctx: Context) -> None:
    """Before the disk is erased: keep the network drivers of the Windows that is on it. Never fatal."""
    if ctx.cfg.partial:
        return
    if ctx.runner.dry_run:
        ctx.log("Would look for the Wi-Fi, network and Bluetooth drivers of a Windows already on the disk.")
        return
    try:
        _harvest(ctx)
    except Exception as exc:  # noqa: BLE001 - nothing here may stop the installation
        ctx.log(f"WARNING: looking for the drivers of the existing Windows failed ({exc}); going on without them.")


def _harvest(ctx: Context) -> None:
    shutil.rmtree(HARVEST_DIR, ignore_errors=True)  # never reuse what an earlier attempt left in the RAM disk
    ids = pci_ids_from_lspci(ctx.capture("lspci", "-nn")) + usb_ids_from_sysfs()
    if not ids:
        ctx.log("No network or Bluetooth adapter was identified: no drivers to look for.")
        return
    ctx.log(f"Looking for drivers of: {', '.join(ids)}")
    partitions = _old_windows_partitions(ctx)
    if not partitions:
        ctx.log("No NTFS partition on this disk: no existing Windows to take drivers from.")
        return

    free = shutil.disk_usage(HARVEST_DIR.parent).free
    cap = min(SIZE_CAP_BYTES, free // 2)
    keep: list[Package] = []
    for number, (device, fstype) in enumerate(partitions, start=1):
        if fstype == "bitlocker":
            ctx.log(f"{device} is encrypted with BitLocker: the drivers in it cannot be read. "
                    "After Windows is installed, connect an Ethernet cable or your phone by USB (tethering) "
                    "once and Windows Update installs the Wi-Fi driver.")
            continue
        mountpoint = f"{OLD_WINDOWS_MOUNT}/{number}"
        if not _mount_read_only(ctx, device, mountpoint):
            ctx.log(f"{device} cannot be read (damaged, or Windows was hibernated): skipped.")
            continue
        try:
            store = Path(mountpoint) / DRIVER_STORE
            if not store.is_dir():
                ctx.log(f"{device}: no Windows driver store.")
                continue
            ctx.log(f"{device}: Windows driver store found, looking for matching drivers (a minute or two).")
            keep += harvest_from_store(store, ids, HARVEST_DIR, cap=cap - sum(p.size for p in keep if not p.skipped))
        finally:
            ctx.run("umount", mountpoint, check=False, quiet=True)

    kept = [p for p in keep if not p.skipped]
    for package in keep:
        if package.skipped:
            ctx.log(f"WARNING: driver package {package.folder} was not kept: {package.skipped}.")
    if kept:
        ctx.state["drivers_harvested"] = [asdict(p) for p in kept]
        ctx.log(f"Kept {len(kept)} driver package(s) of the existing Windows "
                f"({sum(p.size for p in kept) // 1024} KiB): " + json.dumps([asdict(p) for p in kept]))
    else:
        ctx.log("The existing Windows has no driver for these adapters: nothing kept.")


def _package_folders(directory: Path) -> list[Path]:
    try:
        return sorted(item for item in directory.iterdir() if item.is_dir())
    except OSError:
        return []


def driver_sources() -> list[Path]:
    """Folders of driver packages to give to Windows: the harvested ones, then the ones bundled in the ISO."""
    return [directory for directory in (HARVEST_DIR, BUNDLED_DIR) if _package_folders(directory)]


def bundled_driver_count() -> int:
    return len(_package_folders(BUNDLED_DIR))


def stage_drivers(ctx: Context, setup_mount: str, sources: list[Path]) -> int:
    """Copy the driver packages into the Setup partition. Returns how many packages were copied."""
    if not sources:
        return 0
    destination = f"{setup_mount}/{SETUP_DRIVERS_DIR}"
    ctx.run("mkdir", "-p", destination)
    count = 0
    for source in sources:
        ctx.run("rsync", "-rt", "--no-perms", "--no-owner", "--no-group", f"{source}/", f"{destination}/", quiet=True)
        count += len(_package_folders(source))
    ctx.log(f"{count} driver package(s) go to Windows (C:\\Drivers, installed while Windows sets itself up).")
    return count
