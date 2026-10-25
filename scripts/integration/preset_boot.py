#!/usr/bin/env python3
"""Boots an installed disk image (made by preset-boot.sh) and photographs the Hyprland session.

Runs inside the privileged builder container. The VM: QEMU/KVM, OVMF with Secure Boot OFF, an AHCI disk (the
initramfs of an image made in a container does not carry the NVMe or virtio-blk drivers), a virtio-vga screen
without GL (software rendering in the guest), a root shell on the serial port ttyS0, the monitor for screendumps
and key presses. Everything goes to /screens/<preset>/: PNG screenshots (named with the seconds since the
power-on), evidence-*.txt from inside the guest, RESULT.txt.

    preset_boot.py <preset>      env: BOOT_SECONDS (default 180), KEYS (monitor key names to send, space separated)
"""

from __future__ import annotations

import base64
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

PRESET = sys.argv[1]
SECONDS_TO_WATCH = int(os.environ.get("BOOT_SECONDS", "180"))
IMG = Path(f"/img/{PRESET}.img")
VARS = Path(f"/img/{PRESET}-vars.fd")
SHOTS = Path(os.environ.get("SCREENS_DIR", "/screens")) / PRESET
OVMF = Path("/usr/share/edk2/x64")
MON = Path(f"/tmp/boot-{PRESET}.mon")
CON = Path(f"/tmp/boot-{PRESET}.con")
RESOLUTION = os.environ.get("RESOLUTION", "1920x1080")

RESULT: list[str] = []


def say(text: str) -> None:
    print(text, flush=True)
    RESULT.append(text)


