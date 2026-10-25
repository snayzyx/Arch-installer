#!/usr/bin/env python3
"""Does the SDDM greeter of an installed system use the keyboard layout chosen in the wizard?

Boots the installed disk left by `tui-e2e.ps1 -Mode laptop -LaptopDesktop Hyprland` (France answers: keymap fr), starts
SDDM, reads what the X server thinks of the keyboard, and then really types a password through the QEMU monitor
(`sendkey` sends PHYSICAL keys, named after a US keyboard): the password `azerty` on an AZERTY keyboard is the physical
keys q w e r t y, which a US layout turns into `qwerty`. If the greeter logs the user in, it uses the fr layout.

    sddm_keyboard.py daemon      boot, log in as root on the console, wait for commands (tui-e2e.py ctl ...)
    sddm_keyboard.py run         the whole diagnosis (and the fix test when FIX=1)

Run it with:  TUI_ENTRY=sddm_keyboard.py bash tui-e2e.sh run   inside the builder container (volume holding
laptop-target.img and laptop-vars.fd).
"""

from __future__ import annotations

import os
import socket
import sys
import time

import laptop_e2e as L

t = L.t
check, note, log = t.check, t.note, t.log
SCREENS = t.SCREENS
FIX = os.environ.get("FIX", "0") == "1"

MONITOR_KEYS = {"ret": "ret", "enter": "ret"}


def monitor(vm, *commands: str) -> None:
    with socket.socket(socket.AF_UNIX) as sock:
        sock.connect(str(vm.monitor_path))
        sock.settimeout(3)
        try:
            sock.recv(4096)
        except OSError:
            pass
        for command in commands:
            sock.sendall(command.encode() + b"\n")
            time.sleep(0.35)
            try:
                sock.recv(4096)
            except OSError:
                pass


def sendkeys(vm, keys: str) -> None:
    """Physical keys, named after the US layout: 'q w e r t y ret'."""
    monitor(vm, *[f"sendkey {key}" for key in keys.split()])


def boot_installed():
    vm = L.LaptopVm("sddm-kb", disk=t.WORK / "laptop-target.img", vars_file=t.WORK / "laptop-vars.fd", live=False)
    vm.start()
    if not vm.login_as(r"login:", "root", "root-pw", 420):
        print(vm.text()[-2500:])
        raise SystemExit("the installed system did not give a root shell")
    return vm


DIAG = r"""
set +e
echo "== packages"; pacman -Q xorg-server sddm xf86-input-libinput xf86-input-evdev xorg-setxkbmap 2>&1
pacman -Sy --noconfirm --needed xorg-setxkbmap xorg-xinput > /tmp/p.log 2>&1 || { tail -3 /tmp/p.log; }
systemctl start sddm; sleep 20
echo "== sddm"; systemctl is-active sddm
echo "== xauth"; ls -l /run/sddm/ 2>&1
XA=$(ls /run/sddm/xauth_* 2>/dev/null | head -1)
echo "== setxkbmap -query (greeter X server)"; DISPLAY=:0 XAUTHORITY=$XA setxkbmap -query 2>&1
echo "== xinput list"; DISPLAY=:0 XAUTHORITY=$XA xinput list 2>&1
echo "== 00-keyboard.conf"; cat /etc/X11/xorg.conf.d/00-keyboard.conf
echo "== localectl"; localectl status 2>&1
echo "== vconsole.conf"; cat /etc/vconsole.conf
echo "== sddm config"; ls -l /etc/sddm.conf /etc/sddm.conf.d 2>&1; cat /usr/lib/sddm/sddm.conf.d/*.conf 2>/dev/null | head -30
echo "== journal (sddm, keyboard)"; journalctl -u sddm -b --no-pager | grep -i -E 'xkb|keyboard|layout|input|Xorg|X11' | head -20
echo "== Xorg log"; for f in /var/lib/sddm/.local/share/xorg/Xorg.0.log /var/log/Xorg.0.log; do [ -f $f ] && { echo "-- $f"; grep -i -E 'xkb|keyboard|Layout|libinput|evdev|kbd|input device|InputClass|00-keyboard' $f | head -40; }; done
"""


def diagnose(vm) -> str:
    rc, out = vm.script(DIAG, 240)
    (SCREENS / "diagnosis.txt").write_text(out + "\n", encoding="utf-8")
    print(out)
    return out


def type_test(vm, label: str) -> bool:
    """alice's password becomes 'azerty'; type it as the physical keys of an AZERTY keyboard into the greeter."""
    vm.script("echo 'alice:azerty' | chpasswd; systemctl restart sddm; sleep 20; echo restarted", 120)
    time.sleep(2)
    L.screendump(vm, f"greeter-before-{label}")
    sendkeys(vm, "q w e r t y")  # azerty on AZERTY, qwerty on QWERTY
    time.sleep(1)
    L.screendump(vm, f"greeter-typed-{label}")
    sendkeys(vm, "ret")
    time.sleep(12)
    L.screendump(vm, f"greeter-after-{label}")
    rc, out = vm.script("journalctl -u sddm -b --no-pager --since '-40s' | grep -i -E 'auth|login|session|PAM' | tail -12; "
                        "echo '== sessions'; loginctl list-sessions --no-legend", 30)
    (SCREENS / f"login-attempt-{label}.txt").write_text(out + "\n", encoding="utf-8")
    print(out)
    return "Authenticated successfully" in out or "Session started" in out or re.search(r"\balice\b", out.split("== sessions")[-1]) is not None


import re  # noqa: E402


def run() -> int:
    vm = boot_installed()
    try:
        diagnose(vm)
        ok = type_test(vm, "before-fix")
        check(ok, "SDDM greeter: the password typed with the physical keys of an AZERTY keyboard is accepted (fr layout)")
        if FIX or not ok:
            apply_fix(vm)
            ok2 = type_test(vm, "after-fix")
            check(ok2, "SDDM greeter with the fix: the AZERTY-typed password is accepted")
    finally:
        vm.stop()
    failed = [what for ok, what in t.RESULTS if not ok]
    print(f"##### {len(t.RESULTS) - len(failed)} checks passed, {len(failed)} failed")
    for what in failed:
        print(f"  FAILED: {what}")
    return 1 if failed else 0


FIX_SCRIPT = r"""
set +e
install -d -m 755 /etc/sddm.conf.d
cat > /etc/sddm.conf.d/archinstall-keyboard.conf <<'EOF'
[X11]
DisplayCommand=/etc/sddm/Xsetup
EOF
cat > /etc/sddm/Xsetup <<'EOF'
#!/bin/sh
# Written by ArchInstall: the keyboard layout of the SDDM greeter (X11).
setxkbmap -layout fr
EOF
chmod 755 /etc/sddm/Xsetup
echo fixed
"""


def apply_fix(vm) -> None:
    rc, out = vm.script("install -d /etc/sddm; " + FIX_SCRIPT, 60)
    print(out)


def daemon() -> int:
    vm = boot_installed()
    t.__dict__.update({"sendkeys": sendkeys, "monitor": monitor, "diagnose": diagnose, "type_test": type_test,
                       "apply_fix": apply_fix, "L": L})
    print("live system ready; control socket", t.CTL, flush=True)
    t.serve(vm)
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "run":
        sys.exit(run())
    if mode == "daemon":
        sys.exit(daemon())
    if mode == "ctl":
        sys.exit(t.main(["x", "ctl", *sys.argv[2:]]))
    raise SystemExit(f"unknown mode {mode}")
