"""Optional software: developer tools, desktop applications and desktop shell presets (dotfiles).

Everything here is data plus small pure functions, so the wizard screen, the installation steps
and the tests all read the same lists.
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class Preset:
    """A desktop shell with its dotfiles (only with the Hyprland desktop)."""

    id: str
    label: str
    description: str = ""
    packages: tuple[str, ...] = ()  # official repositories
    aur_packages: tuple[str, ...] = ()


DOTFILES: dict[str, Preset] = {
    "none": Preset("none", "Default Hyprland setup", "waybar, wofi, kitty: a plain, light configuration"),
}


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
        packages += preset.packages
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
