#!/usr/bin/env python3
"""Boot-smoke test of the built ISO in QEMU (KVM). Runs inside the builder container.

    smoke-live.py uefi   ISO SHOTDIR   boot through OVMF + systemd-boot (the real boot path,
                                       Secure Boot OFF: the live ISO is not signed) and save
                                       screenshots of the VM display as PNG files
    smoke-live.py serial ISO           boot the ISO's own kernel directly with a serial console,
                                       drive the live root shell and run checks
    smoke-live.py usb    ISO           boot the ISO as a virtio DISK (a USB stick as the kernel sees it)
                                       with the command line of its UEFI entry, and check that the boot
                                       medium stays mounted; a control boot shows the bug without the fix

Needs: qemu-system-x86, edk2-ovmf, python (pacman -S qemu-system-x86 edk2-ovmf).
"""

from __future__ import annotations

import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

OVMF = Path("/usr/share/edk2/x64")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][0-9A-B]|\r")


def ppm_to_png(ppm: Path, png: Path) -> None:
    data = ppm.read_bytes()
    match = re.match(rb"P6\s+(\d+)\s+(\d+)\s+(\d+)\s", data)
    if not match:
        raise ValueError("not a P6 ppm")
    width, height = int(match.group(1)), int(match.group(2))
    pixels = data[match.end():]
    raw = b"".join(b"\x00" + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    png.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def connect(path: str, timeout: float = 30.0) -> socket.socket:
    end = time.time() + timeout
    while True:
        try:
            sock = socket.socket(socket.AF_UNIX)
            sock.connect(path)
            return sock
        except OSError:
            if time.time() > end:
                raise
            time.sleep(0.3)


def monitor(sock: socket.socket, command: str) -> None:
    sock.sendall(command.encode() + b"\n")
    time.sleep(0.5)
    sock.settimeout(0.2)
    try:
        sock.recv(65536)
    except OSError:
        pass


def uefi(iso: str, shots: Path) -> int:
    shots.mkdir(parents=True, exist_ok=True)
    vars_fd = Path("/tmp/vars.fd")
    shutil.copy(OVMF / "OVMF_VARS.4m.fd", vars_fd)
    serial_log = Path("/tmp/uefi-serial.log")
    qemu = subprocess.Popen([
        "qemu-system-x86_64", "-machine", "q35", "-accel", "kvm", "-cpu", "host", "-smp", "2", "-m", "3072",
        "-display", "none", "-vga", "std",
        "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={OVMF / 'OVMF_CODE.4m.fd'}",
        "-drive", f"if=pflash,format=raw,unit=1,file={vars_fd}",
        "-cdrom", iso, "-boot", "d",
        "-serial", f"file:{serial_log}", "-monitor", "unix:/tmp/mon.sock,server=on,wait=off",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    mon = connect("/tmp/mon.sock")
    started = time.time()
    for label, at in (("menu", 8), ("booting", 25), ("live", 70), ("installer", 100)):
        time.sleep(max(0, started + at - time.time()))
        ppm = Path(f"/tmp/shot-{label}.ppm")
        monitor(mon, f"screendump {ppm}")
        if ppm.exists():
            ppm_to_png(ppm, shots / f"uefi-{label}.png")
            print(f"screenshot saved: {shots / f'uefi-{label}.png'}  (t+{int(time.time() - started)}s)")
    monitor(mon, "quit")
    qemu.wait(timeout=20)
    log = ANSI.sub("", serial_log.read_text(errors="replace")) if serial_log.exists() else ""
    print("--- OVMF / boot loader serial output (first 30 lines) ---")
    print("\n".join(log.splitlines()[:30]))
    return 0


class Console:
    """A serial console we can read from and type into."""

    def __init__(self, path: str) -> None:
        self.sock = connect(path)
        self.sock.settimeout(0.5)
        self.buffer = ""

    def read(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            self.buffer += ANSI.sub("", chunk.decode(errors="replace"))

    def wait_for(self, pattern: str, timeout: float) -> bool:
        end = time.time() + timeout
        start = len(self.buffer)
        while time.time() < end:
            self.read(1)
            if re.search(pattern, self.buffer[start:]):
                return True
        return False

    def send(self, text: str) -> None:
        self.sock.sendall(text.encode())

    def run(self, command: str, timeout: float = 60) -> str:
        """Run a command in the quiet bash set up by `quiet_shell`; return its output and exit code."""
        tag = str(int(time.time() * 1000))
        begin, end = f"@@BEGIN{tag}@@", f"@@END{tag}@@"
        start = len(self.buffer)
        # The markers are built from two halves so the typed command line never contains them whole.
        self.send(f"echo @@BEGIN{tag}'@@'; {command}\necho @@END{tag}'@@'$?\n")
        self.wait_for(re.escape(end) + r"\d+", timeout)
        out = self.buffer[start:]
        body = out.split(begin)[-1] if begin in out else out
        code = re.search(re.escape(end) + r"(\d+)", out)
        return body.split(end)[0].strip("\n") + f"\n[exit {code.group(1) if code else '?'}]"

    def quiet_shell(self) -> None:
        """The live root shell is zsh with line editing, which garbles scripted input: use plain bash."""
        self.send("exec bash --norc --noprofile\n")
        time.sleep(2)
        self.send("stty -echo; PS1=''; PS2=''; export TERM=xterm; stty rows 40 cols 120\n")
        time.sleep(1)
        self.read(1)


def serial(iso: str) -> int:
    work = Path("/tmp/live")
    work.mkdir(exist_ok=True)
    subprocess.run(["bsdtar", "-xf", iso, "-C", str(work), "arch/boot/x86_64/vmlinuz-linux",
                    "arch/boot/x86_64/initramfs-linux.img"], check=True)
    qemu = subprocess.Popen([
        "qemu-system-x86_64", "-machine", "q35", "-accel", "kvm", "-cpu", "host", "-smp", "2", "-m", "3072",
        "-display", "none", "-serial", "unix:/tmp/live.sock,server=on,wait=off", "-monitor", "none",
        "-kernel", str(work / "arch/boot/x86_64/vmlinuz-linux"),
        "-initrd", str(work / "arch/boot/x86_64/initramfs-linux.img"),
        "-append", "archisobasedir=arch archisodevice=/dev/sr0 console=ttyS0,115200",
        "-cdrom", iso,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    console = Console("/tmp/live.sock")
    failures = 0
    try:
        print("waiting for the live system (up to 240 s)...")
        if not console.wait_for(r"(login:|archiso.*[#$] )", 240):
            print("FAIL: no login prompt or shell on the serial console")
            print("--- last console output ---")
            print("\n".join(console.buffer.splitlines()[-25:]))
            return 1
        print("reached:", "a root shell" if re.search(r"archiso.*# ", console.buffer) else "a login prompt")
        if re.search(r"login:\s*$", console.buffer[-200:]):
            console.send("root\n")
            console.wait_for(r"# ", 20)
        console.quiet_shell()

        checks = [
            ("live system", "cat /etc/os-release | head -2; uname -r; grep -c . /proc/cpuinfo >/dev/null && echo cpu-ok"),
            ("launcher file", "ls -l /usr/local/bin/archinstall-launcher; head -3 /usr/local/bin/archinstall-launcher"),
            ("installer files", "ls /opt/archinstall/dualboot_installer | head -30; ls /opt/archinstall/shim"),
            ("python + textual", "PYTHONPATH=/opt/archinstall python -c 'import textual, dualboot_installer as d; print(\"textual\", textual.__version__, \"installer\", d.__version__)'"),
            ("installer --version", "PYTHONPATH=/opt/archinstall python -m dualboot_installer --version"),
            ("tools", "for t in sbctl wimlib-imagex rsync mokutil efibootmgr reflector sgdisk cryptsetup sbsign pacstrap arch-chroot; do printf '%s=' $t; command -v $t || echo MISSING; done"),
            ("network tools", "command -v iwctl ip curl"),
        ]
        for label, command in checks:
            print(f"\n## {label}\n{console.run(command, 60)}")

        print("\n## launcher without UEFI (direct kernel boot has none): must refuse cleanly")
        print(console.run("archinstall-launcher --version", 20))

        print("\n## the Textual UI in demo mode, captured through a pty")
        console.run("rm -f /tmp/ui.txt", 5)
        console.run("PYTHONPATH=/opt/archinstall script -qec 'timeout 12 python -m dualboot_installer --demo' /tmp/ui.txt >/dev/null 2>&1", 40)
        screen = console.run("sed -e 's/\\x1b\\[[0-9;?]*[A-Za-z]//g' -e 's/\\x1b[()][0-9A-B]//g' /tmp/ui.txt | tr -d '\\r' | grep -E 'ArchInstall|Welcome|step [0-9]+ of|UEFI|System checks|Next|Back' | head -12", 20)
        print(screen)
        if "Welcome" not in screen:
            print("FAIL: the Welcome screen text was not found in the captured UI output")
            failures += 1
        console.send("poweroff -f\n")
        time.sleep(3)
    finally:
        qemu.kill()
    return failures


def boot_entry(iso: str, work: Path) -> tuple[Path, Path, str]:
    """Kernel, initramfs and command line of the ISO's own UEFI boot entry (what a USB boot uses)."""
    entry = subprocess.run(["bsdtar", "-xOf", iso, "loader/entries/01-archiso-linux.conf"],
                           capture_output=True, text=True, check=True).stdout
    fields: dict[str, list[str]] = {}
    for line in entry.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            fields.setdefault(parts[0], []).append(parts[1].strip())
    kernel_path, initrd_paths, options = fields["linux"][0], fields.get("initrd", []), fields["options"][0]
    members = [kernel_path.lstrip("/")] + [p.lstrip("/") for p in initrd_paths]
    subprocess.run(["bsdtar", "-xf", iso, "-C", str(work), *members], check=True)
    initrd = work / "initrd-all"
    initrd.write_bytes(b"".join((work / p.lstrip("/")).read_bytes() for p in initrd_paths))
    return work / kernel_path.lstrip("/"), initrd, options


def usb(iso: str) -> int:
    """Boot the ISO as a plain virtio DISK (what a USB stick looks like to the kernel) and check that
    the boot medium stays mounted. A control boot with copytoram=auto must show the bug it prevents."""
    work = Path("/tmp/usb")
    work.mkdir(exist_ok=True)
    kernel, initrd, options = boot_entry(iso, work)
    print(f"boot line of the ISO's UEFI entry:\n  {options}\n")
    disk = Path("/tmp/usb-disk.iso")
    shutil.copy(iso, disk)  # the guest may write to its 'stick'; snapshot=on keeps even this copy intact
    failures = 0

    for label, cmdline in (("shipped command line", options),
                           ("CONTROL, copytoram=auto instead", re.sub(r"copytoram=\S+", "copytoram=auto", options))):
        print(f"=== boot as a virtio disk: {label}")
        qemu = subprocess.Popen([
            "qemu-system-x86_64", "-machine", "q35", "-accel", "kvm", "-cpu", "host", "-smp", "2", "-m", "4096",
            "-display", "none", "-serial", "unix:/tmp/usb.sock,server=on,wait=off", "-monitor", "none",
            "-kernel", str(kernel), "-initrd", str(initrd),
            "-append", f"{cmdline} console=ttyS0,115200",
            "-drive", f"file={disk},if=none,id=stick,format=raw,snapshot=on",
            "-device", "virtio-blk-pci,drive=stick",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            time.sleep(1.5)
            if qemu.poll() is not None:
                error = (qemu.stderr.read().decode(errors="replace") if qemu.stderr else "").strip()
                print(f"FAIL: QEMU exited at once (code {qemu.returncode}): {error[:600]}")
                failures += 1
                continue
            console = Console("/tmp/usb.sock")
            if not console.wait_for(r"(login:|archiso.*[#$] )", 240):
                print("FAIL: the live system did not come up\n" + "\n".join(console.buffer.splitlines()[-15:]))
                failures += 1
                continue
            if re.search(r"login:\s*$", console.buffer[-200:]):
                console.send("root\n")
                console.wait_for(r"# ", 20)
            console.quiet_shell()
            mounted = console.run("findmnt -no SOURCE,FSTYPE /run/archiso/bootmnt", 20)
            listing = console.run("ls /run/archiso/bootmnt 2>&1", 20)
            disk_name = console.run(
                "PYTHONPATH=/opt/archinstall python -c "
                "'from dualboot_installer.system.live import LiveSystem; print(LiveSystem()._live_medium_disk())'", 40)
            flags = console.run(
                "PYTHONPATH=/opt/archinstall python -c "
                "'from dualboot_installer.system.live import LiveSystem; "
                "print([(d.path, d.is_live_medium) for d in LiveSystem().disks()])'", 40)
            print(f"  findmnt /run/archiso/bootmnt : {mounted!r}")
            print(f"  ls /run/archiso/bootmnt      : {listing!r}")
            print(f"  _live_medium_disk()          : {disk_name!r}")
            print(f"  disks() as the installer sees: {flags!r}")
            kept = "[exit 0]" in mounted and "iso9660" in mounted and "arch" in listing and "boot" in listing
            if label.startswith("shipped"):
                if kept and "vda" in disk_name:
                    print("  OK: the boot medium is still mounted and the installer recognises it as vda")
                else:
                    print("  FAIL: the boot medium is not usable on a virtio disk"); failures += 1
            else:
                print("  control result: the medium was " + ("STILL mounted (the control did not reproduce the bug)"
                      if kept else "UNMOUNTED by archiso: this is the bug that copytoram=n prevents"))
            console.send("poweroff -f\n")
            time.sleep(3)
        finally:
            qemu.kill()
            qemu.wait()
    return failures


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] not in ("uefi", "serial", "usb"):
        print(__doc__)
        return 2
    if sys.argv[1] == "uefi":
        return uefi(sys.argv[2], Path(sys.argv[3] if len(sys.argv) > 3 else "/shots"))
    if sys.argv[1] == "usb":
        return usb(sys.argv[2])
    return serial(sys.argv[2])


if __name__ == "__main__":
    raise SystemExit(main())
