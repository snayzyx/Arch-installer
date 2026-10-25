"""What a laptop needs: detection, extras, the Wi-Fi network carried over, Windows Fast Startup."""

from pathlib import Path

import pytest

from dualboot_installer.config import InstallConfig
from dualboot_installer.runner import Command, FileWrite
from dualboot_installer.steps import arch
from dualboot_installer.system import live
from dualboot_installer.system.hardware import (
    PowerState,
    detect_laptop,
    hidden_storage_controller,
    power_state,
)
from dualboot_installer.system.live import LiveSystem
from dualboot_installer.windows_unattend import finish_script, specialize_commands

from test_plan import dry_run_everything, make_config


def sysfs(tmp_path: Path, *, supplies: dict[str, dict[str, str]] | None = None, chassis: str | None = None) -> Path:
    for name, files in (supplies or {}).items():
        folder = tmp_path / "class" / "power_supply" / name
        folder.mkdir(parents=True)
        for filename, value in files.items():
            (folder / filename).write_text(value + "\n")
    if chassis is not None:
        dmi = tmp_path / "class" / "dmi" / "id"
        dmi.mkdir(parents=True)
        (dmi / "chassis_type").write_text(chassis + "\n")
    return tmp_path


# -- detection ---------------------------------------------------------------------------------------------


def test_a_battery_means_a_laptop(tmp_path):
    root = sysfs(tmp_path, supplies={"BAT0": {"type": "Battery", "capacity": "80"}, "AC": {"type": "Mains", "online": "1"}})
    assert detect_laptop(root)


@pytest.mark.parametrize("chassis", ["8", "9", "10", "14", "31", "32"])
def test_a_portable_chassis_type_means_a_laptop_even_without_a_battery(tmp_path, chassis):
    assert detect_laptop(sysfs(tmp_path, chassis=chassis))


@pytest.mark.parametrize("chassis", ["3", "6", "7", "13", "35"])  # desktop, tower, all-in-one, mini PC...
def test_desktop_chassis_types_are_not_laptops(tmp_path, chassis):
    assert not detect_laptop(sysfs(tmp_path, chassis=chassis))


def test_the_battery_of_a_wireless_mouse_does_not_make_a_laptop(tmp_path):
    root = sysfs(tmp_path, supplies={"hidpp_battery_0": {"type": "Battery", "scope": "Device", "capacity": "50"}}, chassis="3")
    assert not detect_laptop(root)


def test_nothing_readable_is_not_a_laptop(tmp_path):
    assert not detect_laptop(tmp_path)  # a Windows host, a container...


def test_power_state_on_battery_and_on_charger(tmp_path):
    on_battery = sysfs(tmp_path / "a", supplies={"BAT0": {"type": "Battery", "capacity": "42"}, "AC": {"type": "Mains", "online": "0"}})
    assert power_state(on_battery) == PowerState(has_battery=True, on_ac=False, capacity=42)
    plugged = sysfs(tmp_path / "b", supplies={"BAT0": {"type": "Battery", "capacity": "42"}, "AC": {"type": "Mains", "online": "1"}})
    assert power_state(plugged).on_ac is True


def test_power_state_is_unknown_without_any_charger_entry(tmp_path):
    state = power_state(sysfs(tmp_path, supplies={"BAT0": {"type": "Battery", "capacity": "42"}}))
    assert state.has_battery and state.on_ac is None


# -- the live system warns ---------------------------------------------------------------------------------


NO_BATTERY = PowerState(False, None, None)


def run_with(monkeypatch, lspci: str, power: PowerState = NO_BATTERY):
    monkeypatch.setattr(live, "_run", lambda argv, timeout=15: lspci if argv[0] == "lspci" else "")
    monkeypatch.setattr(live, "power_state", lambda: power)
    monkeypatch.setattr(LiveSystem, "secure_boot_state", lambda self: live.SecureBootState(False, False))
    monkeypatch.setattr(LiveSystem, "embedded_windows", lambda self: None)
    return {check.name: check for check in LiveSystem().checks()}


def test_intel_vmd_or_raid_mode_is_reported_before_anything_is_erased(monkeypatch):
    checks = run_with(monkeypatch, "00:0e.0 RAID bus controller: Intel Corporation Volume Management Device NVMe RAID Controller\n")
    check = checks["Storage controller"]
    assert check.status == "warn" and not check.blocking
    assert "AHCI" in check.detail


