"""The installation plan: an ordered list of steps.

Arch Linux + GRUB come first; then the Windows steps get the ISO ready and stage Windows
Setup, which the next boot starts. With ``cfg.partial`` the Windows steps are skipped: only
Arch Linux and GRUB are installed and the Windows partition stays empty.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from dataclasses import dataclass

from .config import InstallConfig
from .layout import Layout
from .runner import CommandError
from .steps import arch, bootloader, finish, preflight, storage, windows
from .steps.context import Context, StepError


@dataclass
class Step:
    key: str
    title: str
    run: Callable[[Context], None] | None = None
    note: str = ""  # what a missing step will do, shown while it is not implemented

    @property
    def implemented(self) -> bool:
        return self.run is not None


def build_plan(cfg: InstallConfig, layout: Layout) -> list[Step]:
    skipped = "Skipped: partial mode (Windows is not installed)."
    return [
        Step("preflight", "Check the system and the settings", preflight.preflight),
        Step("partition", "Partition the disk", storage.partition),
        Step("format", "Create the filesystems", storage.format_disks),
        Step("mount", "Mount the new system", storage.mount_target),
        Step("mirrors", "Rank the package mirrors", arch.mirrors),
        Step("base", "Install the Arch Linux base system", arch.install_base),
        Step("configure", "Configure Arch Linux", arch.configure_system),
        Step("bootloader", "Install GRUB and set up Secure Boot", bootloader.install_bootloader),
        Step("aur", "Install the AUR helper", arch.install_aur_helper),
        Step("windows-iso", "Get the Windows 11 ISO", None if cfg.partial else windows.fetch_iso, note=skipped),
        Step("windows-stage", "Stage the Windows 11 setup", None if cfg.partial else windows.stage_setup,
             note=skipped),
        Step("finish", "Unmount and finish", finish.finish),
    ]


def can_run_for_real(plan: list[Step], cfg: InstallConfig) -> bool:
    """A real run needs every step; the partial install deliberately leaves the Windows ones out."""
    return cfg.partial or all(step.implemented for step in plan)


def execute_plan(plan: list[Step], ctx: Context,
                 on_status: Callable[[int, str], None] = lambda index, status: None) -> bool:
    """Run the steps in order. Returns False at the first failure (the reason is logged)."""
    for index, step in enumerate(plan):
        on_status(index, "running")
        ctx.log(f"=== {step.title}")
        if step.run is None:
            ctx.log(f"(skipped) {step.note}")
            on_status(index, "skipped")
            continue
        try:
            step.run(ctx)
        except (CommandError, StepError) as exc:
            ctx.log(f"ERROR: {exc}")
            on_status(index, "failed")
            return False
        except Exception as exc:  # a bug must be reported, not kill the worker silently
            ctx.log(f"ERROR: unexpected {type(exc).__name__}: {exc}")
            for line in traceback.format_exc().splitlines():
                ctx.log(line)
            on_status(index, "failed")
            return False
        on_status(index, "done")
    return True
