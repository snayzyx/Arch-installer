"""Final review and confirmation."""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import urlsplit

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.widget import Widget
from textual.widgets import Input, Static

from ..config import InstallConfig, WindowsConfig
from ..drivers import packages_for
from ..layout import ROLE_ESP, ROLE_HOME, ROLE_ROOT, ROLE_SHARED, ROLE_WINDOWS
from ..secureboot import STRATEGY_LABELS
from ..steps.arch import wants_laptop
from ..util import human_size
from ..validate import problems
from ..widgets import field, text_input
from .base import WizardScreen


def _section(title: str, rows: list[tuple[str, str]]) -> Group:
    table = Table.grid(padding=(0, 2))
    for key, value in rows:
        table.add_row(Text(key, style="dim"), Text(value))
    return Group(Text(f"\n{title}", style="bold cyan"), table)


def describe_windows_source(w: WindowsConfig) -> str:
    """Where the Windows ISO comes from, for the summary.

    A download link is shown by host only: links from Microsoft carry a token that works as a
    password for the file, and the summary may be photographed or pasted into a bug report.
    """
    if w.iso_source == "embedded":
        return "included in this installer (offline)"
    if w.iso_source == "download":
        host = urlsplit(w.iso_url).hostname or "unknown host"
        return f"downloaded from a link you provided ({host})"
    return f"local file {w.iso_path}"


def build_summary(cfg: InstallConfig) -> Group:
    d, a, w, sb = cfg.disk, cfg.arch, cfg.windows, cfg.secureboot
    layout = cfg.layout()

    parts_table = Table(box=None, padding=(0, 2), header_style="bold")
    for column in ("#", "Partition", "Size", "Use"):
        parts_table.add_column(column)
    uses = {ROLE_ESP: "FAT32, /boot", ROLE_WINDOWS: f"NTFS, Windows 11 {w.edition}",
            ROLE_ROOT: f"{a.filesystem}{' in LUKS2' if a.luks_enabled else ''}, /",
            ROLE_HOME: f"{a.filesystem}{' in LUKS2' if a.luks_enabled else ''}, /home"}
    for p in layout.partitions:
        if p.role == ROLE_SHARED:
            if not p.temporary:
                use = "NTFS, shared data"
            else:
                use = "temporary, unused (Windows skipped)" if cfg.partial else "temporary, removed after the Windows setup"
        else:
            use = uses[p.role]
        parts_table.add_row(str(p.number), p.label, f"{p.size_mib / 1024:.1f} GiB", use)

    gpu_packages = packages_for(a.gpu_profiles, a.kernel)
    windows_source = describe_windows_source(w)
    windows_status = ("NOT installed (partial mode): its partition stays empty" if cfg.partial
                      else "installed automatically: Windows Setup starts on the first restart")
    regional = (f"{w.timezone} / {w.keyboard}" if w.custom_regional
                else f"same as Arch Linux ({cfg.windows_timezone_id()} / {cfg.windows_keyboard_tag()})")

    return Group(
        _section("Disk (will be ERASED)", [("Device", f"{d.device}  ({human_size(d.size_bytes)}, {d.model or 'unknown model'})")]),
        Text(""), parts_table,
        _section("Arch Linux", [
            ("Country / time zone", f"{a.country} / {a.timezone}"),
            ("Locale / keyboard", f"{a.locale} / {a.keymap}"),
            ("Hostname", a.hostname),
            ("User", f"{a.username} (sudo)" + ("; root has the same password" if a.root_same_password else "")),
            ("Kernel", a.kernel),
            ("Encryption", "LUKS2" if a.luks_enabled else "none"),
            ("Desktop", "Hyprland (ready to use)" if a.desktop == "hyprland" else "minimal, no desktop"),
            ("Graphics", ", ".join(gpu_packages) or "none"),
            ("Laptop extras", "yes: power profiles, Bluetooth, battery, SOF audio firmware"
                              if wants_laptop(cfg) else "no"),
            ("Wi-Fi", f"{cfg.network.wifi_ssid} (kept for the installed system)" if cfg.network.wifi_ssid else "none"),
            ("AUR helper", a.aur_helper),
            ("Extra packages", " ".join(a.extra_packages) or "none"),
            ("Swap", "zram"),
        ]),
        _section("Windows 11", [
            ("Status", windows_status),
            ("Image", f"{w.edition}, {w.language_name}, {windows_source}"),
            ("Computer / user", f"{w.computer_name} / {w.username} (local administrator)"),
            ("Time zone / keyboard", regional),
            *([] if cfg.partial else [("Tweak", tweak) for tweak in WindowsConfig.TWEAKS]),
        ]),
        _section("Boot", [
            ("Boot loader", "GRUB, default entry Arch Linux, 5 s menu"),
            ("Secure Boot", STRATEGY_LABELS.get(sb.resolved, sb.resolved or "not decided")),
        ]),
    )


class SummaryScreen(WizardScreen):
    TITLE_TEXT = "Review and install"
    NEXT_LABEL = "Install"

    def body(self) -> Iterator[Widget]:
        # The warnings come first: the review below is long and scrolls.
        if self.cfg.partial:
            yield Static("PARTIAL MODE: Windows is skipped. Only Arch Linux and GRUB are installed, "
                         "and the Windows partition stays empty.", id="disk-warning")
        yield Static("The disk will be erased. This cannot be undone.", id="disk-warning-erase",
                     classes="disk-warning")
        yield Static("", id="summary")
        if not self.cfg.dry_run:
            yield from field(f"Type {self.cfg.disk.device} to confirm", text_input("confirm"))

    def on_mount(self) -> None:
        self.query_one("#summary", Static).update(build_summary(self.cfg))

    def collect(self) -> str | None:
        found = problems(self.cfg)
        if found:
            return found[0]
        if not self.cfg.dry_run:
            typed = self.query_one("#confirm", Input).value.strip()
            if typed != self.cfg.disk.device:
                return f"Type {self.cfg.disk.device} exactly to confirm that the disk may be erased."
        return None
