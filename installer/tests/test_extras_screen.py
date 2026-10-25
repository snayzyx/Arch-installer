"""The Extras screen: developer tools, applications and desktop shell presets."""

from __future__ import annotations

import pytest
from textual.widgets import RadioButton, RadioSet, SelectionList, Static

from dualboot_installer import extras
from dualboot_installer.app import STEPS, InstallerApp
from dualboot_installer.config import InstallConfig
from dualboot_installer.screens.arch import SoftwareScreen
from dualboot_installer.screens.extras import NEEDS_ONLINE, ExtrasScreen
from dualboot_installer.screens.windows import WindowsSourceScreen
from dualboot_installer.system.demo import DemoSystem

from test_wizard import SIZE, click_back, next_screen, run, set_input, settle, summary_config, summary_text, to_accounts
from dualboot_installer.screens.arch import StorageScreen


class OfflineSystem(DemoSystem):
    def is_online(self) -> bool:
        return False


@pytest.fixture
def demo_preset(monkeypatch):
    """A preset in the registry (it only holds 'none' until the real ones are added)."""
    preset = extras.Preset("demo", "Demo shell", "a preset for the tests", aur_packages=("demo-shell-git",))
    monkeypatch.setitem(extras.DOTFILES, "demo", preset)
    return preset


async def to_extras(app, pilot, *, desktop: str | None = None):
    await to_accounts(app, pilot)
    set_input(app, "#username", "alice")
    set_input(app, "#user-pass", "s3cret")
    set_input(app, "#user-pass2", "s3cret")
    await next_screen(app, pilot, StorageScreen)
    await next_screen(app, pilot, SoftwareScreen)
    if desktop:
        for button in app.screen.query_one("#desktop").query(RadioButton):
            if button.name == desktop:
                button.value = True
        await settle(app, pilot)
    await next_screen(app, pilot, ExtrasScreen)
    await settle(app, pilot)


def selected(app, selector: str) -> list[str]:
    return list(app.screen.query_one(selector, SelectionList).selected)


def pressed(app) -> str:
    button = app.screen.query_one("#dotfiles", RadioSet).pressed_button
    return button.name if button is not None else ""


def preset_buttons(app) -> dict[str, RadioButton]:
    return {b.name: b for b in app.screen.query_one("#dotfiles", RadioSet).query(RadioButton)}


def text_of(app, selector: str) -> str:
    return str(app.screen.query_one(selector, Static).render())


async def wait_for(app, pilot, condition, *, seconds: float = 15.0):
    """Wait for a condition, not for a delay: the machine may be busy."""
    for _ in range(int(seconds / 0.1)):
        if condition():
            return
        await pilot.pause(0.1)
        await app.workers.wait_for_complete()
    raise AssertionError("the condition never became true")


# -- place in the wizard ---------------------------------------------------------------------------------


def test_the_screen_follows_the_software_screen_in_a_14_step_wizard():
    names = [s.__name__ for s in STEPS]
    assert len(STEPS) == 14
    assert names.index("ExtrasScreen") == names.index("SoftwareScreen") + 1
    assert names.index("ExtrasScreen") < names.index("WindowsSourceScreen")

    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await to_extras(app, pilot)
            return text_of(app, "#wizard-step"), text_of(app, "#wizard-title")

    step, title = run(scenario())
    assert "step 9 of 14" in step and title == "Arch Linux: developer tools and applications"


# -- first visit, answers, Back ---------------------------------------------------------------------------


def test_the_first_visit_preselects_the_recommended_items_and_next_stores_them():
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await to_extras(app, pilot)
            first = (selected(app, "#devgroups"), selected(app, "#apps"), pressed(app))
            assert not app.extras_touched
            await next_screen(app, pilot, WindowsSourceScreen)
            return first, app.cfg.arch, app.extras_touched

    (groups, apps, preset), arch, touched = run(scenario())
    assert groups == extras.RECOMMENDED_DEV_GROUPS and apps == extras.RECOMMENDED_APPS and preset == "none"
    assert touched
    assert (arch.dev_groups, arch.apps, arch.dotfiles) == (extras.RECOMMENDED_DEV_GROUPS, extras.RECOMMENDED_APPS, "none")


