#!/usr/bin/env python3
"""Fault injection for the Caelestia preset, through the REAL engine (installer steps/dotfiles.py).

Runs inside the test container, after test-install.sh has installed the system and mounted it:

    PYTHONPATH=/src/installer python dotfiles_caelestia_faults.py <mounted installed system> <user>

Nothing under installer/ is edited. The engine is called as the installer calls it, with a Context whose
target is the mounted system; only the files it reads are swapped, in the test process:

* extras.PRESET_DIR points to a temporary COPY of the preset whose 20-caelestia.sh has an injected failure;
* extras.DOTFILES['caelestia'] is replaced (dataclasses.replace) to give the fallback an OLDER commit of the
  dots than HEAD, so that the test can tell the fallback commit from the latest one.

Scenarios (each prints "ok" / "FAIL" lines; exit code 0 only when every expectation held):
  1  the latest version "fails after the deploy", the fallback commit works
  2  both attempts fail: rolled back twice, the default Hyprland setup is back, the step still returns normally
  2b a cleaner rollback (patch proposed to restore-default.sh) leaves ~/.config exactly as the backup
  4b the proposed keyboard patch (scripts/integration/caelestia-keyboard-patch.sh) through the engine
  3  check_sources (before the disk is erased): unreachable quickshell host, then a missing AUR package
  4  the keyboard layout: the dots hard-code "us"; a hypr-user.lua override validates (and a broken one is reported)
"""
import contextlib
import dataclasses
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

from dualboot_installer import extras
from dualboot_installer.config import InstallConfig
from dualboot_installer.layout import GIB
from dualboot_installer.runner import Runner
from dualboot_installer.steps import dotfiles
from dualboot_installer.steps.context import Context, StepError

TARGET = pathlib.Path(sys.argv[1])
USER = sys.argv[2]
HOME = TARGET / "home" / USER
EVIDENCE = os.environ.get("EVIDENCE_DIR", "")
FAILED = 0
FULL_LOG = open("/tmp/caelestia-faults-full.log", "w", encoding="utf-8")
INTERESTING = re.compile(r"RECIPE-|WARNING|desktop shell|Running |rollback|Nothing was changed|StepError|Fatal|Hyprland")


def expect(what: str, condition: bool) -> None:
    global FAILED
    print(f"  {'ok  ' if condition else 'FAIL'}  {what}")
    if not condition:
        FAILED += 1


def title(text: str) -> None:
    print(f"\n##### {text}")


def sh(command: str, timeout: int = 600) -> str:
    """A command in the installed system as the user (login shell, like the engine)."""
    done = subprocess.run(["arch-chroot", str(TARGET), "runuser", "-l", USER, "-c", command],
                          capture_output=True, text=True, timeout=timeout)
    return (done.stdout + done.stderr).strip()


def read_state() -> dict:
    import json
    try:
        return json.loads((HOME / ".local/state/caelestia/dots-state.json").read_text())
    except (OSError, ValueError):
        return {}


def make_ctx() -> tuple[Context, list[str]]:
    cfg = InstallConfig()
    cfg.disk.device, cfg.disk.size_bytes, cfg.disk.windows_gib = "/dev/loop0", 110 * GIB, 50
    cfg.arch.username, cfg.arch.desktop, cfg.arch.dotfiles, cfg.arch.aur_helper = USER, "hyprland", "caelestia", "yay"
    lines: list[str] = []

    def log(line: str) -> None:
        lines.append(line)
        FULL_LOG.write(line + "\n")
        FULL_LOG.flush()
        if INTERESTING.search(line):
            print("      | " + line[:200])

    return Context(cfg, cfg.layout(), Runner(dry_run=False, log=log), target=str(TARGET)), lines


