#!/usr/bin/env python3
"""The Extras screen (developer tools, applications, desktop shell) and what the installation does with it.

Two real installations of the real wizard in a QEMU/KVM UEFI VM (the Windows part is skipped with --partial):

  E-A  Hyprland desktop, the Extras screen left at its recommended defaults (developer tools: languages + cli;
       applications: VSCodium, LibreWolf, Postman). The AUR is used: yay is built, then vscodium-bin and
       postman-bin are installed as the new user. The disk must then hold all of it and no temporary sudoers file.
  E-B  Minimal desktop, everything unticked: nothing extra may be installed, no yay.

Both check the live AUR note, Back/Next (an empty choice included), the Summary rows, Ctrl+Q during the
installation (refused) and after it (quits, exit code 0), and print how long each installation step took.

Run it with `scripts\\tui-e2e.ps1 -Mode extras -RepoInstaller` (the installer/ folder of the repository is copied into the
live system: the ISO may be older than the code).

    extras_e2e.py run [A|B]
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

import importlib.util

_spec = importlib.util.spec_from_file_location("tui_e2e", Path(__file__).with_name("tui-e2e.py"))
t = importlib.util.module_from_spec(_spec)
sys.modules["tui_e2e"] = t
_spec.loader.exec_module(t)  # type: ignore[union-attr]
check, note, log = t.check, t.note, t.log
WORK, SCREENS = t.WORK, t.SCREENS

DEV_PACKAGES = ["git", "nodejs", "npm", "pnpm", "python", "python-pip", "python-pipx", "python-virtualenv", "go", "rust",
                "cmake", "ninja", "gdb", "github-cli", "openssh", "jq", "ripgrep", "fd", "fzf", "tmux", "neovim", "httpie"]
SECRETS = ("alice-pw", "root-pw", "win-pw", "Abcd1234")
t.SECRETS = t.SECRETS + SECRETS
TIMINGS: dict[str, dict[str, dict[str, int]]] = {}


def walk(vm, w, name: str, hypr: bool) -> bool:
    check(w.wait_step(1, 60), f"{name}: the wizard starts")
    w.total = w.read_total()
    check(w.total == 14, f"{name}: the wizard has 14 screens", str(w.total))
    check(w.next(2), f"{name}: Welcome -> Region")
    check(w.fill("Country", "fran", clear=False), f"{name}: Region filter")
    vm.keys("enter")
    check(w.has(r"Selected: France") and w.has(r"Selected: Europe/Paris"), f"{name}: France")
    check(w.next(3), f"{name}: Region -> Network")
    check(w.next(4), f"{name}: Network -> Disk")
    check(w.wait(r"Selected: /dev/vda", 20), f"{name}: /dev/vda is selected")
    check(w.next(5), f"{name}: Disk -> Layout")
    check(w.next(6), f"{name}: Layout -> Accounts")
    w.fill("Computer name", "extras")
    w.fill("User name", "alice")
    w.fill("User password", "alice-pw")
    w.fill("Confirm the user password", "alice-pw")
    check(w.toggle_switch("same password for the root"), f"{name}: separate root password")
    w.fill("Root password", "root-pw")
    w.fill("Confirm the root password", "root-pw")
    check(w.next(7), f"{name}: Accounts -> Storage")
    check(w.choose("Filesystem", "btrfs"), f"{name}: btrfs")
    check(w.next(8), f"{name}: Storage -> Software")
    check(w.choose("Desktop", "Hyprland" if hypr else "Minimal"), f"{name}: desktop {'Hyprland' if hypr else 'Minimal'}")
    check(w.next(9), f"{name}: Software -> Extras")
    if hypr:
        t.extras_hypr_defaults(vm, w, f"{name}: ", offline_test=True)
    else:
        t.extras_minimal_empty(vm, w, f"{name}: ")
    w.snapshot("windows-image")
    # Partial mode: Windows is skipped, but the screens still ask their questions.
    if w.has(r"Path of the ISO file"):
        w.fill("Path of the ISO file", "/tmp/fake-windows.iso")
    check(w.next(w.n("winaccount")), f"{name}: Windows image -> Windows account")
    w.fill("Computer name", "WINEXTRAS")
    w.fill("User name", "alice")
    w.fill("Password", "win-pw")
    w.fill("Confirm the password", "win-pw")
    check(w.next(w.n("secureboot")), f"{name}: Windows account -> Secure Boot")
    w.fill("Enrollment password", "Abcd1234")
    w.fill("Confirm the enrollment password", "Abcd1234")
    check(w.next(w.n("summary")), f"{name}: Secure Boot -> Summary")
    w.snapshot("summary-top")
    summary = w.summary_text("Type /dev/vda to confirm")
    w.snapshot("summary-bottom")
    if hypr:
        check(re.search(r"Developer tools\s+Languages and build tools, Command line helpers", summary) is not None
              and re.search(r"Applications\s+VSCodium, LibreWolf, Postman", summary) is not None
              and re.search(r"Desktop shell\s+default Hyprland setup", summary) is not None,
              f"{name}: Summary rows Developer tools / Applications / Desktop shell show the defaults")
    else:
        check(re.search(r"Developer tools\s+none", summary) is not None and re.search(r"Applications\s+none", summary) is not None
              and re.search(r"Desktop shell\s+none \(no desktop\)", summary) is not None,
              f"{name}: Summary rows show none / none / none (no desktop)")
    check(not any(secret in summary for secret in SECRETS), f"{name}: Summary shows no secret")
    check(w.fill("Type /dev/vda to confirm", "/dev/vda"), f"{name}: the confirmation is typed")
    return True


def after_install(vm, name: str) -> None:
    """Ctrl+Q quits once the installation is over; the exit code of the installer must be 0."""
    before = len(vm.raw)
    vm.keys("ctrl+q")
    time.sleep(2)
    quit_ok = b"\x1b[?1049l" in bytes(vm.raw[before:])
    check(quit_ok, f"{name}: Ctrl+Q quits the wizard once the installation has finished")
    if not quit_ok:
        return
    rc, out = vm.shell("echo INSTALLER_EXIT_CODE=$?", 20)
    match = re.search(r"INSTALLER_EXIT_CODE=(\d+)", out)
    check(match is not None and match.group(1) == "0", f"{name}: the installer exits with code 0", out[-100:])
    vm.send("stty -echo" + chr(13))  # the console echoes what is typed: only the output of the next commands is wanted
    time.sleep(0.5)
    rc, out = vm.shell("grep -c -F -e alice-pw -e root-pw -e win-pw -e Abcd1234 /var/log/archinstall.log", 20)
    check(out.strip().splitlines()[-1].strip() == "0", f"{name}: /var/log/archinstall.log contains no password", out[-100:])
    rc, out = vm.shell("grep -n -i -E 'AUR|yay|vscodium|postman|librewolf|WARNING|docker|sudoers' /var/log/archinstall.log | cut -c1-200 | tail -40", 30)
    (SCREENS / f"{name.lower().replace(' ', '-')}-install-log-extract.txt").write_text(out + "\n", encoding="utf-8")
    vm.send("sync; systemctl poweroff\r")
    check(vm.wait_exit(120), f"{name}: the live system powers off")


def inspect(name: str, hypr: bool) -> None:
    log(f"{name}: inspecting the installed disk")
    sh = t.sh
    disk = WORK / "tui-target.img"
    loop = sh(f"losetup -f --show -P {disk}")
    for _ in range(30):
        if int(sh(f"ls /sys/class/block | grep -c '^{Path(loop).name}p'") or 0) >= 4:
            break
        time.sleep(0.5)
    sh(f'for d in /sys/class/block/{Path(loop).name}p*; do n=$(basename $d); rm -f /dev/$n; IFS=: read a b < $d/dev; mknod /dev/$n b $a $b; done')
    root = Path("/tmp/extrasmnt-" + name.lower().replace(" ", "-"))
    root.mkdir(parents=True, exist_ok=True)
    try:
        check(sh(f"mount -o ro,subvol=@ {loop}p3 {root} 2>&1; echo rc=$?").endswith("rc=0"), f"{name}: the btrfs root mounts")
        pkgs = sh(f"ls {root}/var/lib/pacman/local")
        have = lambda pkg: re.search(rf"^{re.escape(pkg)}-[0-9]", pkgs, re.MULTILINE) is not None  # noqa: E731
        check(not (root / "etc/sudoers.d/99-archinstall-aur").exists(), f"{name}: the temporary sudoers file is gone")
        if hypr:
            missing = [pkg for pkg in DEV_PACKAGES if not have(pkg)]
            check(not missing, f"{name}: the recommended developer tools are installed ({len(DEV_PACKAGES)} packages)", ", ".join(missing))
            check(have("librewolf"), f"{name}: LibreWolf (official repositories) is installed")
            check(have("yay") or have("yay-bin"), f"{name}: the AUR helper yay is installed (built on its own, aur_helper none)")
            check((root / "usr/bin/yay").exists(), f"{name}: /usr/bin/yay exists")
            check(have("vscodium-bin"), f"{name}: vscodium-bin (AUR) is installed")
            check(have("postman-bin"), f"{name}: postman-bin (AUR) is installed")
            check(not have("docker"), f"{name}: docker is not installed (not chosen)")
            check(have("hyprland") and have("sddm"), f"{name}: the Hyprland desktop is installed")
        else:
            present = [pkg for pkg in ("nodejs", "rust", "go", "neovim", "tmux", "ripgrep", "docker", "yay", "yay-bin",
                                       "vscodium-bin", "postman-bin", "librewolf", "hyprland") if have(pkg)]
            check(not present, f"{name}: nothing extra is installed (no dev tools, no yay, no applications)", ", ".join(present))
        (SCREENS / f"{name.lower().replace(' ', '-')}-packages.txt").write_text(pkgs + "\n", encoding="utf-8")
    finally:
        sh(f"umount {root} 2>/dev/null; losetup -d {loop} 2>/dev/null")


def run_walk(name: str, hypr: bool) -> None:
    log(f"{name}: boot the live ISO with the installer of the repository")
    vm = t.boot_live(name=f"extras-{name[-1].lower()}")
    vm.shell(": > /tmp/fake-windows.iso", 10)
    w = t.Wizard(vm)
    w.prefix = f"{name[-1].lower()}-"
    t.start_wizard(vm, partial=True)
    started = time.time()
    try:
        reached = False
        try:
            reached = walk(vm, w, name, hypr)
        except Exception as error:  # noqa: BLE001
            check(False, f"{name}: the wizard walk crashed: {type(error).__name__}: {error}")
            import traceback

            traceback.print_exc()
            w.snapshot("stopped-here")
            print(vm.screen_text())
        if reached:
            install_start = time.time()
            ok = t.scenario_install(vm, w, partial=True, ctrl_q=True)
            print(f"  {name}: installation took {int(time.time() - install_start)} s in total")
            TIMINGS[name] = dict(t.TIMINGS)
            if ok:
                after_install(vm, name)
    finally:
        vm.stop()
        (WORK / f"extras-console-{name[-1].lower()}.log").write_bytes(bytes(vm.raw))
    if reached:
        inspect(name, hypr)
    print(f"  {name}: whole walk took {int(time.time() - started)} s")


def run(which: str = "AB") -> int:
    SCREENS.mkdir(parents=True, exist_ok=True)
    for stale in SCREENS.glob("*.txt"):
        stale.unlink()
    if "A" in which:
        run_walk("E-A", hypr=True)
    if "B" in which:
        t.TIMINGS.clear()
        run_walk("E-B", hypr=False)
    lines = []
    for name, steps in TIMINGS.items():
        lines.append(f"{name}: time of each installation step (seconds from the start of the Install screen)")
        for title, marks in steps.items():
            begin = marks.get(">")
            end = max((v for k, v in marks.items() if k in "x!-"), default=None)
            lines.append(f"  {title:<46} start {begin if begin is not None else '-':>5}  end {end if end is not None else '-':>5}")
    (SCREENS / "timings.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    failed = [what for ok, what in t.RESULTS if not ok]
    print(f"\n##### {len(t.RESULTS) - len(failed)} checks passed, {len(failed)} failed, {len(t.OBSERVATIONS)} observations")
    for what in failed:
        print(f"  FAILED: {what}")
    for text in t.OBSERVATIONS:
        print(f"  NOTE: {text}")
    return 1 if failed else 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "run":
        sys.exit(run(sys.argv[2].upper() if len(sys.argv) > 2 else "AB"))
    raise SystemExit(f"unknown mode {mode}")
