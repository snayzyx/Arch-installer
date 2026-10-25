#!/usr/bin/env python3
"""Drives the REAL interactive installer (the Textual wizard) inside a QEMU/KVM UEFI VM.

Why: real installs were only run headless (--answers) and the wizard only in demo mode with
a simulated machine. Nothing exercised the link between the TUI and the install steps
(worker thread, log, button states, password handling, summary confirmation).

How: the ISO's live system is booted with a direct kernel boot (serial console on ttyS0), the
wizard is started in the root shell, and the keyboard is the serial line. A terminal emulator
(pyte) rebuilds the screen from the bytes the wizard prints, so every action is decided from
what is on screen, never from blind sleeps. The live ISO is attached as a virtio DISK, like
a USB stick written with dd, so the wizard's "this is the disk you booted from" protection can
be exercised.

Runs inside the privileged builder container (see tui-e2e.sh and scripts/tui-e2e.ps1).

    tui-e2e.py run        the whole automated scenario (exit code 0 = every check passed)
    tui-e2e.py daemon     boot the VM and serve a control socket, for exploring by hand
    tui-e2e.py ctl ...    talk to the daemon: screen | keys <names> | type <text> | py <code> | quit
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pyte

OVMF = Path("/usr/share/edk2/x64")
WORK = Path(os.environ.get("TUI_WORK", "/work"))
SCREENS = Path(os.environ.get("SCREENS_DIR", "/screens"))
ISO = WORK / "offline.iso"
CTL = WORK / "ctl.sock"
ROWS, COLS = 45, 130
# The wizard repaints ~28 KB on each focus change. QEMU's emulated 16550 serial port paces its output like a
# real 115200 baud line (about 2.4 s per repaint), so keys sent meanwhile were processed late and every
# screen read was stale. A virtio console has no such pacing; ttyS0 stays available (TUI_CONSOLE=ttyS0).
CONSOLE = os.environ.get("TUI_CONSOLE", "hvc0")
ACCENT = "0178d4"  # the focus colour of Textual's default theme
GREEN = "8ad4a1"  # the "selected" dot of a radio button

_ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]|\x1b[=>]|\x1b\]\d+;[^\x07\x1b]*(?:\x07|\x1b\\)")

KEYS = {
    "enter": "\r", "tab": "\t", "shift+tab": "\x1b[Z", "esc": "\x1b", "space": " ",
    "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D",
    "home": "\x1b[H", "end": "\x1b[F", "pgup": "\x1b[5~", "pgdn": "\x1b[6~",
    "backspace": "\x7f", "delete": "\x1b[3~", "ctrl+c": "\x03", "ctrl+q": "\x11", "ctrl+u": "\x15",
}

SECRETS = ("alice-pw", "root-pw", "win-pw", "Abcd1234", "different-pw", "win-other", "bob-pw-123", "win2-pw-456", "Efgh5678")

RESULTS: list[tuple[bool, str]] = []
OBSERVATIONS: list[str] = []  # rough edges that are not failures of the check list


def check(ok: bool, what: str, detail: str = "") -> bool:
    RESULTS.append((ok, what))
    print(f"  {'ok  ' if ok else 'FAIL'}  {what}" + (f"   [{detail}]" if detail and not ok else ""), flush=True)
    return ok


def note(text: str) -> None:
    OBSERVATIONS.append(text)
    print(f"  note  {text}", flush=True)


def log(message: str) -> None:
    print(f"\n##### {message}", flush=True)


def shipped_cmdline() -> str:
    """The kernel command line of the ISO's own UEFI boot entry (extracted by tui-e2e.sh), so that the
    boot parameters under test are the ones a person gets from the boot menu."""
    for line in (WORK / "boot-entry.conf").read_text().splitlines():
        if line.startswith("options"):
            return line.split(None, 1)[1].strip()
    raise SystemExit("no options line in boot-entry.conf")


# -- the VM ------------------------------------------------------------------------------------------


class Vm:
    """QEMU + KVM + OVMF. The serial console is stdin/stdout of the process."""

    def __init__(self, name: str, *, disk: Path, vars_file: Path, live: bool, extra_cmdline: str = "",
                 memory: int = int(os.environ.get("TUI_MEMORY", "6144"))) -> None:
        self.name, self.disk, self.vars_file, self.live, self.memory = name, disk, vars_file, live, memory
        self.extra_cmdline = extra_cmdline
        self.monitor_path = WORK / f"{name}.mon"
        self.log_path = WORK / f"{name}.serial.log"
        self.proc: subprocess.Popen | None = None
        self.raw = bytearray()
        self.cursor = 0
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.ByteStream(self.screen)
        self.lock = threading.Lock()

    def start(self) -> None:
        self.monitor_path.unlink(missing_ok=True)
        args = ["qemu-system-x86_64", "-name", self.name, "-machine", "q35,smm=on", "-accel", "kvm", "-cpu", "host",
                "-smp", "4", "-m", str(self.memory),
                "-global", "driver=cfi.pflash01,property=secure,value=on",
                "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={OVMF}/OVMF_CODE.secboot.4m.fd",
                "-drive", f"if=pflash,format=raw,unit=1,file={self.vars_file}",
                "-drive", f"file={self.disk},format=raw,if=none,id=target",
                "-device", "virtio-blk-pci,drive=target,bootindex=1"]
        if self.live:
            # The live ISO as a second DISK (vdb), like a USB stick written with dd: writable for the guest
            # (the wizard hides read-only disks) but snapshot=on keeps the ISO file itself untouched.
            # The kernel is started directly so that the console is a serial line.
            args += ["-drive", f"file={ISO},format=raw,if=none,id=usb,snapshot=on",
                     "-device", "virtio-blk-pci,drive=usb",
                     "-kernel", str(WORK / "vmlinuz-linux"), "-initrd", str(WORK / "initramfs-linux.img"),
                     "-append", f"{shipped_cmdline()} {self.console_arg()} cow_spacesize=3G {self.extra_cmdline}".strip()]
        if CONSOLE == "hvc0":
            args += ["-serial", "none", "-chardev", "stdio,id=con0,signal=off",
                     "-device", "virtio-serial-pci", "-device", "virtconsole,chardev=con0"]
        else:
            args += ["-serial", "stdio"]
        args += ["-nic", "user,model=virtio-net-pci", "-device", "virtio-rng-pci", "-vga", "std", "-display", "none",
                 "-monitor", f"unix:{self.monitor_path},server,nowait", "-no-reboot"]
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        threading.Thread(target=self._read, daemon=True).start()

    def console_arg(self) -> str:
        return "console=hvc0" if CONSOLE == "hvc0" else "console=ttyS0,115200"

    def _read(self) -> None:
        assert self.proc and self.proc.stdout
        with self.log_path.open("wb") as log_file:
            while True:
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    return
                log_file.write(chunk)
                log_file.flush()
                with self.lock:
                    self.raw += chunk
                    self.stream.feed(chunk)

    # -- reading ---------------------------------------------------------------------------------

    def text(self) -> str:
        with self.lock:
            return _ANSI.sub(b"", bytes(self.raw)).replace(b"\r", b"").decode("utf-8", "replace")

    def lines(self) -> list[str]:
        with self.lock:
            return [line.rstrip() for line in self.screen.display]

    def screen_text(self) -> str:
        return "\n".join(self.lines()).rstrip()

    def cell(self, y: int, x: int):
        with self.lock:
            return self.screen.buffer[y][x]

    def wait_for(self, pattern: str, timeout: float = 120) -> re.Match | None:
        regex, deadline = re.compile(pattern), time.time() + timeout
        while time.time() < deadline:
            match = regex.search(self.text(), self.cursor)
            if match:
                self.cursor = match.end()
                return match
            if self.exited():
                return None
            time.sleep(0.4)
        return None

    def wait_screen(self, pattern: str, timeout: float = 60) -> bool:
        regex, deadline = re.compile(pattern, re.MULTILINE), time.time() + timeout
        while time.time() < deadline:
            if regex.search(self.screen_text()):
                return True
            if self.exited():
                return False
            time.sleep(0.3)
        return False

    def exited(self) -> bool:
        return self.proc is not None and self.proc.poll() is not None

    def wait_exit(self, timeout: float) -> bool:
        assert self.proc
        try:
            self.proc.wait(timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

    # -- writing ---------------------------------------------------------------------------------

    def send(self, data: str) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(data.encode())
        self.proc.stdin.flush()

    def settle(self, quiet: float = 0.4, timeout: float = 20) -> None:
        """Wait until the terminal has been quiet for `quiet` seconds: the screen is then what the wizard
        really shows, not a repaint in progress."""
        deadline, last, since = time.time() + timeout, len(self.raw), time.time()
        while time.time() < deadline:
            time.sleep(0.05)
            size = len(self.raw)
            if size != last:
                last, since = size, time.time()
            elif time.time() - since >= quiet:
                return

    def keys(self, *names: str, pause: float = 0.3) -> None:
        """Send keys one by one, waiting for the screen to settle after each (at most `pause` extra)."""
        for name in names:
            self.send(KEYS[name])
            time.sleep(0.1)
            self.settle()
            time.sleep(min(pause, 0.3))

    def type(self, text: str, pause: float = 0.02) -> None:
        for char in text:
            self.send(char)
            time.sleep(pause)
        time.sleep(0.2)
        self.settle()

    def click(self, row: int, col: int) -> None:
        """A mouse click at a screen cell (0-based), as the terminal would report it (SGR mode)."""
        self.send(f"\x1b[<0;{col + 1};{row + 1}M")
        time.sleep(0.1)
        self.send(f"\x1b[<0;{col + 1};{row + 1}m")
        time.sleep(0.5)

    # -- a shell in the guest (only while the wizard is NOT running) ---------------------------------

    def login(self, timeout: float = 240) -> bool:
        if not self.wait_for(r"archiso login:", timeout):
            return False
        self.send("root\r")
        time.sleep(4)
        self.send("exec bash --norc --noprofile\r")
        time.sleep(1.5)
        self.send("export PS1='' TERM=dumb LC_ALL=C; stty -echo cols 200\r")
        time.sleep(1)
        return self.shell("true", 30)[0] == 0

    def shell(self, command: str, timeout: float = 120) -> tuple[int, str]:
        marker = f"__RC{int(time.time() * 1000) % 10**9}__"
        start = self.cursor
        self.send(f"{command}\rprintf '\\n{marker}%s\\n' \"$?\"\r")
        match = self.wait_for(rf"{marker}(\d+)", timeout)
        if match is None:
            return 255, self.text()[start:]
        return int(match.group(1)), self.text()[start:match.start()].strip("\n")


# -- reading the wizard from the screen ------------------------------------------------------------

def option_label(line: str) -> str:
    """The text of a radio button or checkbox line (between the box and the right border of the list)."""
    return line.split("▌", 1)[1].split("▎")[0].strip(" │")


NAV_LABELS = ("Reboot now", "Install", "Next", "Exit", "Back")


class Wizard:
    """Everything the scenario needs to know about the screen, read from the emulated terminal."""

    def __init__(self, vm: Vm) -> None:
        self.vm = vm
        self.snapshots = 0
        self.prefix = ""

    # -- what is on screen ---------------------------------------------------------------------

    def step(self) -> int:
        match = re.search(r"step (\d+) of (\d+)", self.vm.screen_text())
        return int(match.group(1)) if match else 0

    def title(self) -> str:
        lines = [line.strip() for line in self.vm.lines() if line.strip()]
        return lines[1] if len(lines) > 1 else ""

    def error(self) -> str:
        """The red line under the body box (the screen's validation message)."""
        lines = self.vm.lines()
        bottom = max((i for i, line in enumerate(lines) if line.strip().startswith("╰")), default=-1)
        found = []
        for line in lines[bottom + 1:]:
            if "▔▔▔▔" in line:
                break
            if line.strip():
                found.append(line.strip())
        return " ".join(found)

    def has(self, pattern: str) -> bool:
        return re.search(pattern, self.vm.screen_text()) is not None

    def wait(self, pattern: str, timeout: float = 30) -> bool:
        return self.vm.wait_screen(pattern, timeout)

    def wait_step(self, number: int, timeout: float = 30) -> bool:
        return self.vm.wait_screen(rf"step {number} of 13", timeout)

    def snapshot(self, name: str) -> None:
        self.snapshots += 1
        SCREENS.mkdir(parents=True, exist_ok=True)
        (SCREENS / f"{self.prefix}{self.snapshots:02d}-{name}.txt").write_text(self.vm.screen_text() + "\n", encoding="utf-8")

    # -- focus -------------------------------------------------------------------------------------

    def focused_nav(self) -> str | None:
        """The navigation button that has the focus (a focused Textual button is in reverse video)."""
        lines = self.vm.lines()
        for y in range(ROWS - 1, -1, -1):
            line = lines[y]
            if line.strip().startswith("│"):
                continue
            for label in NAV_LABELS:
                column = line.rfind(label)
                if column >= 0 and self.vm.cell(y, column).reverse:
                    return label
        return None

    def focused_box_row(self) -> int | None:
        """Row of the top border of the input, list or button group that has the focus."""
        for y, line in enumerate(self.vm.lines()):
            if "▊▔▔▔" in line and self.vm.cell(y, line.index("▊")).fg == ACCENT:
                return y
        return None

    def box_name(self, row: int) -> str:
        """The caption above a box; for a switch, the label written on its right."""
        lines = self.vm.lines()
        for k in range(row - 1, max(row - 4, -1), -1):
            text = lines[k].strip(" │")
            if text:
                return text
        return ""

    def goto(self, caption: str, limit: int = 40) -> bool:
        """Tab until the box captioned with this text has the focus."""
        for _ in range(limit):
            row = self.focused_box_row()
            if row is not None and caption.lower() in self.box_name(row).lower():
                return True
            self.vm.keys("tab", pause=0.35)
        return False

    def goto_switch(self, label: str, limit: int = 40) -> bool:
        """Tab until the switch with this label has the focus (a switch is a small framed box)."""
        for _ in range(limit):
            row = self.focused_box_row()
            if row is not None:
                lines = self.vm.lines()
                if label.lower() in lines[row + 1].lower():
                    return True
            self.vm.keys("tab", pause=0.35)
        return False

    # -- acting ---------------------------------------------------------------------------------------

    def press(self, label: str, expect_change: bool = True, timeout: float = 5) -> bool:
        """Tab to the navigation button, press it, and report whether the screen changed."""
        for _ in range(60):
            if self.focused_nav() == label:
                time.sleep(0.5)  # an Enter sent right after Tab was sometimes lost
                before = self.vm.screen_text()
                self.vm.send("\r")
                if not expect_change:
                    time.sleep(0.5)
                    self.vm.settle(0.6)
                    return True
                deadline = time.time() + timeout
                while time.time() < deadline:
                    time.sleep(0.3)
                    if self.vm.screen_text() != before:
                        self.vm.settle(0.6)
                        return True
                return False
            self.vm.keys("tab", pause=0.3)
        return False

    def next(self, to_step: int) -> bool:
        """Press Next and check that the wizard is now on the expected step."""
        pressed = self.press("Next")
        if self.wait_step(to_step, 20):
            return True
        print(f"  (Next did not lead to step {to_step}: pressed={pressed}, step={self.step()}, "
              f"focused={self.focused_nav()}, error={self.error()!r})")
        self.snapshot(f"FAILED-next-to-step-{to_step}")
        return False

    def input_text(self) -> str | None:
        """What the focused input shows (dots for a password), or None when no input has the focus."""
        row = self.focused_box_row()
        if row is None:
            return None
        line = self.vm.lines()[row + 1]
        if "▊" not in line:
            return None
        return line.split("▊", 1)[1].split("▎")[0].strip(" │")

    def fill(self, caption: str, text: str, clear: bool = True) -> bool:
        """Put text in the input captioned so, and check on screen that it is what the input now shows (a lost
        key press was seen once; the text is typed again in that case)."""
        for _ in range(3):
            if not self.goto(caption):
                return False
            if clear:
                self.vm.keys("end", pause=0.15)
                for _ in range(40):
                    self.vm.send(KEYS["backspace"])
                    time.sleep(0.01)
                time.sleep(0.3)
                self.vm.settle()
            self.vm.type(text)
            shown = self.input_text()
            if shown is None or not clear or not text:
                return True
            if shown == text or (set(shown) <= {"•"} and len(shown) == len(text)):
                return True
            note(f"fill: '{caption}' showed {shown!r} instead of the {len(text)} characters typed; typing again")
        return False

    def radio_rows(self, caption: str) -> list[int]:
        """Rows of the radio buttons of the group captioned so."""
        lines = self.vm.lines()
        rows: list[int] = []
        start = next((i for i, line in enumerate(lines) if line.strip(" │").lower().startswith(caption.lower())), None)
        if start is None:
            return rows
        for y in range(start + 1, min(start + 12, ROWS)):
            if "▐●▌" in lines[y]:
                rows.append(y)
            elif rows:
                break
        return rows

    def radio_state(self, caption: str) -> tuple[list[str], int | None, int | None]:
        """(labels, index of the selected one, index of the cursor, when the group has the focus)."""
        rows = self.radio_rows(caption)
        lines = self.vm.lines()
        labels, selected, cursor = [], None, None
        for index, y in enumerate(rows):
            x = lines[y].index("●")
            labels.append(option_label(lines[y]))
            if self.vm.cell(y, x).fg == GREEN:
                selected = index
            if self.vm.cell(y, x + 6).bg == ACCENT:
                cursor = index
        return labels, selected, cursor

    def choose(self, caption: str, wanted: str) -> bool:
        """Select a radio button by (part of) its label: arrows + space, then a mouse click if the
        keyboard did not work (recorded as an observation: a real console has no mouse)."""
        if not self.goto(caption):
            return False
        for attempt in range(2):
            labels, selected, cursor = self.radio_state(caption)
            target = next((i for i, text in enumerate(labels) if wanted.lower() in text.lower()), None)
            if target is None:
                return False
            if selected == target:
                return True
            for _ in range(len(labels) + 1):
                labels, selected, cursor = self.radio_state(caption)
                if cursor == target:
                    break
                self.vm.keys("down" if (cursor is None or cursor < target) else "up", pause=0.5)
                time.sleep(0.4)
            if self.radio_state(caption)[2] == target:
                self.vm.keys("space", pause=0.6)
                if self.radio_state(caption)[1] == target:
                    return True
            if attempt == 0:
                note(f"keyboard: arrows+space did not select '{wanted}' in '{caption}' "
                     f"(focus box row: {self.focused_box_row()}); falling back to a mouse click")
                rows = self.radio_rows(caption)
                if target < len(rows):
                    line = self.vm.lines()[rows[target]]
                    self.vm.click(rows[target], line.index("●"))
                    if self.radio_state(caption)[1] == target:
                        return True
        return False

    def toggle_switch(self, label: str) -> bool:
        if not self.goto_switch(label):
            return False
        self.vm.keys("space", pause=0.7)
        return True


# -- helpers for the control socket (exploring by hand) --------------------------------------------


def prepare_work(disk_gib: int = 100) -> tuple[Path, Path]:
    """The target disk (sparse) and a firmware variable store with no keys (Setup Mode)."""
    disk = WORK / "tui-target.img"
    disk.unlink(missing_ok=True)
    subprocess.run(["truncate", "-s", f"{disk_gib}G", str(disk)], check=True)
    vars_file = WORK / "tui-vars.fd"
    vars_file.write_bytes((OVMF / "OVMF_VARS.4m.fd").read_bytes())
    return disk, vars_file


def boot_live(name: str = "tui", extra: str = os.environ.get("TUI_KERNEL_EXTRA", ""), disk_gib: int = 100) -> Vm:
    disk, vars_file = prepare_work(disk_gib)
    vm = Vm(name, disk=disk, vars_file=vars_file, live=True, extra_cmdline=extra)
    vm.start()
    if not vm.login():
        print(vm.text()[-2500:])
        raise SystemExit("the live system did not give a root shell")
    return vm


def start_wizard(vm: Vm) -> None:
    vm.send(f"stty echo rows {ROWS} cols {COLS}; export TERM=linux; archinstall-launcher\r")
    time.sleep(1)


def serve(vm: Vm) -> None:
    CTL.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(CTL))
    server.listen(4)
    wizard = Wizard(vm)
    while True:
        conn, _ = server.accept()
        with conn:
            request = json.loads(conn.makefile().readline())
            op, reply = request["op"], {"ok": True}
            if op == "screen":
                reply["screen"] = vm.screen_text()
            elif op == "keys":
                vm.keys(*request["names"])
                time.sleep(0.8)
                reply["screen"] = vm.screen_text()
            elif op == "type":
                vm.type(request["text"])
                time.sleep(0.8)
                reply["screen"] = vm.screen_text()
            elif op == "wizard":
                start_wizard(vm)
                vm.wait_screen(r"step 1 of", 60)
                reply["screen"] = vm.screen_text()
            elif op == "py":
                import contextlib
                import io

                out = io.StringIO()
                try:
                    with contextlib.redirect_stdout(out):
                        exec(request["code"], globals() | {"vm": vm, "w": wizard})
                except Exception as error:  # noqa: BLE001
                    out.write(f"\n{type(error).__name__}: {error}")
                reply["out"] = out.getvalue()
            elif op == "quit":
                conn.sendall((json.dumps(reply) + "\n").encode())
                vm.stop()
                return
            conn.sendall((json.dumps(reply) + "\n").encode())


