"""Preflight decisions that must be taken before anything is erased."""

import pytest

from dualboot_installer import secureboot
from dualboot_installer.config import InstallConfig
from dualboot_installer.layout import GIB
from dualboot_installer.runner import Command, Runner
from dualboot_installer.steps import bootloader, preflight
from dualboot_installer.steps.context import Context, StepError
from dualboot_installer.system.models import SecureBootState


class NothingRunner(Runner):
    """A runner that behaves like a real one (dry_run False) but never executes anything."""

    def __init__(self) -> None:
        super().__init__(dry_run=False, log=lambda line: None)

    def run(self, command: Command) -> int:
        self.operations.append(command)
        return 0

    def capture(self, argv, stdin=None) -> str:
        self.operations.append(Command(argv))
        return ""


def make_ctx(strategy: str, *, touch_firmware: bool = True) -> Context:
    cfg = InstallConfig()
    cfg.disk.device, cfg.disk.size_bytes, cfg.disk.windows_gib = "/dev/vda", 300 * GIB, 100
    cfg.partial = True  # no Windows image to look at here
    cfg.secureboot.resolved = strategy
    cfg.secureboot.touch_firmware = touch_firmware
    return Context(cfg, cfg.layout(), NothingRunner())


@pytest.fixture(autouse=True)
def tools_present(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(preflight.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(bootloader, "fetch_shim", lambda ctx: "/tmp/shim.rpm")
    monkeypatch.setattr(preflight, "fetch_shim", lambda ctx: "/tmp/shim.rpm")


def test_sbctl_without_a_tpm_is_refused_before_anything_is_erased(monkeypatch):
    monkeypatch.setattr(secureboot, "detect", lambda: SecureBootState(False, True, has_tpm=False))
    with pytest.raises(StepError, match="TPM"):
        preflight.preflight(make_ctx("sbctl"))


def test_sbctl_outside_setup_mode_is_refused(monkeypatch):
    monkeypatch.setattr(secureboot, "detect", lambda: SecureBootState(False, False, has_tpm=True))
    with pytest.raises(StepError, match="Setup Mode"):
        preflight.preflight(make_ctx("sbctl"))


def test_sbctl_with_a_tpm_in_setup_mode_goes_ahead(monkeypatch):
    monkeypatch.setattr(secureboot, "detect", lambda: SecureBootState(False, True, has_tpm=True))
    ctx = make_ctx("sbctl")
    preflight.preflight(ctx)
    assert any(isinstance(op, Command) and op.argv[:2] == ["pacman", "-Sy"] for op in ctx.runner.operations)


def test_the_sbctl_check_uses_the_shared_rule_not_its_own(monkeypatch):
    # secureboot.sbctl_problem is the single source of truth (wizard and preflight agree).
    seen = []
    monkeypatch.setattr(secureboot, "detect", lambda: SecureBootState(False, True, has_tpm=True))
    monkeypatch.setattr(secureboot, "sbctl_problem", lambda state: seen.append(state) or "custom reason")
    with pytest.raises(StepError, match="custom reason"):
        preflight.preflight(make_ctx("sbctl"))
    assert seen == [SecureBootState(False, True, has_tpm=True)]


def test_no_firmware_check_when_the_firmware_is_not_touched(monkeypatch):
    monkeypatch.setattr(secureboot, "detect", lambda: SecureBootState(False, False, has_tpm=False))
    preflight.preflight(make_ctx("sbctl", touch_firmware=False))  # does not raise


def test_shim_does_not_care_about_the_tpm(monkeypatch):
    monkeypatch.setattr(secureboot, "detect", lambda: SecureBootState(False, False, has_tpm=False))
    preflight.preflight(make_ctx("shim"))  # does not raise


# -- the instructions printed for the shim strategy ----------------------------------------------------------


def test_mok_instructions_warn_about_the_10_second_window_and_the_whole_procedure():
    text = bootloader.MOK_INSTRUCTIONS
    assert "PRESS A KEY WITHIN 10 SECONDS" in text
    assert "cancelled" in text and "Verification failed" in text
    # Advice learned in the end-to-end test: enroll the key BEFORE turning Secure Boot on.
    assert "RECOMMENDED" in text and "Secure Boot OFF" in text
    for step in ("Enroll MOK", "Continue", "Yes", "password", "Reboot"):
        assert step in text
    assert text.index("Enroll MOK") < text.index("Continue") < text.index("Yes") < text.index("Reboot")


def test_mok_instructions_say_how_to_import_again_when_the_window_was_missed():
    text = bootloader.MOK_INSTRUCTIONS
    assert "sudo mokutil --import /var/lib/archinstall/secureboot/MOK.cer" in text
    assert bootloader.MOK_CERT_PATH == "/var/lib/archinstall/secureboot/MOK.cer"


def test_the_shim_install_logs_the_instructions(tmp_path):
    from dualboot_installer.steps import arch  # noqa: F401  (imports the whole step chain)

    cfg = InstallConfig(dry_run=True)
    cfg.disk.device, cfg.disk.size_bytes, cfg.disk.windows_gib = "/dev/vda", 300 * GIB, 100
    cfg.secureboot.resolved = "shim"
    cfg.secureboot.mok_password = "Abcd1234"
    lines: list[str] = []
    runner = Runner(dry_run=True, log=lines.append)
    bootloader.install_bootloader(Context(cfg, cfg.layout(), runner))
    assert bootloader.MOK_INSTRUCTIONS in lines