def test_a_plain_nvme_controller_raises_no_storage_warning(monkeypatch):
    checks = run_with(monkeypatch, "01:00.0 Non-Volatile memory controller: Samsung Electronics Co Ltd NVMe SSD Controller\n")
    assert "Storage controller" not in checks


def test_running_on_battery_is_reported(monkeypatch):
    checks = run_with(monkeypatch, "", PowerState(True, False, 37))
    assert checks["Power"].status == "warn" and "37%" in checks["Power"].detail


def test_no_power_warning_when_plugged_in_or_on_a_desktop(monkeypatch):
    assert "Power" not in run_with(monkeypatch, "", PowerState(True, True, 37))
    assert "Power" not in run_with(monkeypatch, "", PowerState(False, None, None))


def test_hidden_storage_controller_text():
    assert hidden_storage_controller("00:17.0 SATA controller: Intel AHCI\n") is None
    line = "00:17.0 RAID bus controller: Intel Corporation SATA Controller [RAID mode]\n"
    assert "RAID mode" in hidden_storage_controller(line)


# -- the installation --------------------------------------------------------------------------------------


def test_laptop_extras_are_installed_only_for_a_laptop():
    cfg = InstallConfig()
    plain = arch.package_list(cfg, [], laptop=False)
    laptop = arch.package_list(cfg, [], laptop=True)
    for package in arch.LAPTOP_PACKAGES:
        assert package not in plain and package in laptop
    assert {"power-profiles-daemon", "bluez", "sof-firmware"} <= set(laptop)


def test_wants_laptop_follows_the_answers_file_then_the_hardware(monkeypatch):
    monkeypatch.setattr(arch, "detect_laptop", lambda: True)
    cfg = InstallConfig()
    assert arch.wants_laptop(cfg)  # None = decided by the hardware
    cfg.arch.laptop = False
    assert not arch.wants_laptop(cfg)
    monkeypatch.setattr(arch, "detect_laptop", lambda: False)
    cfg.arch.laptop = True
    assert arch.wants_laptop(cfg)


def test_hybrid_graphics_get_prime_offload():
    cfg = InstallConfig()
    cfg.arch.gpu_profiles = ["intel", "nvidia-open"]
    assert "nvidia-prime" in arch.package_list(cfg, [], laptop=True)
    cfg.arch.gpu_profiles = ["nvidia-open"]
    assert "nvidia-prime" not in arch.package_list(cfg, [], laptop=True)
    cfg.arch.gpu_profiles = ["intel", "nvidia-open"]
    assert "nvidia-prime" not in arch.package_list(cfg, [], laptop=False)


def test_a_laptop_install_enables_bluetooth_and_power_profiles():
    runner, _ = dry_run_everything(make_config(laptop=True))
    enabled = [op.argv for op in runner.operations if isinstance(op, Command) and "enable" in op.argv]
    flat = [word for argv in enabled for word in argv]
    assert "bluetooth.service" in flat and "power-profiles-daemon.service" in flat


def test_a_desktop_install_does_not_enable_laptop_services():
    runner, _ = dry_run_everything(make_config(laptop=False))
    flat = [word for op in runner.operations if isinstance(op, Command) for word in op.argv]
    assert "bluetooth.service" not in flat and "power-profiles-daemon.service" not in flat


# -- Wi-Fi carried over to the installed system ------------------------------------------------------------


def test_a_wpa_network_becomes_a_networkmanager_profile():
    text = arch.wifi_profile("Home Wi-Fi", "psk", "pass phrase 123")
    assert "ssid=Home Wi-Fi" in text and "id=Home Wi-Fi" in text
    assert "key-mgmt=wpa-psk" in text and "psk=pass phrase 123" in text
    assert "type=wifi" in text and "autoconnect=true" in text and "method=auto" in text


def test_an_open_network_has_no_security_section():
    text = arch.wifi_profile("Cafe", "open", "")
    assert "[wifi-security]" not in text and "ssid=Cafe" in text


def test_networks_that_cannot_be_described_are_not_carried_over():
    assert arch.wifi_profile("Corp", "8021x", "") is None
    assert arch.wifi_profile("Old", "wep", "abc") is None
    assert arch.wifi_profile("NoKey", "psk", "") is None


