"""Internet connection (Ethernet is automatic, Wi-Fi is configured here)."""

from __future__ import annotations

from collections.abc import Iterator

from rich.text import Text
from textual import on, work
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from ..system.models import WifiNetwork
from ..widgets import field, text_input
from .base import WizardScreen


ONLINE_RECHECK_DELAYS = (3, 6, 10, 15, 25)  # seconds after a successful Wi-Fi connection


class NetworkScreen(WizardScreen):
    TITLE_TEXT = "Internet connection"
    INTRO = ("An internet connection is required: Arch packages are downloaded during the installation, "
             "and so is Windows 11 if you give a download link instead of an ISO file.")

    def __init__(self, position: int, total: int) -> None:
        super().__init__(position, total)
        self._online = False
        self._device = ""
        self._networks: list[WifiNetwork] = []

    def intro(self) -> str:
        if self.system.embedded_windows():
            return ("An internet connection is required: Arch Linux packages are downloaded during the "
                    "installation. Windows 11 is already included in this installer.")
        return self.INTRO

    def body(self) -> Iterator[Widget]:
        yield Static("Checking the connection...", id="net-status")
        yield Static("Wi-Fi networks", classes="section")
        yield OptionList(id="wifi-list")
        yield from field("Wi-Fi password (leave empty for an open network)", text_input("wifi-pass", password=True))
        with Horizontal(classes="buttons"):
            yield Button("Scan", id="scan")
            yield Button("Connect", id="connect")

    def on_mount(self) -> None:
        self._check_online()
        self._scan()

    # -- background work ----------------------------------------------------

    @work(thread=True, exclusive=True, group="online")
    def _check_online(self) -> None:
        online = self.system.is_online()
        self.app.call_from_thread(self._set_online, online)

    @work(thread=True, exclusive=True, group="scan")
    def _scan(self) -> None:
        device, networks = self.system.wifi_scan()
        self.app.call_from_thread(self._show_networks, device, networks)

    @work(thread=True, exclusive=True, group="connect")
    def _connect(self, device: str, ssid: str, security: str, passphrase: str) -> None:
        ok, message = self.system.wifi_connect(device, ssid, passphrase)
        self.app.call_from_thread(self._connect_done, ok, message, ssid, security, passphrase)

    # -- UI updates (main thread) --------------------------------------------

    def _set_online(self, online: bool) -> None:
        self._online = online
        try:
            status = self.query_one("#net-status", Static)
        except NoMatches:  # the screen was left while the check was running
            return
        if online:
            status.update(Text("Connected to the internet.", style="bold green"))
        else:
            status.update(Text("Not connected. Plug in an Ethernet cable or connect to a Wi-Fi network below.",
                               style="bold yellow"))

    def _show_networks(self, device: str, networks: list[WifiNetwork]) -> None:
        self._device, self._networks = device, networks
        listing = self.query_one("#wifi-list", OptionList)
        listing.clear_options()
        if not device:
            listing.add_option(Option(Text("No Wi-Fi adapter found."), disabled=True))
            return
        if not networks:
            listing.add_option(Option(Text("No network found. Press Scan to try again."), disabled=True))
            return
        for index, net in enumerate(networks):
            lock = "open" if net.security == "open" else net.security
            listing.add_option(Option(Text(f"{net.ssid}   [{lock}]  {'*' * net.signal}"), id=str(index)))
        listing.highlighted = 0

    def _connect_done(self, ok: bool, message: str, ssid: str = "", security: str = "", passphrase: str = "") -> None:
        self.show_error("" if ok else f"Wi-Fi: {message}")
        if ok and ssid:
            # Kept so that the installed system joins the same network (a laptop may have no Ethernet).
            network = self.cfg.network
            network.wifi_ssid, network.wifi_security, network.wifi_passphrase = ssid, security, passphrase
        self._check_online()
        if ok:
            # The association is done but DHCP is not: the check above usually runs too early, and the
            # screen would keep saying "Not connected" (and block Next) until Scan is pressed.
            for delay in ONLINE_RECHECK_DELAYS:
                self.set_timer(delay, self._recheck_online)

    def _recheck_online(self) -> None:
        if not self._online:
            self._check_online()

    # -- events -------------------------------------------------------------

    @on(Button.Pressed, "#scan")
    def _scan_pressed(self) -> None:
        self._scan()
        self._check_online()

    @on(Button.Pressed, "#connect")
    def _connect_pressed(self) -> None:
        listing = self.query_one("#wifi-list", OptionList)
        index = listing.highlighted
        if not self._device or index is None or index >= len(self._networks):
            self.show_error("Select a Wi-Fi network first.")
            return
        ssid, security = self._networks[index].ssid, self._networks[index].security
        passphrase = self.query_one("#wifi-pass", Input).value
        self.show_error("")
        self.query_one("#net-status", Static).update(Text(f"Connecting to {ssid}...", style="bold"))
        self._connect(self._device, ssid, security, passphrase)

    def collect(self) -> str | None:
        if not self._online and not self.cfg.dry_run:
            return "No internet connection. Connect to a network, then press Next."
        return None