@contextlib.contextmanager
def patched_preset(transform, fallback_sha: str | None = None, **changes):
    """A temporary copy of the preset (with `transform` applied to 20-caelestia.sh) and a replaced registry entry."""
    original_dir, original_entry = extras.PRESET_DIR, extras.DOTFILES["caelestia"]
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copytree(original_dir / "caelestia", pathlib.Path(tmp) / "caelestia")
        script = pathlib.Path(tmp) / "caelestia" / "20-caelestia.sh"
        if transform is not None:
            script.write_text(transform(script.read_text(encoding="utf-8")), encoding="utf-8")
        entry = dataclasses.replace(original_entry, **changes)
        if fallback_sha:
            entry = dataclasses.replace(entry, fallback_env=(("CAELESTIA_DOTS_REF", fallback_sha),))
        extras.PRESET_DIR, extras.DOTFILES["caelestia"] = pathlib.Path(tmp), entry
        try:
            yield pathlib.Path(tmp) / "caelestia"
        finally:
            extras.PRESET_DIR, extras.DOTFILES["caelestia"] = original_dir, original_entry


FAIL_LINE = """[ "$rc" -eq 0 ] || { echo "RECIPE-FAIL caelestia: 'caelestia install' exited with $rc"; exit 23; }"""


def fail_when_latest(text: str) -> str:
    """Scenario 1: the deploy happens, then the script fails unless the known-good ref is set."""
    assert FAIL_LINE in text, "20-caelestia.sh changed: update the injection point"
    return text.replace(FAIL_LINE, 'if [ -z "${CAELESTIA_DOTS_REF:-}" ]; then echo "RECIPE-FAIL caelestia: INJECTED failure after the deploy (latest)"; exit 23; fi\n' + FAIL_LINE)


def always_fail(text: str) -> str:
    assert FAIL_LINE in text, "20-caelestia.sh changed: update the injection point"
    return text.replace(FAIL_LINE, 'echo "RECIPE-FAIL caelestia: INJECTED failure after the deploy (always)"; exit 23')


def tree_diff(a: pathlib.Path, b: pathlib.Path) -> list[str]:
    """Differences between two trees (the container has no `diff`): BAK = a, NOW = b."""
    def entries(root: pathlib.Path) -> dict[str, str]:
        found = {}
        for path in sorted(root.rglob("*")):
            rel = str(path.relative_to(root))
            if path.is_symlink():
                found[rel] = "link:" + os.readlink(path)
            elif path.is_file():
                found[rel] = "file:" + str(path.stat().st_size) + ":" + subprocess.run(["sha256sum", str(path)], capture_output=True, text=True).stdout.split()[0]
            else:
                found[rel] = "dir"
        return found
    left, right = entries(a), entries(b)
    out = [f"Only in NOW: {rel}" for rel in sorted(set(right) - set(left))]
    out += [f"Only in BAK: {rel}" for rel in sorted(set(left) - set(right))]
    out += [f"Differ: {rel}" for rel in sorted(set(left) & set(right)) if left[rel] != right[rel]]
    return out


def run_engine(ctx: Context) -> tuple[float, str]:
    started = time.time()
    outcome = "returned normally"
    try:
        dotfiles.install_dotfiles(ctx)
    except Exception as exc:  # the engine must never raise
        outcome = f"RAISED {type(exc).__name__}: {exc}"
    return time.time() - started, outcome