def test_backslashes_are_escaped_for_the_keyfile_format():
    assert "psk=a\\\\b" in arch.wifi_profile("x", "psk", "a\\b")


def test_the_profile_uuid_is_stable_so_a_dry_run_is_repeatable():
    assert arch.wifi_profile("x", "psk", "p") == arch.wifi_profile("x", "psk", "p")


def test_the_wifi_profile_is_written_private_and_never_logged():
    cfg = make_config()
    cfg.network.wifi_ssid, cfg.network.wifi_security, cfg.network.wifi_passphrase = "Home/Net", "psk", "wifi-secret-9"
    runner, lines = dry_run_everything(cfg)
    written = [op for op in runner.operations if isinstance(op, FileWrite) and "system-connections" in op.path]
    assert len(written) == 1
    assert written[0].path == "/mnt/etc/NetworkManager/system-connections/Home_Net.nmconnection"
    assert written[0].mode == 0o600 and written[0].secret
    assert "psk=wifi-secret-9" in written[0].content
    assert not any("wifi-secret-9" in line for line in lines)


def test_no_wifi_profile_without_a_wifi_connection():
    runner, _ = dry_run_everything(make_config())
    assert not [op for op in runner.operations if isinstance(op, FileWrite) and "system-connections" in op.path]


def test_the_wifi_passphrase_is_masked_in_the_logged_configuration():
    cfg = InstallConfig()
    cfg.network.wifi_ssid, cfg.network.wifi_passphrase = "Home", "hunter2hunter2"
    assert "hunter2" not in str(cfg.redacted())


def test_network_answers_can_come_from_an_answers_file():
    cfg = InstallConfig()
    cfg.update_from({"network": {"wifi_ssid": "Home", "wifi_security": "psk", "wifi_passphrase": "x" * 10},
                     "arch": {"laptop": True}})
    assert cfg.network.wifi_ssid == "Home" and cfg.arch.laptop is True


# -- the desktop -------------------------------------------------------------------------------------------


def test_hyprland_handles_the_laptop_function_keys_and_the_touchpad():
    config = arch.hyprland_config("us")
    for key in ("XF86MonBrightnessUp", "XF86MonBrightnessDown", "XF86AudioRaiseVolume", "XF86AudioMute"):
        assert key in config
    assert "tap-to-click = true" in config and "disable_while_typing = true" in config
    assert "monitor = , preferred, auto, auto" in config  # scale chosen from the screen density


# -- Windows -----------------------------------------------------------------------------------------------


def test_windows_fast_startup_and_hibernation_are_turned_off():
    commands = "\n".join(specialize_commands())
    assert r"Session Manager\Power" in commands and "HiberbootEnabled /t REG_DWORD /d 0" in commands
    assert "HibernateEnabled /t REG_DWORD /d 0" in commands
    script = finish_script(InstallConfig().windows, shared_partition=False)
    assert "powercfg.exe /hibernate off" in script


def test_the_firmware_product_key_is_installed_but_never_logged():
    script = finish_script(InstallConfig().windows, shared_partition=False)
    assert "OA3xOriginalProductKey" in script and "slmgr.vbs" in script
    assert "/ipk $firmwareKey" in script and "/ato" in script
    # The only mention of the key variable outside the licence commands must not be a Say.
    assert not any(line.lstrip().startswith("Say") and "$firmwareKey" in line for line in script.splitlines())


# -- the AUR helper must start, otherwise it is built from source --------------------------------------------

from dualboot_installer.runner import CommandError, Runner  # noqa: E402
from dualboot_installer.steps.context import Context  # noqa: E402


class AurRunner(Runner):
    """A runner where `<helper> --version` fails the first `version_failures` times."""

    def __init__(self, version_failures: int, build_failures: tuple[str, ...] = ()) -> None:
        super().__init__(dry_run=True, log=lambda line: None)
        self.version_failures = version_failures
        self.build_failures = build_failures

    def run(self, command: Command) -> int:
        self.operations.append(command)
        text = " ".join(command.argv)
        failing = False
        if text.endswith("--version") and self.version_failures > 0:
            self.version_failures -= 1
            failing = True
        if any(f"/{name}.git" in text for name in self.build_failures):
            failing = True
        if failing and command.check:
            raise CommandError(command, 127)
        return 127 if failing else 0


def aur_ctx(runner: Runner, helper: str = "paru") -> Context:
    cfg = make_config(aur_helper=helper)
    return Context(cfg, cfg.layout(), runner)


