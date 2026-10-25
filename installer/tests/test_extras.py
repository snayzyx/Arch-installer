"""Developer tools, desktop applications and desktop shell presets (extras.py and their installation)."""

from dualboot_installer import extras
from dualboot_installer.config import InstallConfig
from dualboot_installer.runner import Command, FileWrite
from dualboot_installer.steps import arch
from dualboot_installer.steps.context import Context
from dualboot_installer.validate import problems

from test_laptop import AurRunner
from test_plan import dry_run_everything, make_config


def cfg_with(**arch_fields) -> InstallConfig:
    cfg = InstallConfig()
    for key, value in arch_fields.items():
        setattr(cfg.arch, key, value)
    return cfg


def commands(runner) -> list[str]:
    return [" ".join(op.argv) for op in runner.operations if isinstance(op, Command)]


# -- defaults leave the installation as it was ---------------------------------------------------------------


def test_nothing_extra_is_installed_by_default():
    a = InstallConfig().arch
    assert extras.dev_packages(a) == [] and extras.selected_apps(a) == []
    assert extras.official_packages(a) == [] and extras.aur_packages(a) == []
    assert extras.effective_aur_helper(a) == "none"
    assert extras.selected_preset(a) is None


def test_every_listed_package_name_is_a_plain_package_name():
    names = [p for _, packages in extras.DEV_GROUPS.values() for p in packages]
    names += [app.package for app in extras.APPS.values()]
    assert all(name == name.strip().lower() and " " not in name for name in names)
    assert len(names) == len(set(names))


# -- what gets installed -------------------------------------------------------------------------------------


def test_developer_groups_add_their_packages():
    a = cfg_with(dev_groups=["languages", "cli"]).arch
    packages = extras.dev_packages(a)
    for expected in ("git", "nodejs", "npm", "python", "python-pip", "go", "rust", "ripgrep", "neovim"):
        assert expected in packages
    assert "docker" not in packages


def test_librewolf_is_official_and_vscodium_and_postman_come_from_the_aur():
    a = cfg_with(apps=["vscodium", "librewolf", "postman"]).arch
    assert extras.official_packages(a) == ["librewolf"]
    assert extras.aur_packages(a) == ["vscodium-bin", "postman-bin"]


def test_applications_need_the_desktop():
    a = cfg_with(apps=["vscodium", "librewolf"], desktop="minimal").arch
    assert extras.selected_apps(a) == [] and extras.aur_packages(a) == [] and extras.official_packages(a) == []


def test_the_aur_helper_is_installed_on_its_own_when_an_application_needs_the_aur():
    assert extras.effective_aur_helper(cfg_with(apps=["vscodium"]).arch) == "yay"
    assert extras.effective_aur_helper(cfg_with(apps=["librewolf"]).arch) == "none"  # official: no AUR
    assert extras.effective_aur_helper(cfg_with(apps=["vscodium"], aur_helper="paru").arch) == "paru"
    assert extras.effective_aur_helper(cfg_with(aur_helper="paru").arch) == "paru"


def test_the_package_list_carries_the_extras_and_the_tools_to_build_the_aur():
    cfg = cfg_with(dev_groups=["languages"], apps=["vscodium", "librewolf"])
    packages = arch.package_list(cfg, [])
    assert "nodejs" in packages and "librewolf" in packages
    assert "base-devel" in packages and "git" in packages  # makepkg needs them
    assert "vscodium-bin" not in packages  # built from the AUR, not by pacstrap
    assert "base-devel" not in arch.package_list(InstallConfig(), [])


# -- installation steps --------------------------------------------------------------------------------------


def test_docker_enables_the_service_and_adds_the_user_to_the_group():
    runner, _ = dry_run_everything(make_config(dev_groups=["docker"]))
    text = commands(runner)
    assert any("systemctl enable" in c and "docker.service" in c for c in text)
    assert any(c.startswith("arch-chroot /mnt useradd") and "wheel,docker" in c for c in text)


def test_without_docker_the_user_is_only_in_wheel():
    runner, _ = dry_run_everything(make_config())
    useradd = next(c for c in commands(runner) if "useradd" in c)
    assert "wheel," not in useradd and "docker" not in useradd


def test_the_aur_applications_are_installed_after_the_helper_and_the_sudoers_file_goes_away():
    cfg = make_config(apps=["vscodium", "postman"])
    runner = AurRunner(version_failures=0)
    arch.install_aur_helper(Context(cfg, cfg.layout(), runner))
    text = commands(runner)
    helper_build = next(i for i, c in enumerate(text) if "yay-bin.git" in c)
    installs = [i for i, c in enumerate(text) if "yay -S --needed --noconfirm" in c]
    assert [text[i].rsplit(" ", 1)[1] for i in installs] == ["vscodium-bin", "postman-bin"]
    assert helper_build < installs[0]
    assert text[-1].startswith("rm -f") and "99-archinstall-aur" in text[-1]
    assert any(isinstance(op, FileWrite) and op.path.endswith("99-archinstall-aur") for op in runner.operations)


def test_a_helper_that_cannot_be_installed_skips_the_applications_without_failing():
    cfg = make_config(apps=["vscodium"])
    runner = AurRunner(version_failures=0, build_failures=("yay-bin", "yay"))
    arch.install_aur_helper(Context(cfg, cfg.layout(), runner))  # does not raise
    assert not any("yay -S" in c for c in commands(runner))
    assert commands(runner)[-1].startswith("rm -f")


# -- answers -------------------------------------------------------------------------------------------------


def test_the_answers_file_accepts_the_new_fields():
    cfg = InstallConfig()
    cfg.update_from({"arch": {"dev_groups": ["cli"], "apps": ["librewolf"], "dotfiles": "none"}})
    assert cfg.arch.dev_groups == ["cli"] and cfg.arch.apps == ["librewolf"]


def test_unknown_ids_and_a_preset_without_the_desktop_are_reported():
    cfg = make_config(dev_groups=["nope"], apps=["nope"], dotfiles="nope")
    found = " ".join(problems(cfg))
    assert "developer tool group" in found and "application" in found and "preset" in found
    cfg = make_config(desktop="minimal", dotfiles="none")
    assert not [p for p in problems(cfg) if "preset" in p]