def ctl(request: dict) -> dict:
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(900)
        sock.connect(str(CTL))
        sock.sendall((json.dumps(request) + "\n").encode())
        return json.loads(sock.makefile().readline())


# =====================================================================================================
# The scenario
# =====================================================================================================


def scenario_wizard(vm: Vm, w: Wizard) -> bool:
    """Walks the 13 screens. Returns True when the install screen was reached and finished."""
    # ---- 1. Welcome ----------------------------------------------------------------------------
    log("1. Welcome")
    check(w.wait_step(1, 30), "the wizard starts on step 1 of 13")
    w.snapshot("welcome")
    check(w.has(r"OK\s+UEFI firmware"), "Welcome: UEFI is detected")
    check(w.has(r"Windows 11 image\s+Included in this installer"), "Welcome: the included Windows 11 image is listed")
    check(w.has(r"OK\s+Secure Boot"), "Welcome: Secure Boot state is shown")
    vm.keys("esc", pause=1.0)
    check(w.step() == 1, "Welcome: Escape does nothing (there is no previous screen)")
    before = len(vm.raw)
    vm.keys("ctrl+q")
    time.sleep(2)
    # pyte has no alternate screen: the wizard leaving it is seen in the raw stream, not in the screen text.
    if b"\x1b[?1049l" not in bytes(vm.raw[before:]):
        note("Ctrl+Q on the Welcome screen does nothing")
    else:
        note("Ctrl+Q quits the wizard at once, whatever the screen (Textual default): the shell comes back")
        start_wizard(vm)
        check(w.wait_step(1, 60), "Welcome: the wizard can be started again after Ctrl+Q")
    check(w.next(2), "Welcome -> Region")

    # ---- 2. Region ---------------------------------------------------------------------------------
    log("2. Region and keyboard")
    check(w.has(r"Selected: United States") and w.has(r"Selected: us"), "Region: starts with the US defaults")
    check(w.fill("Country", "fran", clear=False), "Region: the country filter takes the focus")
    vm.keys("enter", pause=1.0)
    check(w.has(r"Selected: France") and w.has(r"Selected: Europe/Paris") and w.has(r"Selected: fr_FR\.UTF-8")
          and w.has(r"Selected: fr\b"), "Region: France sets time zone, locale and keymap")
    w.snapshot("region-france")
    check(w.next(3), "Region -> Network")

    # ---- 3. Network ---------------------------------------------------------------------------------
    log("3. Network")
    check(w.wait(r"Connected to the internet", 20), "Network: connected")
    check(w.has(r"Windows 11 is\s+already included"), "Network: the text knows Windows is included")
    w.snapshot("network")
    check(w.next(4), "Network -> Disk")

    # ---- 4. Disk ------------------------------------------------------------------------------------
    log("4. Disk")
    check(w.wait(r"/dev/vda", 20) and w.has(r"/dev/vdb"), "Disk: both virtio disks are listed")
    check(w.has(r"/dev/vdb.*LIVE MEDIUM"), "Disk: the live medium (vdb, the ISO) is marked LIVE MEDIUM")
    check(w.has(r"Selected: /dev/vda"), "Disk: the first disk is preselected")
    w.snapshot("disk")
    # The table has the focus when the screen opens (it is not framed, so nothing shows it).
    vm.keys("down")
    check(w.has(r"Selected: /dev/vdb"), "Disk: Down moves the selection to the live medium")
    w.press("Next", expect_change=False)
    check(w.step() == 4 and "started from" in w.error(), "Disk: Next refuses the live medium", w.error())
    w.snapshot("disk-live-medium-refused")
    # From Next, Tab goes to the scrolling area (no visible focus), then to the table.
    note("Disk: Tab passes through the invisible focus stop of the scrolling area before reaching the table")
    vm.keys("tab", "tab", "up")
    check(w.has(r"Selected: /dev/vda"), "Disk: back on /dev/vda")
    check(w.next(5), "Disk -> Layout")


    # ---- 5. Layout ------------------------------------------------------------------------------------
    log("5. Layout")
    check(w.has(r"Windows\s+64\.0 GiB"), "Layout: Windows defaults to 64 GiB on a 100 GiB disk")
    w.fill("Windows 11 size", "20")
    time.sleep(1)
    check("at least 64" in w.error(), "Layout: 20 GiB is refused live", w.error())
    w.press("Next", expect_change=False)
    check(w.step() == 5, "Layout: Next is blocked with an invalid size")
    w.fill("Windows 11 size", "70")
    time.sleep(1)
    check("Not enough space" in w.error(), "Layout: 70 GiB leaves too little for Arch and is refused", w.error())
    w.fill("Windows 11 size", "64")
    time.sleep(1)
    check(w.error() == "", "Layout: 64 GiB is accepted", w.error())
    w.snapshot("layout")
    check(w.next(6), "Layout -> Accounts")

    # ---- 6. Accounts ------------------------------------------------------------------------------------
    log("6. Accounts")
    refused(w, "Accounts", 6, [
        ("Computer name", "Bad_Host", "lowercase letters, digits and hyphens", "an invalid hostname"),
        ("Computer name", "testbox", "", ""),
        ("User name", "Alice", "must start with a lowercase letter", "an upper-case user name"),
        ("User name", "root", "reserved for the system", "the reserved user name root"),
        ("User name", "alice", "password is required", "an empty password"),
    ])
    w.fill("Computer name", "testbox")
    w.fill("User name", "alice")
    w.fill("User password", "alice-pw")
    w.fill("Confirm the user password", "different-pw")
    w.press("Next", expect_change=False)
    check(w.step() == 6 and "do not match" in w.error(), "Accounts: mismatching passwords are refused", w.error())
    check(not any(secret in vm.screen_text() for secret in SECRETS), "Accounts: no password is shown on screen")
    w.fill("Confirm the user password", "alice-pw")
    check(w.toggle_switch("same password for the root"), "Accounts: the root switch can be reached")
    check(w.wait(r"Root password", 5), "Accounts: separate root password fields appear")
    w.fill("Root password", "root-pw")
    w.fill("Confirm the root password", "root-pw")
    w.snapshot("accounts")
    check(w.next(7), "Accounts -> Storage")

    # ---- 7. Storage ------------------------------------------------------------------------------------
    log("7. Storage")
    check(w.choose("Filesystem", "btrfs"), "Storage: btrfs can be chosen")
    labels, selected, _ = w.radio_state("Filesystem")
    check(selected is not None and "btrfs" in labels[selected], "Storage: btrfs is the selected filesystem")
    w.snapshot("storage")
    check(w.next(8), "Storage -> Software")

    # ---- 8. Software ------------------------------------------------------------------------------------
    log("8. Software")
    labels, selected, _ = w.radio_state("Kernel")
    check(selected is not None and labels[selected].startswith("linux ") or labels[selected].startswith("linux  "),
          "Software: the stock kernel is the default")
    gpu_ticked = ticked_gpu_profiles(vm, w)
    check(gpu_ticked == ["QEMU / KVM guest (mesa, qemu-guest-agent, spice-vdagent)"],
          "Software: the virtual GPU pre-selects the QEMU profile only", str(gpu_ticked))
    check(w.choose("Desktop", "Minimal"), "Software: minimal desktop can be chosen")
    w.snapshot("software")

    # Back navigation. First what was saved with Next must survive a round trip.
    check(w.next(9), "Software -> Windows image (saves the Software choices)")
    vm.keys("esc", pause=1.0)
    check(w.wait_step(8, 15), "Back: Escape returns from the Windows image screen to Software")
    labels, selected, _ = w.radio_state("Desktop")
    check(selected is not None and labels[selected].startswith("Minimal"),
          "Back: a choice saved with Next (minimal desktop) is still there after Next, Back")
    vm.keys("esc", pause=1.0)
    check(w.wait_step(7, 15), "Back: Escape goes back to Storage")
    labels, selected, _ = w.radio_state("Filesystem")
    check(selected is not None and "btrfs" in labels[selected], "Back: the btrfs choice was kept")
    # Then an edit that was never saved with Next: the user changes the kernel, then goes Back and forward.
    check(w.next(8), "Back: Next returns to Software")
    check(w.choose("Kernel", "lts"), "Back: choose the LTS kernel on the Software screen")
    vm.keys("esc", pause=1.0)
    check(w.wait_step(7, 15), "Back: Escape from Software to Storage with an unsaved kernel choice")
    check(w.next(8), "Back: Next returns to Software again")
    labels, selected, _ = w.radio_state("Kernel")
    check(selected is not None and "lts" in labels[selected],
          "Back: a choice made on the screen you leave with Back (not saved with Next) is kept",
          f"the kernel went back to {labels[selected] if selected is not None else '?'}")

    # Ctrl+C at an odd moment (focus in the middle of a screen).
    vm.keys("ctrl+c", pause=1.2)
    check(w.step() == 8, "Ctrl+C: the wizard is still on the same screen")
    seen = w.vm.screen_text()
    if "quit" in seen.lower() or "ctrl+q" in seen.lower():
        note("Ctrl+C shows a hint to quit with Ctrl+Q (Textual's default); the wizard keeps running")
    check(w.next(9), "Software -> Windows image")

    # ---- 9. Windows image -----------------------------------------------------------------------------------
    log("9. Windows image")
    labels, selected, _ = w.radio_state("Where does the ISO come from")
    check(selected == 0 and "included" in labels[0].lower(), "Windows: the included ISO is the default source")
    check(w.has(r"Windows language: English \(United States\) \(set by the included ISO\)"),
          "Windows: the language is fixed by the included ISO")
    labels, selected, _ = w.radio_state("Edition")
    check(selected is not None and labels[selected].endswith("Pro"), "Windows: Pro is the default edition")
    # The link source: empty and http:// links are refused.
    check(w.choose("Where does the ISO come from", "link I paste"), "Windows: the pasted-link source can be chosen")
    check(w.wait(r"Download link of the Windows 11 ISO", 5), "Windows: the link field appears")
    w.press("Next", expect_change=False)
    check(w.step() == 9 and "Paste the download link" in w.error(), "Windows: an empty link is refused", w.error())
    w.fill("Download link", "http://example.com/win.iso")
    w.press("Next", expect_change=False)
    check(w.step() == 9 and "https://" in w.error(), "Windows: an http:// link is refused", w.error())
    check(w.choose("Where does the ISO come from", "included"), "Windows: back to the included ISO")
    check(w.has(r"set by the included ISO"), "Windows: the fixed language line is back")
    w.snapshot("windows-image")
    check(w.next(10), "Windows image -> Windows account")

    # ---- 10. Windows account -----------------------------------------------------------------------------------
    log("10. Windows account")
    w.fill("Password", "win-pw")
    w.fill("Confirm the password", "win-pw")
    refused(w, "Windows account", 10, [
        ("Computer name", "WINDOWS-TEST-PC-01", "limited to 15 characters", "a computer name over 15 characters"),
        ("Computer name", "WINTEST", "", ""),
        ("User name", "Administrator", "reserved by Windows", "the reserved user name Administrator"),
        ("User name", "wintest", "must differ from the computer name", "a user name equal to the computer name"),
    ])
    w.fill("Computer name", "WINTEST")
    w.fill("User name", "alice")
    w.fill("Password", "win-pw")
    w.fill("Confirm the password", "win-other")
    w.press("Next", expect_change=False)
    check(w.step() == 10 and "do not match" in w.error(), "Windows account: mismatching passwords are refused", w.error())
    w.fill("Confirm the password", "win-pw")
    check(not any(secret in vm.screen_text() for secret in SECRETS), "Windows account: no password is shown")
    w.snapshot("windows-account")
    check(w.next(11), "Windows account -> Secure Boot")

    # ---- 11. Secure Boot ----------------------------------------------------------------------------------------
    log("11. Secure Boot")
    check(w.has(r"Setup Mode on"), "Secure Boot: the empty firmware variable store means Setup Mode")
    check(w.has(r"TPM not found"), "Secure Boot: the VM has no TPM and the screen says so")
    auto_shim = w.has(r"Strategy that will be used: shim")
    check(auto_shim, "Secure Boot: Automatic resolves to shim + MOK when there is no TPM (sbctl would not boot)")
    w.snapshot("secureboot-auto")
    # Explicit choice of shim + MOK in the Select, then a password.
    check(w.goto("Strategy"), "Secure Boot: the strategy list can be focused")
    vm.keys("enter", pause=1.0)
    vm.keys("down", "down", pause=0.5)  # Automatic -> sbctl -> shim
    vm.keys("enter", pause=1.0)
    check(w.has(r"Strategy that will be used: shim"), "Secure Boot: shim + MOK is selected explicitly")
    # sbctl without a TPM must be refused by the wizard itself.
    check(w.goto("Strategy"), "Secure Boot: focus the list again")
    vm.keys("enter", pause=1.0)
    vm.keys("up", pause=0.5)
    vm.keys("enter", pause=1.0)
    if w.has(r"Strategy that will be used: Custom keys with sbctl"):
        w.press("Next", expect_change=False)
        check(w.step() == 11 and "TPM" in w.error(), "Secure Boot: sbctl is refused by the wizard without a TPM", w.error())
        check(w.goto("Strategy"), "Secure Boot: focus the list a third time")
        vm.keys("enter", pause=1.0)
        vm.keys("down", pause=0.5)
        vm.keys("enter", pause=1.0)
    else:
        note("Secure Boot: could not select sbctl with the keyboard to test its refusal")
    check(w.has(r"Strategy that will be used: shim"), "Secure Boot: shim + MOK is selected again")
    w.press("Next", expect_change=False)
    check(w.step() == 11 and "enrollment password" in w.error().lower(), "Secure Boot: an empty enrollment password is refused",
          w.error())
    w.fill("Enrollment password", "Abcd1234")
    w.fill("Confirm the enrollment password", "Abcd1234")
    w.snapshot("secureboot-shim")
    check(w.next(12), "Secure Boot -> Summary")

    # ---- 12. Summary ---------------------------------------------------------------------------------------------
    log("12. Summary")
    summary = vm.screen_text()
    check("/dev/vda" in summary, "Summary: names the target disk")
    check(re.search(r"Windows\s+70\.0|Windows\s+64\.0", summary) is not None, "Summary: lists the partitions")
    check("btrfs" in summary, "Summary: shows the btrfs choice")
    check("testbox" in summary and "alice" in summary, "Summary: shows the hostname and the user")
    check("WINTEST" in summary, "Summary: shows the Windows computer name")
    check(re.search(r"Kernel\s+linux-lts", summary) is not None,
          "Summary: shows the linux-lts kernel chosen before the Back/Next round trip")
    check("included in this installer" in summary.lower(), "Summary: says Windows comes from the included ISO")
    check(not any(secret in summary for secret in SECRETS), "Summary: no secret is displayed")
    w.snapshot("summary")
    check(w.fill("Type /dev/vda to confirm", "/dev/sdz"), "Summary: the confirmation field can be reached")
    w.press("Install", expect_change=False)
    check(w.step() == 12 and "exactly" in w.error().lower(), "Summary: a wrong device path is refused", w.error())
    w.fill("Type /dev/vda to confirm", "/dev/vda")
    return True