def aur_commands(runner: Runner) -> list[str]:
    return [" ".join(op.argv) for op in runner.operations if isinstance(op, Command)]


def test_a_prebuilt_aur_helper_that_starts_is_kept():
    runner = AurRunner(version_failures=0)
    arch.install_aur_helper(aur_ctx(runner))
    builds = [c for c in aur_commands(runner) if ".git" in c]
    assert len(builds) == 1 and "paru-bin.git" in builds[0]


def test_a_prebuilt_aur_helper_that_does_not_start_is_replaced_by_the_source_package():
    runner = AurRunner(version_failures=1)  # paru-bin: libalpm.so.15 not found
    arch.install_aur_helper(aur_ctx(runner))
    commands = aur_commands(runner)
    builds = [c for c in commands if ".git" in c]
    assert "paru-bin.git" in builds[0] and builds[1].count("paru.git") == 1 and "paru-bin" not in builds[1]
    removal = next(i for i, c in enumerate(commands) if "pacman -Rns" in c)
    assert commands.index(builds[0]) < removal < commands.index(builds[1])  # -bin goes before the source build


def test_the_aur_helper_failing_everywhere_is_not_fatal_and_the_temporary_sudoers_is_removed():
    runner = AurRunner(version_failures=0, build_failures=("yay-bin", "yay"))
    arch.install_aur_helper(aur_ctx(runner, "yay"))  # does not raise
    assert any(c.startswith("rm -f") and "99-archinstall-aur" in c for c in aur_commands(runner))


def test_no_aur_helper_means_no_aur_commands():
    runner = AurRunner(version_failures=0)
    arch.install_aur_helper(aur_ctx(runner, "none"))
    assert aur_commands(runner) == []


def test_python_gobject_comes_with_the_power_profiles_daemon():
    # powerprofilesctl is a Python script that imports gi; the daemon package does not depend on it.
    assert "python-gobject" in arch.LAPTOP_PACKAGES


# -- the Network screen follows a slow Wi-Fi connection ----------------------------------------------------

import asyncio  # noqa: E402

from dualboot_installer.app import STEPS, InstallerApp  # noqa: E402
from dualboot_installer.screens.network import ONLINE_RECHECK_DELAYS, NetworkScreen  # noqa: E402
from dualboot_installer.system.demo import DemoSystem  # noqa: E402


class SlowDhcpSystem(DemoSystem):
    """Offline until `online_after` is_online() calls have been made after the Wi-Fi connected."""

    def __init__(self) -> None:
        super().__init__()
        self.connected = False
        self.calls_since_connect = 0

    def is_online(self) -> bool:
        if not self.connected:
            return False
        self.calls_since_connect += 1
        return self.calls_since_connect >= 2  # the first check, right after association, is too early

    def wifi_connect(self, device, ssid, passphrase):
        self.connected = True
        return True, "Connected."


def test_the_network_screen_says_connected_once_dhcp_is_done_without_pressing_scan(monkeypatch):
    delays: list[float] = []
    pending: list = []

    def recording_timer(self, delay, callback=None, **kwargs):
        # The real timers fire seconds apart, each after the previous check has finished: the test
        # fires them one at a time, by hand (all at once, each check would cancel the previous one).
        delays.append(delay)
        pending.append(callback)

    monkeypatch.setattr(NetworkScreen, "set_timer", recording_timer)

    async def scenario():
        app = InstallerApp(SlowDhcpSystem(), dry_run=False)
        async with app.run_test(size=(140, 60)) as pilot:
            await pilot.pause()
            app.index = STEPS.index(NetworkScreen)
            app.switch_screen(app._make(app.index))
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert not screen._online
            screen.query_one("#wifi-list").highlighted = 0
            screen._connect(screen._device, "HomeNetwork", "psk", "wifi-test-pass-2026")
            for _ in range(150):  # up to 15 s: the machine may be busy, wait for the condition, not a delay
                await pilot.pause(0.1)
                await app.workers.wait_for_complete()
                if screen._online:
                    break
                if pending:
                    pending.pop(0)()
            return screen._online, screen.cfg.network.wifi_ssid, screen.collect()

    online, ssid, error = asyncio.run(scenario())
    assert online and ssid == "HomeNetwork" and error is None
    assert tuple(delays) == ONLINE_RECHECK_DELAYS
