"""Developer tools, desktop applications and desktop shell presets (dotfiles)."""

from __future__ import annotations

from collections.abc import Iterator
from copy import copy
from typing import TYPE_CHECKING

from textual import on, work
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.widgets import RadioButton, RadioSet, SelectionList, Static

from .. import extras
from ..widgets import choice_set, chosen
from .base import WizardScreen

if TYPE_CHECKING:
    from ..config import ArchConfig

NEEDS_ONLINE = ("The desktop shells are downloaded from GitHub when you install, so they need an internet "
                "connection: go Back to the Network screen.")
NEEDS_DESKTOP = ("Applications and desktop shells need the Hyprland desktop (chosen on the previous screen). "
                 "Developer tools work with both desktops.")


def short_label(label: str) -> str:
    """The name at the start of a label: 'VSCodium (VS Code ...)' -> 'VSCodium', 'Languages: git, ...' -> 'Languages'."""
    return label.split(":")[0].split(" (")[0].strip()


def preset_label(preset: extras.Preset) -> str:
    return f"{preset.label} - {preset.description}" if preset.description else preset.label


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def aur_note(a: "ArchConfig") -> str:
    """What will be built from the AUR for these answers (empty when nothing is)."""
    packages = extras.aur_packages(a)
    if not packages:
        return ""
    names = [short_label(app.label) for app in extras.selected_apps(a) if app.aur]
    preset = extras.selected_preset(a)
    if preset and preset.aur_packages:
        names += list(preset.aur_packages)
    helper = extras.effective_aur_helper(a)
    text = f"{_join(names)} will be built from the AUR with {helper} on the installed system: a few minutes."
    if preset and preset.aur_packages:
        text += " Building the desktop shell can take a long time."
    return text


class ExtrasScreen(WizardScreen):
    TITLE_TEXT = "Arch Linux: developer tools and applications"
    INTRO = "Everything here is optional. Space ticks or unticks an item."

    DEFAULT_CSS = """
    ExtrasScreen #devgroups, ExtrasScreen #apps {
        height: auto;
        max-height: 12;
    }
    """

    def __init__(self, position: int, total: int) -> None:
        super().__init__(position, total)
        self._online: bool | None = None  # None until the connection has been checked

    # -- layout -------------------------------------------------------------

    def body(self) -> Iterator[Widget]:
        a = self.cfg.arch
        first_visit = not self.wizard.extras_touched
        groups = extras.RECOMMENDED_DEV_GROUPS if first_visit else a.dev_groups
        yield Static("Developer tools", classes="section")
        yield SelectionList[str](*[(label, group_id, group_id in groups)
                                   for group_id, (label, _packages) in extras.DEV_GROUPS.items()], id="devgroups")
        if extras.has_desktop(a):
            apps = extras.RECOMMENDED_APPS if first_visit else a.apps
            yield Static("Applications", classes="section")
            yield SelectionList[str](*[(app.label, app_id, app_id in apps) for app_id, app in extras.APPS.items()],
                                     id="apps")
            current = a.dotfiles if (not first_visit and a.dotfiles in extras.DOTFILES) else "none"
            yield Static("Desktop shell", classes="section")
            yield choice_set([(preset_label(preset), preset_id) for preset_id, preset in extras.DOTFILES.items()],
                             current, id="dotfiles")
            yield Static("", id="dotfiles-note", classes="hint")
        else:
            yield Static(NEEDS_DESKTOP, id="needs-desktop", classes="hint")
        yield Static("", id="aur-note", classes="hint")

    def on_mount(self) -> None:
        self._apply_online()
        self._refresh_note()
        self._check_online()

    # -- connection ---------------------------------------------------------

    @work(thread=True, exclusive=True, group="online")
    def _check_online(self) -> None:
        online = self.system.is_online()
        self.app.call_from_thread(self._set_online, online)

    def _set_online(self, online: bool) -> None:
        self._online = online
        self._apply_online()
        self._refresh_note()

    def _apply_online(self) -> None:
        """Presets other than the default are only selectable with a connection."""
        try:
            radio = self.query_one("#dotfiles", RadioSet)
            note = self.query_one("#dotfiles-note", Static)
        except NoMatches:  # minimal desktop, or the screen was left while the check was running
            return
        for button in radio.query(RadioButton):
            if button.name != "none":
                button.disabled = self._online is not True  # unknown counts as offline until the check ends
        if self._online is False:
            # Offline for sure: a preset chosen earlier must not stay selected behind a disabled button.
            for button in radio.query(RadioButton):
                if button.name == "none":
                    button.value = True
        note.update(NEEDS_ONLINE if self._online is False else "")

    # -- live summary -------------------------------------------------------

    def _current_answers(self) -> "ArchConfig":
        """A copy of the answers with what is ticked on this screen."""
        a = copy(self.cfg.arch)
        a.dev_groups = [g for g in extras.DEV_GROUPS if g in self.query_one("#devgroups", SelectionList).selected]
        try:
            a.apps = [i for i in extras.APPS if i in self.query_one("#apps", SelectionList).selected]
            a.dotfiles = chosen(self.query_one("#dotfiles", RadioSet), "none")
        except NoMatches:
            a.apps, a.dotfiles = [], "none"
        return a

    def _refresh_note(self) -> None:
        try:
            note = aur_note(self._current_answers())
            self.query_one("#aur-note", Static).update(note)
        except NoMatches:
            pass

    @on(SelectionList.SelectedChanged)
    def _selection_changed(self) -> None:
        self._refresh_note()

    @on(RadioSet.Changed, "#dotfiles")
    def _dotfiles_changed(self) -> None:
        self._refresh_note()

    # -- answers ------------------------------------------------------------

    def collect(self) -> str | None:
        a = self.cfg.arch
        chosen_answers = self._current_answers()
        desktop = extras.has_desktop(a)
        a.dev_groups = chosen_answers.dev_groups
        a.apps = chosen_answers.apps if desktop else []
        self.wizard.extras_touched = True
        preset = chosen_answers.dotfiles if desktop else "none"
        if preset not in extras.DOTFILES:
            preset = "none"
        if preset != "none" and self._online is not True:
            return NEEDS_ONLINE
        a.dotfiles = preset
        return None
