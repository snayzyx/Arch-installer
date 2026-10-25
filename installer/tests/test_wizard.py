"""End-to-end tests of the wizard in demo mode (simulated hardware, dry run)."""

from __future__ import annotations

import asyncio
import io

from rich.console import Console
from textual.widgets import Button, DataTable, Input, RadioButton, RichLog, Static, Switch

from dualboot_installer.app import STEPS, InstallerApp
from dualboot_installer.config import InstallConfig
from dualboot_installer.screens import install as install_module
from dualboot_installer.screens.arch import AccountsScreen, SoftwareScreen, StorageScreen
from dualboot_installer.screens.disk import DiskScreen, LayoutScreen
from dualboot_installer.screens.extras import ExtrasScreen
from dualboot_installer.screens.install import InstallScreen, final_button_label
from dualboot_installer.screens.network import NetworkScreen
from dualboot_installer.screens.region import RegionScreen
from dualboot_installer.screens.secureboot import SecureBootScreen
from dualboot_installer.screens.summary import SummaryScreen, build_summary, describe_windows_source
from dualboot_installer.screens.welcome import WelcomeScreen
from dualboot_installer.screens.windows import LOCAL_ISO_HINT, URL_HINT, WindowsAccountScreen, WindowsSourceScreen
from dualboot_installer.system.demo import DEMO_WINDOWS_ISO, DemoSystem
from dualboot_installer.widgets import FilterableList
from dualboot_installer.windows_iso import WimImage, WindowsIsoInfo

SIZE = (140, 60)


def run(coro):
    return asyncio.run(coro)


async def settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


# A Textual Button ignores clicks while it plays its 0.2 s "pressed" animation, and the
# tests click faster than a person can. Waiting a little longer avoids dropped clicks.
BUTTON_COOLDOWN = 0.25


async def click_next(app, pilot):
    await pilot.pause(BUTTON_COOLDOWN)
    await pilot.click("#next")
    await settle(app, pilot)


async def click_back(app, pilot):
    await pilot.pause(BUTTON_COOLDOWN)
    await pilot.click("#back")
    await settle(app, pilot)


async def next_screen(app, pilot, expected):
    await click_next(app, pilot)
    assert isinstance(app.screen, expected), f"expected {expected.__name__}, got {type(app.screen).__name__}"


def set_input(app, selector, value):
    app.screen.query_one(selector, Input).value = value


def error_text(app) -> str:
    return str(app.screen.query_one("#wizard-error", Static).render())


async def to_accounts(app, pilot):
    await settle(app, pilot)
    await next_screen(app, pilot, RegionScreen)
    await next_screen(app, pilot, NetworkScreen)
    await next_screen(app, pilot, DiskScreen)
    await next_screen(app, pilot, LayoutScreen)
    await next_screen(app, pilot, AccountsScreen)


async def to_windows_source(app, pilot):
    await to_accounts(app, pilot)
    set_input(app, "#username", "alice")
    set_input(app, "#user-pass", "s3cret")
    set_input(app, "#user-pass2", "s3cret")
    await next_screen(app, pilot, StorageScreen)
    await next_screen(app, pilot, SoftwareScreen)
    await next_screen(app, pilot, ExtrasScreen)
    await next_screen(app, pilot, WindowsSourceScreen)


async def pick_source(app, pilot, name):
    for button in app.screen.query_one("#iso-source").query(RadioButton):
        if button.name == name:
            button.value = True
    await settle(app, pilot)


def source_buttons(app) -> dict[str, RadioButton]:
    return {b.name: b for b in app.screen.query_one("#iso-source").query(RadioButton)}


def test_step_order_starts_with_region_so_the_keyboard_is_set_before_passwords():
    names = [s.__name__ for s in STEPS]
    assert names.index("RegionScreen") < names.index("AccountsScreen")
    assert names[0] == "WelcomeScreen" and names[-1] == "InstallScreen"


