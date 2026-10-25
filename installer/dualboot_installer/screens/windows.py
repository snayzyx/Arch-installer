"""Windows 11 questions: image source, edition, account and regional settings."""

from __future__ import annotations

import random
import string
from collections.abc import Iterator
from pathlib import Path

from rich.text import Text
from textual import on
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Input, RadioButton, RadioSet, Select, Static, Switch

from ..config import WindowsConfig
from ..data.windows import (
    EDITIONS,
    KEYBOARDS,
    LANGUAGES,
    default_keyboard,
    default_language,
    language_by_name,
    language_by_tag,
)
from ..validators import (
    validate_iso_path,
    validate_iso_url,
    validate_password,
    validate_windows_computer_name,
    validate_windows_username,
)
from ..widgets import FilterableList, choice_set, chosen, field, field_box, switch_row, text_input
from ..windows_iso import WindowsIsoInfo
from .base import WizardScreen

# Shown under the file path. /mnt is deliberately not used as the example: it is where the new
# system is mounted during the installation, and the installer refuses to start if it is in use.
LOCAL_ISO_HINT = ("Mount the USB drive or partition that holds the file first, for example: "
                  "mkdir -p /media && mount /dev/sdX1 /media")
URL_HINT = ("Open https://www.microsoft.com/software-download/windows11 in a browser on any device, "
            "choose the language, copy the download link (valid about 24 hours) and paste it here.")
LANGUAGE_HINT = ("Windows Setup uses the language of the ISO itself: if you pick another one here, "
                 "it is corrected automatically from the ISO.")


def _random_computer_name() -> str:
    return "DESKTOP-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=7))


class WindowsSourceScreen(WizardScreen):
    TITLE_TEXT = "Windows 11: image"
    INTRO = ("Windows 11 is installed from an official ISO that you provide. The installer does not "
             "download it by itself from Microsoft.")

    def __init__(self, position: int, total: int) -> None:
        super().__init__(position, total)
        self._embedded: WindowsIsoInfo | None = None

    def body(self) -> Iterator[Widget]:
        w = self.cfg.windows
        self._embedded = self.system.embedded_windows()

        options = [
            ("Use an ISO file I already have (USB drive or partition)", "local"),
            ("Download it from a link I paste", "download"),
        ]
        if self._embedded:
            options.insert(0, ("Use the Windows 11 included in this installer (offline)", "embedded"))
        # First visit: the included ISO if there is one, else a file. Later visits: keep the choice.
        known = {value for _, value in options}
        current = w.iso_source if self.wizard.windows_touched and w.iso_source in known else options[0][1]

        yield Static("Where does the ISO come from?", classes="section")
        yield choice_set(options, current, id="iso-source")
        yield field_box("Path of the ISO file (for example /media/usb/Win11.iso)",
                        text_input("iso-path", w.iso_path if w.iso_source == "local" else ""),
                        id="iso-path-box", hint=LOCAL_ISO_HINT)
        yield field_box("Download link of the Windows 11 ISO (https://...)",
                        text_input("iso-url", w.iso_url), id="iso-url-box", hint=URL_HINT)
        yield Vertical(Static("Windows language", classes="section"), FilterableList(id="language"),
                       Static(LANGUAGE_HINT, classes="hint"), id="language-box", classes="field-box")
        yield Static("", id="language-fixed", classes="section")
        yield Static("Edition", classes="section")
        yield choice_set([(f"Windows 11 {e}", e) for e in EDITIONS], w.edition, id="edition")

    def on_mount(self) -> None:
        w = self.cfg.windows
        language = (language_by_name(w.language_name) if self.wizard.windows_touched
                    else default_language(self.cfg.arch.country))
        self.query_one("#language", FilterableList).set_items([(lang.name, lang.name) for lang in LANGUAGES],
                                                              language.name)
        self._sync()

    def _source(self) -> str:
        return chosen(self.query_one("#iso-source", RadioSet), "local")

    def _sync(self) -> None:
        source = self._source()
        embedded = self._embedded if source == "embedded" else None
        self.query_one("#iso-path-box").display = source == "local"
        self.query_one("#iso-url-box").display = source == "download"
        self.query_one("#language-box").display = embedded is None

        fixed = self.query_one("#language-fixed", Static)
        fixed.display = embedded is not None
        if embedded:
            language = language_by_tag(embedded.language_tag)
            fixed.update(Text(f"Windows language: {language.name} (set by the included ISO)", style="bold"))

        # An included ISO may lack an edition: grey it out and move the choice to one it has.
        edition_set = self.query_one("#edition", RadioSet)
        buttons = list(edition_set.query(RadioButton))
        for button in buttons:
            button.disabled = embedded is not None and button.name not in embedded.editions
        pressed = edition_set.pressed_button
        if pressed is not None and pressed.disabled:
            next(b for b in buttons if not b.disabled).value = True

    @on(RadioSet.Changed, "#iso-source")
    def _source_changed(self) -> None:
        self._sync()

    def collect(self) -> str | None:
        source = self._source()
        path = self.query_one("#iso-path", Input).value.strip()
        url = self.query_one("#iso-url", Input).value.strip()
        edition = chosen(self.query_one("#edition", RadioSet), "Pro")
        w = self.cfg.windows

        if source == "embedded":
            info = self._embedded
            if info is None:
                return "No Windows ISO is included in this installer."
            if edition not in info.editions:
                return f"The included ISO has no Windows 11 {edition}. Available: {', '.join(info.editions)}."
            language = language_by_tag(info.language_tag)
            w.iso_path, w.iso_url = str(info.path), ""
        else:
            if source == "local":
                error = validate_iso_path(path)
                if error:
                    return error
                if not self.cfg.dry_run and not Path(path).is_file():
                    return f"File not found: {path}"
            else:
                error = validate_iso_url(url)
                if error:
                    return error
            language_name = self.query_one("#language", FilterableList).value
            if not language_name:
                return "Choose the Windows language."
            language = language_by_name(language_name)
            w.iso_path = path if source == "local" else ""
            w.iso_url = url if source == "download" else ""

        w.iso_source, w.edition = source, edition
        w.language_name, w.language_tag = language.name, language.tag
        self.wizard.windows_touched = True
        return None


