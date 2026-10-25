"""Country, time zone, locale and keyboard layout."""

from __future__ import annotations

from collections.abc import Iterator

from textual import on
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Static

from ..data.countries import defaults_for
from ..widgets import FilterableList
from .base import WizardScreen


class RegionScreen(WizardScreen):
    TITLE_TEXT = "Region and keyboard"
    INTRO = ("The country sets sensible defaults for the rest. The keyboard layout is applied "
             "immediately, so the passwords you type next use the right keys.")

    def body(self) -> Iterator[Widget]:
        with Horizontal(id="region-columns"):
            with Vertical(classes="column"):
                yield Static("Country", classes="section")
                yield FilterableList(id="country")
                yield Static("Time zone", classes="section")
                yield FilterableList(id="timezone")
            with Vertical(classes="column"):
                yield Static("Keyboard layout", classes="section")
                yield FilterableList(id="keymap")
                yield Static("System locale", classes="section")
                yield FilterableList(id="locale")

    def on_mount(self) -> None:
        arch = self.cfg.arch
        countries = self.system.countries()
        codes = {c.code for c in countries}
        country = arch.country if arch.country in codes else (countries[0].code if countries else "US")

        # Read once: on the live system these lists come from files and subprocesses.
        self._locales = self.system.locales()
        self._keymaps = self.system.keymaps()

        self.query_one("#country", FilterableList).set_items([(c.name, c.code) for c in countries], country)
        self.query_one("#timezone", FilterableList).set_items([(z, z) for z in self.system.timezones()], arch.timezone)
        self.query_one("#keymap", FilterableList).set_items([(k, k) for k in self._keymaps], arch.keymap)
        self.query_one("#locale", FilterableList).set_items([(loc, loc) for loc in self._locales], arch.locale)

        if not self.wizard.region_touched:
            self._apply_country(country)

    @on(FilterableList.Changed, "#country")
    def _country_changed(self, event: FilterableList.Changed) -> None:
        self.wizard.region_touched = True
        self._apply_country(event.value)

    @on(FilterableList.Changed, "#keymap")
    def _keymap_changed(self, event: FilterableList.Changed) -> None:
        self.wizard.region_touched = True
        self.system.apply_keymap(event.value)

    def _apply_country(self, code: str) -> None:
        """Pre-select the time zone, locale and keymap that fit a country."""
        defaults = defaults_for(code)
        zones = self.system.country_timezones(code)
        timezone_list = self.query_one("#timezone", FilterableList)
        if zones:
            timezone_list.value = zones[0]

        locale_list = self.query_one("#locale", FilterableList)
        if defaults.locale in self._locales:
            locale_list.value = defaults.locale

        keymap_list = self.query_one("#keymap", FilterableList)
        if defaults.keymap in self._keymaps:
            keymap_list.value = defaults.keymap
            self.system.apply_keymap(defaults.keymap)

    def collect(self) -> str | None:
        country = self.query_one("#country", FilterableList).value
        timezone = self.query_one("#timezone", FilterableList).value
        locale = self.query_one("#locale", FilterableList).value
        keymap = self.query_one("#keymap", FilterableList).value
        if not (country and timezone and locale and keymap):
            return "Choose a country, a time zone, a keyboard layout and a locale."
        arch = self.cfg.arch
        arch.country, arch.timezone, arch.locale, arch.keymap = country, timezone, locale, keymap
        self.wizard.region_touched = True
        return None