def ppm_to_png(ppm: bytes) -> tuple[bytes, float]:
    """(PNG bytes, share of non-black samples) of a binary PPM written by QEMU's screendump."""
    parts = ppm.split(b"\n", 3)
    width, height = map(int, parts[1].split())
    data = parts[3]
    rows, lit = [], 0
    for y in range(height):
        row = data[y * width * 3:(y + 1) * width * 3]
        rows.append(b"\x00" + row)
        lit += sum(1 for x in range(0, len(row), 3 * 8) if row[x:x + 3] != b"\x00\x00\x00")

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) + chunk(b"IEND", b""))
    return png, lit / max(1, height * (width // 8))


class Vm:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.t0 = 0.0
        self.raw = bytearray()
        self.lock = threading.Lock()
        self.cursor = 0
        self.console: socket.socket | None = None

    def start(self) -> None:
        for path in (MON, CON):
            path.unlink(missing_ok=True)
        VARS.write_bytes((OVMF / "OVMF_VARS.4m.fd").read_bytes())
        x, y = RESOLUTION.split("x")
        args = ["qemu-system-x86_64", "-name", f"boot-{PRESET}", "-machine", "q35,smm=on", "-accel", "kvm",
                "-cpu", "host", "-smp", "4", "-m", "4096",
                "-global", "driver=cfi.pflash01,property=secure,value=on",
                "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={OVMF}/OVMF_CODE.4m.fd",
                "-drive", f"if=pflash,format=raw,unit=1,file={VARS}",
                "-drive", f"file={IMG},format=raw,if=none,id=d0,cache=none,aio=native",
                "-device", "ich9-ahci,id=ahci", "-device", "ide-hd,drive=d0,bus=ahci.0,bootindex=0",
                "-device", "qemu-xhci", "-device", "usb-kbd", "-device", "usb-tablet",
                "-nic", "user,model=virtio-net-pci", "-device", "virtio-rng-pci",
                *(["-vga", "std"] if os.environ.get("VGA") == "std" else ["-device", f"virtio-vga,xres={x},yres={y}"]),
                "-display", "none",
                "-serial", f"unix:{CON},server,nowait",
                "-monitor", f"unix:{MON},server,nowait", "-no-reboot"]
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.t0 = time.time()
        for _ in range(100):
            if CON.exists() and MON.exists():
                break
            time.sleep(0.1)
        for _ in range(100):  # the socket file exists a moment before QEMU listens on it
            if self.proc.poll() is not None:
                raise SystemExit("QEMU stopped at once:\n" + (self.proc.stdout.read().decode(errors="replace") if self.proc.stdout else ""))
            self.console = socket.socket(socket.AF_UNIX)
            try:
                self.console.connect(str(CON))
                break
            except OSError:
                self.console.close()
                time.sleep(0.2)
        else:
            raise SystemExit("cannot connect to the serial console of the VM")
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.console
        while True:
            try:
                chunk = self.console.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            with self.lock:
                self.raw += chunk

    def elapsed(self) -> int:
        return int(time.time() - self.t0)

    def text(self) -> str:
        with self.lock:
            return re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]", b"", bytes(self.raw)).replace(b"\r", b"").decode("utf-8", "replace")

    def send(self, data: str) -> None:
        assert self.console
        self.console.sendall(data.encode())

    def monitor(self, *commands: str) -> None:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.connect(str(MON))
            sock.settimeout(3)
            try:
                sock.recv(4096)
            except OSError:
                pass
            for command in commands:
                sock.sendall(command.encode() + b"\n")
                time.sleep(0.4)
                try:
                    sock.recv(4096)
                except OSError:
                    pass

    def shot(self, name: str) -> float:
        ppm = Path(f"/tmp/{PRESET}-{name}.ppm")
        ppm.unlink(missing_ok=True)
        self.monitor(f"screendump {ppm}")
        for _ in range(20):
            if ppm.exists() and ppm.stat().st_size > 1000:
                break
            time.sleep(0.25)
        if not ppm.exists():
            return -1.0
        time.sleep(0.5)
        png, lit = ppm_to_png(ppm.read_bytes())
        (SHOTS / f"{name}.png").write_bytes(png)
        ppm.unlink(missing_ok=True)
        return lit

    def wait_for(self, pattern: str, timeout: float) -> bool:
        regex, deadline = re.compile(pattern), time.time() + timeout
        while time.time() < deadline:
            match = regex.search(self.text(), self.cursor)
            if match:
                self.cursor = match.end()
                return True
            if self.proc and self.proc.poll() is not None:
                return False
            time.sleep(0.5)
        return False

    def shell(self, command: str, timeout: float = 90) -> tuple[int, str]:
        marker = f"__RC{int(time.time() * 1000) % 10**9}__"
        start = len(self.text())
        self.send(f"{command}\nprintf '\\n{marker}%s\\n' \"$?\"\n")
        regex, deadline = re.compile(rf"{marker}(\d+)"), time.time() + timeout
        while time.time() < deadline:
            text = self.text()
            match = regex.search(text, start)
            if match:
                return int(match.group(1)), text[start:match.start()].strip("\n")
            time.sleep(0.4)
        return 255, self.text()[start:]

    def script(self, text: str, timeout: float = 120) -> str:
        data = base64.b64encode(text.encode()).decode()
        self.shell("rm -f /tmp/s.b64 /tmp/s.sh", 20)
        for i in range(0, len(data), 800):
            self.shell(f"printf %s '{data[i:i + 800]}' >> /tmp/s.b64", 20)
        return self.shell("base64 -d /tmp/s.b64 > /tmp/s.sh && bash /tmp/s.sh 2>&1", timeout)[1]

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


EVIDENCE = r"""
U=alice; XR=/run/user/$(id -u $U); SIG=$(ls $XR/hypr 2>/dev/null | head -1)
as() { su $U -c "XDG_RUNTIME_DIR=$XR HYPRLAND_INSTANCE_SIGNATURE=$SIG WAYLAND_DISPLAY=wayland-1 $*" 2>&1; }
echo "== uptime / sddm / sessions"; uptime -p; systemctl is-active sddm; loginctl list-sessions --no-legend
echo "== processes"; ps -eo pid,user,etime,args | grep -E 'Hyprland|quickshell|/qs |waybar|caelestia|swaync|swayosd|hypridle|nm-applet|kitty|wofi|rofi|eqsh|macshell' | grep -v -E 'grep|ps -eo' | cut -c1-170
echo "== hyprland instance dir"; ls $XR/hypr 2>&1 | head -3
echo "== kb_layout"; as "hyprctl getoption input:kb_layout" | head -4
echo "== monitors"; as "hyprctl monitors" | head -12
echo "== clients"; as "hyprctl clients" | grep -E 'class|title|workspace' | head -24
echo "== layers"; as "hyprctl layers" | grep -E 'Layer level|namespace' | head -30
echo "== hyprland log"; tail -n 40 $XR/hypr/$SIG/hyprland.log 2>&1 | cut -c1-200
echo "== journal (warnings and worse)"; journalctl -b -p warning --no-pager 2>&1 | tail -n 30 | cut -c1-220
echo "== quickshell logs"; find $XR/quickshell /home/$U/.cache/quickshell -name '*.log' 2>/dev/null | head -5 | while read f; do echo "-- $f"; tail -n 25 "$f" | cut -c1-200; done
"""


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    if not IMG.exists():
        raise SystemExit(f"no image at {IMG}: run preset-boot.sh {PRESET} install first")
    vm = Vm()
    vm.start()
    say(f"VM started (preset {PRESET}, {RESOLUTION}, 4 GiB, 4 vCPUs, AHCI disk, virtio-vga without GL)")
    lit_by_time: dict[int, float] = {}
    shell_ready = False
    evidence_done = 0
    keys_done = False
    next_shot = 15
    try:
        while vm.elapsed() < SECONDS_TO_WATCH:
            now = vm.elapsed()
            if vm.proc and vm.proc.poll() is not None:
                say(f"the VM stopped by itself at {now}s")
                break
            if now >= next_shot:
                lit_by_time[now] = vm.shot(f"boot-{now:04d}s")
                next_shot += 15
            if not shell_ready and "login:" not in vm.text()[-200:] and re.search(r"\[root@[^\]]+\][#$]", vm.text()):
                vm.send("exec bash --norc --noprofile\n")
                time.sleep(1.5)
                vm.send("export PS1='' TERM=dumb LC_ALL=C; stty -echo cols 200\n")
                time.sleep(1)
                shell_ready = vm.shell("true", 20)[0] == 0
                say(f"root shell on the serial port after {now}s: {shell_ready}")
            if shell_ready and evidence_done == 0 and now >= SECONDS_TO_WATCH * 0.5:
                out = vm.script(EVIDENCE, 150)
                (SHOTS / "evidence-1.txt").write_text(out + "\n", encoding="utf-8")
                evidence_done = 1
                say(f"evidence 1 collected at {vm.elapsed()}s ({len(out)} characters)")
            if shell_ready and evidence_done == 1 and not keys_done and vm.elapsed() >= SECONDS_TO_WATCH * 0.75:
                for combo in os.environ.get("KEYS", "meta_l-q").split():
                    vm.monitor(f"sendkey {combo}")
                    time.sleep(6)
                    lit_by_time[vm.elapsed()] = vm.shot(f"after-keys-{combo}-{vm.elapsed():04d}s")
                keys_done = True
            time.sleep(1)
        if shell_ready:
            out = vm.script(EVIDENCE, 150)
            (SHOTS / "evidence-2.txt").write_text(out + "\n", encoding="utf-8")
            say(f"evidence 2 collected at {vm.elapsed()}s")
        lit_by_time[vm.elapsed()] = vm.shot(f"final-{vm.elapsed():04d}s")
        # What the last 4 KB of the serial console said, in case the shell never came
        (SHOTS / "serial-tail.txt").write_text(vm.text()[-6000:], encoding="utf-8")
    finally:
        vm.stop()
    say("lit share of each screenshot (0 = black screen): " + ", ".join(f"{t}s:{lit:.2f}" for t, lit in lit_by_time.items()))
    (SHOTS / "RESULT-auto.txt").write_text("\n".join(RESULT) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