def refused(w: Wizard, screen: str, step: int, cases: list[tuple[str, str, str, str]]) -> None:
    """For each (field caption, text, expected part of the message, what it is): type it, press Next and check that the
    wizard stays on the step and says why. An empty message part means "just fill this field" (no check)."""
    for caption, text, expected, what in cases:
        w.fill(caption, text)
        if not expected:
            continue
        w.press("Next", expect_change=False)
        check(w.step() == step and expected.lower() in w.error().lower(),
              f"{screen}: {what} is refused", f"step {w.step()}, message: {w.error()!r}")


def goto_table(vm: Vm, w: Wizard) -> None:
    for _ in range(8):
        vm.keys("tab", pause=0.3)
        if w.focused_nav() is None and w.focused_box_row() is None:
            return


def ticked_gpu_profiles(vm: Vm, w: Wizard) -> list[str]:
    ticked = []
    for y, line in enumerate(vm.lines()):
        if "▐X▌" in line:
            x = line.index("▐X▌") + 1
            if vm.cell(y, x).fg == GREEN:
                ticked.append(option_label(line))
    return ticked


STEP_LINE = re.compile(r"\[([ x>!-])\] ([A-Z][^│]*?)\s*(?:│\s*)?$")


def install_steps(vm: Vm) -> dict[str, str]:
    """{step title: status mark} as drawn by the Install screen."""
    found: dict[str, str] = {}
    for line in vm.lines():
        match = STEP_LINE.search(line.strip(" "))
        if match:
            found[match.group(2).strip()] = match.group(1)
    return found


