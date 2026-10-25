"""The Textual application: owns the configuration and walks through the screens."""

from __future__ import annotations

from textual.app import App

from .config import InstallConfig
from .screens.arch import AccountsScreen, SoftwareScreen, StorageScreen
from .screens.base import WizardScreen
from .screens.disk import DiskScreen, LayoutScreen
from .screens.install import InstallScreen
from .screens.network import NetworkScreen
from .screens.region import RegionScreen
from .screens.secureboot import SecureBootScreen
from .screens.summary import SummaryScreen
from .screens.welcome import WelcomeScreen
from .screens.windows import WindowsAccountScreen, WindowsSourceScreen
from .system.base import System

# Region comes first: it switches the console keyboard layout, so every
# password typed afterwards uses the right keys.
STEPS: tuple[type[WizardScreen], ...] = (
    WelcomeScreen,
    RegionScreen,
    NetworkScreen,
    DiskScreen,
    LayoutScreen,
    AccountsScreen,
    StorageScreen,
    SoftwareScreen,
    WindowsSourceScreen,
    WindowsAccountScreen,
    SecureBootScreen,
    SummaryScreen,
    InstallScreen,
)


class InstallerApp(App[None]):
    CSS_PATH = "installer.tcss"
    TITLE = "ArchInstall"
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, system: System, *, dry_run: bool = False, partial: bool = False) -> None:
        super().__init__()
        self.system = system
        self.cfg = InstallConfig(dry_run=dry_run, partial=partial)
        self.index = 0
        # Set once the user has answered a screen, so going Back keeps their
        # choices instead of re-applying country defaults.
        self.region_touched = False
        self.windows_touched = False

    def on_mount(self) -> None:
        self.push_screen(self._make(0))

    async def action_quit(self) -> None:
        """Ctrl+Q. Not while the installation runs: quitting would leave the disk half installed."""
        screen = self.screen
        if isinstance(screen, InstallScreen) and not screen.finished:
            self.notify("The installation is running. Quitting now would leave the disk half installed: "
                        "wait until it ends.", severity="warning", timeout=8)
            return
        self.exit()

    def _make(self, index: int) -> WizardScreen:
        return STEPS[index](index, len(STEPS))

    def go_next(self) -> None:
        if self.index < len(STEPS) - 1:
            self.index += 1
            self.switch_screen(self._make(self.index))

    def go_back(self) -> None:
        if self.index > 0:
            self.index -= 1
            self.switch_screen(self._make(self.index))
