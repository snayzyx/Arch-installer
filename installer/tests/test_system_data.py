from dualboot_installer import secureboot
from dualboot_installer.data.windows import (
    KEYBOARDS,
    LANGUAGES,
    default_keyboard,
    default_language,
    windows_timezone,
)
from dualboot_installer.drivers import packages_for, suggest_profiles
from dualboot_installer.system.models import GpuInfo, SecureBootState
from dualboot_installer.system.regional import parse_iso3166, parse_supported_locales, parse_zone_tab
from dualboot_installer.util import human_size


def test_human_size():
    assert human_size(512) == "512 B"
    assert human_size(1024**3) == "1 GiB"
    assert human_size(int(476.9 * 1024**3)) == "476.9 GiB"


def test_parse_zone_tab_and_iso3166():
    zone_tab = "# comment\nFR\t+4852+00220\tEurope/Paris\nUS\t+404251-0740023\tAmerica/New_York\tEastern\nUS\t+415100-0873900\tAmerica/Chicago\tCentral\n"
    assert parse_zone_tab(zone_tab) == {"FR": ["Europe/Paris"], "US": ["America/New_York", "America/Chicago"]}
    assert parse_iso3166("# c\nFR\tFrance\nUS\tUnited States\n") == {"FR": "France", "US": "United States"}


def test_parse_supported_locales_keeps_utf8_only():
    text = "#en_US.UTF-8 UTF-8\n#en_US ISO-8859-1\nfr_FR.UTF-8 UTF-8\nfr_FR@euro ISO-8859-15\n"
    assert parse_supported_locales(text) == ["en_US.UTF-8", "fr_FR.UTF-8"]


def test_windows_timezone_mapping():
    assert windows_timezone("Europe/Paris") == "Romance Standard Time"
    assert windows_timezone("America/New_York") == "Eastern Standard Time"
    assert windows_timezone("Not/AZone") == "UTC"


def test_windows_default_language_follows_the_country():
    assert default_language("FR").name == "French"
    assert default_language("BR").name == "Portuguese (Brazil)"
    assert default_language("US").name == "English (United States)"
    assert default_language("ZZ").tag == "en-US"
    assert default_language("CA").tag.startswith("en")


def test_every_default_language_and_keyboard_exists_in_the_lists():
    tags = {lang.tag for lang in LANGUAGES}
    assert len(tags) == len(LANGUAGES)
    keyboard_tags = {tag for _, tag in KEYBOARDS}
    assert default_keyboard("fr", "FR") in keyboard_tags
    assert default_keyboard("us", "US") in keyboard_tags
    assert default_keyboard("unknown-map", "DE") == "de-DE"


def test_gpu_profiles_and_packages():
    gpus = [GpuInfo("8086", "Intel"), GpuInfo("10de", "NVIDIA"), GpuInfo("8086", "Intel again")]
    assert suggest_profiles(gpus) == ["intel", "nvidia-open"]
    assert "nvidia-open" in packages_for(["nvidia-open"], "linux")
    lts = packages_for(["nvidia-open"], "linux-lts")
    assert "nvidia-open-dkms" in lts and "linux-lts-headers" in lts and "nvidia-open" not in lts
    assert packages_for(["intel", "amd"], "linux").count("mesa") == 1


def test_secure_boot_detection_reads_efivars(tmp_path):
    guid = secureboot.EFI_GLOBAL_GUID
    (tmp_path / f"SecureBoot-{guid}").write_bytes(b"\x06\x00\x00\x00\x01")
    (tmp_path / f"SetupMode-{guid}").write_bytes(b"\x06\x00\x00\x00\x00")
    tpm = tmp_path / "tpm0"
    tpm.mkdir()
    state = secureboot.detect(tmp_path, tpm=tpm)
    assert state == SecureBootState(enabled=True, setup_mode=False, has_tpm=True)


def test_secure_boot_detection_without_efivars_or_tpm(tmp_path):
    state = secureboot.detect(tmp_path, tpm=tmp_path / "no-tpm")
    assert state == SecureBootState(enabled=None, setup_mode=None, has_tpm=False)


def test_strategy_resolution():
    assert secureboot.resolve_strategy("auto", SecureBootState(False, True)) == "sbctl"
    assert secureboot.resolve_strategy("auto", SecureBootState(False, False)) == "shim"
    assert secureboot.resolve_strategy("auto", SecureBootState(None, None)) == "shim"
    assert secureboot.resolve_strategy("sbctl", SecureBootState(False, False)) == "sbctl"
    assert secureboot.resolve_strategy("shim", SecureBootState(False, True)) == "shim"


def test_automatic_choice_never_picks_sbctl_without_a_tpm():
    # Verified in a UEFI VM: GRUB without shim cannot start Linux under Secure Boot without a TPM.
    no_tpm = SecureBootState(False, True, has_tpm=False)
    assert secureboot.resolve_strategy("auto", no_tpm) == "shim"
    assert secureboot.resolve_strategy("sbctl", no_tpm) == "sbctl"  # an explicit choice is respected...
    assert "TPM" in secureboot.sbctl_problem(no_tpm)  # ...but flagged as impossible


def test_sbctl_problems():
    assert secureboot.sbctl_problem(SecureBootState(False, True, has_tpm=True)) is None
    assert "Setup Mode" in secureboot.sbctl_problem(SecureBootState(False, False, has_tpm=True))
    assert secureboot.sbctl_problem(SecureBootState(None, None, has_tpm=True)) is None  # unknown: let it try