def final_button(vm: Vm) -> str | None:
    """The label of the button shown under the box once the run is over."""
    for line in vm.lines()[-9:]:
        if not line.strip().startswith("│"):
            for label in ("Reboot now", "Exit"):
                if label in line:
                    return label
    return None


def scenario_install(vm: Vm, w: Wizard, partial: bool = False, ctrl_q: bool = False) -> bool:
    log("13. Install")
    w.press("Install", expect_change=True, timeout=20)
    check(w.wait_step(13, 30), "Summary -> Install screen")
    w.snapshot("install-start")
    started = time.time()
    # Keys a person might press while waiting must not interrupt the install (there is no way back from here).
    time.sleep(8)
    vm.keys("esc", "esc", "ctrl+c", "enter")
    steps_now = install_steps(vm)
    check(w.step() == 13 and len(steps_now) >= 10 and "!" not in steps_now.values(),
          "Install: Esc, Ctrl+C and Enter while it runs do not interrupt it", str(steps_now))
    if ctrl_q:
        before = len(vm.raw)
        vm.keys("ctrl+q")
        time.sleep(1.5)
        left_the_screen = b"\x1b[?1049l" in bytes(vm.raw[before:])
        check(not left_the_screen and w.step() == 13 and w.has(r"The installation is running"),
              "Install: Ctrl+Q is refused while the install runs (a warning is shown, the wizard stays)",
              "the wizard quit" if left_the_screen else vm.screen_text()[-400:])
    seen: dict[str, str] = {}
    last_size, last_change = len(vm.raw), time.time()
    next_shot = started + 300
    result = "timeout"
    while time.time() - started < 90 * 60:
        time.sleep(4)
        steps = install_steps(vm)
        for title, mark in steps.items():
            if seen.get(title) != mark:
                seen[title] = mark
                print(f"  [{int(time.time() - started):5d}s] [{mark}] {title}", flush=True)
        button = final_button(vm)
        if button:
            result = button
            break
        if len(vm.raw) != last_size:
            last_size, last_change = len(vm.raw), time.time()
        elif time.time() - last_change > 20 * 60:
            result = "stalled"
            break
        if time.time() > next_shot:
            next_shot += 300
            (SCREENS / "install-progress.txt").write_text(vm.screen_text() + "\n", encoding="utf-8")
    elapsed = int(time.time() - started)
    print(f"  install screen finished after {elapsed}s with: {result}", flush=True)
    time.sleep(2)
    w.snapshot("install-end")
    final = vm.screen_text()
    steps = install_steps(vm)
    check(len(steps) >= 10, f"Install: the step list is shown ({len(steps)} steps)")
    check(result == "Reboot now", "Install: the run ends with a 'Reboot now' button", result)
    failed = [title for title, mark in steps.items() if mark == "!"]
    check(not failed, "Install: no step failed", ", ".join(failed))
    pending = [title for title, mark in steps.items() if mark in (" ", ">")]
    check(not pending, "Install: no step is left pending or running", ", ".join(pending))
    for title in ("Partition the disk", "Create the filesystems", "Install the Arch Linux base system",
                  "Configure Arch Linux", "Install GRUB and set up Secure Boot",
                  *(() if partial else ("Stage the Windows 11 setup",)), "Unmount and finish"):
        check(steps.get(title) == "x", f"Install: '{title}' is done", steps.get(title, "missing"))
    skipped = [title for title, mark in steps.items() if mark == "-"]
    if skipped:
        note(f"Install: steps shown as skipped: {', '.join(skipped)}")
    check("Finished. Remove the installation medium" in final, "Install: the log ends with the 'Finished' message")
    everything = vm.text()
    check("Traceback" not in everything, "Install: no Python traceback on the console")
    leaked = [secret for secret in SECRETS if secret in everything]
    check(not leaked, "Install: no password appears in the log or anywhere on the console", str(leaked))
    errors = [line.strip() for line in everything.splitlines() if re.search(r"\bERROR\b|Traceback|command failed", line)]
    check(not errors, "Install: no ERROR line in the log", errors[0] if errors else "")
    return result == "Reboot now"


