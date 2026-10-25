"""Welcome screen and system checks."""

from __future__ import annotations

from collections.abc import Iterator

from rich.table import Table
from rich.text import Text
from textual.widget import Widget
from textual.widgets import Static

from .base import WizardScreen

WELCOME_TEXT = """\
What this installer does:

  1. Asks you how to set up Arch Linux and Windows 11 (region, accounts, software...).
  2. ERASES the disk you choose and partitions it automatically.
  3. Installs Arch Linux with a signed GRUB boot loader, so Secure Boot can stay enabled.
  4. Installs Windows 11 unattended from an ISO you provide (included, a file, or a download
     link), using the answers you give here.

Move with Tab / Shift+Tab, press Enter to confirm, Escape to go back."""

_STATUS_STYLE = {"ok": ("OK", "bold green"), "warn": ("WARN", "bold yellow"), "fail": ("FAIL", "bold red")}


class WelcomeScreen(WizardScreen):
    TITLE_TEXT = "Welcome to ArchInstall"
    INTRO = "Arch Linux + Windows 11 dual boot, with Secure Boot."
    SHOW_BACK = False

    def body(self) -> Iterator[Widget]:
        yield Static(WELCOME_TEXT, id="welcome-text")
        yield Static("System checks", classes="section")
        yield Static("", id="checks")

    def on_mount(self) -> None:
        table = Table.grid(padding=(0, 2))
        for check in self.system.checks():
            label, style = _STATUS_STYLE[check.status]
            table.add_row(Text(label, style=style), Text(check.name, style="bold"), Text(check.detail))
        self.query_one("#checks", Static).update(table)

    def collect(self) -> str | None:
        failed = [c for c in self.system.checks() if c.blocking and c.status == "fail"]
        if failed:
            return "Cannot continue: " + " ".join(f"{c.name}: {c.detail}" for c in failed)
        return None