class WindowsAccountScreen(WizardScreen):
    TITLE_TEXT = "Windows 11: account and settings"
    INTRO = ("A local administrator account is created, so no Microsoft account or "
             "internet connection is needed to finish the Windows setup.")

    def body(self) -> Iterator[Widget]:
        w, a = self.cfg.windows, self.cfg.arch
        yield from field("Computer name", text_input("computer-name", w.computer_name or _random_computer_name()),
                         hint="Up to 15 letters, digits and hyphens.")
        yield from field("User name", text_input("win-user", w.username or a.username))
        yield switch_row("Use the same password as the Arch Linux user", w.same_password_as_arch, id="same-pass")
        yield field_box("Password", text_input("win-pass", w.password, password=True), id="win-pass-box1")
        yield field_box("Confirm the password", text_input("win-pass2", w.password, password=True), id="win-pass-box2")
        yield switch_row("Use a different time zone and keyboard layout than Arch Linux", w.custom_regional,
                         id="custom-regional")
        yield field_box("Windows time zone", FilterableList(id="win-timezone"), id="win-tz-box")
        known_tags = {tag for _, tag in KEYBOARDS}
        keyboard = w.keyboard or default_keyboard(a.keymap, a.country)
        yield field_box("Windows keyboard layout",
                        Select(list(KEYBOARDS), value=keyboard if keyboard in known_tags else "en-US",
                               allow_blank=False, id="win-keyboard"),
                        id="win-kbd-box")
        yield Static("Always applied", classes="section")
        yield Static("\n".join(f"  - {tweak}" for tweak in WindowsConfig.TWEAKS))

    def on_mount(self) -> None:
        w = self.cfg.windows
        zones = self.system.timezones()
        self.query_one("#win-timezone", FilterableList).set_items([(z, z) for z in zones],
                                                                  w.timezone or self.cfg.arch.timezone)
        self._sync()

    def _sync(self) -> None:
        same = self.query_one("#same-pass", Switch).value
        self.query_one("#win-pass-box1").display = not same
        self.query_one("#win-pass-box2").display = not same
        custom = self.query_one("#custom-regional", Switch).value
        self.query_one("#win-tz-box").display = custom
        self.query_one("#win-kbd-box").display = custom

    @on(Switch.Changed)
    def _switch_changed(self) -> None:
        self._sync()

    def collect(self) -> str | None:
        name = self.query_one("#computer-name", Input).value.strip()
        user = self.query_one("#win-user", Input).value.strip()
        same = self.query_one("#same-pass", Switch).value
        password = self.query_one("#win-pass", Input).value
        confirm = self.query_one("#win-pass2", Input).value
        custom = self.query_one("#custom-regional", Switch).value

        error = (validate_windows_computer_name(name)
                 or validate_windows_username(user, name)
                 or (None if same else validate_password(password, confirm, label="The Windows password")))
        if error:
            return error
        w = self.cfg.windows
        w.computer_name, w.username = name, user
        w.same_password_as_arch, w.password = same, "" if same else password
        w.custom_regional = custom
        w.timezone = self.query_one("#win-timezone", FilterableList).value or ""
        w.keyboard = str(self.query_one("#win-keyboard", Select).value)
        return None