def test_full_dry_run_flow():
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            assert isinstance(app.screen, WelcomeScreen)
            await next_screen(app, pilot, RegionScreen)

            # Pick France by typing in the country filter, like a user would.
            country = app.screen.query_one("#country", FilterableList)
            country.query_one(".fl-filter", Input).focus()
            await pilot.press(*"fran", "enter")
            await settle(app, pilot)
            await next_screen(app, pilot, NetworkScreen)
            assert (app.cfg.arch.country, app.cfg.arch.timezone) == ("FR", "Europe/Paris")
            assert (app.cfg.arch.locale, app.cfg.arch.keymap) == ("fr_FR.UTF-8", "fr")

            await next_screen(app, pilot, DiskScreen)
            await next_screen(app, pilot, LayoutScreen)
            assert app.cfg.disk.device == "/dev/nvme0n1"
            assert app.cfg.disk.windows_gib >= 64

            await next_screen(app, pilot, AccountsScreen)
            set_input(app, "#username", "alice")
            set_input(app, "#user-pass", "s3cret")
            set_input(app, "#user-pass2", "s3cret")
            await next_screen(app, pilot, StorageScreen)
            await next_screen(app, pilot, SoftwareScreen)
            await next_screen(app, pilot, ExtrasScreen)
            await next_screen(app, pilot, WindowsSourceScreen)
            arch = app.cfg.arch
            assert arch.filesystem == "ext4" and not arch.luks_enabled
            # The simulated machine has an Intel and an NVIDIA GPU: only those are pre-selected.
            assert arch.gpu_profiles == ["intel", "nvidia-open"]
            assert (arch.kernel, arch.desktop, arch.aur_helper) == ("linux", "hyprland", "none")
            assert app.screen.query_one("#language", FilterableList).value == "French"
            # Nothing is included in this installer: a file is the default, and it needs a path.
            assert source_buttons(app)["local"].value
            set_input(app, "#iso-path", "/media/usb/Win11.iso")  # a dry run does not look for the file

            await next_screen(app, pilot, WindowsAccountScreen)
            assert (app.cfg.windows.iso_source, app.cfg.windows.edition) == ("local", "Pro")
            assert (app.cfg.windows.iso_path, app.cfg.windows.iso_url) == ("/media/usb/Win11.iso", "")
            set_input(app, "#win-pass", "w1ndows")
            set_input(app, "#win-pass2", "w1ndows")
            await next_screen(app, pilot, SecureBootScreen)
            set_input(app, "#mok-pass", "Abcd1234")
            set_input(app, "#mok-pass2", "Abcd1234")
            await next_screen(app, pilot, SummaryScreen)
            assert app.cfg.secureboot.resolved == "shim"

            await next_screen(app, pilot, InstallScreen)
            await settle(app, pilot)
            await pilot.pause(0.3)
            lines = [strip.text for strip in app.screen.query_one("#log", RichLog).lines]
            text = "\n".join(lines)
            assert "DRY RUN" in text
            assert "$ sgdisk --zap-all /dev/nvme0n1" in text
            assert "Dry run finished" in text
            # A dry run never offers to restart the machine.
            assert str(app.screen.query_one("#next", Button).label) == "Exit"
            return app.cfg

    cfg = run(scenario())
    assert cfg.arch.username == "alice" and cfg.windows.username == "alice"
    assert cfg.windows.language_tag == "fr-FR"
    assert cfg.windows_keyboard_tag() == "fr-FR"
    assert cfg.windows_timezone_id() == "Romance Standard Time"
    assert "s3cret" not in str(cfg.redacted()) and "w1ndows" not in str(cfg.redacted())


