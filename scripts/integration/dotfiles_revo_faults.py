#!/usr/bin/env python3
"""Fault injection for the Revo Shell preset, against the REAL installer code and a REAL installed target.

Run from checks-dotfiles-revo.sh inside the test container, after a normal install has been mounted at $CHECK
(default /tmp/check). Nothing in installer/ is edited: the preset folder is COPIED to a temp directory, its
20-revo.sh is modified in the copy, and `extras.PRESET_DIR` is pointed at the copy before
`dualboot_installer.steps.dotfiles.install_dotfiles` runs for that target (arch-chroot, the real scripts, the
real network).

    dotfiles_revo_faults.py retry        20-revo.sh exits 30 unless REVO_REF is set -> the latest version "fails",
                                         restore-default.sh runs, the retry with the known-good commit works
    dotfiles_revo_faults.py bothfail     20-revo.sh always exits 30 -> rollback, retry, rollback, default setup,
                                         no exception
    dotfiles_revo_faults.py sources      check_sources with github.com unreachable -> StepError "Nothing was changed"
    dotfiles_revo_faults.py sources-ok   check_sources with the real network -> no error (positive control)
    dotfiles_revo_faults.py aur-missing  check_sources with one AUR package that does not exist added to the
                                         preset (in memory) -> StepError naming it, "Nothing was changed"
    dotfiles_revo_faults.py patched      NO fault: install_dotfiles with the files of PRESET_PATCH_DIR replacing the
                                         same-named files of the preset copy (proves a proposed patch)

Environment: TARGET (default /tmp/check), ANSWERS (default /tmp/answers.json), INSTALLER_PATH (default /src/installer),
PRESET_PATCH_DIR (optional): files that replace the same-named files of the copied preset.
The log of the run goes to stdout; the last line is `FAULT-RESULT ...`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.environ.get("INSTALLER_PATH", "/src/installer"))

from dualboot_installer import extras  # noqa: E402
from dualboot_installer.config import InstallConfig  # noqa: E402
from dualboot_installer.runner import Runner  # noqa: E402
from dualboot_installer.steps import dotfiles  # noqa: E402
from dualboot_installer.steps.context import Context, StepError  # noqa: E402

PRESET = "revo"
TARGET = os.environ.get("TARGET", "/tmp/check")
FIRST_SCRIPT = extras.DOTFILES[PRESET].scripts[0]
FAIL_CODE = 30  # the code 20-revo.sh uses for "installed but the verification failed"
NO_SUCH_AUR_PACKAGE = "archinstall-no-such-package-xyz"


def make_context(lines: list[str]) -> Context:
    cfg = InstallConfig()
    cfg.update_from(json.loads(Path(os.environ.get("ANSWERS", "/tmp/answers.json")).read_text()))
    cfg.dry_run = False

    def log(line: str) -> None:
        lines.append(line)
        print(line, flush=True)

    return Context(cfg, cfg.layout(), Runner(dry_run=False, log=log), target=TARGET)


def patched_preset_dir(mode: str) -> Path:
    """A copy of presets/ whose 20-revo.sh fails: always, or only while REVO_REF is not set."""
    root = Path(tempfile.mkdtemp(prefix="presets-"))
    shutil.copytree(extras.PRESET_DIR, root, dirs_exist_ok=True)
    patch = os.environ.get("PRESET_PATCH_DIR")
    if patch:
        for path in sorted(Path(patch).iterdir()):
            shutil.copy(path, root / PRESET / path.name)
            print(f"[fault] patched file applied to the preset copy: {path.name}", flush=True)
    if mode == "none":
        return root
    script = root / PRESET / FIRST_SCRIPT
    guard = {
        "always": f'echo "RECIPE-FAIL {PRESET}: injected fault (always)"; exit {FAIL_CODE}\n',
        # With REVO_REF set (the retry) the script first records what the rollback left behind, to prove it.
        "no-ref": ('if [ -n "${REVO_REF:-}" ]; then\n'
                   '  { echo "--- hyprland.conf (first lines):"; head -n 3 "$HOME/.config/hypr/hyprland.conf" 2>&1;\n'
                   '    echo "--- ~/.config:"; ls "$HOME/.config" 2>&1;\n'
                   '    echo "--- marker files:"; ls -a "$HOME/.config/hypr" 2>&1 | grep -c revo_default_shell;\n'
                   '  } > "$HOME/state-at-retry-start.txt" 2>&1\n'
                   'else\n'
                   f'  echo "RECIPE-FAIL {PRESET}: injected fault (no REVO_REF)"; exit {FAIL_CODE}\n'
                   'fi\n'),
    }[mode]
    lines = script.read_text(encoding="utf-8").splitlines(keepends=True)
    insert_at = 1 if lines and lines[0].startswith("#!") else 0
    script.write_text("".join(lines[:insert_at]) + guard + "".join(lines[insert_at:]), encoding="utf-8")
    return root


def run_install(mode: str, case: str) -> int:
    lines: list[str] = []
    ctx = make_context(lines)
    extras.PRESET_DIR = patched_preset_dir(mode)  # `_stage` reads the scripts from here
    print(f"[fault] case={case}: preset copy at {extras.PRESET_DIR}, target {TARGET}", flush=True)
    try:
        dotfiles.install_dotfiles(ctx)
    except Exception as exc:  # the step must never raise
        print(f"FAULT-RESULT case={case} exception={type(exc).__name__}: {exc}", flush=True)
        return 2
    print(f"FAULT-RESULT case={case} exception=none", flush=True)
    return 0


def run_sources(case: str, *, extra_aur: str | None = None) -> int:
    lines: list[str] = []
    ctx = make_context(lines)
    if extra_aur:  # in memory only: the same preset with one more AUR package that does not exist
        preset = extras.DOTFILES[PRESET]
        extras.DOTFILES[PRESET] = dataclasses.replace(preset, aur_packages=(*preset.aur_packages, extra_aur))
    try:
        dotfiles.check_sources(ctx)
    except StepError as exc:
        print(f"FAULT-RESULT case={case} StepError={exc}", flush=True)
        return 0
    print(f"FAULT-RESULT case={case} StepError=none", flush=True)
    return 0


def main() -> int:
    case = sys.argv[1] if len(sys.argv) > 1 else ""
    if case == "retry":
        return run_install("no-ref", case)
    if case == "bothfail":
        return run_install("always", case)
    if case == "patched":
        return run_install("none", case)
    if case in ("sources", "sources-ok"):
        return run_sources(case)
    if case == "aur-missing":
        return run_sources(case, extra_aur=NO_SUCH_AUR_PACKAGE)
    print(__doc__)
    return 64


if __name__ == "__main__":
    sys.exit(main())
