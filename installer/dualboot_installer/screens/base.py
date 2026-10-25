"""Base class for the wizard screens."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Button, Footer, Static

if TYPE_CHECKING:
    from ..app import InstallerApp
    from ..config import InstallConfig
    from ..system.base import System


class WizardScreen(Screen):
    """One step of the wizard.

    Subclasses yield their widgets from ``body()`` and validate/save their
    answers in ``collect()``, which returns an error message or None.
    """

    TITLE_TEXT = ""
    INTRO = ""
    NEXT_LABEL = "Next"
    SHOW_BACK = True

    BINDINGS = [Binding("escape", "back", "Back")]

    def __init__(self, position: int, total: int) -> None:
        super().__init__()
        self.position = position
        self.total = total

    # -- shortcuts ----------------------------------------------------------

    @property
    def wizard(self) -> "InstallerApp":
        return self.app  # type: ignore[return-value]

    @property
    def cfg(self) -> "InstallConfig":
        return self.wizard.cfg

    @property
    def system(self) -> "System":
        return self.wizard.system

    # -- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="wizard"):
            yield Static(f"ArchInstall  -  step {self.position + 1} of {self.total}", id="wizard-step")
            yield Static(self.TITLE_TEXT, id="wizard-title")
            intro = self.intro()
            if intro:
                yield Static(intro, id="wizard-intro")
            with VerticalScroll(id="wizard-body"):
                yield from self.body()
            yield Static("", id="wizard-error")
            with Horizontal(id="wizard-nav"):
                yield Button("Back", id="back", disabled=not self.SHOW_BACK or self.position == 0)
                yield Button(self.NEXT_LABEL, id="next", variant="primary")
        yield Footer()

    def intro(self) -> str:
        """The paragraph under the title. Override to make it depend on the machine."""
        return self.INTRO

    def body(self) -> Iterator[Widget]:
        return iter(())

    # -- behaviour ----------------------------------------------------------

    def collect(self) -> str | None:
        """Validate the answers and store them in the config. Return an error or None."""
        return None

    def show_error(self, message: str) -> None:
        self.query_one("#wizard-error", Static).update(Text(message, style="bold red") if message else "")

    @on(Button.Pressed, "#next")
    def _next_pressed(self) -> None:
        self.action_next()

    @on(Button.Pressed, "#back")
    def _back_pressed(self) -> None:
        self.action_back()

    def action_next(self) -> None:
        error = self.collect()
        self.show_error(error or "")
        if error is None:
            self.wizard.go_next()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Hide the Back binding on screens that cannot go back.
        if action == "back" and not (self.SHOW_BACK and self.position > 0):
            return False
        return True

    def action_back(self) -> None:
        if self.SHOW_BACK and self.position > 0:
            # Keep what the user changed on this screen (found by the end-to-end test: a choice
            # made and then Esc was lost). Invalid input is simply not saved: the error is dropped.
            try:
                self.collect()
            except Exception:  # a screen that cannot save must never block going back
                pass
            self.wizard.go_back()
