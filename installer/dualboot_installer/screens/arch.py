"""Arch Linux questions: accounts, storage and software."""

from __future__ import annotations

from collections.abc import Iterator

from textual import on
from textual.widget import Widget
from textual.widgets import Input, RadioSet, Select, SelectionList, Static, Switch

from ..drivers import PROFILES, suggest_profiles
from ..validators import (
    parse_package_list,
    validate_hostname,
    validate_linux_username,
    validate_luks_passphrase,
    validate_password,
)
from ..widgets import choice_set, chosen, field, field_box, switch_row, text_input
from .base import WizardScreen


class AccountsScreen(WizardScreen):
    TITLE_TEXT = "Arch Linux: computer and accounts"
    INTRO = "Your user gets administrator rights through sudo."

    def body(self) -> Iterator[Widget]:
        a = self.cfg.arch
        yield from field("Computer name (hostname)", text_input("hostname", a.hostname),
                         hint="Lowercase letters, digits and hyphens.")
        yield from field("User name", text_input("username", a.username))
        yield from field("User password", text_input("user-pass", a.user_password, password=True))
        yield from field("Confirm the user password", text_input("user-pass2", a.user_password, password=True))
        yield switch_row("Use the same password for the root account", a.root_same_password, id="root-same")
        yield field_box("Root password", text_input("root-pass", a.root_password, password=True), id="root-box1")
        yield field_box("Confirm the root password", text_input("root-pass2", a.root_password, password=True),
                        id="root-box2")

    def on_mount(self) -> None:
        self._sync()

    def _sync(self) -> None:
        shown = not self.query_one("#root-same", Switch).value
        self.query_one("#root-box1").display = shown
        self.query_one("#root-box2").display = shown

    @on(Switch.Changed, "#root-same")
    def _root_same_changed(self) -> None:
        self._sync()

    def _text(self, widget_id: str) -> str:
        return self.query_one(widget_id, Input).value

    def collect(self) -> str | None:
        hostname, username = self._text("#hostname").strip(), self._text("#username").strip()
        user_pass, user_pass2 = self._text("#user-pass"), self._text("#user-pass2")
        same = self.query_one("#root-same", Switch).value
        root_pass, root_pass2 = self._text("#root-pass"), self._text("#root-pass2")

        error = (validate_hostname(hostname)
                 or validate_linux_username(username)
                 or validate_password(user_pass, user_pass2, label="The user password")
                 or (None if same else validate_password(root_pass, root_pass2, label="The root password")))
        if error:
            return error
        a = self.cfg.arch
        a.hostname, a.username, a.user_password = hostname, username, user_pass
        a.root_same_password, a.root_password = same, "" if same else root_pass
        return None


class StorageScreen(WizardScreen):
    TITLE_TEXT = "Arch Linux: filesystem and encryption"
    INTRO = "These settings apply to the Arch Linux partitions only."

    def body(self) -> Iterator[Widget]:
        a = self.cfg.arch
        yield Static("Filesystem", classes="section")
        yield choice_set([
            ("ext4  - simple and robust (recommended)", "ext4"),
            ("btrfs - snapshots and compression, subvolumes @ and @home", "btrfs"),
            ("xfs   - high performance on large files", "xfs"),
        ], a.filesystem, id="filesystem")
        yield Static("Encryption", classes="section")
        yield switch_row("Encrypt Arch Linux with LUKS2", a.luks_enabled, id="luks")
        yield field_box("Encryption passphrase (asked at every boot)",
                        text_input("luks-pass", a.luks_passphrase, password=True), id="luks-box1")
        yield field_box("Confirm the passphrase",
                        text_input("luks-pass2", a.luks_passphrase, password=True), id="luks-box2")
        yield Static("/boot stays unencrypted on the EFI partition so that the signed GRUB and "
                     "Secure Boot keep working. Windows is not encrypted by this installer.", classes="hint")

    def on_mount(self) -> None:
        self._sync()

    def _sync(self) -> None:
        shown = self.query_one("#luks", Switch).value
        self.query_one("#luks-box1").display = shown
        self.query_one("#luks-box2").display = shown

    @on(Switch.Changed, "#luks")
    def _luks_changed(self) -> None:
        self._sync()

    def collect(self) -> str | None:
        luks = self.query_one("#luks", Switch).value
        passphrase = self.query_one("#luks-pass", Input).value
        confirm = self.query_one("#luks-pass2", Input).value
        if luks:
            error = validate_luks_passphrase(passphrase, confirm)
            if error:
                return error
        a = self.cfg.arch
        a.filesystem = chosen(self.query_one("#filesystem", RadioSet), "ext4")
        a.luks_enabled, a.luks_passphrase = luks, passphrase if luks else ""
        return None


class SoftwareScreen(WizardScreen):
    TITLE_TEXT = "Arch Linux: software"

    def body(self) -> Iterator[Widget]:
        a = self.cfg.arch
        yield Static("Kernel", classes="section")
        yield choice_set([
            ("linux      - the stock kernel", "linux"),
            ("linux-lts  - long-term support", "linux-lts"),
            ("linux-zen  - tuned for desktop responsiveness", "linux-zen"),
        ], a.kernel, id="kernel")

        detected = suggest_profiles(self.system.gpus())
        selected = set(a.gpu_profiles or detected)
        yield Static("Graphics drivers (detected hardware is pre-selected)", classes="section")
        yield SelectionList[str](*[(profile.label, profile_id, profile_id in selected)
                                   for profile_id, profile in PROFILES.items()], id="gpu")

        yield Static("Desktop", classes="section")
        yield choice_set([
            ("Hyprland - ready to use: SDDM, waybar, wofi, kitty, PipeWire, network applet", "hyprland"),
            ("Minimal   - no desktop, text console only", "minimal"),
        ], a.desktop, id="desktop")

        yield from field("AUR helper", Select([("None", "none"), ("paru", "paru"), ("yay", "yay")],
                                              value=a.aur_helper, allow_blank=False, id="aur"))
        yield from field("Extra packages (pacman names separated by spaces)",
                         text_input("extra", " ".join(a.extra_packages)),
                         hint="Optional. Checked against the repositories during the installation.")

    def collect(self) -> str | None:
        packages, error = parse_package_list(self.query_one("#extra", Input).value)
        if error:
            return error
        a = self.cfg.arch
        a.kernel = chosen(self.query_one("#kernel", RadioSet), "linux")
        a.desktop = chosen(self.query_one("#desktop", RadioSet), "hyprland")
        a.gpu_profiles = list(self.query_one("#gpu", SelectionList).selected)
        a.aur_helper = str(self.query_one("#aur", Select).value)
        a.extra_packages = packages
        return None