def test_accounts_validation_blocks_and_back_keeps_answers():
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await to_accounts(app, pilot)

            await click_next(app, pilot)  # nothing typed yet
            await settle(app, pilot)
            assert isinstance(app.screen, AccountsScreen)
            assert "user name" in error_text(app).lower()

            set_input(app, "#username", "Bad Name")
            await click_next(app, pilot)
            await settle(app, pilot)
            assert isinstance(app.screen, AccountsScreen)

            set_input(app, "#username", "alice")
            set_input(app, "#user-pass", "one")
            set_input(app, "#user-pass2", "two")
            await click_next(app, pilot)
            await settle(app, pilot)
            assert "do not match" in error_text(app)

            set_input(app, "#user-pass2", "one")
            await next_screen(app, pilot, StorageScreen)
            await click_back(app, pilot)
            await settle(app, pilot)
            assert isinstance(app.screen, AccountsScreen)
            assert app.screen.query_one("#username", Input).value == "alice"

    run(scenario())


def test_going_back_keeps_the_choice_made_on_the_screen():
    # Found by the end-to-end test in a VM: a choice changed and then Esc (without Next) was lost.
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            for expected in (RegionScreen, NetworkScreen, DiskScreen, LayoutScreen, AccountsScreen):
                await next_screen(app, pilot, expected)
            set_input(app, "#username", "alice")
            set_input(app, "#user-pass", "s3cret")
            set_input(app, "#user-pass2", "s3cret")
            await next_screen(app, pilot, StorageScreen)
            await next_screen(app, pilot, SoftwareScreen)

            buttons = {b.name: b for b in app.screen.query_one("#kernel").query(RadioButton)}
            buttons["linux-lts"].value = True
            await settle(app, pilot)
            app.screen.action_back()
            await settle(app, pilot)
            assert isinstance(app.screen, StorageScreen)
            return app.cfg.arch.kernel

    assert run(scenario()) == "linux-lts"


def test_root_password_fields_appear_when_not_shared():
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await to_accounts(app, pilot)
            assert app.screen.query_one("#root-box1").display is False
            app.screen.query_one("#root-same", Switch).value = False
            await settle(app, pilot)
            assert app.screen.query_one("#root-box1").display is True

            set_input(app, "#username", "alice")
            set_input(app, "#user-pass", "one")
            set_input(app, "#user-pass2", "one")
            await click_next(app, pilot)
            await settle(app, pilot)
            assert "root password" in error_text(app).lower()

    run(scenario())


def test_live_medium_cannot_be_selected_as_target():
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await next_screen(app, pilot, RegionScreen)
            await next_screen(app, pilot, NetworkScreen)
            await next_screen(app, pilot, DiskScreen)
            table = app.screen.query_one("#disks", DataTable)
            table.move_cursor(row=2)  # /dev/sdb, the simulated live USB stick
            await pilot.pause()
            await click_next(app, pilot)
            await settle(app, pilot)
            assert isinstance(app.screen, DiskScreen)
            assert "started from" in error_text(app)

    run(scenario())


def test_layout_rejects_a_windows_partition_that_is_too_small():
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await next_screen(app, pilot, RegionScreen)
            await next_screen(app, pilot, NetworkScreen)
            await next_screen(app, pilot, DiskScreen)
            await next_screen(app, pilot, LayoutScreen)
            set_input(app, "#windows-gib", "20")
            await click_next(app, pilot)
            await settle(app, pilot)
            assert isinstance(app.screen, LayoutScreen)
            assert "at least 64" in error_text(app)

            app.screen.query_one("#home-switch", Switch).value = True
            app.screen.query_one("#shared-switch", Switch).value = True
            set_input(app, "#windows-gib", "100")
            await settle(app, pilot)
            await next_screen(app, pilot, AccountsScreen)
            assert app.cfg.disk.home_enabled and app.cfg.disk.shared_enabled

    run(scenario())


