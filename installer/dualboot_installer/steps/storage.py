"""Partitioning, filesystems and mounting the target."""

from __future__ import annotations

from ..config import InstallConfig
from ..layout import (
    ROLE_ESP,
    ROLE_HOME,
    ROLE_ROOT,
    ROLE_SHARED,
    ROLE_WINDOWS,
    Layout,
    partition_path,
)
from ..runner import Command
from .context import TARGET, Context, StepError

# sgdisk type codes
_TYPE_ESP = "EF00"
_TYPE_MS_BASIC_DATA = "0700"
_TYPE_LINUX_ROOT = "8304"
_TYPE_LINUX_HOME = "8302"
_TYPE_LINUX_LUKS = "8309"

CRYPT_ROOT = "cryptroot"
CRYPT_HOME = "crypthome"
BTRFS_OPTIONS = "compress=zstd,noatime"
UNMOUNT_ATTEMPTS = 10


def _typecode(role: str, luks: bool) -> str:
    if role == ROLE_ESP:
        return _TYPE_ESP
    if role in (ROLE_WINDOWS, ROLE_SHARED):
        return _TYPE_MS_BASIC_DATA
    if luks:
        return _TYPE_LINUX_LUKS
    return _TYPE_LINUX_ROOT if role == ROLE_ROOT else _TYPE_LINUX_HOME


def partition_commands(cfg: InstallConfig, layout: Layout) -> list[Command]:
    device = cfg.disk.device
    luks = cfg.arch.luks_enabled

    create = ["sgdisk"]
    for p in layout.partitions:
        create += [
            "-n", f"{p.number}:{p.start_sector}:{p.end_sector}",
            "-t", f"{p.number}:{_typecode(p.role, luks)}",
            "-c", f"{p.number}:{p.label}",
        ]
    create.append(device)

    return [
        Command(["wipefs", "--all", "--force", device]),
        Command(["sgdisk", "--zap-all", device]),
        Command(create),
        # The WinPE launcher finds this disk by GUID: Windows numbers disks differently from Linux.
        Command(["sgdisk", f"--disk-guid={cfg.disk.guid}", device]),
        Command(["partprobe", device]),
        Command(["udevadm", "settle"]),
    ]


def _mkfs(filesystem: str, label: str, target: str) -> Command:
    if filesystem == "ext4":
        return Command(["mkfs.ext4", "-F", "-L", label, target])
    if filesystem == "btrfs":
        return Command(["mkfs.btrfs", "-f", "-L", label, target])
    if filesystem == "xfs":
        return Command(["mkfs.xfs", "-f", "-L", label, target])
    raise ValueError(f"Unsupported filesystem: {filesystem}")


def _luks_and_mkfs(cfg: InstallConfig, part_path: str, mapper: str, label: str) -> list[Command]:
    arch = cfg.arch
    if not arch.luks_enabled:
        return [_mkfs(arch.filesystem, label, part_path)]
    passphrase = arch.luks_passphrase
    return [
        Command(["cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", "--key-file=-", part_path],
                stdin=passphrase, secret=True),
        Command(["cryptsetup", "open", "--key-file=-", part_path, mapper], stdin=passphrase, secret=True),
        _mkfs(arch.filesystem, label, f"/dev/mapper/{mapper}"),
    ]


def format_commands(cfg: InstallConfig, layout: Layout) -> list[Command]:
    device = cfg.disk.device
    commands: list[Command] = []

    esp = layout.get(ROLE_ESP)
    staging = layout.get(ROLE_SHARED)
    root = layout.get(ROLE_ROOT)
    home = layout.get(ROLE_HOME)
    assert esp and staging and root

    commands.append(Command(["mkfs.fat", "-F", "32", "-n", "ESP", partition_path(device, esp.number)]))
    # The Windows partition is deliberately left unformatted: Windows Setup formats it itself.
    commands.append(Command(["mkfs.fat", "-F", "32", "-n", "WINSETUP", partition_path(device, staging.number)]))
    commands += _luks_and_mkfs(cfg, partition_path(device, root.number), CRYPT_ROOT, "archroot")
    if home:
        commands += _luks_and_mkfs(cfg, partition_path(device, home.number), CRYPT_HOME, "archhome")
    return commands


