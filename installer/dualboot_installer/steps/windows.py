"""Installing Windows 11: get the ISO, stage Windows Setup, arrange the first boot.

How it works (docs/ARCHITECTURE.md):

1. The ISO's files are copied to the FAT32 ``WINSETUP`` partition. UEFI can only boot from
   FAT32 and install.wim is bigger than 4 GiB, so the image is split into ``install.swm`` parts.
2. boot.wim (the Setup image) gets a launcher that finds the target disk by its GPT GUID and
   starts Setup with the matching answer file.
3. A one-time firmware boot entry ("Windows 11 Setup", BootNext) starts Microsoft's boot manager
   from WINSETUP. GRUB cannot chainload it: the boot manager then looks for its BCD on the ESP
   and stops with 0xc000000f, so the firmware has to start it.
4. At the end of Windows Setup, a script puts GRUB first again, deletes the one-time entry and
   turns WINSETUP into the shared partition, or deletes it.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..layout import ROLE_SHARED, ROLE_WINDOWS
from ..windows_iso import WimImage, parse_wimlib_info
from ..windows_unattend import (
    ANSWER_DIR,
    MAX_DISKS,
    SETUP_ENTRY_TITLE,
    SETUP_LOADER,
    bootorder_script,
    build_unattend,
    finish_script,
    launcher_cmd,
    setup_complete_cmd,
    winpeshl_ini,
)
from .context import TARGET, Context, StepError

ISO_MOUNT = "/tmp/archinstall-windows-iso"
SETUP_MOUNT = "/tmp/archinstall-windows-setup"
STAGE_DIR = "/tmp/archinstall-windows-stage"
DOWNLOAD_TARGET = f"{TARGET}/var/tmp/archinstall-windows11.iso"
CONVERTED_IMAGE = f"{TARGET}/var/tmp/archinstall-install.wim"

FAT32_PART_MIB = 3800  # install.swm parts: below FAT32's 4 GiB file limit
MIN_ISO_BYTES = 3 * 1024**3


# -- pure helpers (unit tested) -----------------------------------------------------------------


def parse_headers(text: str) -> tuple[int, int]:
    """(status, content-length) of the LAST response in `curl -sIL` output; 0 when absent."""
    blocks = [b for b in re.split(r"\r?\n\r?\n", text.strip()) if b.strip()]
    if not blocks:
        return 0, 0
    last = blocks[-1]
    status = re.search(r"^HTTP/\S+\s+(\d{3})", last, re.MULTILINE)
    length = re.search(r"^content-length:\s*(\d+)", last, re.IGNORECASE | re.MULTILINE)
    return (int(status.group(1)) if status else 0, int(length.group(1)) if length else 0)


def pick_image(images: list[WimImage], edition: str) -> WimImage | None:
    return next((image for image in images if image.edition == edition), None)


def boot_wim_setup_index(info_text: str) -> int:
    """Index of the 'Windows Setup' image inside boot.wim (normally 2; index 1 is plain WinPE)."""
    best = 0
    for match in re.finditer(r"^Index:\s+(\d+)\s*\r?\nName:\s+(.*)$", info_text, re.MULTILINE):
        if "setup" in match.group(2).lower():
            best = int(match.group(1))
    return best or 2


def wim_update_script(stage_dir: str) -> str:
    """wimlib `update` commands that put the launcher into the Setup image."""
    return (f"add {stage_dir}/winpeshl.ini /Windows/System32/winpeshl.ini\n"
            f"add {stage_dir}/archinstall.cmd /Windows/System32/archinstall.cmd\n")


# -- helpers that need the system -------------------------------------------------------------------


def mount_iso(ctx: Context, iso: str, mount_point: str = ISO_MOUNT) -> None:
    ctx.run("mkdir", "-p", mount_point)
    # Windows ISOs are UDF; the ISO 9660 view of them is just a README.
    if ctx.run("mount", "-t", "udf", "-o", "loop,ro", iso, mount_point, check=False, quiet=True):
        ctx.run("mount", "-t", "iso9660", "-o", "loop,ro", iso, mount_point)


def install_image_path(ctx: Context) -> str:
    wim = f"{ISO_MOUNT}/sources/install.wim"
    if ctx.runner.dry_run or Path(wim).is_file():
        return wim
    esd = f"{ISO_MOUNT}/sources/install.esd"
    if Path(esd).is_file():
        return esd
    raise StepError("This ISO has no sources/install.wim: it is not a Windows installation ISO.")


def inspect_iso(ctx: Context, iso: str) -> WimImage:
    """Mount the ISO, read its images, return the one for the chosen edition. Unmounts again."""
    edition = ctx.cfg.windows.edition
    mount_iso(ctx, iso)
    try:
        info = ctx.capture("wimlib-imagex", "info", install_image_path(ctx))
    finally:
        ctx.run("umount", ISO_MOUNT, check=False)
    images = parse_wimlib_info(info)
    if not images:
        if ctx.runner.dry_run:
            return WimImage(1, f"Windows 11 {edition}", "", (), ctx.cfg.windows.language_tag, "", "x86_64")
        raise StepError(f"Cannot read the Windows images in {iso}.")
    image = pick_image(images, edition)
    if image is None:
        available = ", ".join(sorted({i.edition for i in images if i.edition})) or "none"
        raise StepError(f"This ISO has no Windows 11 {edition} image (editions available: {available}).")
    return image


# -- steps ---------------------------------------------------------------------------------------------


def check_source(ctx: Context) -> None:
    """Preflight: the Windows image must be usable BEFORE the disk is erased."""
    w = ctx.cfg.windows
    if w.iso_source == "download":
        headers = ctx.capture("curl", "-sIL", "--max-time", "30", w.iso_url)
        if headers or not ctx.runner.dry_run:
            status, size = parse_headers(headers)
            if status not in (200, 206) or size < MIN_ISO_BYTES:
                raise StepError(
                    "The download link does not give a Windows ISO (expired? the links from Microsoft "
                    f"last 24 hours). HTTP status {status or 'none'}, size {size} bytes.")
        return

    iso = w.iso_path
    if not ctx.runner.dry_run and not Path(iso).is_file():
        raise StepError(f"The Windows ISO was not found: {iso}")
    image = inspect_iso(ctx, iso)
    # The answer file must use the language the ISO is really in.
    if image.default_language:
        w.language_tag = image.default_language
    ctx.state["image_index"] = image.index
    ctx.log(f"Windows image: {image.name} (index {image.index}, {image.default_language or 'language unknown'}).")


def fetch_iso(ctx: Context) -> None:
    """Make the ISO available as a local file, and find the image to install."""
    w = ctx.cfg.windows
    if w.iso_source == "download":
        # curl resumes (-C -) and retries: the link is good for about a day, not for flaky Wi-Fi.
        ctx.log("Downloading the Windows ISO. This takes a while and shows no progress.")
        # --retry alone only covers timeouts and HTTP 5xx: a connection that drops midway (exit 56)
        # needs --retry-all-errors, otherwise the install stops after the disk was erased.
        ctx.run("curl", "-sS", "-L", "--fail", "--retry", "5", "--retry-all-errors", "--retry-delay", "5",
                "-C", "-", "-o", DOWNLOAD_TARGET, w.iso_url)
        image = inspect_iso(ctx, DOWNLOAD_TARGET)
        if image.default_language:
            w.language_tag = image.default_language
        ctx.state["windows_iso"] = DOWNLOAD_TARGET
        ctx.state["image_index"] = image.index
    else:
        ctx.state["windows_iso"] = w.iso_path
        ctx.state.setdefault("image_index", 1)


def schedule_setup_boot(ctx: Context) -> None:
    """Make the NEXT boot start Windows Setup from the WINSETUP partition, once.

    The firmware starts Microsoft's boot manager itself (a one-time BootNext entry): chain-loaded
    from GRUB, that boot manager looks for its BCD on the EFI system partition instead of its
    own partition and fails with 0xc000000f (seen in the QEMU test). Booted by the firmware,
    it uses the partition it was loaded from, like Rufus media do.
    """
    if not ctx.cfg.secureboot.touch_firmware:
        ctx.log("Skipping the firmware boot entry for Windows Setup (touch_firmware is off).")
        return
    setup = ctx.layout.get(ROLE_SHARED)
    if setup is None:
        raise StepError("The layout has no WINSETUP partition.")

    pattern = rf"^Boot([0-9A-Fa-f]{{4}})\*?\s+{re.escape(SETUP_ENTRY_TITLE)}\b"
    for number in re.findall(pattern, ctx.capture("efibootmgr"), re.MULTILINE):
        ctx.run("efibootmgr", "--bootnum", number, "--delete-bootnum")  # left by an earlier attempt

    # --create puts the entry FIRST in the boot order. BootNext alone is not enough: some firmwares
    # ignore it (the QEMU/OVMF test did), and only "first in the boot order" was proven to work with
    # Windows Setup. The script that ends Windows Setup deletes this temporary entry again.
    created = ctx.capture("efibootmgr", "--create", "--disk", ctx.cfg.disk.device, "--part", str(setup.number),
                          "--label", SETUP_ENTRY_TITLE, "--loader", SETUP_LOADER)
    found = re.search(pattern, created, re.MULTILINE)
    if found is None and not ctx.runner.dry_run:
        raise StepError("The firmware refused the boot entry for Windows Setup (efibootmgr failed).")
    ctx.run("efibootmgr", "--bootnext", found.group(1) if found else "XXXX")
    ctx.log(f"The next boot starts '{SETUP_ENTRY_TITLE}'. Windows installs itself and restarts several times.")
    ctx.log(f"If the computer starts Arch Linux instead, open the firmware boot menu (often F12, F11 or Esc "
            f"at power on) and choose '{SETUP_ENTRY_TITLE}'.")


def stage_setup(ctx: Context) -> None:
    cfg, w = ctx.cfg, ctx.cfg.windows
    iso = str(ctx.state["windows_iso"])
    index = int(ctx.state.get("image_index", 1))  # type: ignore[call-overload]
    windows_part = ctx.layout.get(ROLE_WINDOWS)
    if windows_part is None:
        raise StepError("The layout has no Windows partition.")

    ctx.run("mkdir", "-p", SETUP_MOUNT, STAGE_DIR)
    ctx.run("mount", ctx.partition(ROLE_SHARED), SETUP_MOUNT)
    mount_iso(ctx, iso)
    wim = install_image_path(ctx)

    # 1. Everything except the big image, then the image in FAT32-sized parts.
    ctx.log("Copying the Windows Setup files (several minutes)...")
    ctx.run("rsync", "-rt", "--no-perms", "--no-owner", "--no-group",
            "--exclude=/sources/install.wim", "--exclude=/sources/install.esd",
            f"{ISO_MOUNT}/", f"{SETUP_MOUNT}/", quiet=True)
    if wim.endswith(".esd"):
        # wimlib cannot split an ESD directly: convert it first (needs room on the new system).
        ctx.run("wimlib-imagex", "export", wim, "all", CONVERTED_IMAGE, "--compress=LZX", quiet=True)
        wim = CONVERTED_IMAGE
    ctx.log("Splitting the Windows image for FAT32...")
    ctx.run("wimlib-imagex", "split", wim, f"{SETUP_MOUNT}/sources/install.swm", str(FAT32_PART_MIB), quiet=True)

    # 2. The launcher inside the Setup image.
    ctx.write_host(f"{STAGE_DIR}/winpeshl.ini", winpeshl_ini())
    ctx.write_host(f"{STAGE_DIR}/archinstall.cmd", launcher_cmd(cfg.disk.guid))
    boot_wim = f"{SETUP_MOUNT}/sources/boot.wim"
    setup_index = boot_wim_setup_index(ctx.capture("wimlib-imagex", "info", boot_wim))
    ctx.run("wimlib-imagex", "update", boot_wim, str(setup_index), stdin=wim_update_script(STAGE_DIR), quiet=True)

    # 3. One answer file per possible disk number, and the scripts for the end of Setup.
    for disk in range(MAX_DISKS):
        ctx.write_host(f"{SETUP_MOUNT}/{ANSWER_DIR}/unattend-{disk}.xml",
                       build_unattend(cfg, disk_id=disk, partition_id=windows_part.number, image_index=index),
                       secret=True)
    scripts = f"{SETUP_MOUNT}/sources/$OEM$/$$/Setup/Scripts"
    ctx.write_host(f"{scripts}/SetupComplete.cmd", setup_complete_cmd())
    ctx.write_host(f"{scripts}/archinstall-finish.ps1", finish_script(w, shared_partition=cfg.disk.shared_enabled))
    ctx.write_host(f"{scripts}/archinstall-bootorder.ps1", bootorder_script())

    # 4. Clean up, and make the next boot go to Windows Setup.
    ctx.run("sync")
    ctx.run("umount", SETUP_MOUNT)
    ctx.run("umount", ISO_MOUNT)
    ctx.run("rm", "-rf", STAGE_DIR)
    if iso == DOWNLOAD_TARGET:
        ctx.run("rm", "-f", DOWNLOAD_TARGET)
    if wim == CONVERTED_IMAGE:
        ctx.run("rm", "-f", CONVERTED_IMAGE)
    schedule_setup_boot(ctx)