def test_offline_build_offers_the_included_windows_iso_and_locks_language_and_edition():
    async def scenario():
        app = InstallerApp(DemoSystem(embedded_windows=DEMO_WINDOWS_ISO), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            image_check = next(c for c in app.system.checks() if c.name == "Windows 11 image")
            assert image_check.status == "ok" and "Included in this installer" in image_check.detail
            assert app.screen.query_one("#checks", Static).content.row_count == len(app.system.checks())

            await next_screen(app, pilot, RegionScreen)
            country = app.screen.query_one("#country", FilterableList)
            country.query_one(".fl-filter", Input).focus()
            await pilot.press(*"fran", "enter")  # a French user: the ISO language must still win
            await settle(app, pilot)
            await next_screen(app, pilot, NetworkScreen)
            assert "already included" in str(app.screen.query_one("#wizard-intro", Static).render())
            await next_screen(app, pilot, DiskScreen)
            await next_screen(app, pilot, LayoutScreen)
            await next_screen(app, pilot, AccountsScreen)
            set_input(app, "#username", "alice")
            set_input(app, "#user-pass", "s3cret")
            set_input(app, "#user-pass2", "s3cret")
            await next_screen(app, pilot, StorageScreen)
            await next_screen(app, pilot, SoftwareScreen)
            await next_screen(app, pilot, ExtrasScreen)
            await next_screen(app, pilot, WindowsSourceScreen)

            # The included ISO is the default; its language replaces the language picker.
            assert app.screen.query_one("#language-box").display is False
            assert app.screen.query_one("#language-fixed").display is True
            await next_screen(app, pilot, WindowsAccountScreen)
            return app.cfg.windows

    w = run(scenario())
    assert w.iso_source == "embedded"
    assert w.iso_path == str(DEMO_WINDOWS_ISO.path) and w.iso_url == ""
    assert (w.language_name, w.language_tag) == ("English (United States)", "en-US")


def test_the_included_iso_is_listed_first_and_selected_by_default():
    async def scenario():
        app = InstallerApp(DemoSystem(embedded_windows=DEMO_WINDOWS_ISO), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await to_windows_source(app, pilot)
            names = [b.name for b in app.screen.query_one("#iso-source").query(RadioButton)]
            assert names == ["embedded", "local", "download"]
            assert source_buttons(app)["embedded"].value
            assert app.screen.query_one("#iso-path-box").display is False
            assert app.screen.query_one("#iso-url-box").display is False

    run(scenario())


def test_included_iso_without_the_chosen_edition_is_refused():
    async def scenario():
        home_only = WindowsIsoInfo(
            path=DEMO_WINDOWS_ISO.path, size=1, sha256="0" * 64,
            images=(WimImage(1, "Windows 11 Home", "Core", ("en-US",), "en-US", "26300", "x86_64"),))
        app = InstallerApp(DemoSystem(embedded_windows=home_only), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            for expected in (RegionScreen, NetworkScreen, DiskScreen, LayoutScreen, AccountsScreen):
                await next_screen(app, pilot, expected)
            set_input(app, "#username", "alice")
            set_input(app, "#user-pass", "s3cret")
            set_input(app, "#user-pass2", "s3cret")
            await next_screen(app, pilot, StorageScreen)
            await next_screen(app, pilot, SoftwareScreen)
            await next_screen(app, pilot, ExtrasScreen)
            await next_screen(app, pilot, WindowsSourceScreen)
            # Pro is missing from this ISO, so it is greyed out and Home is selected instead.
            buttons = {b.name: b for b in app.screen.query_one("#edition").query(RadioButton)}
            assert buttons["Pro"].disabled and not buttons["Home"].disabled
            assert buttons["Home"].value
            await next_screen(app, pilot, WindowsAccountScreen)
            return app.cfg.windows.edition

    assert run(scenario()) == "Home"


def test_without_an_included_iso_a_file_is_the_default():
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await to_windows_source(app, pilot)
            names = [b.name for b in app.screen.query_one("#iso-source").query(RadioButton)]
            assert names == ["local", "download"]
            assert source_buttons(app)["local"].value
            assert app.screen.query_one("#iso-path-box").display is True
            assert app.screen.query_one("#iso-url-box").display is False
            assert app.screen.query_one("#language-box").display is True

    run(scenario())


def test_local_file_needs_an_iso_path():
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await to_windows_source(app, pilot)
            await click_next(app, pilot)
            assert isinstance(app.screen, WindowsSourceScreen)
            assert ".iso" in error_text(app) or "path" in error_text(app).lower()

            set_input(app, "#iso-path", "/media/usb/readme.txt")
            await click_next(app, pilot)
            assert isinstance(app.screen, WindowsSourceScreen) and ".iso" in error_text(app)

            set_input(app, "#iso-path", "/media/usb/Win11.iso")
            await next_screen(app, pilot, WindowsAccountScreen)
            return app.cfg.windows

    w = run(scenario())
    assert (w.iso_source, w.iso_path, w.iso_url) == ("local", "/media/usb/Win11.iso", "")


def test_local_hint_points_to_media_because_mnt_is_the_install_target():
    # The installer refuses to start when /mnt is mounted: it is where the new system goes.
    assert "/media" in LOCAL_ISO_HINT and "/mnt" not in LOCAL_ISO_HINT

    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await to_windows_source(app, pilot)
            box = app.screen.query_one("#iso-path-box")
            texts = [str(s.content) for s in box.query(Static)]
            assert any("/media" in t for t in texts)
            assert not any("/mnt" in t for t in texts)
            label = str(box.query_one(".field-label").content)
            assert "/media" in label and "/mnt" not in label

    run(scenario())


def test_download_link_option_shows_the_url_box_and_validates_the_link():
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await to_windows_source(app, pilot)
            await pick_source(app, pilot, "download")
            assert app.screen.query_one("#iso-url-box").display is True
            assert app.screen.query_one("#iso-path-box").display is False
            hints = [str(s.content) for s in app.screen.query_one("#iso-url-box").query(Static)]
            assert URL_HINT in hints and "software-download/windows11" in URL_HINT

            await click_next(app, pilot)  # nothing pasted
            assert isinstance(app.screen, WindowsSourceScreen)
            assert "link" in error_text(app).lower()

            set_input(app, "#iso-url", "http://download.example.com/Win11.iso")  # not https
            await click_next(app, pilot)
            assert isinstance(app.screen, WindowsSourceScreen)
            assert "https" in error_text(app)

            set_input(app, "#iso-url", "https://download.example.com/Win11.iso?t=abc123")
            await next_screen(app, pilot, WindowsAccountScreen)
            return app.cfg.windows

    w = run(scenario())
    assert w.iso_source == "download"
    assert w.iso_url == "https://download.example.com/Win11.iso?t=abc123"
    assert w.iso_path == ""


def test_switching_source_keeps_only_the_matching_field():
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await to_windows_source(app, pilot)
            set_input(app, "#iso-path", "/media/usb/Win11.iso")
            await pick_source(app, pilot, "download")
            set_input(app, "#iso-url", "https://download.example.com/Win11.iso?t=abc")
            await next_screen(app, pilot, WindowsAccountScreen)
            return app.cfg.windows

    w = run(scenario())
    assert w.iso_source == "download" and w.iso_path == ""  # the path typed earlier is dropped
    assert w.iso_url.startswith("https://")


def test_language_hint_says_the_iso_decides():
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await to_windows_source(app, pilot)
            texts = " ".join(str(s.content) for s in app.screen.query_one("#language-box").query(Static))
            assert "language of the ISO" in texts and "corrected automatically" in texts

    run(scenario())


def test_summary_shows_the_host_of_a_download_link_but_never_the_link():
    cfg = InstallConfig(dry_run=True)
    cfg.windows.iso_source = "download"
    cfg.windows.iso_url = "https://software.download.prss.microsoft.com/dbazure/Win11.iso?t=SECRET-TOKEN&P1=1"
    text = describe_windows_source(cfg.windows)
    assert "software.download.prss.microsoft.com" in text
    assert "SECRET-TOKEN" not in text and "t=" not in text and "?" not in text

    cfg.windows.iso_source, cfg.windows.iso_path = "local", "/media/usb/Win11.iso"
    assert describe_windows_source(cfg.windows) == "local file /media/usb/Win11.iso"
    cfg.windows.iso_source = "embedded"
    assert "included in this installer" in describe_windows_source(cfg.windows)


def summary_text(cfg: InstallConfig) -> str:
    console = Console(file=io.StringIO(), width=130, record=True, force_terminal=False)
    console.print(build_summary(cfg))
    return console.export_text()


def summary_config(**windows) -> InstallConfig:
    cfg = InstallConfig(dry_run=True)
    cfg.disk.device, cfg.disk.size_bytes, cfg.disk.windows_gib = "/dev/nvme0n1", 512 * 1024**3, 200
    cfg.arch.username = "alice"
    cfg.windows.computer_name, cfg.windows.username = "WINPC", "alice"
    cfg.secureboot.resolved = "shim"
    for key, value in windows.items():
        setattr(cfg.windows, key, value)
    return cfg


def test_summary_never_prints_the_token_of_a_download_link():
    cfg = summary_config(iso_source="download", iso_url="https://dl.example.com/Win11.iso?t=SECRET-TOKEN")
    text = summary_text(cfg)
    assert "dl.example.com" in text and "SECRET-TOKEN" not in text
    assert "installed automatically" in text


def test_summary_says_clearly_that_windows_is_skipped_in_partial_mode():
    cfg = summary_config(iso_source="local", iso_path="/media/usb/Win11.iso")
    assert "NOT installed" not in summary_text(cfg)
    cfg.partial = True
    text = summary_text(cfg)
    assert "NOT installed (partial mode)" in text and "unused (Windows skipped)" in text
    assert "Tweak" not in text  # nothing is applied to a Windows that is not installed


def test_partial_banner_is_shown_on_the_review_screen():
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True, partial=True)
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.cfg.disk.device, app.cfg.disk.size_bytes, app.cfg.disk.windows_gib = (
                "/dev/nvme0n1", 512 * 1024**3, 200)
            app.index = STEPS.index(SummaryScreen)
            app.switch_screen(app._make(app.index))
            await settle(app, pilot)
            return str(app.screen.query_one("#disk-warning", Static).content)

    assert "PARTIAL MODE: Windows is skipped" in run(scenario())


def test_only_a_real_successful_install_offers_to_reboot():
    assert final_button_label(ok=True, dry_run=False) == "Reboot now"
    assert final_button_label(ok=True, dry_run=True) == "Exit"
    assert final_button_label(ok=False, dry_run=False) == "Exit"
    assert final_button_label(ok=False, dry_run=True) == "Exit"


def test_reboot_button_restarts_the_machine_after_a_real_successful_install(monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(install_module.subprocess, "run", lambda argv, **kwargs: commands.append(list(argv)))
    # The steps are NOT run: only the end-of-run behaviour of the screen is under test.
    monkeypatch.setattr(InstallScreen, "_execute", lambda self: None)
    monkeypatch.setattr(install_module, "can_run_for_real", lambda plan, cfg: True)

    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=False)
        exits: list[bool] = []
        app.exit = lambda *args, **kwargs: exits.append(True)  # type: ignore[method-assign]
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.cfg.disk.device, app.cfg.disk.size_bytes, app.cfg.disk.windows_gib = (
                "/dev/nvme0n1", 512 * 1024**3, 200)
            app.index = len(STEPS) - 1
            app.switch_screen(app._make(app.index))
            await settle(app, pilot)
            screen = app.screen
            assert not screen.finished  # nothing happens while the install is still running
            screen.action_next()
            assert commands == [] and exits == []

            screen._done(True)
            await settle(app, pilot)
            assert str(screen.query_one("#next", Button).label) == "Reboot now"
            screen.action_next()
            return exits

    exits = run(scenario())
    assert commands == [["systemctl", "reboot"]] and exits == [True]


def test_ctrl_q_does_not_quit_while_the_installation_runs_but_does_once_it_is_over(monkeypatch):
    monkeypatch.setattr(InstallScreen, "_execute", lambda self: None)  # the install stays "running"
    monkeypatch.setattr(install_module, "can_run_for_real", lambda plan, cfg: True)

    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=False)
        exits: list[bool] = []
        app.exit = lambda *args, **kwargs: exits.append(True)  # type: ignore[method-assign]
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.cfg.disk.device, app.cfg.disk.size_bytes, app.cfg.disk.windows_gib = (
                "/dev/nvme0n1", 512 * 1024**3, 200)
            app.index = len(STEPS) - 1
            app.switch_screen(app._make(app.index))
            await settle(app, pilot)
            await pilot.press("ctrl+q")
            await settle(app, pilot)
            while_running = list(exits)
            app.screen._done(True)
            await settle(app, pilot)
            await pilot.press("ctrl+q")
            await settle(app, pilot)
            return while_running, exits

    while_running, after = run(scenario())
    assert while_running == [] and after == [True]


def test_ctrl_q_still_quits_on_the_first_screen():
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        exits: list[bool] = []
        app.exit = lambda *args, **kwargs: exits.append(True)  # type: ignore[method-assign]
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await pilot.press("ctrl+q")
            await settle(app, pilot)
            return exits

    assert run(scenario()) == [True]


def test_failed_install_only_offers_exit_and_never_reboots(monkeypatch):
    commands: list[list[str]] = []
    monkeypatch.setattr(install_module.subprocess, "run", lambda argv, **kwargs: commands.append(list(argv)))
    monkeypatch.setattr(InstallScreen, "_execute", lambda self: None)
    monkeypatch.setattr(install_module, "can_run_for_real", lambda plan, cfg: True)

    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=False)
        exits: list[bool] = []
        app.exit = lambda *args, **kwargs: exits.append(True)  # type: ignore[method-assign]
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            app.cfg.disk.device, app.cfg.disk.size_bytes, app.cfg.disk.windows_gib = (
                "/dev/nvme0n1", 512 * 1024**3, 200)
            app.index = len(STEPS) - 1
            app.switch_screen(app._make(app.index))
            await settle(app, pilot)
            screen = app.screen
            screen._done(False)
            await settle(app, pilot)
            assert str(screen.query_one("#next", Button).label) == "Exit"
            screen.action_next()
            return exits

    assert run(scenario()) == [True]
    assert commands == []


def test_real_run_is_refused_when_the_plan_is_incomplete_and_executes_nothing(monkeypatch):
    # A real run (dry_run=False) would execute commands. Whatever the plan says, make sure
    # that nothing can run in this test: reaching execute_plan fails it.
    def must_not_run(*args, **kwargs):
        raise AssertionError("the plan must not be executed when it is refused")

    monkeypatch.setattr(install_module, "can_run_for_real", lambda plan, cfg: False)
    monkeypatch.setattr(install_module, "execute_plan", must_not_run)

    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=False)
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            cfg = app.cfg
            cfg.disk.device, cfg.disk.size_bytes, cfg.disk.windows_gib = "/dev/nvme0n1", 512 * 1024**3, 200
            app.index = len(STEPS) - 1
            app.switch_screen(app._make(app.index))
            await settle(app, pilot)
            lines = [strip.text for strip in app.screen.query_one("#log", RichLog).lines]
            text = "\n".join(lines)
            assert "Refusing to start" in text
            assert "--partial" in text and "--dry-run" in text
            assert not any(line.startswith("$ ") for line in lines)
            assert str(app.screen.query_one("#next", Button).label) == "Exit"

    run(scenario())
