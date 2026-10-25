#!/usr/bin/env python3
"""Drives QEMU/KVM + OVMF VMs to check what container tests cannot: the installer's writes to
REAL UEFI firmware (efivars, boot entries, Secure Boot keys, MOK).

Runs inside the privileged builder container (see live-test.sh / scripts/live-test.ps1).

    live-vm.py a-sbctl   # Setup Mode -> sbctl enrolls keys -> installed disk boots under Secure Boot
    live-vm.py b-shim    # PK present -> sbctl refused -> shim + mokutil --import -> MokManager -> login
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

OVMF = Path("/usr/share/edk2/x64")
# A Docker volume in practice (see scripts/live-test.ps1): the disk and the firmware variables
# survive between runs, so a failed boot can be debugged without reinstalling.
WORK = Path(os.environ.get("LIVE_WORK", "/tmp/live"))
SCREENS = Path(os.environ.get("SCREENS_DIR", "/screens"))
GUEST = "/root/t"  # where the snapshot tar is unpacked inside the live system

_ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]|\x1b[=>]|\x1b\]\d+;[^\x07\x1b]*(?:\x07|\x1b\\)")
RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, what: str, detail: str = "") -> bool:
    RESULTS.append((ok, what))
    print(f"  {'ok  ' if ok else 'FAIL'}  {what}" + (f"   [{detail}]" if detail and not ok else ""), flush=True)
    return ok


def log(message: str) -> None:
    print(f"\n##### {message}", flush=True)


# -- PPM -> PNG ---------------------------------------------------------------------------------


def ppm_to_png(ppm: Path, png: Path) -> None:
    data = ppm.read_bytes()
    tokens, pos = [], 0
    while len(tokens) < 4:  # P6 width height maxval
        while data[pos:pos + 1].isspace():
            pos += 1
        end = pos
        while not data[end:end + 1].isspace():
            end += 1
        tokens.append(data[pos:end])
        pos = end
    pos += 1
    width, height = int(tokens[1]), int(tokens[2])
    raw = data[pos:pos + width * height * 3]
    rows = b"".join(b"\x00" + raw[y * width * 3:(y + 1) * width * 3] for y in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return len(body).to_bytes(4, "big") + kind + body + crc.to_bytes(4, "big")

    png.write_bytes(b"\x89PNG\r\n\x1a\n"
                    + chunk(b"IHDR", width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 2, 0, 0, 0]))
                    + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


# -- the VM ---------------------------------------------------------------------------------------

KEYS = {"\n": "ret", " ": "spc", "-": "minus", ".": "dot", "/": "slash", "_": "shift-minus", ":": "shift-semicolon"}


class Vm:
    """A QEMU VM whose serial console is read by a thread and driven with send()/wait_for()."""

    def __init__(self, name: str, vars_file: Path, drives: list[Path], *, live: bool = False,
                 reboot: bool = False, memory: int = 4096, tpm: bool = False, cpus: int = 4,
                 disk_bus: str = "virtio", nic: str = "virtio-net-pci", tpm_dir: Path | None = None,
                 tpm_device: str = "tpm-tis", iso: Path | None = None,
                 kernel: Path | None = None, initrd: Path | None = None, boot_index: bool = True,
                 cpu: str = "host", extra: list[str] | None = None) -> None:
        self.name, self.vars_file, self.drives, self.live = name, vars_file, drives, live
        self.reboot, self.memory, self.tpm, self.cpus = reboot, memory, tpm, cpus
        # disk_bus applies to the FIRST drive: Windows PE has no virtio-blk driver, but boots from NVMe.
        self.disk_bus, self.nic, self.tpm_dir, self.tpm_device = disk_bus, nic, tpm_dir, tpm_device
        self.iso = iso or WORK / "arch.iso"
        self.extra = extra or []  # more QEMU arguments, e.g. -acpitable file=msdm.bin
        self.cpu = cpu  # e.g. "host,-vmx": Windows 11 must not find virtualization to build VBS on, under nested KVM
        self.boot_index = boot_index  # False: like a real PC, the firmware keeps the boot order it has in NVRAM
        self.kernel = kernel or WORK / "vmlinuz-linux"
        self.initrd = initrd or WORK / "initramfs-linux.img"
        self.swtpm: subprocess.Popen | None = None
        self.monitor_path = WORK / f"{name}.mon"
        self.log_path = WORK / f"{name}.serial.log"
        self.buffer = bytearray()
        self.cursor = 0
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.monitor_path.unlink(missing_ok=True)
        tpm_args: list[str] = []
        if self.tpm:  # a TPM 2.0 in software: some GRUB code only runs when a TPM is present
            state = self.tpm_dir or WORK / f"{self.name}.tpm"
            if self.tpm_dir is None:  # a persistent state directory is kept: Windows keeps using the same TPM
                shutil.rmtree(state, ignore_errors=True)
            state.mkdir(parents=True, exist_ok=True)
            sock = WORK / f"{self.name}.tpm.sock"
            sock.unlink(missing_ok=True)
            self.swtpm = subprocess.Popen(["swtpm", "socket", "--tpm2", "--tpmstate", f"dir={state}",
                                           "--ctrl", f"type=unixio,path={sock}"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                if sock.exists():
                    break
                time.sleep(0.1)
            tpm_args = ["-chardev", f"socket,id=chrtpm,path={sock}", "-tpmdev", "emulator,id=tpm0,chardev=chrtpm",
                        "-device", f"{self.tpm_device},tpmdev=tpm0"]
        args = ["qemu-system-x86_64", "-name", self.name, "-machine", "q35,smm=on", "-accel", "kvm", "-cpu", self.cpu,
                "-smp", str(self.cpus), "-m", str(self.memory),
                "-global", "driver=cfi.pflash01,property=secure,value=on",
                "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={OVMF}/OVMF_CODE.secboot.4m.fd",
                "-drive", f"if=pflash,format=raw,unit=1,file={self.vars_file}"]
        for number, drive in enumerate(self.drives):
            # The live system is started by its kernel, so only the installed disk needs a boot index.
            boot = f",bootindex={number}" if not self.live and number == 0 and self.boot_index else ""
            device = (f"nvme,drive=d{number},serial=ARCHE2E{number}{boot}"
                      if number == 0 and self.disk_bus == "nvme" else f"virtio-blk-pci,drive=d{number}{boot}")
            args += ["-drive", f"file={drive},format=raw,if=none,id=d{number}", "-device", device]
        if self.live:
            args += ["-cdrom", str(self.iso), "-kernel", str(self.kernel),
                     "-initrd", str(self.initrd),
                     "-append", "archisobasedir=arch archisodevice=/dev/sr0 console=ttyS0,115200 cow_spacesize=2G"]
        args += tpm_args + self.extra
        args += ["-nic", f"user,model={self.nic}", "-device", "virtio-rng-pci", "-vga", "std", "-display", "none",
                 "-serial", "stdio", "-monitor", f"unix:{self.monitor_path},server,nowait"]
        if not self.reboot:
            args.append("-no-reboot")
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        threading.Thread(target=self._read, daemon=True).start()
        for _ in range(100):  # the monitor socket appears a moment after start
            if self.monitor_path.exists():
                break
            time.sleep(0.1)

    def _read(self) -> None:
        assert self.proc and self.proc.stdout
        with self.log_path.open("wb") as raw_log:
            while True:
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    return
                raw_log.write(chunk)
                raw_log.flush()
                with self.lock:
                    self.buffer += chunk

    # -- serial ------------------------------------------------------------------------------

    def text(self) -> str:
        with self.lock:
            return _ANSI.sub(b"", bytes(self.buffer)).replace(b"\r", b"").decode("utf-8", "replace")

    def tail(self, lines: int = 40) -> str:
        """The last non-empty lines of the console, for error messages."""
        kept = [line.rstrip() for line in self.text().splitlines() if line.strip()]
        return "\n".join("      | " + line[:180] for line in kept[-lines:])

    def send(self, data: str) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(data.encode())
        self.proc.stdin.flush()

    def wait_for(self, pattern: str, timeout: float = 120, *, from_start: bool = False) -> re.Match | None:
        """Wait for a regex in the console output produced since the last successful wait."""
        deadline = time.time() + timeout
        regex = re.compile(pattern)
        while time.time() < deadline:
            text = self.text()
            start = 0 if from_start else self.cursor
            match = regex.search(text, start)
            if match:
                self.cursor = match.end()
                return match
            if self.proc and self.proc.poll() is not None:
                return None
            time.sleep(0.5)
        return None

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
        if self.swtpm and self.swtpm.poll() is None:
            self.swtpm.kill()
            self.swtpm.wait()

    # -- shell in the guest -------------------------------------------------------------------------

    def login(self, prompt: str, user: str = "root", password: str | None = None, timeout: float = 300) -> bool:
        if not self.wait_for(prompt, timeout):
            return False
        self.send(user + "\n")
        if password is not None:
            if not self.wait_for(r"(?:[Pp]assword|Mot de passe)\s*:", 30):
                return False
            self.send(password + "\n")
        time.sleep(3)
        # A plain shell without echo makes the output of run() easy to read.
        self.send("exec bash --norc --noprofile\n")
        time.sleep(1.5)
        self.send("export PS1='' TERM=dumb LC_ALL=C; stty -echo; stty cols 200\n")
        time.sleep(1)
        return self.run("true", 30)[0] == 0

    def run(self, command: str, timeout: float = 120) -> tuple[int, str]:
        """Run a shell command in the guest. Returns (exit code, output)."""
        marker = f"__RC{int(time.time() * 1000) % 10**9}__"
        start = self.cursor
        self.send(f"{command}\nprintf '\\n{marker}%s\\n' \"$?\"\n")
        match = self.wait_for(rf"{marker}(\d+)", timeout)
        if match is None:
            return 255, self.text()[start:]
        output = self.text()[start:match.start()]
        return int(match.group(1)), output.strip("\n")

    # -- QEMU monitor ------------------------------------------------------------------------------

    def monitor(self, command: str, wait: float = 1.0) -> str:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(5)
            sock.connect(str(self.monitor_path))
            time.sleep(0.2)
            try:
                sock.recv(65536)  # greeting
            except TimeoutError:
                pass
            sock.sendall((command + "\n").encode())
            time.sleep(wait)
            sock.settimeout(1)
            reply = b""
            try:
                while True:
                    part = sock.recv(65536)
                    if not part:
                        break
                    reply += part
            except TimeoutError:
                pass
            return _ANSI.sub(b"", reply).decode("utf-8", "replace")

    def screendump(self, name: str) -> Path:
        SCREENS.mkdir(parents=True, exist_ok=True)
        ppm = WORK / f"{name}.ppm"
        self.monitor(f"screendump {ppm}", wait=1.5)
        png = SCREENS / f"{name}.png"
        ppm_to_png(ppm, png)
        return png

    def key(self, name: str, pause: float = 0.6) -> None:
        self.monitor(f"sendkey {name}", wait=0.2)
        time.sleep(pause)

    def type_text(self, text: str) -> None:
        for char in text:
            if char in KEYS:
                self.key(KEYS[char], 0.15)
            elif char.isupper():
                self.key(f"shift-{char.lower()}", 0.15)
            else:
                self.key(char, 0.15)


# -- helpers ----------------------------------------------------------------------------------------


def fresh_vars(name: str, *flags: str) -> Path:
    """A copy of the empty OVMF variable store (Setup Mode), optionally with keys enrolled."""
    target = WORK / f"vars-{name}.fd"
    template = OVMF / "OVMF_VARS.4m.fd"
    if flags:
        subprocess.run(["virt-fw-vars", "--input", str(template), "--output", str(target), *flags],
                       check=True, capture_output=True)
    else:
        shutil.copy(template, target)
    return target


def new_disk(name: str, size: str = "100G") -> Path:
    path = WORK / f"{name}.img"
    path.unlink(missing_ok=True)
    subprocess.run(["truncate", "-s", size, str(path)], check=True)
    return path


def vars_summary(vars_file: Path) -> str:
    """The variable store as text (virt-fw-dump shows names and sizes, not certificates)."""
    result = subprocess.run(["virt-fw-dump", "--input", str(vars_file)], capture_output=True, text=True)
    return result.stdout + result.stderr


def vars_certs(vars_file: Path) -> dict[str, list[str]]:
    """Subjects of the X.509 certificates held by PK, KEK and db, decoded from the variable store."""
    dump = WORK / "vars-dump.json"
    subprocess.run(["virt-fw-vars", "--input", str(vars_file), "--output-json", str(dump)],
                   check=True, capture_output=True)
    import json
    found: dict[str, list[str]] = {}
    for variable in json.loads(dump.read_text())["variables"]:
        if variable["name"] not in ("PK", "KEK", "db"):
            continue
        data, position, subjects = bytes.fromhex(variable["data"]), 0, []
        while position + 28 <= len(data):  # a series of EFI_SIGNATURE_LISTs
            list_size, header_size, entry_size = struct.unpack_from("<III", data, position + 16)
            cursor, end = position + 28 + header_size, position + list_size
            while cursor + entry_size <= end:
                der = data[cursor + 16:cursor + entry_size]  # 16 bytes of owner GUID, then the certificate
                shown = subprocess.run(["openssl", "x509", "-inform", "DER", "-noout", "-subject"],
                                       input=der, capture_output=True)
                subjects.append(shown.stdout.decode().strip().removeprefix("subject=").strip() or "(not X.509)")
                cursor += entry_size
            position = end
        found[variable["name"]] = subjects
    return found


def boot_live(vars_file: Path, disk: Path, name: str) -> Vm | None:
    """Boot the official Arch ISO (kernel booted directly, so there is a serial console)."""
    vm = Vm(name, vars_file, [disk, WORK / "snap.tar"], live=True)
    vm.start()
    if not vm.login(r"archiso login:"):
        print(vm.text()[-1500:])
        check(False, "the live system boots and gives a root shell on the serial console")
        vm.stop()
        return None
    check(True, "the live system boots and gives a root shell on the serial console")
    code, _ = vm.run(f"mkdir -p {GUEST} && tar -xf /dev/vdb -C {GUEST}", 60)
    check(code == 0, "the installer snapshot and scripts are unpacked in the live system")
    return vm


def prepare_live(vm: Vm) -> bool:
    code, out = vm.run(f"bash {GUEST}/live-guest-prepare.sh 2>&1 | tail -n 8", 600)
    print("      " + out.replace("\n", "\n      "))
    return check(code == 0 and "PREPARE-OK" in out, "sbctl, sbsigntools, mokutil, wimlib installed in the live system")


def run_installer(vm: Vm, answers: str, timeout: float = 1500) -> tuple[int, str]:
    code, out = vm.run(f"bash {GUEST}/live-guest-install.sh {GUEST}/{answers}", timeout)
    return code, out


def section(output: str, start: str, end: str) -> str:
    """The lines of `output` between two marker lines (markers excluded), indented for the log."""
    lines, keep = [], False
    for line in output.splitlines():
        if line.startswith(end):
            break
        if keep:
            lines.append("      " + line[:200])
        if line.startswith(start):
            keep = True
    return "\n".join(lines)


def boot_entry_cycle(vm: Vm, loader: str) -> None:
    """What the installer does on a SECOND install, with the real efibootmgr: find, delete, recreate."""
    code, out = vm.run(f"bash {GUEST}/live-guest-bootentry.sh '{loader}' 2>&1", 60)
    print("      " + out.replace("\n", "\n      "))
    found = re.search(r"regex finds: \['([0-9A-Fa-f]{4})'\]", out)
    check(bool(found), "the installer's regex finds the ArchInstall entry in the real `efibootmgr` output", out[-500:])
    check("delete" in out and re.search(r"delete [0-9A-Fa-f]{4}: exit 0", out) is not None,
          "efibootmgr --bootnum N --delete-bootnum succeeds", out[-500:])
    check("still listed after deleting: False" in out, "...and the entry is really gone afterwards")
    first = re.search(r"BootOrder: ([0-9A-Fa-f,]+)", out)
    entry = re.search(r"Boot([0-9A-Fa-f]{4})\* ArchInstall", out.split("create exit")[-1])
    check("create exit: 0" in out and bool(first and entry) and first.group(1).split(",")[0] == entry.group(1),
          "recreating the entry works and it is first in the boot order again", out[-500:])


def key_lines(output: str, *patterns: str) -> str:
    wanted = re.compile("|".join(patterns), re.IGNORECASE)
    return "\n".join("      " + line[:200] for line in output.splitlines() if wanted.search(line))


# -- scenario A: sbctl in Setup Mode --------------------------------------------------------------------


def scenario_a() -> None:
    log("A. sbctl: Setup Mode -> keys enrolled by the installer -> installed disk boots under Secure Boot")
    vars_file, disk = fresh_vars("a"), new_disk("a")
    if install_a(vars_file, disk):
        # GRUB without shim only starts Linux under Secure Boot when its tpm module has a TPM.
        boot_installed_a(vars_file, disk, tpm=True)
        boot_without_tpm(vars_file, disk)


def boot_without_tpm(vars_file: Path, disk: Path) -> None:
    """KNOWN LIMITATION: the same disk and keys, but no TPM in the machine."""
    log("A3. the SAME installed disk on a machine WITHOUT a TPM (known limitation of the sbctl path)")
    vm = Vm("a-notpm", vars_file, [disk])
    vm.start()
    try:
        refused = vm.wait_for(r"verification requested but nobody cares", 120) is not None
        time.sleep(3)
        print(vm.tail(8))
        if vm.proc and vm.proc.poll() is None:
            print(f"      screendump: {vm.screendump('a-notpm-refused')}")
        check(refused, "KNOWN LIMITATION confirmed: without a TPM, GRUB refuses to load the kernel under Secure Boot")
        check("Linux version" not in vm.text(), "...and Linux does not start")
    finally:
        vm.stop()


def scenario_a_boot() -> None:
    """Only the second half of A, on the disk and variables left by the last a-sbctl run."""
    log("A2 (alone). boot the persisted installed disk with its persisted variable store")
    vars_file, disk = WORK / "vars-a.fd", WORK / "a.img"
    if not (vars_file.exists() and disk.exists()):
        print("  nothing persisted yet: run a-sbctl first")
        return
    boot_installed_a(vars_file, disk)  # no TPM here: expect the refusal, see boot_without_tpm


def scenario_a_boot_chainload() -> None:
    """No TPM: GRUB's own `linux` command is refused, but the signed kernel can be started with
    `chainloader`, which uses the firmware's signature check instead of GRUB's."""
    log("A3. no TPM: start the signed kernel from the GRUB command line with chainloader")
    vars_file, disk = WORK / "vars-a.fd", WORK / "a.img"
    vm = Vm("a-chain", vars_file, [disk])
    vm.start()
    try:
        if not vm.wait_for(r"GNU GRUB", 120):
            print(vm.tail(20))
            check(False, "GRUB menu appears")
            return
        time.sleep(1.5)
        vm.send("c")
        check(vm.wait_for(r"grub> ", 30) is not None, "the GRUB command line opens on the serial console")
        # The EFI stub of the kernel reads initrd=... from its load options; the path uses backslashes.
        for command in ("set root=(hd0,gpt1)",
                        r"chainloader /vmlinuz-linux root=/dev/vda3 rw console=ttyS0,115200 initrd=\initramfs-linux.img",
                        "boot"):
            vm.send(command + "\n")
            time.sleep(2)
        ok = vm.wait_for(r"Linux version", 120) is not None
        print(vm.tail(14))
        check(ok, "the kernel started through chainloader under Secure Boot WITHOUT a TPM")
        check(vm.wait_for(r"testbox login:", 240) is not None, "...and the system reaches the login prompt")
    finally:
        vm.stop()


def without_boot_entries(source: Path, name: str) -> Path:
    """The same firmware variables minus every boot entry: what a NVRAM reset or a BIOS update does.
    The firmware then falls back to \\EFI\\BOOT\\BOOTX64.EFI on the disk."""
    target = WORK / f"vars-{name}.fd"
    subprocess.run(["virt-fw-vars", "--input", str(source), "--output", str(target), "--delete", "BootOrder",
                    "--delete", "Boot0002"], check=True, capture_output=True)
    return target


def scenario_a_fallback() -> None:
    """sbctl path: boot the installed disk with no boot entry at all (fallback path), with a TPM."""
    log("A4. sbctl: the firmware lost its boot entries, so it uses the fallback path \\EFI\\BOOT\\BOOTX64.EFI")
    source, disk = WORK / "vars-a.fd", WORK / "a.img"
    boot_installed_a(without_boot_entries(source, "a-fallback"), disk, tpm=True)


def scenario_b_fallback() -> None:
    """shim path, key already enrolled (state left by b-mok): the fallback path must be shim again."""
    log("B4. shim: no boot entry, fallback path \\EFI\\BOOT\\BOOTX64.EFI (shim) with the key already enrolled")
    source, disk = WORK / "vars-b-try.fd", WORK / "b.img"
    vars_file = without_boot_entries(source, "b-fallback")
    vm = Vm("b-fallback", vars_file, [disk])
    vm.start()
    try:
        ok = vm.login(r"testbox login:", password="root-pw", timeout=300)
        if not ok:
            print(vm.tail(30))
            print(f"      screendump: {vm.screendump('b-fallback-failed')}")
        check(ok, "with no boot entry, shim from the fallback path starts GRUB and Linux (key already enrolled)")
        if ok:
            code, out = vm.run("dmesg | grep -iE 'secure ?boot|ArchInstall' | head -4", 30)
            print("      " + out.replace("\n", "\n      "))
    finally:
        vm.stop()


def scenario_a_boot_tpm() -> None:
    """Same as a-boot, with a software TPM 2.0 in the VM."""
    vars_file, disk = WORK / "vars-a.fd", WORK / "a.img"
    boot_installed_a(vars_file, disk, tpm=True)


def install_a(vars_file: Path, disk: Path) -> bool:
    """Boot the live ISO in Setup Mode and run the installer for real. True when it ran to the end."""
    vm = boot_live(vars_file, disk, "a-live")
    if vm is None or not prepare_live(vm):
        return False
    installed = False
    try:
        code, out = vm.run(f"bash {GUEST}/live-guest-state.sh 2>&1", 60)
        print(key_lines(out, "setup", "secure", "efivars"))
        check("setup_mode=True" in out, "the live system sees the firmware in Setup Mode (SetupMode=1)", out[-300:])

        code, out = run_installer(vm, "answers-sbctl.json")
        print(key_lines(out, r"^--- \[", "ERROR", "WARNING", "Secure Boot keys"))
        print("      what the real sbctl and efibootmgr printed:")
        print(section(out, "--- what the real", "--- last 12"))
        installed = code == 0 and "INSTALLER-EXIT 0" in out
        check(installed, "the installer ran to the end with real firmware writes (sbctl)", out[-800:])

        code, out = vm.run("efibootmgr -v 2>&1", 30)
        print(key_lines(out, "ArchInstall", "BootOrder", "Boot0"))
        entry = re.search(r"Boot(\d{4})\*? ArchInstall\s+(.*)", out)
        check(bool(entry) and "grubx64.efi" in entry.group(2).lower(),
              "efibootmgr -v lists the ArchInstall entry pointing at grubx64.efi", out[-600:])
        order = re.search(r"BootOrder: ([0-9A-Fa-f,]+)", out)
        check(bool(entry and order) and order.group(1).split(",")[0] == entry.group(1),
              "ArchInstall is FIRST in the boot order", out[-600:])

        code, out = vm.run("sbctl status 2>&1", 30)
        print("      " + out.replace("\n", "\n      "))
        setup_line = next((line for line in out.splitlines() if "Setup Mode" in line), "")
        check("Disabled" in setup_line, "after the install the live system reports Setup Mode disabled (sbctl status)", out)

        boot_entry_cycle(vm, r"\EFI\ArchInstall\grubx64.efi")

        # Serial console for the installed system's GRUB and kernel (grub.cfg is not verified).
        code, out = vm.run(f"bash {GUEST}/live-guest-serial.sh 2>&1 | tail -n 5", 120)
        check(code == 0 and "SERIAL-OK" in out, "console=ttyS0 added to the installed grub.cfg", out)
        vm.run("sync; poweroff", 10)
        check(vm.wait_exit(60), "the live system powers off")
    finally:
        vm.stop()

    certs = vars_certs(vars_file)
    for name in ("PK", "KEK", "db"):
        for subject in certs.get(name, []):
            print(f"      {name:3} {subject[:150]}")
    check(certs.get("PK") == ["CN=Platform Key"], "the Platform Key is the one created by sbctl", str(certs.get("PK")))
    check("CN=Database Key" in certs.get("db", []), "db holds the signing key created by sbctl", str(certs.get("db")))
    check(any("Microsoft Windows Production PCA 2011" in s for s in certs.get("db", [])),
          "db also holds Microsoft's Windows PCA (--microsoft): Windows Boot Manager will still load", str(certs.get("db")))
    check(any("Microsoft Corporation UEFI CA 2011" in s for s in certs.get("db", [])),
          "...and Microsoft's UEFI CA (option ROMs, shim)", str(certs.get("db")))
    return installed


def boot_installed_a(vars_file: Path, disk: Path, tpm: bool = False) -> None:
    log(f"A2. the installed disk, Secure Boot firmware, the SAME variable store, nothing added by hand (tpm={tpm})")
    summary = vars_summary(vars_file)
    print("      variable store before this boot:")
    print(key_lines(summary, r"name=PK ", r"name=KEK ", r"name=db ", "name=SecureBootEnable", "name=Boot0002"))
    vm = Vm("a-installed", vars_file, [disk], tpm=tpm)
    vm.start()
    try:
        ok = vm.login(r"testbox login:", password="root-pw", timeout=300)
        if not ok:
            time.sleep(2)
            print("      serial console of the installed boot (last lines):")
            print(vm.tail(45))
            if vm.proc and vm.proc.poll() is None:
                print(f"      screendump: {vm.screendump('a-installed-failed')}")
        check(ok, "the installed system reaches the login prompt and accepts the root password")
        if ok:
            vm.screendump("a-installed-login")
            code, out = vm.run("sbctl status 2>&1", 30)
            print("      " + out.replace("\n", "\n      "))
            setup_line = next((line for line in out.splitlines() if "Setup Mode" in line), "")
            secure_line = next((line for line in out.splitlines() if "Secure Boot" in line), "")
            check("Disabled" in setup_line, "sbctl status in the installed system: Setup Mode disabled", out)
            check("Enabled" in secure_line, "sbctl status in the installed system: Secure Boot ENABLED", out)
            code, out = vm.run("od -An -tu1 -j4 -N1 /sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c", 30)
            check(out.strip() == "1", "the firmware variable SecureBoot is 1", out)
            code, out = vm.run("dmesg | grep -iE 'secure ?boot|lockdown' | head -5", 30)
            print("      " + out.replace("\n", "\n      "))
    finally:
        vm.stop()


# -- scenario B: shim + MOK -------------------------------------------------------------------------------


def mok_screens(vm: Vm, prefix: str, plan: str) -> None:
    """Drive shim's MokManager (blue screen) with the keyboard from a plan, taking screendumps.

    The plan is a list separated by ';': wait=SECONDS, shot, key=NAME (QEMU key name), type=TEXT.
    Example: "wait=8;shot;key=ret;wait=1;shot".
    """
    count = 0
    for step in (s.strip() for s in plan.split(";") if s.strip()):
        name, _, value = step.partition("=")
        if name == "wait":
            time.sleep(float(value))
        elif name == "shot":
            count += 1
            png = vm.screendump(f"{prefix}-{count:02d}")
            print(f"      screendump {png.name}")
        elif name == "key":
            vm.key(value, 0.8)
        elif name == "type":
            vm.type_text(value)
        else:
            raise ValueError(f"unknown step in the plan: {step}")


def scenario_b() -> None:
    vars_file = WORK / "vars-b.fd"
    disk = WORK / "b.img"
    if install_b(vars_file, disk):
        mok_b(vars_file, disk)


def scenario_b_mok() -> None:
    """Only the MokManager half of B, again and again from the variables saved after the install."""
    mok_b(WORK / "vars-b.fd", WORK / "b.img")


def install_b(vars_file: Path, disk: Path) -> bool:
    log("B1. shim: preflight must refuse sbctl when the firmware is NOT in Setup Mode")
    # A Platform Key is enrolled (User Mode) but Secure Boot is switched off, so the unsigned live
    # kernel can start; that is the state of a real PC the first time the user boots the installer.
    fresh = fresh_vars("b", "--enroll-redhat", "--set-false", "SecureBootEnable")
    assert fresh == vars_file
    disk = new_disk("b")
    vm = boot_live(vars_file, disk, "b-live")
    if vm is None or not prepare_live(vm):
        return False
    installed = False
    try:
        code, out = vm.run(f"bash {GUEST}/live-guest-state.sh 2>&1", 60)
        print(key_lines(out, "setup", "secure"))
        check("setup_mode=False" in out, "the live system sees a firmware with a Platform Key (User Mode)", out[-300:])

        code, out = run_installer(vm, "answers-sbctl.json", 300)
        print(key_lines(out, "Setup Mode", "ERROR", "Refusing", r"^---"))
        check("INSTALLER-EXIT 0" not in out and "Setup Mode" in out,
              "sbctl is REFUSED in User Mode, with an explanation", out[-600:])
        # A blank disk has no filesystem or partition-table signature at all.
        code, out = vm.run("wipefs /dev/vda 2>&1; echo END", 30)
        check(out.strip() == "END", "...and the disk was not touched (no signature, no partition table)", out)

        log("B2. shim + MOK with the real mokutil and efibootmgr")
        code, out = run_installer(vm, "answers-shim.json")
        print(key_lines(out, r"^---", "ERROR", "WARNING", "mokutil", "efibootmgr", "NEXT BOOT"))
        installed = code == 0 and "INSTALLER-EXIT 0" in out
        check(installed, "the installer ran to the end with real firmware writes (shim)", out[-800:])

        code, out = vm.run("mokutil --list-new 2>&1; echo ---; mokutil --sb-state 2>&1; echo ---; efibootmgr -v 2>&1", 30)
        print(key_lines(out, "ArchInstall", "BootOrder", "SHA1", "Subject:", "Issuer:", "SecureBoot", "Boot0", "---"))
        check("ArchInstall Secure Boot Key" in out, "mokutil --list-new shows the queued ArchInstall certificate", out[-900:])
        check("SecureBoot disabled" in out or "SecureBoot enabled" in out, "mokutil --sb-state is readable", out[-500:])
        check("shimx64.efi" in out.lower() and "ArchInstall" in out, "the firmware entry points at shimx64.efi", out[-600:])

        boot_entry_cycle(vm, r"\EFI\ArchInstall\shimx64.efi")

        code, out = vm.run(f"bash {GUEST}/live-guest-serial.sh 2>&1 | tail -n 5", 120)
        check(code == 0 and "SERIAL-OK" in out, "console=ttyS0 added to the installed grub.cfg", out)
        vm.run("sync; poweroff", 10)
        check(vm.wait_exit(60), "the live system powers off")
    finally:
        vm.stop()
    if installed:  # keep the state right after the install: MokManager consumes the pending request
        shutil.copy(vars_file, WORK / "vars-b-installed.fd")
    return installed


# The whole MokManager dialog, as checked screen by screen with screendumps: press a key within the 10 s
# countdown -> "Enroll MOK" -> Continue -> Yes -> password -> Reboot. Missing the countdown cancels the request.
DEFAULT_PLAN = ("wait=2;key=ret;wait=1.5;key=down;wait=0.8;key=ret;wait=1.2;key=down;wait=0.6;key=ret;wait=1.2;"
                "key=down;wait=0.6;key=ret;wait=1.2;type=Abcd1234;wait=0.5;key=ret;wait=1.5;shot;key=ret")


def mok_b(vars_file: Path, disk: Path) -> None:
    secure_boot = os.environ.get("MOK_SB", "1") == "1"
    plan = os.environ.get("MOK_PLAN") or DEFAULT_PLAN
    log(f"B3. boot the installed disk, MokManager, Secure Boot {'ON' if secure_boot else 'OFF'}")
    saved = WORK / "vars-b-installed.fd"
    if not saved.exists():
        print("  nothing persisted yet: run b-shim first")
        return
    start_vars = WORK / "vars-b-try.fd"
    if secure_boot:
        # What ticking "Secure Boot" in the firmware setup does: SecureBootEnable=1. Nothing else.
        subprocess.run(["virt-fw-vars", "--input", str(saved), "--output", str(start_vars), "--set-true",
                        "SecureBootEnable"], check=True, capture_output=True)
    else:
        shutil.copy(saved, start_vars)
    check("MokNew" in vars_summary(start_vars), "the pending enrollment (MokNew) is in the firmware variable store")
    vm = Vm("b-installed", start_vars, [disk], reboot=True)
    vm.start()
    try:
        mok_screens(vm, f"b-sb{'on' if secure_boot else 'off'}", plan)
        if not os.environ.get("MOK_PLAN") or os.environ.get("MOK_EXPECT_LOGIN") == "1":
            # The plan ended with "Reboot": shim must now trust our key and start GRUB and the kernel.
            ok = vm.login(r"testbox login:", password="root-pw", timeout=240)
            if not ok:
                print(vm.tail(30))
                print(f"      screendump: {vm.screendump('b-after-reboot-failed')}")
            check(ok, "after enrolling the key, the installed system reaches the login prompt under Secure Boot")
            if ok:
                vm.screendump("b-after-reboot-login")
                code, out = vm.run("od -An -tu1 -j4 -N1 /sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c", 30)
                check(out.strip() == "1", "the firmware variable SecureBoot is 1", out)
                code, out = vm.run("dmesg | grep -iE \"ArchInstall|secure ?boot\" | head -5", 30)
                print("      " + out.replace("\n", "\n      "))
                check("ArchInstall Secure Boot Key" in out, "the kernel loaded our key from the MOK list")
        else:
            print(vm.tail(15))
    finally:
        vm.stop()


def scenario_a_notpm() -> None:
    boot_without_tpm(WORK / "vars-a.fd", WORK / "a.img")


SCENARIOS = {"a-sbctl": scenario_a, "a-boot": scenario_a_boot, "a-boot-tpm": scenario_a_boot_tpm,
             "a-notpm": scenario_a_notpm,
             "a-chain": scenario_a_boot_chainload, "a-fallback": scenario_a_fallback,
             "b-shim": scenario_b, "b-mok": scenario_b_mok, "b-fallback": scenario_b_fallback}


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    names = sys.argv[1:] or list(SCENARIOS)
    for name in names:
        SCENARIOS[name]()
    failed = [what for ok, what in RESULTS if not ok]
    print(f"\n##### SUMMARY: {len(RESULTS) - len(failed)} ok, {len(failed)} failed")
    for what in failed:
        print(f"  FAIL  {what}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
