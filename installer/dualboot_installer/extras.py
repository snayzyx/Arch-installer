"""Optional software: developer tools, desktop applications and desktop shell presets (dotfiles).

Everything here is data plus small pure functions, so the wizard screen, the installation steps
and the tests all read the same lists.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # config.py imports the defaults from this module
    from .config import ArchConfig

# Developer tool groups: id -> (label, official packages). All of them are in the Arch repositories.
DEV_GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "languages": (
        "Languages and build tools: git, Node.js (npm, pnpm), Python (pip, pipx, virtualenv), Go, Rust, "
        "cmake, ninja, gdb",
        ("git", "nodejs", "npm", "pnpm", "python", "python-pip", "python-pipx", "python-virtualenv", "go",
         "rust", "cmake", "ninja", "gdb"),
    ),
    "cli": (
        "Command line helpers: GitHub CLI, ssh, jq, ripgrep, fd, fzf, tmux, neovim, httpie",
        ("github-cli", "openssh", "jq", "ripgrep", "fd", "fzf", "tmux", "neovim", "httpie"),
    ),
    "docker": (
        "Docker and docker-compose (you join the docker group, which is equivalent to root access)",
        ("docker", "docker-compose"),
    ),
}
# Nothing is installed unless asked for: the wizard screen that offers these is not written yet, so the
# defaults leave the installation exactly as before. The screen will preselect the RECOMMENDED lists.
DEFAULT_DEV_GROUPS: list[str] = []
RECOMMENDED_DEV_GROUPS = ["languages", "cli"]


@dataclass(frozen=True)
class App:
    id: str
    label: str
    package: str
    aur: bool  # True: built from the AUR, False: in the official repositories


APPS: dict[str, App] = {
    "vscodium": App("vscodium", "VSCodium (VS Code without Microsoft's telemetry), from the AUR", "vscodium-bin", True),
    "librewolf": App("librewolf", "LibreWolf (privacy-focused Firefox)", "librewolf", False),
    "postman": App("postman", "Postman (API client), from the AUR", "postman-bin", True),
}
DEFAULT_APPS: list[str] = []
RECOMMENDED_APPS = ["vscodium", "librewolf", "postman"]

# AUR helper used when the person chose none but something has to be built from the AUR.
# yay-bin is a prebuilt Go program (seconds); paru has to compile Rust (minutes).
FALLBACK_AUR_HELPER = "yay"


# The files of each preset (installation scripts, base Hyprland config, package list) live in
# presets/<id>/. They are plain text, copied to the new system and run there as the new user.
PRESET_DIR = Path(__file__).parent / "presets"


def read_packages(preset_id: str) -> tuple[str, ...]:
    """Package names of presets/<id>/packages-official.txt (comments start with #)."""
    text = (PRESET_DIR / preset_id / "packages-official.txt").read_text(encoding="utf-8")
    names = [word for line in text.splitlines() if not line.lstrip().startswith("#") for word in line.split()]
    return tuple(dict.fromkeys(names))


@dataclass(frozen=True)
class Preset:
    """A desktop shell with its dotfiles (only with the Hyprland desktop).

    Nothing of it is stored in the ISO. Its scripts download the LATEST version of each project
    from GitHub while installing, so the preset needs a connection. They print `RECIPE-RESULT`
    lines (with the installed commit) and exit with a distinct code when the project changed in a
    way they no longer understand.
    """

    id: str
    label: str
    description: str = ""
    packages: tuple[str, ...] = ()  # official repositories, installed with the system
    aur_packages: tuple[str, ...] = ()  # built from the AUR after the system is installed
    aur_check: tuple[str, ...] = ()  # more AUR packages the preset's own installer pulls in: checked before the erase
    aur_helper: str = ""  # the helper the preset's installer insists on (it replaces the person's choice)
    sources: tuple[str, ...] = ()  # https URLs of the git repositories: checked before the disk is erased
    scripts: tuple[str, ...] = ()  # files of presets/<id>/, run in this order as the new user
    # Environment variables for the retry when the latest version failed: the commits that were tested.
    fallback_env: tuple[tuple[str, str], ...] = ()
    rollback: str = ""  # script that puts the default setup back after a failure
    services: tuple[str, ...] = ()  # systemd units the shell needs, enabled by the installer (their packages are in `packages`)
    cleanup: tuple[str, ...] = ()  # paths relative to the person's home that are safe to delete once the preset is installed
    base_config: str = ""  # file of presets/<id>/ written as ~/.config/hypr/hyprland.lua (@KB_LAYOUT@ is replaced)
    script_timeout_minutes: int = 60


DOTFILES: dict[str, Preset] = {
    "none": Preset("none", "Default Hyprland setup", "waybar, wofi, kitty: a plain, light configuration"),
    "macshell": Preset(
        "macshell", "MacShell + MacOS-Like icons",
        "a macOS-style menu bar, Dock and lock screen, with macOS-like icons",
        packages=read_packages("macshell"),
        sources=("https://github.com/0-ss/MacShell", "https://github.com/vinceliuice/MacTahoe-icon-theme",
                 "https://github.com/Scrimas/MacOS-Like"),
        scripts=("20-macshell.sh", "30-icons.sh", "40-verify.sh"),
        fallback_env=(("MACSHELL_REF", "1bdb51870b621100fd2709f66c8f8e00ab17b9c4"),
                      ("MACTAHOE_REF", "839848b9a8a38a92a6936e30c4abe35cc6f2546d"),
                      ("MACOSLIKE_REF", "efbc63d48d52bbaf52e9a70fb18e0d0c3c0719b2")),
        rollback="restore-default.sh",
        services=("bluetooth.service", "power-profiles-daemon.service"),
        cleanup=(".cache/archinstall",),  # MacShell and the icon installer copy their files, nothing links into the clones
        base_config="hyprland-base.lua",
    ),
    "revo": Preset(
        "revo", "Revo Shell (experimental)",
        "several Quickshell shells, macOS-like by default; large (about 5 GB, 15-25 minutes) and the project "
        "has no license",
        packages=read_packages("revo"),
        aur_packages=("mpvpaper", "python-pywal16", "ttf-rubik-vf", "kde-material-you-colors"),
        sources=("https://github.com/vzbc/revo-shell",),
        scripts=("20-revo.sh",),
        fallback_env=(("REVO_REF", "72f7113c9530577acff57778246b0583324c3774"),),
        rollback="restore-default.sh",
        services=("bluetooth.service", "upower.service", "power-profiles-daemon.service"),
        # The clone (about 225 MB) and pip's cache (about 700 MB with PySide6): nothing links into them.
        cleanup=(".cache/archinstall", ".cache/pip"),
    ),
    "caelestia": Preset(
        "caelestia", "Caelestia",
        "a fluid, themed desktop shell (bar, launcher, dashboard, lock screen) with its Hyprland setup",
        packages=read_packages("caelestia"),
        aur_packages=("caelestia-cli",),
        aur_check=("yay-bin", "caelestia-shell", "quickshell-git", "qt6-m3shapes-git", "libcava",
                   "python-materialyoucolor", "ttf-rubik-vf", "pwvucontrol", "papirus-folders"),
        aur_helper="yay",
        sources=("https://github.com/caelestia-dots/caelestia", "https://github.com/caelestia-dots/cli",
                 "https://github.com/caelestia-dots/shell", "https://git.outfoxxed.me/quickshell/quickshell",
                 "https://github.com/soramanew/m3shapes"),
        scripts=("20-caelestia.sh", "40-verify.sh"),
        fallback_env=(("CAELESTIA_DOTS_REF", "c034621845453260b245692b13ce514e7371d735"),),
        rollback="restore-default.sh",
        services=("bluetooth.service", "power-profiles-daemon.service"),
    ),
}


def base_hyprland_config(preset: Preset, xkb_layout: str) -> str:
    """The default Hyprland setup in the form the preset needs (Lua), with the person's keyboard layout."""
    text = (PRESET_DIR / preset.id / preset.base_config).read_text(encoding="utf-8")
    return text.replace("@KB_LAYOUT@", xkb_layout)


def preset_sources(preset: Preset) -> list[str]:
    """URLs that must answer before the installation starts (the disk is not erased yet)."""
    return list(preset.sources)


def has_desktop(a: ArchConfig) -> bool:
    return a.desktop == "hyprland"


def dev_packages(a: ArchConfig) -> list[str]:
    return [package for group in a.dev_groups if group in DEV_GROUPS for package in DEV_GROUPS[group][1]]


def selected_apps(a: ArchConfig) -> list[App]:
    """The desktop applications to install: none without a desktop."""
    return [APPS[app_id] for app_id in a.apps if app_id in APPS] if has_desktop(a) else []


def selected_preset(a: ArchConfig) -> Preset | None:
    """The chosen desktop shell preset, None for the default setup (or without a desktop)."""
    if not has_desktop(a) or a.dotfiles == "none":
        return None
    return DOTFILES.get(a.dotfiles)


def official_packages(a: ArchConfig) -> list[str]:
    """Extra packages from the official repositories (installed by pacstrap with the system)."""
    packages = dev_packages(a)
    packages += [app.package for app in selected_apps(a) if not app.aur]
    preset = selected_preset(a)
    if preset:
        packages += ["git", *preset.packages]
    return list(dict.fromkeys(packages))


def aur_packages(a: ArchConfig) -> list[str]:
    """Packages that have to be built from the AUR after the system is installed."""
    packages = [app.package for app in selected_apps(a) if app.aur]
    preset = selected_preset(a)
    if preset:
        packages += preset.aur_packages
    return list(dict.fromkeys(packages))


def effective_aur_helper(a: ArchConfig) -> str:
    """The helper to install: the chosen one, or the fallback when something needs the AUR."""
    preset = selected_preset(a)
    if preset is not None and preset.aur_helper:
        return preset.aur_helper
    if a.aur_helper != "none":
        return a.aur_helper
    return FALLBACK_AUR_HELPER if aur_packages(a) else "none"


def problems(a: ArchConfig) -> list[str]:
    """Unknown ids in an answers file (the wizard cannot produce them)."""
    found = [f"Unknown developer tool group: {group!r}." for group in a.dev_groups if group not in DEV_GROUPS]
    found += [f"Unknown application: {app!r}." for app in a.apps if app not in APPS]
    if a.dotfiles not in DOTFILES:
        found.append(f"Unknown desktop shell preset: {a.dotfiles!r}.")
    elif a.dotfiles != "none" and not has_desktop(a):
        found.append("A desktop shell preset needs the Hyprland desktop.")
    return found