def root_device(cfg: InstallConfig, layout: Layout) -> str:
    """Block device holding the Arch root filesystem (the LUKS mapping when encrypted)."""
    if cfg.arch.luks_enabled:
        return f"/dev/mapper/{CRYPT_ROOT}"
    root = layout.get(ROLE_ROOT)
    assert root
    return partition_path(cfg.disk.device, root.number)


def home_device(cfg: InstallConfig, layout: Layout) -> str | None:
    home = layout.get(ROLE_HOME)
    if home is None:
        return None
    return f"/dev/mapper/{CRYPT_HOME}" if cfg.arch.luks_enabled else partition_path(cfg.disk.device, home.number)


def mount_commands(cfg: InstallConfig, layout: Layout) -> list[Command]:
    """Mount the new system under /mnt, ready for pacstrap."""
    fs = cfg.arch.filesystem
    root_dev, home_dev = root_device(cfg, layout), home_device(cfg, layout)
    esp = layout.get(ROLE_ESP)
    assert esp
    esp_dev = partition_path(cfg.disk.device, esp.number)
    commands: list[Command] = []

    if fs == "btrfs":
        # Subvolumes @ and @home; @home only when /home is not a partition of its own.
        commands += [
            Command(["mount", root_dev, TARGET]),
            Command(["btrfs", "subvolume", "create", f"{TARGET}/@"]),
        ]
        if home_dev is None:
            commands.append(Command(["btrfs", "subvolume", "create", f"{TARGET}/@home"]))
        commands += [
            Command(["umount", TARGET]),
            Command(["mount", "-o", f"subvol=@,{BTRFS_OPTIONS}", root_dev, TARGET]),
        ]
        commands.append(Command(["mkdir", "-p", f"{TARGET}/home", f"{TARGET}/boot"]))
        if home_dev is None:
            commands.append(Command(["mount", "-o", f"subvol=@home,{BTRFS_OPTIONS}", root_dev, f"{TARGET}/home"]))
    else:
        commands += [
            Command(["mount", root_dev, TARGET]),
            Command(["mkdir", "-p", f"{TARGET}/home", f"{TARGET}/boot"]),
        ]

    if home_dev is not None:
        options = ["-o", BTRFS_OPTIONS] if fs == "btrfs" else []
        commands.append(Command(["mount", *options, home_dev, f"{TARGET}/home"]))

    commands.append(Command(["mount", "-o", "umask=0077", esp_dev, f"{TARGET}/boot"]))
    return commands


# -- step functions ------------------------------------------------------------


def partition(ctx: Context) -> None:
    for command in partition_commands(ctx.cfg, ctx.layout):
        ctx.runner.run(command)


def format_disks(ctx: Context) -> None:
    for command in format_commands(ctx.cfg, ctx.layout):
        ctx.runner.run(command)


def mount_target(ctx: Context) -> None:
    for command in mount_commands(ctx.cfg, ctx.layout):
        ctx.runner.run(command)


def unmount_target(ctx: Context) -> None:
    ctx.run("sync")
    # pacstrap starts a gpg-agent (and dirmngr) inside the new system; left running, they keep
    # /mnt busy and the unmount fails.
    ctx.run("gpgconf", "--homedir", ctx.target_path("/etc/pacman.d/gnupg"), "--kill", "all", check=False)
    # The agents take a moment to exit after being told to, so retry for a few seconds.
    for _ in range(UNMOUNT_ATTEMPTS):
        if ctx.run("umount", "-R", TARGET, check=False, quiet=True) == 0:
            break
        ctx.run("sleep", "1")
    else:
        ctx.run("umount", "-R", TARGET, check=False)  # show the real error once
        ctx.run("fuser", "-vm", TARGET, check=False)  # and what is holding the mount
        raise StepError(f"Cannot unmount {TARGET}: something is still using it (listed above).")
    if ctx.cfg.arch.luks_enabled:
        if ctx.layout.get(ROLE_HOME):
            ctx.run("cryptsetup", "close", CRYPT_HOME)
        ctx.run("cryptsetup", "close", CRYPT_ROOT)