def main() -> int:
    title("Start state (the installer's own run of the preset)")
    expect("the preset is installed (dots-state.json exists)", bool(read_state()))
    head_before = sh("git -C ~/.local/state/caelestia/dots rev-parse HEAD")
    old_sha = sh("git -C ~/.local/state/caelestia/dots log --format=%H -n 8 | tail -n 1")
    print(f"      HEAD of the dots clone: {head_before}\n      OLDER commit used as the 'last tested' one: {old_sha}")
    expect("both are 40-hex commits and differ", bool(re.fullmatch(r"[0-9a-f]{40}", head_before)) and bool(re.fullmatch(r"[0-9a-f]{40}", old_sha)) and head_before != old_sha)

    # ---------------------------------------------------------------------------------------------- 1
    title("1. The latest version fails after the deploy; the fallback commit works")
    ctx, lines = make_ctx()
    with patched_preset(fail_when_latest, fallback_sha=old_sha):
        seconds, outcome = run_engine(ctx)
    text = "\n".join(lines)
    print(f"      engine: {outcome} in {seconds:.0f} s")
    expect("the engine returned normally", outcome == "returned normally")
    expect("the injected failure was seen (exit code 23)", "INJECTED failure after the deploy (latest)" in text and "exit code 23" in text)
    expect("restore-default.sh ran after the failure", "RECIPE-RESULT rollback=done" in text)
    expect("the engine said it retries with the versions that were tested", "trying the versions that were tested" in text)
    expect("the final message says 'the last tested versions, not the latest'", "the last tested versions, not the latest" in text)
    order = [text.find(s) for s in ("INJECTED failure", "rollback=done", "trying the versions that were tested", "last tested versions, not the latest")]
    expect("in that order: failure, rollback, retry, success", all(i >= 0 for i in order) and order == sorted(order))
    state = read_state()
    expect(f"applied_rev is the fallback commit ({old_sha[:10]})", state.get("applied_rev") == old_sha)
    expect("the clone of the dots is at the fallback commit", sh("git -C ~/.local/state/caelestia/dots rev-parse HEAD") == old_sha)
    expect("no cli.json left (it would pin `caelestia update` to the mirror)", not (HOME / ".config/caelestia/cli.json").exists())
    expect("hyprland.lua is deployed", (HOME / ".config/hypr/hyprland.lua").is_file())
    expect("the backup still holds the installer's own hyprland.conf", "exec-once = waybar" in (HOME / ".config.bak/hypr/hyprland.conf").read_text())
    expect("the temporary sudoers file is gone", not any("NOPASSWD" in p.read_text() for p in (TARGET / "etc/sudoers.d").glob("*") if p.is_file()))
    expect("the RECIPE-RESULT line of the retry names the fallback commit", f"dots={old_sha}" in text)
    verify = sh("export XDG_RUNTIME_DIR=$(mktemp -d); chmod 700 $XDG_RUNTIME_DIR; Hyprland --verify-config 2>&1 | tail -n 3")
    expect("Hyprland --verify-config still says config ok on the older dots", "config ok" in verify)

    # ---------------------------------------------------------------------------------------------- 4
    title("4. The keyboard layout of the person (the dots hard-code kb_layout = \"us\")")
    input_lua = (HOME / ".config/hypr/hyprland/input.lua").read_text()
    expect("the dots' own input.lua sets kb_layout = \"us\" (so a French keymap is NOT carried over by default)", 'kb_layout          = "us"' in input_lua)
    expect("... and hyprland.lua loads hypr-user.lua AFTER hyprland.input",
           (HOME / ".config/hypr/hyprland.lua").read_text().index('require("hyprland.input")') < (HOME / ".config/hypr/hyprland.lua").read_text().index('require("hypr-user")'))
    user_lua = HOME / ".config/caelestia/hypr-user.lua"
    previous = user_lua.read_text() if user_lua.exists() else None
    verify_cmd = "export XDG_RUNTIME_DIR=$(mktemp -d); chmod 700 $XDG_RUNTIME_DIR; Hyprland --verify-config 2>&1 | tail -n 4"
    user_lua.parent.mkdir(parents=True, exist_ok=True)
    user_lua.write_text('hl.config({ input = { kb_layout = "fr" } })\n')
    expect("a hypr-user.lua with the person's layout validates (config ok)", "config ok" in sh(verify_cmd))
    user_lua.write_text('hl.config({ input = { kb_layout = "fr" } \n')  # negative control: a syntax error
    bad = sh(verify_cmd)
    expect("negative control: a broken hypr-user.lua is reported (so verify-config does read it)", "config ok" not in bad)
    print("      broken file answer: " + bad.replace("\n", " | ")[:200])
    if previous is None:
        user_lua.unlink()
    else:
        user_lua.write_text(previous)

    # ---------------------------------------------------------------------------------------------- 4b
    title("4b. The proposed patch: 20-caelestia.sh writes the layout into hypr-user.lua (KB_LAYOUT from the engine)")
    block = pathlib.Path(__file__).with_name("caelestia-keyboard-patch.sh").read_text(encoding="utf-8")
    block = "".join(line for line in block.splitlines(keepends=True) if not line.startswith("#"))

    def with_keyboard_patch(text: str) -> str:
        anchor = "for f in .config/hypr/hyprland.lua"
        assert anchor in text, "20-caelestia.sh changed: update the injection point"
        # The engine does not pass KB_LAYOUT yet (that is the other half of the patch): simulate it at the top.
        text = text.replace("set -uo pipefail\n", 'set -uo pipefail\nexport KB_LAYOUT="${KB_LAYOUT:-fr}"\n', 1)
        return text.replace(anchor, block + anchor, 1)

    ctx, lines = make_ctx()
    with patched_preset(with_keyboard_patch, fallback_sha=old_sha):
        seconds, outcome = run_engine(ctx)
    print(f"      engine: {outcome} in {seconds:.0f} s")
    expect("the engine returned normally and installed the latest version",
           outcome == "returned normally" and "installed (latest version)" in "\n".join(lines))
    written = user_lua.read_text() if user_lua.exists() else ""
    print("      hypr-user.lua now contains: " + written.replace("\n", " | "))
    expect('hypr-user.lua has the layout ("fr")', 'kb_layout = "fr"' in written)
    expect("... once", written.count("kb_layout") == 1)
    expect("Hyprland --verify-config says config ok with it", "config ok" in sh(verify_cmd))
    ctx, lines = make_ctx()
    with patched_preset(with_keyboard_patch, fallback_sha=old_sha):
        run_engine(ctx)
    expect("a second run of the preset does not duplicate the line", user_lua.read_text().count("kb_layout") == 1)

    # ---------------------------------------------------------------------------------------------- 2
    title("2. Both attempts fail: rolled back twice, default setup back, the step still returns")
    ctx, lines = make_ctx()
    with patched_preset(always_fail, fallback_sha=old_sha):
        seconds, outcome = run_engine(ctx)
    text = "\n".join(lines)
    print(f"      engine: {outcome} in {seconds:.0f} s")
    expect("the engine returned normally (the installation goes on)", outcome == "returned normally")
    expect("restore-default.sh ran twice", text.count("RECIPE-RESULT rollback=done") == 2)
    expect("the engine tried the fallback before giving up", "trying the versions that were tested" in text)
    expect("the final message says the default Hyprland setup is back", "The default Hyprland setup is back" in text)
    expect("hyprland.lua is gone", not (HOME / ".config/hypr/hyprland.lua").exists())
    expect("variables.lua and the dots' hypr folders are gone", not (HOME / ".config/hypr/variables.lua").exists() and not (HOME / ".config/hypr/hyprland").exists())
    conf = HOME / ".config/hypr/hyprland.conf"
    expect("the installer's hyprland.conf is back, with waybar and the network applet", conf.is_file() and "exec-once = waybar" in conf.read_text() and "nm-applet" in conf.read_text())
    expect("the dots' state and the CLI configuration are gone", not (HOME / ".local/state/caelestia").exists() and not (HOME / ".config/caelestia").exists())
    expect("the backup is still there for the next attempt", (HOME / ".config.bak/hypr/hyprland.conf").is_file())
    expect("the temporary sudoers file is gone", not any("NOPASSWD" in p.read_text() for p in (TARGET / "etc/sudoers.d").glob("*") if p.is_file()))
    leftovers = tree_diff(HOME / ".config.bak", HOME / ".config")
    print(f"      what the ORIGINAL restore-default.sh leaves in ~/.config compared with the backup ({len(leftovers)} differences):")
    for line in leftovers[:40]:
        print("        " + line)
    extra_in_home = sorted(p.name for p in HOME.iterdir() if not p.name.startswith(".") and p.is_dir())
    print(f"      visible folders created in the home directory: {extra_in_home}")
    shell_files = [p for p in ("fish", "foot", "gtk-3.0", "gtk-4.0", "btop", "starship.toml", "fastfetch", "qtengine", "thunar", "Thunar") if (HOME / ".config" / p).exists()]
    print(f"      deployed by the dots and still there after the rollback: {shell_files}")
    expect("INFO the rollback leaves files deployed by the dots (fish, foot, gtk...)", True)

    title("2b. A cleaner rollback: restore the whole backup (proposed patch to restore-default.sh)")
    # Put the dots back first (a plain `caelestia install`, no injection), then roll back with the patched script.
    (TARGET / "etc/sudoers.d/99-test-faults").write_text(f"{USER} ALL=(ALL) NOPASSWD: ALL\n")
    os.chmod(TARGET / "etc/sudoers.d/99-test-faults", 0o440)
    code = subprocess.run(["arch-chroot", str(TARGET), "runuser", "-l", USER, "-c",
                           "export XDG_RUNTIME_DIR=$(mktemp -d); chmod 700 $XDG_RUNTIME_DIR; "
                           "caelestia install --noconfirm --aur-helper yay --disable-components firefox,qt"],
                          capture_output=True, text=True, timeout=1200).returncode
    (TARGET / "etc/sudoers.d/99-test-faults").unlink()
    expect("`caelestia install` works again on the rolled-back system (exit 0)", code == 0)
    expect("... and deployed hyprland.lua once more", (HOME / ".config/hypr/hyprland.lua").is_file())
    patched = '''#!/usr/bin/env bash
# ROLLBACK when the preset failed: put the installer's own setup back (normal user).
# `caelestia install` copies ~/.config to ~/.config.bak before it deploys anything, and it deploys much more than
# Hyprland (fish, foot, GTK, btop, starship... files): restore the WHOLE configuration, not only ~/.config/hypr.
set -u
if [ -d "$HOME/.config.bak" ]; then
  rm -rf "$HOME/.config"
  cp -a "$HOME/.config.bak" "$HOME/.config"   # the backup stays, for the retry
fi
rm -rf "$HOME/.local/state/caelestia" "$HOME/.config/caelestia"
echo "RECIPE-RESULT rollback=done"
'''
    script = HOME / "restore-default-patched.sh"
    script.write_text(patched)
    os.chmod(script, 0o755)
    owner = HOME.stat()  # the user is unknown to the test container: use the ids of the home directory
    os.chown(script, owner.st_uid, owner.st_gid)
    out = sh("bash ~/restore-default-patched.sh")
    script.unlink()
    expect("the patched script prints rollback=done", "rollback=done" in out)
    remaining = tree_diff(HOME / ".config.bak", HOME / ".config")
    print(f"      differences with the backup after the patched rollback: {len(remaining)}")
    for line in remaining[:10]:
        print("        " + line)
    expect("~/.config is identical to the backup (no difference at all)", not remaining)
    expect("hyprland.conf is back and hyprland.lua is gone", (HOME / ".config/hypr/hyprland.conf").is_file() and not (HOME / ".config/hypr/hyprland.lua").exists())

    # ---------------------------------------------------------------------------------------------- 3
    title("3. check_sources (before the disk is erased)")
    ctx, lines = make_ctx()
    try:
        dotfiles.check_sources(ctx)
        passed = True
    except StepError as exc:
        passed = False
        print(f"      unexpected: {exc}")
    expect("with everything reachable it passes silently", passed)
    sources = extras.preset_sources(extras.DOTFILES["caelestia"])
    print(f"      sources checked: {sources}")
    hosts = pathlib.Path("/etc/hosts")
    saved = hosts.read_text()
    try:
        with hosts.open("a") as handle:
            handle.write("\n0.0.0.0 git.outfoxxed.me\n")
        ctx, lines = make_ctx()
        message = ""
        try:
            dotfiles.check_sources(ctx)
        except StepError as exc:
            message = str(exc)
        print(f"      StepError: {message[:400]}")
        expect("an unreachable git.outfoxxed.me raises StepError", bool(message))
        expect("the message names the repository that does not answer", "git.outfoxxed.me" in message)
        expect("the message says 'Nothing was changed'", "Nothing was changed" in message)
    finally:
        hosts.write_text(saved)
    with patched_preset(None, aur_check=(*extras.DOTFILES["caelestia"].aur_check, "zzz-no-such-package-archinstall")):
        ctx, lines = make_ctx()
        message = ""
        try:
            dotfiles.check_sources(ctx)
        except StepError as exc:
            message = str(exc)
        print(f"      StepError: {message[:400]}")
        expect("a missing AUR package raises StepError", bool(message))
        expect("the message names the missing package", "zzz-no-such-package-archinstall" in message)
        expect("the message says 'Nothing was changed'", "Nothing was changed" in message)

    title("RESULT")
    print(f"  {'ALL EXPECTATIONS HELD' if not FAILED else str(FAILED) + ' EXPECTATION(S) FAILED'}")
    if EVIDENCE:
        with contextlib.suppress(OSError):
            shutil.copy("/tmp/caelestia-faults-full.log", pathlib.Path(EVIDENCE) / "faults-full-engine-log.txt")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
