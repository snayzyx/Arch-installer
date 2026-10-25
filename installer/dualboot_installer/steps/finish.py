"""The last step."""

from __future__ import annotations

from .context import Context
from .storage import unmount_target


def finish(ctx: Context) -> None:
    unmount_target(ctx)
    if ctx.cfg.partial:
        ctx.log("Arch Linux is installed. Windows was NOT installed (partial mode): the Windows "
                "partition is empty.")
    ctx.log("Done. Remove the installation medium and reboot.")
