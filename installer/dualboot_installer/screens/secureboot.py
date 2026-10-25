"""Secure Boot strategy."""

from __future__ import annotations

from collections.abc import Iterator

from rich.text import Text
from textual import on
from textual.widget import Widget
from textual.widgets import Input, Select, Static

from ..config import SECURE_BOOT_CHOICES
from ..secureboot import STRATEGY_LABELS, resolve_strategy, sbctl_problem
from ..validators import validate_mok_password
from ..widgets import field, field_box, text_input
from .base import WizardScreen

_EXPLANATION = {
    "sbctl": ("The installer creates its own Secure Boot keys, enrolls them in the firmware (keeping the "
              "Microsoft keys so Windows still boots) and signs GRUB and the kernel. This works because the "
              "firmware is in Setup Mode."),
    "shim": ("The installer signs GRUB and the kernel with a new key and installs Microsoft's signed shim. "
             "On the next boot a blue 'MOK management' screen appears. PRESS A KEY WITHIN 10 SECONDS or the "
             "request is cancelled. Then choose 'Enroll MOK', 'Continue', 'Yes', type the password chosen "
             "below and choose 'Reboot'."),
}


class SecureBootScreen(WizardScreen):
    TITLE_TEXT = "Secure Boot"
    INTRO = "GRUB is signed so that Secure Boot can stay enabled with both systems installed."

    def body(self) -> Iterator[Widget]:
        yield Static("", id="sb-state")
        yield from field("Strategy", Select(
            [("Automatic (recommended)", "auto"), (STRATEGY_LABELS["sbctl"], "sbctl"), (STRATEGY_LABELS["shim"], "shim")],
            value=self.cfg.secureboot.strategy, allow_blank=False, id="sb-strategy"))
        yield Static("", id="sb-explain", classes="hint")
        yield field_box("Enrollment password (typed once on the blue MOK screen)",
                        text_input("mok-pass", self.cfg.secureboot.mok_password, password=True), id="mok-box1")
        yield field_box("Confirm the enrollment password",
                        text_input("mok-pass2", self.cfg.secureboot.mok_password, password=True), id="mok-box2")

    def on_mount(self) -> None:
        self._refresh()

    @on(Select.Changed, "#sb-strategy")
    def _strategy_changed(self) -> None:
        self._refresh()

    def _choice(self) -> str:
        value = self.query_one("#sb-strategy", Select).value
        return value if value in SECURE_BOOT_CHOICES else "auto"

    def _refresh(self) -> None:
        state = self.system.secure_boot_state()
        resolved = resolve_strategy(self._choice(), state)

        def word(flag: bool | None, yes: str, no: str) -> str:
            return "unknown" if flag is None else (yes if flag else no)

        self.query_one("#sb-state", Static).update(Text(
            f"Firmware: Secure Boot {word(state.enabled, 'enabled', 'disabled')}, "
            f"Setup Mode {word(state.setup_mode, 'on', 'off')}, TPM {'found' if state.has_tpm else 'not found'}.\n"
            f"Strategy that will be used: {STRATEGY_LABELS[resolved]}", style="bold"))
        explanation = _EXPLANATION[resolved]
        problem = sbctl_problem(state) if resolved == "sbctl" else None
        if problem:
            explanation += f"\nWARNING: {problem}"
        self.query_one("#sb-explain", Static).update(Text(explanation))
        shim = resolved == "shim"
        self.query_one("#mok-box1").display = shim
        self.query_one("#mok-box2").display = shim

    def collect(self) -> str | None:
        choice = self._choice()
        state = self.system.secure_boot_state()
        resolved = resolve_strategy(choice, state)
        password = self.query_one("#mok-pass", Input).value
        confirm = self.query_one("#mok-pass2", Input).value
        if resolved == "sbctl":
            problem = sbctl_problem(state)
            if problem:
                return problem  # better to stop here than after the disk was erased
        if resolved == "shim":
            error = validate_mok_password(password, confirm)
            if error:
                return error
        sb = self.cfg.secureboot
        sb.strategy, sb.resolved = choice, resolved
        sb.mok_password = password if resolved == "shim" else ""
        return None
