"""Desktop shell presets: the dotfiles are downloaded from GitHub while installing.

Nothing of a preset is stored in the ISO but its small installation scripts (`presets/<id>/`). They
clone the LATEST commit of each project on the installed system and run the project's own installer
as the new user, so the person gets the current version. Because tracking the latest commit can break
when a project changes, three safeguards apply:

* before the disk is erased, every source must answer (`check_sources`), otherwise nothing is touched;
* a script checks that the files and options it relies on still exist and exits with a distinct code if
  not; the preset is then rolled back and retried with the commits that were tested (`fallback_env`);
* if that fails too, the default Hyprland setup is put back and the installation goes on (this step
  never fails the installation).
"""

from __future__ import annotations

import shlex

from .. import extras
from ..data.xkb import xkb_layout
from .arch import clean_build_caches, passwordless_sudo
from .context import Context, StepError

# Where the scripts of the preset are copied inside the new system.
TARGET_DIR = "/usr/local/share/archinstall/presets"
AUR_RPC = "https://aur.archlinux.org/rpc/v5/info"


def _probe(ctx: Context, url: str) -> bool:
    """True when the repository answers the way `git clone` needs (there is no git on the live system)."""
    return ctx.run("curl", "-fsSL", "--max-time", "30", "-o", "/dev/null",
                   f"{url}/info/refs?service=git-upload-pack", check=False, quiet=True) == 0


def _aur_missing(ctx: Context, packages: list[str]) -> list[str]:
    query = "&".join(f"arg[]={package}" for package in packages)
    answer = ctx.capture("curl", "-fsS", "--max-time", "30", f"{AUR_RPC}?{query}").replace(" ", "")
    return [package for package in packages if f'"Name":"{package}"' not in answer]


def check_sources(ctx: Context) -> None:
    """Before anything is erased: what has to be downloaded from outside the Arch repositories exists.

    The sources of a desktop shell preset are required (a missing one stops the installation, nothing has
    been touched yet). The AUR applications are optional extras: a problem is only reported.
    """
    a = ctx.cfg.arch
    preset = extras.selected_preset(a)
    apps = [app.package for app in extras.selected_apps(a) if app.aur]
    if preset is None and not apps:
        return
    if ctx.runner.dry_run:
        ctx.log("Would check that the downloads answer: "
                + ", ".join((extras.preset_sources(preset) if preset else []) + apps) + ".")
        return
    if preset is not None:
        unreachable = [url for url in extras.preset_sources(preset) if not _probe(ctx, url)]
        wanted = [*preset.aur_packages, *preset.aur_check]
        missing = _aur_missing(ctx, wanted) if wanted else []
        if unreachable or missing:
            parts = []
            if unreachable:
                parts.append("these repositories do not answer: " + ", ".join(unreachable))
            if missing:
                parts.append("these AUR packages cannot be found: " + ", ".join(missing))
            raise StepError(f"The desktop shell {preset.label} cannot be downloaded right now "
                            f"({'; '.join(parts)}). Check the connection, or choose the default Hyprland "
                            "setup. Nothing was changed.")
    for package in _aur_missing(ctx, apps) if apps else []:
        ctx.log(f"WARNING: the AUR package {package} could not be found; it will be skipped.")


def _stage(ctx: Context, preset: extras.Preset) -> None:
    """Copy the scripts of the preset into the new system."""
    for path in sorted((extras.PRESET_DIR / preset.id).iterdir()):
        if path.is_file():
            ctx.write(f"{TARGET_DIR}/{preset.id}/{path.name}", path.read_text(encoding="utf-8"),
                      0o755 if path.suffix == ".sh" else 0o644)


def _as_user(ctx: Context, script: str) -> int:
    return ctx.chroot("runuser", "-l", ctx.cfg.arch.username, "-c", script, check=False)


def _run_script(ctx: Context, preset: extras.Preset, name: str, env: tuple[tuple[str, str], ...]) -> int:
    assignments = "".join(f"{key}={shlex.quote(value)} " for key, value in env)
    path = f"{TARGET_DIR}/{preset.id}/{name}"
    return _as_user(ctx, f"timeout {preset.script_timeout_minutes}m env {assignments}bash {path}")


def _attempt(ctx: Context, preset: extras.Preset, env: tuple[tuple[str, str], ...]) -> bool:
    # KB_LAYOUT: some projects hard-code "us" and need the person's layout written for them (Caelestia).
    env = (*env, ("KB_LAYOUT", xkb_layout(ctx.cfg.arch.keymap)))
    for name in preset.scripts:
        ctx.log(f"Running {name} for {preset.label}.")
        code = _run_script(ctx, preset, name, env)
        if code != 0:
            ctx.log(f"WARNING: {name} failed (exit code {code}; the RECIPE-FAIL line above says why).")
            return False
    return True


def _clean_up(ctx: Context, preset: extras.Preset) -> None:
    """Delete what the preset no longer needs (clone caches), only after it installed."""
    for path in preset.cleanup:
        _as_user(ctx, f'rm -rf "$HOME/"{shlex.quote(path)}')


def _roll_back(ctx: Context, preset: extras.Preset) -> None:
    if preset.rollback:
        _run_script(ctx, preset, preset.rollback, ())


def install_dotfiles(ctx: Context) -> None:
    """Install the chosen desktop shell preset. Never fatal."""
    preset = extras.selected_preset(ctx.cfg.arch)
    if preset is None:
        return
    _stage(ctx, preset)
    with passwordless_sudo(ctx):
        if _attempt(ctx, preset, ()):
            ctx.log(f"The desktop shell {preset.label} is installed (latest version).")
            _clean_up(ctx, preset)
            clean_build_caches(ctx)
            return
        _roll_back(ctx, preset)
        if preset.fallback_env:
            ctx.log(f"WARNING: the latest version of {preset.label} did not install: trying the versions "
                    "that were tested.")
            if _attempt(ctx, preset, preset.fallback_env):
                ctx.log(f"The desktop shell {preset.label} is installed (the last tested versions, "
                        "not the latest).")
                _clean_up(ctx, preset)
                clean_build_caches(ctx)
                return
            _roll_back(ctx, preset)
    clean_build_caches(ctx)
    ctx.log(f"WARNING: the desktop shell {preset.label} could not be installed. The default Hyprland setup "
            "is back; see the messages above, and try the project's own instructions later.")
