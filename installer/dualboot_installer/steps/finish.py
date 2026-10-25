"""The last step."""

from __future__ import annotations

from .context import Context
from .storage import unmount_target


def finish(ctx: Context) -> None:
    # pacstrap leaves every package it downloaded in the new system's cache (1-2 GB): a laptop disk can
    # do without it, and the next update downloads what it needs anyway.
    ctx.chroot("bash", "-c", "rm -rf /var/cache/pacman/pkg/*", check=False)
    unmount_target(ctx)
    if ctx.cfg.partial:
        ctx.log("Arch Linux is installed. Windows was NOT installed (partial mode): the Windows "
                "partition is empty.")
    ctx.log("Done. Remove the installation medium and reboot.")