def scenario_reboot(vm: Vm, w: Wizard) -> bool:
    log("Reboot")
    ok = w.press("Reboot now", expect_change=False)
    check(ok, "Reboot: the 'Reboot now' button can be pressed")
    exited = vm.wait_exit(120)
    check(exited, "Reboot: 'Reboot now' restarts the machine (QEMU exits because of -no-reboot)")
    return exited


# -- inspecting the disk the wizard produced ----------------------------------------------------------------


def sh(command: str, check_rc: bool = False) -> str:
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
    if check_rc and result.returncode != 0:
        raise RuntimeError(f"{command}\n{result.stderr}")
    return (result.stdout + result.stderr).strip()


PROFILE_A = {
    "tag": "a", "disk_gib": 100, "hostname": "testbox", "user": "alice", "win_name": "WINTEST", "win_user": "alice",
    "win_pw": "win-pw", "locale": "fr_FR.UTF-8", "keymap": "fr", "tz": "Europe/Paris",
    "win_tz": "Romance Standard Time", "win_input": "fr-FR", "fs": "btrfs", "kernel": "linux-lts", "home": False,
    "key": "VK7JG-NPHTM-C97JM-9MPGT-3V66T", "separate_root_pw": True,
}
PROFILE_B = {
    "tag": "b", "disk_gib": 140, "hostname": "second", "user": "bob", "win_name": "WINTWO", "win_user": "bob",
    "win_pw": "win2-pw-456", "locale": "de_DE.UTF-8", "keymap": "de-latin1", "tz": "Europe/Berlin",
    "win_tz": "W. Europe Standard Time", "win_input": "de-DE", "fs": "ext4", "kernel": "linux", "home": True,
    "key": "YTMG3-N6DKC-DKB77-7M9GH-8HVX7", "separate_root_pw": False,
}
IMAGE_INDEX: dict[str, str] = {}  # walk tag -> the Windows image index written in the answer file