def test_back_keeps_what_was_chosen_even_when_everything_was_unticked():
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await to_extras(app, pilot)
            app.screen.query_one("#devgroups", SelectionList).deselect_all()
            app.screen.query_one("#apps", SelectionList).deselect_all()
            await settle(app, pilot)
            await next_screen(app, pilot, WindowsSourceScreen)
            await click_back(app, pilot)
            assert isinstance(app.screen, ExtrasScreen)
            await settle(app, pilot)
            empty = (selected(app, "#devgroups"), selected(app, "#apps"))

            # A partial choice survives too (and the docker group, which is not recommended).
            app.screen.query_one("#devgroups", SelectionList).select("docker")
            app.screen.query_one("#apps", SelectionList).select("librewolf")
            await settle(app, pilot)
            await next_screen(app, pilot, WindowsSourceScreen)
            await click_back(app, pilot)
            await settle(app, pilot)
            return empty, (selected(app, "#devgroups"), selected(app, "#apps")), app.cfg.arch

    empty, partial, arch = run(scenario())
    assert empty == ([], [])
    assert partial == (["docker"], ["librewolf"])
    assert (arch.dev_groups, arch.apps) == (["docker"], ["librewolf"])


def test_esc_on_the_screen_saves_the_choice_before_going_back():
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await to_extras(app, pilot)
            app.screen.query_one("#devgroups", SelectionList).deselect("cli")
            await settle(app, pilot)
            app.screen.action_back()
            await settle(app, pilot)
            assert isinstance(app.screen, SoftwareScreen)
            return list(app.cfg.arch.dev_groups)

    assert run(scenario()) == ["languages"]


# -- desktop ---------------------------------------------------------------------------------------------


def test_the_minimal_desktop_has_no_applications_and_no_shell_but_keeps_developer_tools():
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await to_extras(app, pilot, desktop="minimal")
            screen = app.screen
            present = (bool(screen.query("#apps")), bool(screen.query("#dotfiles")), bool(screen.query("#devgroups")))
            note = text_of(app, "#needs-desktop")
            await next_screen(app, pilot, WindowsSourceScreen)
            return present, note, app.cfg.arch

    present, note, arch = run(scenario())
    assert present == (False, False, True)
    assert "Hyprland" in note
    assert arch.apps == [] and arch.dotfiles == "none"
    assert arch.dev_groups == extras.RECOMMENDED_DEV_GROUPS


def test_going_back_to_pick_the_minimal_desktop_clears_applications_and_shell():
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await to_extras(app, pilot)
            await next_screen(app, pilot, WindowsSourceScreen)
            assert app.cfg.arch.apps == extras.RECOMMENDED_APPS
            await click_back(app, pilot)
            await click_back(app, pilot)
            assert isinstance(app.screen, SoftwareScreen)
            for button in app.screen.query_one("#desktop").query(RadioButton):
                if button.name == "minimal":
                    button.value = True
            await settle(app, pilot)
            await next_screen(app, pilot, ExtrasScreen)
            await next_screen(app, pilot, WindowsSourceScreen)
            return app.cfg.arch

    arch = run(scenario())
    assert arch.apps == [] and arch.dotfiles == "none"


# -- connection ------------------------------------------------------------------------------------------


def test_the_presets_are_enabled_when_online(demo_preset):
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await to_extras(app, pilot)
            await wait_for(app, pilot, lambda: not preset_buttons(app)["demo"].disabled)
            buttons = preset_buttons(app)
            assert not buttons["none"].disabled
            buttons["demo"].value = True
            await settle(app, pilot)
            note_online = text_of(app, "#dotfiles-note")
            await next_screen(app, pilot, WindowsSourceScreen)
            stored = app.cfg.arch.dotfiles
            await click_back(app, pilot)
            await settle(app, pilot)
            return note_online, stored, pressed(app)

    note, stored, after_back = run(scenario())
    assert note == "" and stored == "demo" and after_back == "demo"


