"""Target disk and partition sizes."""

from __future__ import annotations

from collections.abc import Iterator

from rich.table import Table
from rich.text import Text
from textual import on
from textual.widget import Widget
from textual.widgets import DataTable, Input, Static, Switch

from ..layout import (
    MIN_WINDOWS_GIB,
    ROLE_ESP,
    ROLE_HOME,
    ROLE_ROOT,
    ROLE_SHARED,
    ROLE_WINDOWS,
    STAGING_GIB,
    compute_layout,
    default_sizes,
)
from ..system.models import DiskInfo
from ..util import human_size
from ..widgets import field, field_box, switch_row, text_input
from .base import WizardScreen


class DiskScreen(WizardScreen):
    TITLE_TEXT = "Target disk"
    INTRO = "Choose the disk for both systems."

    def __init__(self, position: int, total: int) -> None:
        super().__init__(position, total)
        self._disks: list[DiskInfo] = []

    def body(self) -> Iterator[Widget]:
        yield Static("ALL DATA ON THE DISK YOU SELECT WILL BE PERMANENTLY ERASED.", id="disk-warning")
        yield DataTable(id="disks", cursor_type="row", zebra_stripes=True)
        yield Static("", id="disk-selected")

    def on_mount(self) -> None:
        self._disks = self.system.disks()
        table = self.query_one("#disks", DataTable)
        table.add_columns("Device", "Size", "Model", "Bus", "Notes")
        for disk in self._disks:
            notes = []
            if disk.is_live_medium:
                notes.append("LIVE MEDIUM - cannot be used")
            if disk.removable:
                notes.append("removable")
            if disk.partition_table:
                notes.append(f"existing {disk.partition_table} partition table")
            table.add_row(disk.path, human_size(disk.size_bytes), disk.model or "-",
                          disk.transport or "-", ", ".join(notes))
        if not self._disks:
            self.query_one("#disk-selected", Static).update(Text("No disk found.", style="bold red"))
            return
        current = self.cfg.disk.device
        row = next((i for i, d in enumerate(self._disks) if d.path == current), 0)
        table.move_cursor(row=row)
        self._describe(row)
        table.focus()

    @on(DataTable.RowHighlighted, "#disks")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._describe(event.cursor_row)

    def _describe(self, row: int) -> None:
        if 0 <= row < len(self._disks):
            disk = self._disks[row]
            self.query_one("#disk-selected", Static).update(
                Text(f"Selected: {disk.path}  ({human_size(disk.size_bytes)}, {disk.model or 'unknown model'})",
                     style="bold"))

    def collect(self) -> str | None:
        table = self.query_one("#disks", DataTable)
        if not self._disks:
            return "No disk available."
        disk = self._disks[table.cursor_row]
        if disk.is_live_medium:
            return "This is the disk the installer was started from. Choose another one."
        d = self.cfg.disk
        d.device, d.model, d.size_bytes, d.sector_size = disk.path, disk.model, disk.size_bytes, disk.sector_size
        if d.sizes_for_device != disk.path:  # new disk: restart from sensible sizes
            sizes = default_sizes(disk.size_bytes)
            d.windows_gib, d.root_gib, d.shared_gib = sizes["windows_gib"], sizes["root_gib"], sizes["shared_gib"]
            d.sizes_for_device = disk.path
        return None


class LayoutScreen(WizardScreen):
    TITLE_TEXT = "Partition sizes"
    INTRO = ("The disk is split automatically. Choose how much space Windows gets; Arch Linux "
             "takes what is left.")

    def body(self) -> Iterator[Widget]:
        d = self.cfg.disk
        yield from field(f"Windows 11 size (GiB, minimum {MIN_WINDOWS_GIB})",
                         text_input("windows-gib", str(d.windows_gib), type="integer"))
        yield switch_row("Use a separate /home partition for Arch Linux", d.home_enabled, id="home-switch")
        yield field_box("Arch Linux root size (GiB); /home takes the rest",
                        text_input("root-gib", str(d.root_gib), type="integer"), id="root-box")
        yield switch_row("Create a shared NTFS partition readable by both systems", d.shared_enabled,
                         id="shared-switch")
        yield field_box(f"Shared partition size (GiB, minimum {STAGING_GIB})",
                        text_input("shared-gib", str(d.shared_gib), type="integer"), id="shared-box")
        yield Static("Resulting layout", classes="section")
        yield Static("", id="layout-preview")
        yield Static(f"Without a shared partition, a {STAGING_GIB} GiB temporary partition holds the Windows "
                     "setup files at the end of the disk and is removed once Windows is installed.",
                     classes="hint")

    def on_mount(self) -> None:
        self._sync_visibility()
        self._refresh()

    @staticmethod
    def _int(widget: Input) -> int:
        try:
            return int(widget.value)
        except ValueError:
            return 0

    def _inputs(self) -> dict:
        return {
            "windows_gib": self._int(self.query_one("#windows-gib", Input)),
            "root_gib": self._int(self.query_one("#root-gib", Input)),
            "home_enabled": self.query_one("#home-switch", Switch).value,
            "shared_enabled": self.query_one("#shared-switch", Switch).value,
            "shared_gib": self._int(self.query_one("#shared-gib", Input)),
        }

    def _sync_visibility(self) -> None:
        values = self._inputs()
        self.query_one("#root-box").display = values["home_enabled"]
        self.query_one("#shared-box").display = values["shared_enabled"]

    def _layout(self):
        d = self.cfg.disk
        return compute_layout(d.size_bytes, d.sector_size, **self._inputs())

    def _refresh(self) -> None:
        layout = self._layout()
        table = Table(box=None, padding=(0, 2), show_header=True, header_style="bold")
        for column in ("#", "Partition", "Size", "Use"):
            table.add_column(column)
        uses = {
            ROLE_ESP: "EFI system partition, mounted on /boot (GRUB, kernels)",
            ROLE_WINDOWS: "Windows 11 (C:)",
            ROLE_ROOT: "Arch Linux (/)",
            ROLE_HOME: "Arch Linux (/home)",
        }
        for p in layout.partitions:
            if p.role == ROLE_SHARED:
                use = "Shared NTFS data" if not p.temporary else "Temporary Windows setup files (removed later)"
            else:
                use = uses[p.role]
            table.add_row(str(p.number), p.label, f"{p.size_mib / 1024:.1f} GiB", use)
        renderable = table if layout.partitions else Text("The sizes do not fit on this disk.", style="red")
        self.query_one("#layout-preview", Static).update(renderable)
        self.show_error(layout.errors[0] if layout.errors else "")

    @on(Input.Changed)
    def _input_changed(self) -> None:
        self._refresh()

    @on(Switch.Changed)
    def _switch_changed(self) -> None:
        self._sync_visibility()
        self._refresh()

    def collect(self) -> str | None:
        layout = self._layout()
        if layout.errors:
            return layout.errors[0]
        values = self._inputs()
        d = self.cfg.disk
        d.windows_gib, d.root_gib = values["windows_gib"], values["root_gib"]
        d.home_enabled, d.shared_enabled, d.shared_gib = values["home_enabled"], values["shared_enabled"], values["shared_gib"]
        return None
