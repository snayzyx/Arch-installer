"""Checks done before the first byte is written to the disk."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .. import secureboot
from ..config import InstallConfig
from .arch import cpu_ucode, package_list, strategy_of, wants_laptop
from .bootloader import fetch_shim
from .context import TARGET, Context, StepError
from .storage import CRYPT_HOME, CRYPT_ROOT
from .windows import check_source

STALE_MAPPINGS = (CRYPT_HOME, CRYPT_ROOT)


def required_tools(cfg: InstallConfig) -> list[str]:
    tools = ["sgdisk", "wipefs", "partprobe", "udevadm", "blkid", "lsblk", "findmnt", "mkfs.fat",
             f"mkfs.{cfg.arch.filesystem}", "mount", "umount", "pacstrap", "arch-chroot",
             "reflector", "pacman", "efibootmgr"]
    if cfg.arch.filesystem == "btrfs":
        tools.append("btrfs")
    if cfg.arch.luks_enabled:
        tools.append("cryptsetup")
    if strategy_of(cfg) == "shim":
        tools += ["bsdtar", "curl", "sha256sum", "openssl"]
        if cfg.secureboot.touch_firmware:
            tools.append("mokutil")
    if not cfg.partial:  # Windows is installed
        tools += ["wimlib-imagex", "rsync", "curl"]
    return list(dict.fromkeys(tools))


def preflight(ctx: Context) -> None:
    cfg = ctx.cfg
    dry_run = ctx.runner.dry_run

    if not dry_run:
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            raise StepError("The installer must run as root.")
        missing = [tool for tool in required_tools(cfg) if shutil.which(tool) is None]
        if missing:
            raise StepError("These tools are missing on this live system: " + ", ".join(missing))

        # Never touch a disk that is in use, or a /mnt left over from an earlier attempt.
        if ctx.capture("findmnt", "-n", TARGET):
            raise StepError(f"{TARGET} is already mounted. Unmount it first: umount -R {TARGET}")
        mounted = [line for line in ctx.capture("lsblk", "-nro", "MOUNTPOINTS", cfg.disk.device).splitlines()
                   if line.strip()]
        if mounted:
            raise StepError(f"{cfg.disk.device} has mounted partitions ({', '.join(mounted)}).")

        # A failed earlier attempt can leave its encrypted volumes open. They would hold the disk
        # busy and make `cryptsetup open` fail with "device already exists".
        for name in STALE_MAPPINGS:
            if Path(f"/dev/mapper/{name}").exists():
                ctx.log(f"/dev/mapper/{name} was left open by an earlier attempt: closing it.")
                ctx.run("cryptsetup", "close", name)

    strategy = strategy_of(cfg)
    if strategy == "sbctl" and cfg.secureboot.touch_firmware and not dry_run:
        # No TPM or not in Setup Mode: sbctl cannot work. Better to stop now than after the erase.
        problem = secureboot.sbctl_problem(secureboot.detect())
        if problem:
            raise StepError(problem)
    if strategy == "shim":
        fetch_shim(ctx)  # download and verify now, not after the disk has been erased
    if not cfg.partial:
        check_source(ctx)  # the Windows image must be usable before anything is erased

    # A package name that does not exist must fail here, not halfway through pacstrap.
    ctx.run("pacman", "-Sy", "--noconfirm", quiet=True)
    ctx.run("pacman", "-Sp", "--print-format", "%n", *package_list(cfg, cpu_ucode(), laptop=wants_laptop(cfg)), quiet=True)
