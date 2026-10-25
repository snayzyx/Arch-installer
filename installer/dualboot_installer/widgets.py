"""Reusable widgets and small compose helpers."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Label, OptionList, RadioButton, RadioSet, Static, Switch
from textual.widgets.option_list import Option


class FilterableList(Vertical):
    """A searchable single-choice list, for long option sets (countries, time zones, ...).

    Type in the box to filter, press Enter to pick the first match, or press
    Down to move into the list and Enter to pick an entry.
    """

    DEFAULT_CSS = """
    FilterableList { height: auto; }
    FilterableList > .fl-current { color: $text-muted; height: 1; }
    FilterableList > .fl-options { height: 7; }
    """

    BINDINGS = [Binding("down", "focus_options", "List", show=False)]

    class Changed(Message):
        def __init__(self, widget: "FilterableList", value: str) -> None:
            super().__init__()
            self.widget = widget
            self.value = value

        @property
        def control(self) -> "FilterableList":
            return self.widget

    def __init__(self, items: Iterable[tuple[str, str]] = (), value: str | None = None,
                 *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._choices: list[tuple[str, str]] = list(items)
        self._selected: str | None = value
        self._query = ""

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Type to filter...", classes="fl-filter")
        yield Static("", classes="fl-current")
        yield OptionList(classes="fl-options")

    def on_mount(self) -> None:
        self._rebuild()

    # -- public API ---------------------------------------------------------

    @property
    def value(self) -> str | None:
        return self._selected

    @value.setter
    def value(self, new_value: str | None) -> None:
        self._selected = new_value
        if self.is_mounted:
            self._rebuild()

    def set_items(self, items: Iterable[tuple[str, str]], value: str | None = None) -> None:
        self._choices = list(items)
        self._selected = value
        if self.is_mounted:
            self._rebuild()

    # -- internals ----------------------------------------------------------

    def _label_of(self, value: str | None) -> str:
        return next((label for label, v in self._choices if v == value), "")

    def _rebuild(self) -> None:
        query = self._query.casefold()
        matches = [(label, v) for label, v in self._choices if query in label.casefold()]
        options = self.query_one(".fl-options", OptionList)
        options.clear_options()
        options.add_options([Option(Text(label), id=v) for label, v in matches])
        highlight = next((i for i, (_, v) in enumerate(matches) if v == self._selected), 0 if matches else None)
        if highlight is not None:
            options.highlighted = highlight
        current = self._label_of(self._selected)
        self.query_one(".fl-current", Static).update(Text(f"Selected: {current}" if current else "Nothing selected"))

    def _choose(self, value: str) -> None:
        if value == self._selected:
            return
        self._selected = value
        self._rebuild()
        self.post_message(self.Changed(self, value))

    def action_focus_options(self) -> None:
        self.query_one(".fl-options", OptionList).focus()

    @on(Input.Changed, ".fl-filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._query = event.value
        self._rebuild()

    @on(Input.Submitted, ".fl-filter")
    def _filter_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        options = self.query_one(".fl-options", OptionList)
        if options.option_count:
            index = options.highlighted if options.highlighted is not None else 0
            option_id = options.get_option_at_index(index).id
            if option_id is not None:
                self._choose(option_id)

    @on(OptionList.OptionSelected, ".fl-options")
    def _option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id is not None:
            self._choose(event.option.id)


def choice_set(options: list[tuple[str, str]], current: str, *, id: str) -> RadioSet:
    """A RadioSet whose buttons carry their value in ``name``."""
    return RadioSet(*[RadioButton(label, value=(value == current), name=value) for label, value in options], id=id)


def chosen(radio: RadioSet, default: str) -> str:
    button = radio.pressed_button
    return button.name if button is not None and button.name else default


def field(label: str, widget: Widget, *, hint: str = "") -> Iterator[Widget]:
    """A caption above a widget, optionally followed by a muted hint."""
    yield Label(label, classes="field-label")
    yield widget
    if hint:
        yield Static(hint, classes="hint")


def field_box(label: str, widget: Widget, *, id: str, hint: str = "") -> Vertical:
    """Like ``field`` but wrapped in one container, so it can be shown or hidden as a whole."""
    return Vertical(*field(label, widget, hint=hint), id=id, classes="field-box")


def switch_row(label: str, value: bool, *, id: str) -> Horizontal:
    return Horizontal(Switch(value=value, id=id), Label(label, classes="switch-label"), classes="switch-row")


def text_input(id: str, value: str = "", *, password: bool = False, placeholder: str = "",
               type: str = "text") -> Input:
    return Input(value=value, password=password, placeholder=placeholder, type=type, id=id)  # type: ignore[arg-type]