LOCAL_ISO = "/run/archiso/bootmnt/windows/Windows11.iso"


def scenario_wizard_b(vm: Vm, w: Wizard) -> bool:
    """A second walk with other answers: ext4, minimal desktop, a separate /home, German settings and a Windows ISO
    given as a local file (the one bundled on the medium). Validation of bad input was done in the first walk."""
    log("Walk B: Germany, ext4, separate /home, minimal desktop, Windows ISO from a local file, Home edition")
    check(w.wait_step(1, 30), "B: the wizard starts on step 1 of 13")
    check(w.next(2), "B: Welcome -> Region")
    check(w.fill("Country", "germ", clear=False), "B: the country filter takes the focus")
    vm.keys("enter")
    check(w.has(r"Selected: Germany") and w.has(r"Selected: Europe/Berlin") and w.has(r"Selected: de_DE.UTF-8")
          and w.has(r"Selected: de-latin1"), "B: Germany sets time zone, locale and keymap (de-latin1)")
    w.snapshot("region-germany")
    check(w.next(3), "B: Region -> Network")
    check(w.next(4), "B: Network -> Disk")
    check(w.wait(r"/dev/vda\s+140 GiB", 20) and w.has(r"Selected: /dev/vda"), "B: the 140 GiB disk is listed and selected")
    check(w.next(5), "B: Disk -> Layout")
    check(w.toggle_switch("separate /home"), "B: the separate /home switch can be reached and toggled")
    check(w.wait(r"Arch Linux root size", 5), "B: the root size field appears")
    w.fill("Arch Linux root size", "30")
    time.sleep(1)
    check(w.error() == "" and w.has(r"Arch home"), "B: 64 GiB Windows + 30 GiB root + /home fits on 140 GiB", w.error())
    w.snapshot("layout-home")
    check(w.next(6), "B: Layout -> Accounts")
    w.fill("Computer name", "second")
    w.fill("User name", "bob")
    w.fill("User password", "bob-pw-123")
    w.fill("Confirm the user password", "bob-pw-123")
    check(w.next(7), "B: Accounts (same password for root) -> Storage")
    check(w.choose("Filesystem", "ext4"), "B: ext4 can be chosen")
    w.snapshot("storage-ext4")
    check(w.next(8), "B: Storage -> Software")
    check(w.choose("Desktop", "Minimal"), "B: minimal desktop can be chosen")
    w.snapshot("software")
    check(w.next(9), "B: Software -> Windows image")
    check(w.choose("Where does the ISO come from", "ISO file I already have"), "B: the local-file source can be chosen")
    check(w.wait(r"Path of the ISO file", 5), "B: the path field appears")
    refused(w, "B Windows image", 9, [
        ("Path of the ISO file", "", "Enter the path", "an empty path"),
        ("Path of the ISO file", "/etc/hostname", ".iso", "a file that is not an .iso"),
        ("Path of the ISO file", "/nonexistent/Win11.iso", "File not found", "a missing file"),
    ])
    w.fill("Path of the ISO file", LOCAL_ISO)
    check(w.choose("Edition", "Home"), "B: the Home edition can be chosen")
    w.snapshot("windows-image-local")
    check(w.next(10), "B: Windows image (local file, Home) -> Windows account")
    w.fill("Computer name", "WINTWO")
    w.fill("User name", "bob")
    w.fill("Password", "win2-pw-456")
    w.fill("Confirm the password", "win2-pw-456")
    check(w.next(11), "B: Windows account -> Secure Boot")
    check(w.has(r"Strategy that will be used: shim"), "B: Automatic resolves to shim + MOK (no TPM)")
    w.fill("Enrollment password", "Efgh5678")
    w.fill("Confirm the enrollment password", "Efgh5678")
    w.snapshot("secureboot")
    check(w.next(12), "B: Secure Boot -> Summary")
    summary = vm.screen_text()
    w.snapshot("summary")
    check("ext4" in summary and "Arch home" in summary, "B Summary: ext4 and the /home partition are listed")
    check(re.search(r"Windows\s+64\.0 GiB", summary) is not None, "B Summary: Windows 64 GiB")
    check("second" in summary and "bob" in summary and "WINTWO" in summary, "B Summary: hostname, users and computer name")
    check("Windows 11 Home" in summary, "B Summary: the Home edition")
    check("Windows11.iso" in summary or "file" in summary.lower(), "B Summary: the Windows image is the local file")
    check("Germany" in summary or "DE / Europe/Berlin" in summary, "B Summary: German region", "")
    check(not any(secret in summary for secret in SECRETS), "B Summary: no secret is displayed")
    check(w.fill("Type /dev/vda to confirm", "/dev/vda"), "B: the confirmation is typed")
    return True


def write_disk_evidence(loop: str, root: Path, esp: Path, setup: Path, p: dict) -> None:
    """Copy of what was found on the installed disk, for the report (no password hash is written)."""
    parts: list[str] = []

    def add(title: str, command: str) -> None:
        parts.append(f"=== {title}\n$ {command}\n{sh(command)}\n")

    add("partition table", f"sgdisk -p {loop}")
    add("filesystems", f"blkid {loop}p1 {loop}p2 {loop}p3 {loop}p4 {loop}p5; true")
    add("ESP (depth 2)", f"cd {esp} && find . -maxdepth 2 -not -path './grub/*' | sort | head -50")
    add("fstab", f"cat {root}/etc/fstab")
    add("identity", f"cat {root}/etc/hostname {root}/etc/locale.conf {root}/etc/vconsole.conf; readlink {root}/etc/localtime; "
                    f"grep -E '^({p['user']}|root):' {root}/etc/passwd; grep -E '^wheel:' {root}/etc/group")
    add("enabled services", f"find {root}/etc/systemd/system -path '*.wants/*' | sed 's|.*etc/systemd/system/||' | sort")
    add("Secure Boot files", f"ls -l {root}/var/lib/archinstall/secureboot {root}/etc/archinstall {root}/usr/local/sbin")
    add("GRUB menu entries", f"grep -nE 'menuentry|chainloader|linux |initrd ' {esp}/grub/grub.cfg | head -40")
    add("WINSETUP (depth 2)", f"cd {setup} && find . -maxdepth 2 | sort | head -60; du -sh {setup}")
    scripts = f"{setup}/sources/$OEM$/$$/Setup/Scripts"
    add("Windows first-boot scripts", f"ls -l '{scripts}'; echo; echo '--- SetupComplete.cmd'; cat '{scripts}/SetupComplete.cmd'")
    add("archinstall-bootorder.ps1 (first 60 lines)", f"sed -n 1,60p '{scripts}/archinstall-bootorder.ps1'")
    answers = sorted((setup / "archinstall").glob("unattend-*.xml"))
    if answers:
        add("first answer file (secrets hidden)",
            f"sed -E 's|(<Value>)[^<]{{12,}}(</Value>)|\\1<hidden>\\2|' {answers[0]}")
    (SCREENS / f"{p['tag']}-disk-evidence.txt").write_text("\n".join(parts), encoding="utf-8")


