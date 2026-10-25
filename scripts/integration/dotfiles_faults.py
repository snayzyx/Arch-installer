#!/usr/bin/env python3
"""Fault injection for the desktop shell presets, against the REAL installer code and a REAL installed target.

Run from checks-dotfiles-<id>.sh inside the test container, after a normal install has been mounted at
$CHECK (default /tmp/check). Nothing in installer/ is edited: the preset folder is COPIED to a temp
directory, one script of the copy is modified to fail, and `extras.PRESET_DIR` is pointed at the copy
before `dualboot_installer.steps.dotfiles.install_dotfiles` runs for that target (arch-chroot, the real
scripts, the real network).

    dotfiles_faults.py retry      20-<id>.sh fails (exit 21) unless the fallback env is set -> the latest
                                  version "fails", rollback runs, the retry with the tested commits works
    dotfiles_faults.py bothfail   20-<id>.sh always fails -> rollback, retry, rollback, default setup, no exception
    dotfiles_faults.py sources    check_sources with github.com unreachable -> StepError "Nothing was changed"
    dotfiles_faults.py sources-ok check_sources with the real network -> no error (positive control)

Environment: PRESET (default macshell), TARGET (default /tmp/check), ANSWERS (default /tmp/answers.json),
PRESET_PATCH_DIR (optional): files in it replace the same-named files of the copied preset (to test a patch).
The whole log of the run goes to stdout (and /tmp/fault-<case>.log); the last line is `FAULT-RESULT ...`.
"""

from __future__ import annotations

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

PRESET = os.environ.get("PRESET", "macshell")
TARGET = os.environ.get("TARGET", "/tmp/check")
FIRST_SCRIPT = extras.DOTFILES[PRESET].scripts[0]
FAIL_CODE = 21


def make_context(lines: list[str]) -> Context:
    cfg = InstallConfig()
    cfg.update_from(json.loads(Path(os.environ.get("ANSWERS", "/tmp/answers.json")).read_text()))
    cfg.dry_run = False

    def log(line: str) -> None:
        lines.append(line)
        print(line, flush=True)

    return Context(cfg, cfg.layout(), Runner(dry_run=False, log=log), target=TARGET)


def patched_preset_dir(mode: str) -> Path:
    """A copy of presets/ whose first script fails: always (mode 'always') or only without the fallback env.

    PRESET_PATCH_DIR (optional): files that replace the same-named files of the copy BEFORE the fault is added,
    to test a proposed patch of the preset scripts.
    """
    root = Path(tempfile.mkdtemp(prefix="presets-"))
    shutil.copytree(extras.PRESET_DIR, root, dirs_exist_ok=True)
    patch = os.environ.get("PRESET_PATCH_DIR")
    if patch:
        for path in sorted(Path(patch).iterdir()):
            shutil.copy(path, root / PRESET / path.name)
            print(f"[fault] patched file applied to the preset copy: {path.name}", flush=True)
    script = root / PRESET / FIRST_SCRIPT
    text = script.read_text(encoding="utf-8")
    guard = {
        "always": f'echo "RECIPE-FAIL {PRESET}: injected fault (always)"; exit {FAIL_CODE}\n',
        # the fallback env of the preset sets MACSHELL_REF (or the preset's own ref variable)
        "no-ref": (f'[ -n "${{MACSHELL_REF:-}}${{CAELESTIA_DOTS_REF:-}}" ] || '
                   f'{{ echo "RECIPE-FAIL {PRESET}: injected fault (no ref)"; exit {FAIL_CODE}; }}\n'),
    }[mode]
    lines = text.splitlines(keepends=True)
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
        Path(f"/tmp/fault-{case}.log").write_text("\n".join(lines) + "\n")
        return 2
    Path(f"/tmp/fault-{case}.log").write_text("\n".join(lines) + "\n")
    print(f"FAULT-RESULT case={case} exception=none", flush=True)
    return 0


def run_sources(case: str) -> int:
    lines: list[str] = []
    ctx = make_context(lines)
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
    if case in ("sources", "sources-ok"):
        return run_sources(case)
    print(__doc__)
    return 64


if __name__ == "__main__":
    sys.exit(main())
