"""Installation progress."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

from rich.text import Text
from textual import work
from textual.widget import Widget
from textual.widgets import Button, RichLog, Static

from ..plan import Step, build_plan, can_run_for_real, execute_plan
from ..runner import Runner
from ..steps.context import Context
from .base import WizardScreen

LOG_FILE = Path("/var/log/archinstall.log")

_ICONS = {"pending": ("[ ]", "dim"), "running": ("[>]", "bold yellow"), "done": ("[x]", "bold green"),
          "failed": ("[!]", "bold red"), "skipped": ("[-]", "dim")}


def final_button_label(ok: bool, dry_run: bool) -> str:
    """Only a real, successful installation offers to restart the machine."""
    return "Reboot now" if ok and not dry_run else "Exit"


def append_to_log_file(line: str) -> None:
    """Keep a copy of the log on the live system, for bug reports. Best effort."""
    try:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


class InstallScreen(WizardScreen):
    TITLE_TEXT = "Installing"
    SHOW_BACK = False

    def __init__(self, position: int, total: int) -> None:
        super().__init__(position, total)
        self._plan: list[Step] = []
        self._status: list[str] = []
        self.finished = False
        self.failed = False
        self.reboot_on_exit = False

    def body(self) -> Iterator[Widget]:
        yield Static("", id="steps")
        yield RichLog(id="log", wrap=True, highlight=False, markup=False)

    def on_mount(self) -> None:
        self.query_one("#next", Button).display = False
        self.query_one("#back", Button).display = False
        layout = self.cfg.layout()
        self._plan = build_plan(self.cfg, layout)
        self._status = ["pending"] * len(self._plan)
        self._render_steps()
        if self.cfg.dry_run:
            self._write("DRY RUN: commands are printed, nothing is executed.")
        elif self.cfg.partial:
            self._write("PARTIAL MODE: Windows is skipped; only Arch Linux and GRUB are installed.")

        if not self.cfg.dry_run and not can_run_for_real(self._plan, self.cfg):
            self.failed = self.finished = True
            self._write("Refusing to start: the plan is incomplete, so the disk is untouched.")
            for step in self._plan:
                if not step.implemented:
                    self._write(f"  - missing: {step.title}")
            self._write("Restart with --partial to skip the Windows installation, "
                        "or with --dry-run to preview the commands.")
            self.show_error("The installer cannot run this plan: nothing was changed. "
                            "Use --partial (skip Windows) or --dry-run (preview).")
            self._show_exit(ok=False)
            return
        self._execute()

    # -- worker -------------------------------------------------------------

    @work(thread=True)
    def _execute(self) -> None:
        def log(line: str) -> None:
            if not self.cfg.dry_run:
                append_to_log_file(line)
            self.app.call_from_thread(self._write, line)

        ctx = Context(self.cfg, self.cfg.layout(), Runner(dry_run=self.cfg.dry_run, log=log))
        ok = execute_plan(
            self._plan, ctx, lambda index, status: self.app.call_from_thread(self._set_status, index, status))
        self.app.call_from_thread(self._done, ok)

    # -- UI (main thread) ----------------------------------------------------

    def _write(self, line: str) -> None:
        self.query_one("#log", RichLog).write(Text(line))

    def _set_status(self, index: int, status: str) -> None:
        self._status[index] = status
        self._render_steps()

    def _render_steps(self) -> None:
        text = Text()
        for step, status in zip(self._plan, self._status):
            icon, style = _ICONS[status]
            text.append(f"{icon} {step.title}\n", style=style)
        self.query_one("#steps", Static).update(text)

    def _done(self, ok: bool) -> None:
        self.finished, self.failed = True, not ok
        if ok and self.cfg.dry_run:
            self._write("Dry run finished. Nothing was changed.")
        elif ok:
            self._write("Finished. Remove the installation medium, then reboot. The first restart "
                        "continues with the Windows installation (unless Windows was skipped).")
        else:
            self._write("The installation failed. See the log above.")
        self._show_exit(ok=ok)

    def _show_exit(self, *, ok: bool) -> None:
        button = self.query_one("#next", Button)
        button.label = final_button_label(ok, self.cfg.dry_run)
        self.reboot_on_exit = button.label == "Reboot now"
        button.display = True

    def action_next(self) -> None:
        # The "Next" button is relabelled "Reboot now" or "Exit" once the run is over.
        if not self.finished:
            return
        if self.reboot_on_exit:
            subprocess.run(["systemctl", "reboot"], check=False)
        self.app.exit()