def scenario_inspect(disk: Path, p: dict) -> None:
    tag = p["tag"]

    def c(ok: bool, what: str, detail: str = "") -> bool:
        return check(ok, f"[{tag.upper()}] {what}", detail)

    log(f"Inspecting the installed disk (walk {tag.upper()})")
    loop = sh(f"losetup -f --show -P {disk}")
    sh(f'for d in /sys/class/block/{Path(loop).name}p*; do n=$(basename $d); [ -e /dev/$n ] || {{ IFS=: read a b < $d/dev; mknod /dev/$n b $a $b; }}; done')
    mounts = Path(f"/tmp/tuimnt-{tag}")
    root, esp, setup, home = (mounts / name for name in ("root", "esp", "setup", "home"))
    for path in (root, esp, setup, home):
        path.mkdir(parents=True, exist_ok=True)
    count = 5 if p["home"] else 4
    setup_dev, home_dev = f"{loop}p{count}", f"{loop}p4"
    try:
        c(sh(f"sgdisk -p {loop} | grep -c '^ *[0-9]'") == str(count), f"Disk: {count} partitions were created")
        devices = [f"{loop}p1", f"{loop}p3"] + ([home_dev] if p["home"] else []) + [setup_dev]
        wanted = ["vfat", p["fs"]] + ([p["fs"]] if p["home"] else []) + ["vfat"]
        got = [sh(f"blkid -s TYPE -o value {d}") for d in devices]
        c(got == wanted, f"Disk: filesystems are {' / '.join(wanted)} (ESP, root{', home' if p['home'] else ''}, WINSETUP)", str(got))
        c("TYPE=" not in sh(f"blkid {loop}p2"), "Disk: the Windows partition is left unformatted for Windows Setup")
        c("EBD0A0A2" in sh(f"sgdisk -i 2 {loop}").upper(), "Disk: the Windows partition has the Microsoft basic data type")
        sh(f"mount -o {'ro,subvol=@' if p['fs'] == 'btrfs' else 'ro'} {loop}p3 {root}")
        sh(f"mount -o ro {loop}p1 {esp}")
        sh(f"mount -o ro {setup_dev} {setup}")
        if p["home"]:
            sh(f"mount -o ro {home_dev} {home}")

        read = lambda name: (root / name).read_text(errors="replace") if (root / name).exists() else ""  # noqa: E731
        kernel = p["kernel"]
        c((esp / f"vmlinuz-{kernel}").exists() and (esp / f"initramfs-{kernel}.img").exists(),
          f"Disk: the {kernel} kernel and its initramfs are on the ESP")
        c((esp / "EFI/ArchInstall/grubx64.efi").exists() and (esp / "EFI/ArchInstall/shimx64.efi").exists(),
          "Disk: GRUB and shim are on the ESP")
        cert = root / "var/lib/archinstall/secureboot/MOK.crt"
        c(cert.exists() and sh(f"sbverify --cert {cert} {esp}/EFI/ArchInstall/grubx64.efi >/dev/null 2>&1; echo $?") == "0",
          "Disk: GRUB is signed with the generated key")
        c(sh(f"sbverify --cert {cert} {esp}/vmlinuz-{kernel} >/dev/null 2>&1; echo $?") == "0",
          "Disk: the kernel is signed with the generated key")
        fstab = read("etc/fstab")
        rows = [line.split() for line in fstab.splitlines() if line.strip() and not line.startswith("#")]
        c(all(row[0].startswith("UUID=") for row in rows), "Disk: fstab uses UUIDs only")
        by_mount = {row[1]: row for row in rows}
        if p["fs"] == "btrfs":
            c("subvol=/@" in fstab and "subvol=/@home" in fstab, "Disk: fstab has the btrfs subvolumes @ and @home")
        else:
            c(by_mount.get("/", ["", "", ""])[2] == "ext4", "Disk: / is ext4 in fstab")
        if p["home"]:
            c("/home" in by_mount and by_mount["/home"][0] != by_mount["/"][0] and by_mount["/home"][2] == p["fs"],
              "Disk: /home is a partition of its own in fstab (another UUID than /)")
            c((home / p["user"]).is_dir(), f"Disk: the home directory of {p['user']} lives on the /home partition")
        passwd = read("etc/passwd")
        c(f"{p['user']}:" in passwd, f"Disk: the user {p['user']} exists")
        c(read("etc/hostname").strip() == p["hostname"], f"Disk: hostname is {p['hostname']}")
        c(f"LANG={p['locale']}" in read("etc/locale.conf"), f"Disk: locale is {p['locale']}")
        c(f"KEYMAP={p['keymap']}" in read("etc/vconsole.conf"), f"Disk: console keymap is {p['keymap']}")
        tz = sh(f"readlink {root}/etc/localtime")
        c(tz.endswith(p["tz"]), f"Disk: time zone is {p['tz']}", tz)
        shadow = read("etc/shadow")
        user_hash = next((l.split(":")[1] for l in shadow.splitlines() if l.startswith(p["user"] + ":")), "")
        root_hash = next((l.split(":")[1] for l in shadow.splitlines() if l.startswith("root:")), "")
        c(user_hash.startswith("$") and root_hash.startswith("$"), "Disk: the user and root have password hashes")
        if p["separate_root_pw"]:
            c(user_hash != root_hash, "Disk: the user and root passwords differ")
        c(re.search(rf"^wheel:.*\b{p['user']}\b", read("etc/group"), re.MULTILINE) is not None,
          f"Disk: {p['user']} is in the wheel group")
        c(sh(f"grep -rEh '^%wheel' {root}/etc/sudoers {root}/etc/sudoers.d 2>/dev/null").startswith("%wheel"),
          "Disk: the wheel group may use sudo")
        c(not (root / "etc/sudoers.d/99-archinstall-aur").exists(), "Disk: no temporary sudoers file is left")
        grub_cfg = (esp / "grub/grub.cfg").read_text(errors="replace")
        c("rd.luks" not in grub_cfg, "Disk: no LUKS parameter (not chosen)")
        c(f"/vmlinuz-{kernel} " in grub_cfg, f"Disk: GRUB boots /vmlinuz-{kernel}")
        c("bootmgfw.efi" in grub_cfg or "bootmgfw.efi" in sh(f"cat {root}/etc/grub.d/*"),
          "Disk: GRUB knows how to chainload the Windows boot manager")
        enabled = sh(f"find {root}/etc/systemd/system -name '*.service' -path '*.wants/*' | sed 's|.*/||' | sort -u")
        c("NetworkManager.service" in enabled, "Disk: NetworkManager is enabled", enabled.replace(chr(10), " "))
        c("archinstall-mok-enroll.service" in enabled, "Disk: the MOK re-queue service is enabled (shim strategy)",
          enabled.replace(chr(10), " "))
        write_disk_evidence(loop, root, esp, setup, p)

        # The staged Windows Setup.
        c((setup / "efi/boot/bootx64.efi").exists(), "WINSETUP: Microsoft's boot manager is there")
        swm = sorted((setup / "sources").glob("install*.swm"))
        c(len(swm) >= 2 and all(f.stat().st_size < 4 * 1024**3 for f in swm),
          f"WINSETUP: install.wim is split in {len(swm)} parts, each below 4 GiB")
        scripts = setup / "sources" / "$OEM$" / "$$" / "Setup" / "Scripts"
        c((scripts / "SetupComplete.cmd").is_file(), "WINSETUP: SetupComplete.cmd is staged")
        finish = scripts / "archinstall-finish.ps1"
        finish_text = finish.read_text(errors="replace") if finish.is_file() else ""
        c("powercfg.exe /hibernate off" in finish_text, "WINSETUP: archinstall-finish.ps1 turns hibernation off (powercfg.exe /hibernate off)")
        c("OA3xOriginalProductKey" in finish_text, "WINSETUP: archinstall-finish.ps1 reads the firmware product key (OA3xOriginalProductKey)")
        bootorder = scripts / "archinstall-bootorder.ps1"
        c(bootorder.is_file() and bootorder.stat().st_size > 200,
          "WINSETUP: sources\\$OEM$\\$$\\Setup\\Scripts\\archinstall-bootorder.ps1 is staged")
        answers = sorted((setup / "archinstall").glob("unattend-*.xml"))
        c(len(answers) == 16, f"WINSETUP: 16 answer files, found {len(answers)}")
        if answers:
            text = answers[0].read_text(encoding="utf-8")
            tree = ET.fromstring(text)
            ns = "{urn:schemas-microsoft-com:unattend}"
            names = [e.text for e in tree.iter(ns + "ComputerName")]
            c(names == [p["win_name"]], f"Answer file: computer name is {p['win_name']}", str(names))
            accounts = [e.text for e in tree.iter(ns + "Name")]
            c(p["win_user"] in accounts, f"Answer file: local account {p['win_user']}", str(accounts))
            values = [e.text for e in tree.iter(ns + "Value") if e.text and len(e.text) > 20]
            decoded = [base64.b64decode(v).decode("utf-16-le") for v in values if re.fullmatch(r"[A-Za-z0-9+/=]+", v)]
            c(p["win_pw"] + "Password" in decoded, "Answer file: the Windows password is the one typed in the wizard")
            c(p["win_tz"] in text, f"Answer file: Windows time zone is {p['win_tz']}")
            c(f"<InputLocale>{p['win_input']}</InputLocale>" in text, f"Answer file: Windows keyboard is {p['win_input']}")
            c("<UILanguage>en-US</UILanguage>" in text, "Answer file: language comes from the ISO (en-US)")
            c(p["key"] in text, "Answer file: the generic key of the chosen edition is used")
            c("<DiskID>0</DiskID>" in text, "Answer file: disk 0 variant")
            c("BypassTPMCheck" in text, "Answer file: the hardware checks are bypassed")
            index = re.search(r"/IMAGE/INDEX</Key><Value>(\d+)</Value>", text)
            IMAGE_INDEX[tag] = index.group(1) if index else "?"
            if "a" in IMAGE_INDEX and "b" in IMAGE_INDEX:
                c(IMAGE_INDEX["a"] != IMAGE_INDEX["b"], "Answer files: Pro and Home use different image indexes",
                  str(IMAGE_INDEX))
    finally:
        sh(f"umount {setup} {esp} {root} {home} 2>/dev/null; losetup -d {loop} 2>/dev/null")