def test_the_presets_are_disabled_and_refused_when_offline(demo_preset):
    async def scenario():
        app = InstallerApp(OfflineSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await to_extras(app, pilot)
            await wait_for(app, pilot, lambda: NEEDS_ONLINE in text_of(app, "#dotfiles-note"))
            buttons = preset_buttons(app)
            assert buttons["demo"].disabled and not buttons["none"].disabled
            assert pressed(app) == "none"
            # Even forced, the preset is refused and the other answers are kept.
            buttons["demo"].value = True
            await settle(app, pilot)
            app.screen.query_one("#devgroups", SelectionList).deselect("cli")
            await pilot.pause(0.3)
            await pilot.click("#next")
            await settle(app, pilot)
            return type(app.screen), text_of(app, "#wizard-error"), app.cfg.arch

    screen_type, error, arch = run(scenario())
    assert screen_type is ExtrasScreen and "internet connection" in error
    assert arch.dotfiles == "none" and arch.dev_groups == ["languages"]


def test_a_preset_chosen_earlier_does_not_stay_selected_once_the_connection_is_gone(demo_preset):
    async def scenario():
        app = InstallerApp(OfflineSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            app.extras_touched = True
            app.cfg.arch.dotfiles = "demo"
            await to_extras(app, pilot)
            await wait_for(app, pilot, lambda: pressed(app) == "none")
            return preset_buttons(app)["demo"].disabled

    assert run(scenario()) is True


def test_the_aur_note_follows_the_choices(demo_preset):
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await to_extras(app, pilot)
            notes = [text_of(app, "#aur-note")]
            app.screen.query_one("#apps", SelectionList).deselect("vscodium")
            app.screen.query_one("#apps", SelectionList).deselect("postman")
            await settle(app, pilot)
            notes.append(text_of(app, "#aur-note"))
            await wait_for(app, pilot, lambda: not preset_buttons(app)["demo"].disabled)
            preset_buttons(app)["demo"].value = True
            await settle(app, pilot)
            notes.append(text_of(app, "#aur-note"))
            return notes

    first, none_left, with_preset = run(scenario())
    assert "VSCodium and Postman" in first and "AUR with yay" in first
    assert none_left == ""
    assert "demo-shell-git" in with_preset and "long time" in with_preset


def test_the_chosen_aur_helper_is_the_one_named_in_the_note():
    cfg = InstallConfig()
    cfg.arch.aur_helper, cfg.arch.apps = "paru", ["vscodium"]
    from dualboot_installer.screens.extras import aur_note

    assert "VSCodium will be built from the AUR with paru" in aur_note(cfg.arch)
    cfg.arch.apps = ["librewolf"]
    assert aur_note(cfg.arch) == ""  # LibreWolf is an official package


# -- robustness ------------------------------------------------------------------------------------------


def test_unknown_values_in_the_answers_do_not_crash_the_screen():
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            app.extras_touched = True
            app.cfg.arch.dev_groups = ["nope", "cli"]
            app.cfg.arch.apps = ["nope"]
            app.cfg.arch.dotfiles = "bogus"
            await to_extras(app, pilot)
            assert pressed(app) == "none"
            await next_screen(app, pilot, WindowsSourceScreen)
            return app.cfg.arch

    arch = run(scenario())
    assert arch.dev_groups == ["cli"] and arch.apps == [] and arch.dotfiles == "none"


@pytest.mark.parametrize("size", [(80, 24), (140, 60)])
def test_the_screen_renders_and_works_at_small_and_large_terminal_sizes(size):
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=size) as pilot:
            await to_extras(app, pilot)
            assert isinstance(app.screen, ExtrasScreen)
            await next_screen(app, pilot, WindowsSourceScreen)
            return app.cfg.arch.dev_groups

    assert run(scenario()) == extras.RECOMMENDED_DEV_GROUPS


def test_the_keyboard_reaches_every_part_in_the_expected_order():
    async def scenario():
        app = InstallerApp(DemoSystem(), dry_run=True)
        async with app.run_test(size=SIZE) as pilot:
            await to_extras(app, pilot)
            order = []
            for _ in range(10):
                focused = app.screen.focused
                if focused is not None and focused.id in ("devgroups", "apps", "dotfiles", "back", "next"):
                    if not order or order[-1] != focused.id:
                        order.append(focused.id)
                await pilot.press("tab")
                await pilot.pause(0.05)
            return order

    assert run(scenario())[:5] == ["devgroups", "apps", "dotfiles", "back", "next"]


# -- summary ---------------------------------------------------------------------------------------------


def test_the_summary_lists_the_choices():
    cfg = summary_config()
    text = summary_text(cfg)
    assert "Developer tools" in text and "Applications" in text and "Desktop shell" in text
    assert "default Hyprland setup" in text
    cfg.arch.dev_groups, cfg.arch.apps = ["languages", "docker"], ["vscodium", "librewolf"]
    text = summary_text(cfg)
    assert "Languages and build tools, Docker and docker-compose" in text
    assert "VSCodium, LibreWolf" in text


def test_the_summary_says_none_when_nothing_was_chosen_or_there_is_no_desktop():
    cfg = summary_config()
    text = summary_text(cfg)
    assert "none" in text.split("Developer tools")[1].splitlines()[0]
    cfg.arch.apps, cfg.arch.desktop = ["vscodium"], "minimal"
    text = summary_text(cfg)
    assert "none (no desktop)" in text
    assert "VSCodium" not in text  # applications do not exist without the desktop


def test_the_summary_names_the_chosen_shell(demo_preset):
    cfg = summary_config()
    cfg.arch.dotfiles = "demo"
    assert "Demo shell" in summary_text(cfg)