def scenario_firmware_vars(p: dict) -> None:
    """What the installer left in the VM's UEFI variable store (the NVRAM file the VM ran with)."""
    log(f"Inspecting the UEFI variables the installer wrote (walk {p['tag'].upper()})")
    out = sh(f"virt-fw-vars --input {WORK / 'tui-vars.fd'} --print 2>&1")
    (SCREENS / f"{p['tag']}-uefi-variables.txt").write_text(out + "\n", encoding="utf-8")
    count = 5 if p["home"] else 4
    entries = {m.group(1): (m.group(2), m.group(3)) for m in re.finditer(
        r"^Boot([0-9A-F]{4})\s*: boot entry: title=\"([^\"]*)\" devpath=(.*)$", out, re.MULTILINE)}
    next_boot = re.search(r"^BootNext\s*: boot order: ([0-9A-F]{4})", out, re.MULTILINE)
    order = re.search(r"^BootOrder\s*: boot order: (.*)$", out, re.MULTILINE)
    windows = next((number for number, (title, _) in entries.items() if title == "Windows 11 Setup"), None)
    tag = p["tag"].upper()
    check(windows is not None and f"Partition(nr={count})" in entries[windows][1],
          f"[{tag}] UEFI: a 'Windows 11 Setup' boot entry points to partition {count} (WINSETUP)", str(entries))
    check(next_boot is not None and next_boot.group(1) == windows,
          f"[{tag}] UEFI: BootNext is that entry (the next boot starts Windows Setup once)",
          str(next_boot and next_boot.group(1)))
    arch = next((v for v in entries.values() if v[0] == "ArchInstall"), None)
    check(arch is not None and "shimx64.efi" in arch[1] and "Partition(nr=1)" in arch[1],
          f"[{tag}] UEFI: an 'ArchInstall' boot entry starts shimx64.efi on the ESP", str(arch))
    check("MokNew" in out, f"[{tag}] UEFI: the MOK enrollment request (MokNew) is queued for the next boot")
    print(f"  boot entries: {', '.join(f'{n}={t}' for n, (t, _) in sorted(entries.items()))}; "
          f"BootOrder: {order.group(1) if order else '?'}")


# -- entry points ----------------------------------------------------------------------------------------------


def run_walk(profile: dict, scenario) -> None:
    tag = profile["tag"].upper()
    log(f"Walk {tag}: boot the live ISO (as a USB-stick-like disk) with the kernel command line of its own boot entry")
    print(f"  kernel command line: {shipped_cmdline()}")
    vm = boot_live(disk_gib=profile["disk_gib"])
    probe = "mountpoint -q /run/archiso/bootmnt && test -f /run/archiso/bootmnt/windows/manifest.json"
    if not check(vm.shell(probe)[0] == 0, f"[{tag}] Live ISO: the boot medium stays mounted (the embedded Windows 11 ISO is on it)",
                 "archiso's copytoram=auto copied the system to RAM and unmounted the medium"):
        note("BUG: the shipped boot entries lack copytoram=n; continuing with copytoram=n added to the kernel command line")
        vm.stop()
        vm = boot_live(extra="copytoram=n", disk_gib=profile["disk_gib"])
        check(vm.shell(probe)[0] == 0, f"[{tag}] Live ISO: with copytoram=n the medium and the Windows ISO are there")
    w = Wizard(vm)
    w.prefix = f"{profile['tag']}-"
    start_wizard(vm)
    try:
        reached = False
        try:
            reached = scenario(vm, w)
        except Exception as error:  # noqa: BLE001 - keep what we saw
            check(False, f"[{tag}] the wizard scenario crashed: {type(error).__name__}: {error}")
            import traceback

            traceback.print_exc()
        if reached and os.environ.get("TUI_STOP_AT_SUMMARY") == "1":
            note("TUI_STOP_AT_SUMMARY=1: the install was not started")
        elif reached:
            if scenario_install(vm, w):
                scenario_reboot(vm, w)
        else:
            w.snapshot("stopped-here")
            print(vm.screen_text())
    finally:
        vm.stop()
        (WORK / f"tui-console-{profile['tag']}.log").write_bytes(bytes(vm.raw))
    if os.environ.get("TUI_STOP_AT_SUMMARY") != "1":
        scenario_inspect(WORK / "tui-target.img", profile)
        scenario_firmware_vars(profile)


def run() -> int:
    SCREENS.mkdir(parents=True, exist_ok=True)
    for stale in SCREENS.glob("*.txt"):
        stale.unlink()
    run_walk(PROFILE_A, scenario_wizard)
    if os.environ.get("TUI_SECOND_WALK", "1") == "1":
        run_walk(PROFILE_B, scenario_wizard_b)

    failed = [what for ok, what in RESULTS if not ok]
    print(f"\n##### {len(RESULTS) - len(failed)} checks passed, {len(failed)} failed, {len(OBSERVATIONS)} observations")
    for what in failed:
        print(f"  FAILED: {what}")
    for text in OBSERVATIONS:
        print(f"  NOTE: {text}")
    return 1 if failed else 0


def quick_pass(extra: str, label: str, windows_expected: bool) -> None:
    """Boot the ISO as a plain disk with ITS OWN kernel command line (+ `extra`, normally nothing), then check
    what depends on the live medium: the Welcome line about the included Windows and the Disk screen's refusal."""
    log(f"Quick pass ({label}): boot with the ISO's own boot entry" + (f" + '{extra}'" if extra else ""))
    vm = boot_live(extra=extra)
    w = Wizard(vm)
    try:
        cmdline = vm.shell("cat /proc/cmdline")[1].strip().splitlines()[-1]
        print(f"  /proc/cmdline: {cmdline}")
        mounted = vm.shell("mountpoint -q /run/archiso/bootmnt")[0] == 0
        check(mounted == windows_expected, f"{label}: /run/archiso/bootmnt is {'mounted' if windows_expected else 'not mounted'}")
        if windows_expected:
            check(vm.shell("test -f /run/archiso/bootmnt/windows/manifest.json")[0] == 0,
                  f"{label}: the embedded Windows ISO manifest is reachable")
        start_wizard(vm)
        check(w.wait_step(1, 60), f"{label}: the wizard starts")
        w.snapshot(f"{label}-welcome")
        check(w.has(r"Windows 11 image\s+Included in this installer") == windows_expected,
              f"{label}: Welcome " + ("shows 'Windows 11 image: Included in this installer'" if windows_expected
                                      else "does not claim an included Windows image"))
        check(w.next(2) and w.next(3) and w.next(4), f"{label}: Welcome -> Disk")
        w.snapshot(f"{label}-disk")
        check(w.wait(r"/dev/vdb.*LIVE MEDIUM", 10), f"{label}: the ISO disk (vdb) is marked LIVE MEDIUM")
        vm.keys("down")
        check(w.has(r"Selected: /dev/vdb"), f"{label}: the cursor can be moved onto the live medium")
        w.press("Next", expect_change=False)
        check(w.step() == 4 and "started from" in w.error(), f"{label}: the Disk screen refuses the live medium", w.error())
        w.snapshot(f"{label}-disk-refused")
    finally:
        vm.stop()


def quick() -> int:
    quick_pass("", "shipped-cmdline", windows_expected=True)
    if os.environ.get("TUI_QUICK_FORCE_COPYTORAM", "1") == "1":
        # The same boot as a PC with plenty of RAM would do if the entries did not forbid copytoram: the
        # medium is unmounted, yet the Disk screen must still know which disk it was started from.
        quick_pass("copytoram=y", "copytoram-forced", windows_expected=False)
    failed = [what for ok, what in RESULTS if not ok]
    print(f"\n##### {len(RESULTS) - len(failed)} checks passed, {len(failed)} failed")
    for what in failed:
        print(f"  FAILED: {what}")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "run"
    if mode == "run":
        return run()
    if mode == "quick":
        return quick()
    if mode == "daemon":
        vm = boot_live()
        print("live system ready; control socket", CTL, flush=True)
        serve(vm)
        return 0
    if mode == "ctl":
        op, request = argv[2], {"op": argv[2]}
        if op == "keys":
            request["names"] = argv[3:]
        elif op == "type":
            request["text"] = " ".join(argv[3:])
        elif op == "py":
            request["code"] = " ".join(argv[3:])
        reply = ctl(request)
        print(reply.get("screen") or reply.get("out") or "")
        return 0
    raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
