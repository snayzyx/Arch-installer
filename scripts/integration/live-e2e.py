#!/usr/bin/env python3
"""FULL end-to-end acceptance test of the product, in a QEMU/KVM VM with real UEFI firmware.

Boots the OFFLINE ISO, runs the installer for real (nothing skipped: Arch, GRUB, Secure Boot,
Windows staging), then lets the firmware start Windows Setup by itself, and walks through everything
the user will see afterwards. Runs inside the privileged builder container (scripts/live-test.ps1 -E2e).

    live-e2e.py shim                 # all phases of the "user's real case": Platform Key enrolled, TPM
    live-e2e.py sbctl                # all phases, firmware in Setup Mode (sbctl enrolls the keys)
    live-e2e.py shim-install         # one phase: install | windows | mok | boot | inspect
    ...

Each phase starts from the disk, variables and TPM saved by the previous one (ckpt1..ckpt4 in the work
volume), so a failed phase can be run again without redoing the 40 minutes before it.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_spec = importlib.util.spec_from_file_location("live_vm", Path(__file__).with_name("live-vm.py"))
lv = importlib.util.module_from_spec(_spec)
sys.modules["live_vm"] = lv
_spec.loader.exec_module(lv)

Vm, check, log = lv.Vm, lv.check, lv.log
WORK, GUEST = lv.WORK, lv.GUEST
lv.SCREENS = lv.SCREENS / os.environ.get("E2E_EVIDENCE", "e2e2")  # every picture and log of this run goes here
SHOTS = lv.SCREENS

ISO = Path(os.environ.get("E2E_ISO", str(WORK / "offline-dev.iso")))
DEV_INSTALLER = os.environ.get("E2E_DEV_INSTALLER", "0") == "1"  # a snapshot of installer/, not the ISO's own copy
MEMORY = int(os.environ.get("E2E_MEMORY", "6144"))
CPUS = int(os.environ.get("E2E_CPUS", "4"))
# No VMX for the guest: with it Windows 11 starts its own hypervisor (VBS) and the nested KVM dies with "entry failed"
CPU = os.environ.get("E2E_CPU", "host,-vmx")
WINDOWS_MINUTES = int(os.environ.get("E2E_WINDOWS_MINUTES", "90"))
WINDOWS_TAIL = int(os.environ.get("E2E_WINDOWS_TAIL", "180"))  # seconds to let Windows finish its first start after the finish script
LAPTOP = os.environ.get("E2E_LAPTOP", "0") == "1"  # laptop answers + the checks that go with the laptop/Windows changes
MSDM = os.environ.get("E2E_MSDM", "0") == "1"  # a fake MSDM (firmware product key) table in the virtual machine
FAKE_KEY = "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE"
MSDM_FILE = WORK / "e2e-msdm.bin"
EXTRA = ["-acpitable", f"file={MSDM_FILE}"] if MSDM else []
TAG = os.environ.get("E2E_TAG", "r2")  # prefix of every file and VM name of a run
WINDOWS_ISO_PATH = "/run/archiso/bootmnt/windows/Windows11.iso"
TARGET = "/dev/nvme0n1"  # Windows Setup has no virtio driver; NVMe is what both Windows and Linux boot from
LAUNCH = ("PYTHONUNBUFFERED=1 archinstall-launcher" if not DEV_INSTALLER
          else f"PYTHONUNBUFFERED=1 PYTHONPATH={GUEST} python3 -m dualboot_installer")
STATE_PATH = GUEST if DEV_INSTALLER else "/opt/archinstall"
# What of the installer's output is shown live; the whole log is saved anyway.
LIVE_LINES = r"^(=== |--- \[|ERROR|WARNING|Invalid|Refusing|The next boot|Windows image|If the computer|SECURE BOOT)"
GENERIC_LOGIN = r"(?:archiso|archlinux|testbox) login:"
MOK_PASSWORD = "Abcd1234"


@dataclass
class Flavor:
    tag: str  # prefix of every file and VM name of this scenario
    expected: str  # what "auto" must resolve to on this firmware
    vars_flags: tuple[str, ...]

    @property
    def disk(self) -> Path:
        return WORK / f"{self.tag}.img"

    @property
    def vars(self) -> Path:
        return WORK / f"vars-{self.tag}.fd"

    @property
    def tpm(self) -> Path:
        return WORK / f"{self.tag}.tpm"


FLAVORS = {
    # A Platform Key and Microsoft's keys, Secure Boot switched off while the unsigned live system runs.
    "shim": Flavor(TAG, "shim", ("--enroll-redhat", "--set-false", "SecureBootEnable")),
    # No key at all: Setup Mode.
    "sbctl": Flavor(TAG + "s", "sbctl", ()),
}


# -- checkpoints ----------------------------------------------------------------------------------------


def save_ckpt(f: Flavor, number: int) -> None:
    target = WORK / f"{f.tag}-ckpt{number}"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir()
    started = time.time()
    subprocess.run(["cp", "--sparse=always", str(f.disk), str(target / "disk.img")], check=True)
    shutil.copy(f.vars, target / "vars.fd")
    if f.tpm.exists():
        shutil.copytree(f.tpm, target / "tpm")
    print(f"      checkpoint {number} saved in {time.time() - started:.0f} s ({target})", flush=True)


def load_ckpt(f: Flavor, number: int) -> bool:
    source = WORK / f"{f.tag}-ckpt{number}"
    if not (source / "disk.img").exists():
        print(f"  no checkpoint {number} for {f.tag}: run the earlier phase first")
        return False
    f.disk.unlink(missing_ok=True)
    subprocess.run(["cp", "--sparse=always", str(source / "disk.img"), str(f.disk)], check=True)
    shutil.copy(source / "vars.fd", f.vars)
    shutil.rmtree(f.tpm, ignore_errors=True)
    if (source / "tpm").exists():
        shutil.copytree(source / "tpm", f.tpm)
    print(f"      restored checkpoint {number}", flush=True)
    return True


# -- firmware variables -----------------------------------------------------------------------------------


def nvram(vars_file: Path) -> dict:
    """The boot-related variables of an OVMF variable store (it is written live by the running VM)."""
    dump = WORK / f"nvram-{os.getpid()}.json"
    data = None
    for _ in range(8):
        result = subprocess.run(["virt-fw-vars", "--input", str(vars_file), "--output-json", str(dump)],
                                capture_output=True)
        if result.returncode == 0:
            try:
                data = json.loads(dump.read_text())
                break
            except ValueError:
                pass
        time.sleep(0.5)
    if data is None:
        return {"error": True, "order": [], "entries": {}, "next": None, "names": set()}
    variables = {v["name"]: bytes.fromhex(v["data"]) for v in data["variables"]}
    order_raw = variables.get("BootOrder", b"")
    order = list(struct.unpack(f"<{len(order_raw) // 2}H", order_raw[:len(order_raw) // 2 * 2]))
    entries = {}
    for name, blob in variables.items():
        found = re.fullmatch(r"Boot([0-9A-F]{4})", name)
        if found and len(blob) > 6:
            entries[int(found.group(1), 16)] = blob[6:].decode("utf-16-le", "replace").split("\x00")[0]
    boot_next = struct.unpack("<H", variables["BootNext"][:2])[0] if len(variables.get("BootNext", b"")) >= 2 else None
    return {"error": False, "order": order, "entries": entries, "next": boot_next, "names": set(variables),
            "secure_boot_enable": variables.get("SecureBootEnable", b"")[:1]}


def describe(nv: dict) -> str:
    def name(number: int) -> str:
        return f"{number:04X}={nv['entries'].get(number, '?')}"

    order = ", ".join(name(n) for n in nv["order"])
    after = f" BootNext={name(nv['next'])}" if nv["next"] is not None else ""
    mok = " MokNew" if "MokNew" in nv["names"] else ""
    return f"BootOrder=[{order}]{after}{mok}"


def first_entry(nv: dict) -> str:
    return nv["entries"].get(nv["order"][0], "?") if nv["order"] else "(none)"


def set_secure_boot(f: Flavor, on: bool) -> None:
    """What ticking or unticking 'Secure Boot' in the firmware setup does: SecureBootEnable, nothing else."""
    temp = WORK / f"{f.tag}-vars-edit.fd"
    subprocess.run(["virt-fw-vars", "--input", str(f.vars), "--output", str(temp),
                    "--set-true" if on else "--set-false", "SecureBootEnable"], check=True, capture_output=True)
    shutil.move(temp, f.vars)
    print(f"      firmware setup: Secure Boot {'ENABLED' if on else 'DISABLED'}", flush=True)


# -- screens -----------------------------------------------------------------------------------------------


def shot(vm: Vm, name: str) -> tuple[Path, str, float]:
    """A screendump; also returns a digest of the picture and the fraction of MokManager-blue pixels."""
    png = vm.screendump(name)
    ppm = WORK / f"{name}.ppm"
    data = ppm.read_bytes()
    ppm.unlink(missing_ok=True)
    header = re.match(rb"P6\s+(\d+)\s+(\d+)\s+(\d+)\s", data)
    pixels = data[header.end():] if header else data
    digest = hashlib.md5(pixels).hexdigest()[:8]
    blue = total = 0
    for i in range(0, len(pixels) - 2, 3 * 61):
        total += 1
        if pixels[i] < 10 and pixels[i + 1] < 10 and 140 <= pixels[i + 2] <= 165:  # EFI blue (0,0,152): not a wallpaper
            blue += 1
    return png, digest, blue / max(total, 1)


def disk_used_gib(path: Path) -> float:
    return os.stat(path).st_blocks * 512 / 2**30


# -- the disk, offline ---------------------------------------------------------------------------------------


@contextlib.contextmanager
def offline(disk: Path, *, write: bool = False):
    """Mount every partition of a disk image whose VM is stopped. Yields {partition number: mount point}."""
    table = json.loads(subprocess.run(["sfdisk", "-J", str(disk)], capture_output=True, text=True,
                                      check=True).stdout)["partitiontable"]
    loops: list[str] = []
    mounts: dict[int, Path] = {}
    notes: dict[int, str] = {}
    try:
        for part in table["partitions"]:
            number = int(re.search(r"(\d+)$", part["node"]).group(1))
            args = ["losetup", "--find", "--show", "--offset", str(part["start"] * 512),
                    "--sizelimit", str(part["size"] * 512)]
            if not write:
                args.append("--read-only")
            device = subprocess.run([*args, str(disk)], capture_output=True, text=True, check=True).stdout.strip()
            loops.append(device)
            kind = subprocess.run(["blkid", "-o", "value", "-s", "TYPE", device], capture_output=True,
                                  text=True).stdout.strip()
            point = Path(f"/mnt/e2e-offline/p{number}")
            point.mkdir(parents=True, exist_ok=True)
            options = "rw" if write else "ro"
            if kind == "ntfs":
                command = ["ntfs-3g", "-o", options, device, str(point)]
            elif kind == "vfat":
                command = ["mount", "-t", "vfat", "-o", options, device, str(point)]
            elif kind in ("ext4", "xfs", "btrfs"):
                command = ["mount", "-o", options + (",noload" if kind == "ext4" and not write else ""),
                           device, str(point)]
            else:
                notes[number] = f"not mounted ({kind or 'no filesystem'})"
                continue
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode == 0:
                mounts[number] = point
                notes[number] = kind
            else:
                notes[number] = f"{kind}: mount failed: {(result.stderr or result.stdout).strip()[:200]}"
        print("      partitions: " + ", ".join(f"p{n}={notes[n]}" for n in sorted(notes)), flush=True)
        yield mounts
    finally:
        for point in reversed(list(mounts.values())):
            subprocess.run(["umount", str(point)], capture_output=True)
        for device in loops:
            subprocess.run(["losetup", "-d", device], capture_output=True)


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", "replace")
    return raw.decode("utf-8", "replace")


def menu_titles(grub_cfg: str) -> list[str]:
    """The top-level entries of a grub.cfg, in the order GRUB shows them."""
    titles, depth = [], 0
    for line in grub_cfg.splitlines():
        entry = re.match(r"\s*(?:menuentry|submenu)\s+(?:'([^']*)'|\"([^\"]*)\")", line)
        if entry and depth == 0:
            titles.append(entry.group(1) or entry.group(2))
        depth += line.count("{") - line.count("}")
    return titles


# -- the guest -----------------------------------------------------------------------------------------------


def run_follow(vm: Vm, command: str, timeout: float, show: str) -> tuple[int, str]:
    """vm.run(), printing the lines that match `show` while the command runs (it may take 40 minutes)."""
    marker = f"__RC{int(time.time() * 1000) % 10**9}__"
    start = vm.cursor
    vm.send(f"{command}\nprintf '\\n{marker}%s\\n' \"$?\"\n")
    wanted, done = re.compile(show), re.compile(rf"{marker}(\d+)")
    shown, deadline = start, time.time() + timeout
    while time.time() < deadline:
        text = vm.text()
        end = text.rfind("\n") + 1
        for line in text[shown:end].splitlines():
            if wanted.search(line):
                print(f"      [{time.strftime('%H:%M:%S')}] {line[:200]}", flush=True)
        shown = max(shown, end)
        match = done.search(text, start)
        if match:
            vm.cursor = match.end()
            return int(match.group(1)), text[start:match.start()].strip("\n")
        if vm.exited():
            return 254, text[start:]
        time.sleep(2)
    return 255, vm.text()[start:]


def indent(text: str, width: int = 6, limit: int = 200) -> str:
    return "\n".join(" " * width + line[:limit] for line in text.splitlines())


def build_snapshot(f: Flavor) -> Path:
    """The tar the live system reads from its second disk: the guest scripts and the answers (and, for
    development runs only, a snapshot of the installer)."""
    root = WORK / f"{f.tag}-snaproot"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir()
    for script in Path(__file__).parent.glob("live-guest-*.sh"):  # no carriage returns, whatever git did
        (root / script.name).write_bytes(script.read_bytes().replace(b"\r\n", b"\n"))
    if DEV_INSTALLER:
        shutil.copytree("/snap/dualboot_installer", root / "dualboot_installer")
    answers = {
        "disk": {"device": TARGET, "windows_gib": 64},
        "arch": {"country": "FR", "timezone": "Europe/Paris", "locale": "fr_FR.UTF-8", "keymap": "fr",
                 "hostname": "testbox", "username": "alice", "user_password": "alice-pw",
                 "root_same_password": False, "root_password": "root-pw", "filesystem": "ext4",
                 "kernel": "linux", "desktop": "minimal", "aur_helper": "none"},
        "windows": {"iso_source": "embedded", "iso_path": WINDOWS_ISO_PATH, "edition": "Pro",
                    "computer_name": "WINTEST", "username": "alice", "password": "win-pw"},
        "secureboot": {"strategy": "auto", "mok_password": MOK_PASSWORD, "touch_firmware": True},
    }
    if LAPTOP:
        answers["arch"]["laptop"] = True
        answers["network"] = {"wifi_ssid": "TestNet", "wifi_security": "psk", "wifi_passphrase": "wifi-test-pass-2026"}
    (root / "answers.json").write_text(json.dumps(answers, indent=2))
    tar = WORK / f"{f.tag}-snap.tar"
    subprocess.run(["tar", "-C", str(root), "-cf", str(tar), "."], check=True)
    return tar


def write_msdm() -> None:
    """A minimal MSDM table (the product key stored in the firmware of PCs sold with Windows), with a FAKE key."""
    key = FAKE_KEY.encode()
    body = struct.pack("<IIIII", 1, 0, 1, 0, len(key)) + key  # SLS version, reserved, data type, reserved, length
    header = b"MSDM" + struct.pack("<IBB", 36 + len(body), 3, 0) + b"ARCHIN" + b"E2ETEST " + struct.pack("<I", 1) + b"INTL" + struct.pack("<I", 1)
    table = bytearray(header + body)
    table[9] = (-sum(table)) & 0xFF  # checksum
    MSDM_FILE.write_bytes(bytes(table))
    print(f"      fake MSDM table written: {len(table)} bytes, key {FAKE_KEY} (it must never show up in a log)", flush=True)


def ntfs_rw_probe(f: Flavor, when: str) -> None:
    """The dual-boot Fast Startup problem: after a normal Windows shutdown the NTFS volume must mount READ-WRITE
    from Linux (ntfs-3g refuses a hibernated volume). Mounts it rw, writes and deletes a small file."""
    log(f"E2E-rw [{f.tag}] the Windows partition mounted read-write from Linux, {when}")
    table = json.loads(subprocess.run(["sfdisk", "-J", str(f.disk)], capture_output=True, text=True, check=True).stdout)["partitiontable"]
    part = table["partitions"][1]
    device = subprocess.run(["losetup", "--find", "--show", "--offset", str(part["start"] * 512), "--sizelimit",
                             str(part["size"] * 512), str(f.disk)], capture_output=True, text=True, check=True).stdout.strip()
    point = Path("/mnt/e2e-rw")
    point.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(["ntfs-3g", "-o", "rw", device, str(point)], capture_output=True, text=True)
        message = (result.stderr or result.stdout).strip()
        # ntfs-3g quietly falls back to a read-only mount on a hibernated volume, so look at the real mount options.
        options = subprocess.run(["findmnt", "-no", "OPTIONS", str(point)], capture_output=True, text=True).stdout.strip()
        mounted_rw = result.returncode == 0 and "rw" in options.split(",") and "hibernat" not in message.lower()
        check(mounted_rw, "ntfs-3g mounts the Windows partition READ-WRITE after Windows' own shutdown (not hibernated)",
              f"options={options!r} {message[:300]}")
        if result.returncode == 0:
            probe = point / "archinstall-rw-test.tmp"
            try:
                probe.write_text("test")
                probe.unlink()
                check(True, "...and a file can be written and deleted on it")
            except OSError as exc:
                check(False, "...and a file can be written and deleted on it", str(exc))
            check(not (point / "hiberfil.sys").exists(), r"there is no C:\hiberfil.sys")
            subprocess.run(["umount", str(point)], capture_output=True)
        else:
            print(indent(message))
    finally:
        subprocess.run(["losetup", "-d", device], capture_output=True)


def check_laptop_arch(vm: Vm) -> None:
    """The laptop answers must show in the installed Arch: Wi-Fi profile, packages, services."""
    code, out = vm.run("stat -c '%a %U %n' /etc/NetworkManager/system-connections/TestNet.nmconnection 2>&1; "
                       "pacman -Q power-profiles-daemon bluez sof-firmware 2>&1; "
                       "systemctl is-enabled bluetooth.service power-profiles-daemon.service 2>&1; "
                       "grep -c '^ssid=TestNet' /etc/NetworkManager/system-connections/TestNet.nmconnection 2>&1", 60)
    print(indent(out))
    check(out.lstrip().startswith("600 root"), "/etc/NetworkManager/system-connections/TestNet.nmconnection exists, mode 600, owned by root", out)
    check(all(pkg in out for pkg in ("power-profiles-daemon ", "bluez ", "sof-firmware ")),
          "packages power-profiles-daemon, bluez and sof-firmware are installed", out)
    check(out.count("enabled") >= 2 and "disabled" not in out, "services bluetooth and power-profiles-daemon are enabled", out)
    code, out = vm.run("pacman -Q python-gobject 2>&1; powerprofilesctl list 2>&1 | head -12", 60)
    print(indent(out))
    check("python-gobject " in out, "package python-gobject is installed", out)
    check("balanced" in out and "Traceback" not in out and "ModuleNotFoundError" not in out,
          "`powerprofilesctl list` works in the installed Arch (it lists the power profiles)", out)


def decode_dacl(data: bytes) -> tuple[int, list[tuple[int, int, str]]]:
    """(control flags, [(ACE type, access mask, SID)]) of a self-relative NTFS security descriptor."""
    control = struct.unpack_from("<H", data, 2)[0]
    offset = struct.unpack_from("<I", data, 16)[0]
    aces: list[tuple[int, int, str]] = []
    if offset:
        _, _, _, count, _ = struct.unpack_from("<BBHHH", data, offset)
        position = offset + 8
        for _ in range(count):
            kind, _, size, mask = struct.unpack_from("<BBHI", data, position)
            sid = data[position + 8:position + size]
            parts = struct.unpack_from(f"<{sid[1]}I", sid, 8)
            aces.append((kind, mask, f"S-{sid[0]}-{int.from_bytes(sid[2:8], 'big')}-" + "-".join(map(str, parts))))
            position += size
    return control, aces


def check_midsetup_drivers(f: Flavor) -> None:
    """The copy of the disk taken while Windows was still setting itself up: C:\\Drivers must be there."""
    log(f"E2E-drivers [{f.tag}] the disk copied in the middle of Windows Setup (VM paused for the copy)")
    copy = WORK / f"{f.tag}-midsetup.img"
    if not copy.exists():
        check(False, "a copy of the disk was taken in the middle of Windows Setup")
        return
    try:
        with offline(copy) as mounts:
            windows = mounts.get(2)
            if not windows:
                check(False, "the Windows partition of the mid-Setup copy can be read")
                return
            folder = windows / "Drivers"
            infs = sorted(str(p.relative_to(folder)) for p in folder.rglob("*.[iI][nN][fF]")) if folder.exists() else []
            for name in infs[:12]:
                print(f"        C:\\Drivers\\{name}")
            check(folder.exists() and bool(infs), r"C:\Drivers exists while Windows is setting itself up, with INF files "
                  f"({len(infs)})", str(folder))
            check(any("e1" in name.lower() or "net" in name.lower() for name in infs),
                  "...among them a network driver package (the Intel NIC of this VM)", str(infs[:8]))
    finally:
        copy.unlink(missing_ok=True)


def extract_kernel() -> tuple[Path, Path]:
    kernel, initrd = WORK / "e2e-vmlinuz", WORK / "e2e-initramfs.img"
    stamp = WORK / "e2e-kernel.stamp"
    signature = f"{ISO} {ISO.stat().st_size} {int(ISO.stat().st_mtime)}"
    if not (kernel.exists() and initrd.exists() and stamp.exists() and stamp.read_text() == signature):
        subprocess.run(["bsdtar", "-xf", str(ISO), "-C", str(WORK), "arch/boot/x86_64/vmlinuz-linux",
                        "arch/boot/x86_64/initramfs-linux.img"], check=True)
        shutil.move(WORK / "arch/boot/x86_64/vmlinuz-linux", kernel)
        shutil.move(WORK / "arch/boot/x86_64/initramfs-linux.img", initrd)
        stamp.write_text(signature)
    return kernel, initrd


def poweroff(vm: Vm, name: str) -> bool:
    vm.run("sync; poweroff", 15)
    return check(vm.wait_exit(120), f"{name} powers off")


# =============================================================================================================
# Phase 1: the offline ISO, live, the installer for real
# =============================================================================================================


def phase_install(f: Flavor, second: bool = False) -> bool:
    number = "6" if second else "1"
    log(f"E2E-{number} [{f.tag}] " + ("SECOND install of the same ISO on the disk that holds the first one (driver harvest)"
                                   if second else "offline ISO, live system, the REAL installer (no --partial), firmware writes for real"))
    if not ISO.exists():
        check(False, f"the ISO {ISO} exists")
        return False
    print(f"      ISO {ISO} ({ISO.stat().st_size / 2**30:.2f} GiB)", flush=True)
    if second:
        if not load_ckpt(f, 4):  # the disk with the first install: Windows (driver store included) and Arch
            return False
        set_secure_boot(f, False)  # the unsigned live system has to start
    else:
        f.disk.unlink(missing_ok=True)
        subprocess.run(["truncate", "-s", "100G", str(f.disk)], check=True)
        fresh = lv.fresh_vars(f.tag, *f.vars_flags)
        assert fresh == f.vars
        shutil.rmtree(f.tpm, ignore_errors=True)
    certs = lv.vars_certs(f.vars)
    for name in ("PK", "KEK", "db"):
        for subject in certs.get(name, []):
            print(f"      firmware {name:3} {subject[:140]}", flush=True)
    kernel, initrd = extract_kernel()
    vm = Vm(f"{f.tag}-live", f.vars, [f.disk, build_snapshot(f)], live=True, disk_bus="nvme", cpu=CPU, extra=EXTRA, tpm=True, tpm_dir=f.tpm,
            tpm_device="tpm-crb", iso=ISO, kernel=kernel, initrd=initrd, memory=MEMORY, cpus=CPUS,
            nic="e1000e" if second else "virtio-net-pci")  # the harvest matches the NIC of THIS machine
    vm.start()
    installed = False
    try:
        if not vm.login(GENERIC_LOGIN, timeout=420):
            print(vm.tail(40))
            check(False, "the live system (from the OFFLINE ISO) boots and gives a root shell")
            return False
        check(True, "the live system (from the OFFLINE ISO) boots and gives a root shell")
        code, out = vm.run(f"mkdir -p {GUEST} && tar -xf /dev/vda -C {GUEST} && ls {GUEST} | tr '\\n' ' '", 60)
        check(code == 0 and "answers.json" in out, "the answers and test scripts are unpacked in the live system", out)

        code, out = vm.run(f"bash {GUEST}/live-guest-e2e-state.sh {STATE_PATH} 2>&1", 90)
        print(indent(out))
        want_setup = "1" if f.expected == "sbctl" else "0"
        check(f"SetupMode byte: {want_setup}" in out,
              f"the firmware is in {'Setup Mode' if want_setup == '1' else 'User Mode (a Platform Key is enrolled)'}", out)
        check("TPM devices: tpm0" in out and "version: 2" in out, "the live system sees the virtual TPM 2.0", out)
        check(f"automatic strategy: {f.expected}" in out,
              f"the installer's own detection resolves 'auto' to {f.expected}", out)
        check(f"WINDOWS-ISO-PATH {WINDOWS_ISO_PATH}" in out,
              "the bundled Windows ISO is found through its manifest (embedded source)", out)
        check("MISSING" not in out, "every tool the installer needs is on the ISO", out)

        log("E2E-1b the installer, as typed by the user (headless): " + LAUNCH.split("PYTHONPATH")[0].strip())
        started = time.time()
        command = (f"cd /root; {LAUNCH} --answers {GUEST}/answers.json --confirm-erase {TARGET} 2>&1 | "
                   f"tee /root/install.log | grep --line-buffered -E '{LIVE_LINES}'; "
                   "echo INSTALLER-EXIT ${PIPESTATUS[0]}")
        code, out = run_follow(vm, command, 4200, LIVE_LINES)
        minutes = (time.time() - started) / 60
        exit_code = re.search(r"INSTALLER-EXIT (\d+)", out)
        print(f"      installer finished in {minutes:.1f} min, exit code {exit_code.group(1) if exit_code else '?'}", flush=True)
        _, whole = vm.run("echo ===LOG-BEGIN===; cat /root/install.log; echo ===LOG-END===", 180)
        body = whole.split("===LOG-BEGIN===", 1)[-1].split("===LOG-END===", 1)[0]
        SHOTS.mkdir(parents=True, exist_ok=True)
        (SHOTS / f"{f.tag}-{number}-installer.log").write_text(body)
        print(f"      full installer log: {SHOTS / (f.tag + '-' + number + '-installer.log')} ({len(body.splitlines())} lines)", flush=True)

        statuses = re.findall(r"^--- \[(\w+)\] (.+)$", body, re.MULTILINE)
        for status, title in statuses:
            print(f"        {status:8} {title}")
        installed = bool(exit_code) and exit_code.group(1) == "0"
        check(installed, "the installer exits with code 0", out[-1200:])
        check(len(statuses) >= 12 and all(s == "done" for s, _ in statuses),
              f"all {len(statuses)} steps of the plan are 'done' (none skipped, none failed)", str(statuses))
        # The installer's own failures start with "ERROR:" (a transient pacman mirror error is not one of them).
        check(re.search(r"(?m)^(?:\[[^\]]*\]\s*)?ERROR:", body) is None, "no 'ERROR:' line from the installer in its log")
        if LAPTOP:
            check("TestNet" in body and "is kept for the installed system" in body,
                  "the installer says the Wi-Fi network 'TestNet' is kept for the installed system")
            check(all(word not in body for word in ("wifi-test-pass-2026",)), "the Wi-Fi passphrase does not appear in the installer log")
        if LAPTOP and not second:
            check("No NTFS partition on this disk" in body, "on a blank disk the driver harvest finds no Windows and the install goes on")
        if second:
            for line in body.splitlines():
                if re.search(r"drivers? of|driver store|Kept \d+ driver|driver package|BitLocker|cannot be read", line):
                    print(f"      installer: {line[:260]}")
            check("PCI\\VEN_8086&DEV_10D3" in body, "the harvest looks for the Intel e1000e NIC of this VM (PCI\\VEN_8086&DEV_10D3)")
            check("Windows driver store found" in body, "the old Windows on the disk is mounted read-only and its driver store is found")
            kept = re.search(r"Kept (\d+) driver package", body)
            check(bool(kept) and int(kept.group(1)) >= 1, "the installer keeps at least one matching driver package of the old Windows",
                  kept.group(0) if kept else "no 'Kept N driver package' line")
            check("go to Windows (C:\\Drivers" in body, "the installer says the packages go to Windows (C:\\Drivers)")
        if not installed:
            print(indent("\n".join(body.splitlines()[-40:])))
            return False

        resolved = re.search(r"(?i)strategy[^\n]*\b(sbctl|shim)\b", body)
        print(f"      installer's own words about the strategy: {resolved.group(0)[:150] if resolved else '(none found)'}")

        log("E2E-1c what the installer left in the firmware and on the disk")
        code, out = vm.run("efibootmgr -v 2>&1", 30)
        print(indent(out))
        order = re.search(r"BootOrder: ([0-9A-Fa-f,]+)", out)
        setup = re.search(r"Boot([0-9A-Fa-f]{4})\*? Windows 11 Setup", out)
        arch = re.search(r"Boot([0-9A-Fa-f]{4})\*? ArchInstall", out)
        nxt = re.search(r"BootNext: ([0-9A-Fa-f]{4})", out)
        check(bool(setup and nxt and nxt.group(1) == setup.group(1)), "BootNext is the one-time 'Windows 11 Setup' entry", out)
        check(bool(setup and order and order.group(1).split(",")[0] == setup.group(1)),
              "'Windows 11 Setup' is FIRST in the boot order", out)
        check(bool(arch), "the 'ArchInstall' entry exists", out)
        setup_line = out.split("Windows 11 Setup")[-1].split("\n")[0] if setup else ""
        check("BOOTX64.EFI" in setup_line.upper(), "'Windows 11 Setup' points at \\EFI\\BOOT\\BOOTX64.EFI", out)
        if f.expected == "shim":
            code, out = vm.run("mokutil --list-new 2>&1 | grep -E 'SHA1|Subject:|Issuer:|Validity|Not After' | head -8; "
                               "mokutil --sb-state 2>&1", 30)
            print(indent(out))
            check("ArchInstall Secure Boot Key" in out, "mokutil --list-new shows the queued ArchInstall key", out)
        else:
            code, out = vm.run("sbctl status 2>&1", 30)
            print(indent(out))
            setup_line = next((line for line in out.splitlines() if "Setup Mode" in line), "")
            check("Disabled" in setup_line, "sbctl keys are enrolled (Setup Mode is now disabled)", out)

        code, out = vm.run("sgdisk -p " + TARGET + " 2>&1 | tail -n 8; lsblk -f " + TARGET + " 2>&1", 30)
        print(indent(out))
        code, out = vm.run(
            "mkdir -p /mnt/ws && mount -o ro " + TARGET + "p4 /mnt/ws && cd /mnt/ws && "
            "echo 'WINSETUP top:' $(ls) && echo 'EFI/BOOT:' $(ls EFI/BOOT) && echo 'archinstall/:' $(ls archinstall | head -3) '...' && "
            "echo 'sources:' $(ls -l sources | awk '/install|boot.wim|setup.exe/ {print $9\":\"$5}') && "
            "echo OEM: $(cd 'sources/$OEM$/$$/Setup/Scripts' && ls) && "
            "wimlib-imagex dir sources/boot.wim 2 --path=/Windows/System32 2>&1 | grep -iE 'winpeshl.ini|archinstall.cmd'; "
            "cd /; umount /mnt/ws", 120)
        print(indent(out))
        lowered = out.lower()  # the ISO's own files are lower case on FAT
        if LAPTOP:
            check("archinstall-wifi.xml" in lowered and "archinstall-wifi.ps1" in lowered,
                  "the Setup partition has $OEM$\\$$\\Setup\\Scripts\\archinstall-wifi.xml and archinstall-wifi.ps1", out)
        check("install.swm" in lowered and "archinstall.cmd" in lowered and "winpeshl.ini" in lowered
              and "setupcomplete.cmd" in lowered and "bootx64.efi" in lowered and "archinstall-finish.ps1" in lowered,
              "WINSETUP holds Windows Setup, split install.swm, the launcher inside boot.wim, the answer files and SetupComplete",
              out)
        if second:
            code, out = vm.run(f"mkdir -p /mnt/ws && mount -o ro {TARGET}p4 /mnt/ws && cd /mnt/ws && "
                               "echo DRIVER-PACKAGES: $(ls 'sources/$OEM$/$1/Drivers' 2>&1 | head -30) && "
                               "echo DRIVERPATHS-IN-ANSWER-FILES: $(grep -l DriverPaths archinstall/unattend-*.xml | wc -l); cd /; umount /mnt/ws", 60)
            print(indent(out))
            packages = out.split("DRIVER-PACKAGES:")[-1].split("DRIVERPATHS")[0].strip()
            check(bool(packages) and "No such file" not in packages, "the Setup partition holds sources\\$OEM$\\$1\\Drivers with driver packages", packages)
            check(re.search(r"DRIVERPATHS-IN-ANSWER-FILES: (\d+)", out) is not None and
                  int(re.search(r"DRIVERPATHS-IN-ANSWER-FILES: (\d+)", out).group(1)) == 16,
                  "the 16 answer files all list C:\\Drivers as a driver path (DriverPaths)", out)
        code, out = vm.run(f"mkdir -p /mnt/esp && mount -o ro {TARGET}p1 /mnt/esp && cd /mnt/esp && "
                           "find . -maxdepth 3 -type f | sort | head -40; cd /; umount /mnt/esp", 60)
        print(indent(out))
    finally:
        if installed:
            poweroff(vm, "the live system")
        vm.stop()

    nv = nvram(f.vars)
    print(f"      NVRAM after the install: {describe(nv)}", flush=True)
    check(first_entry(nv) == "Windows 11 Setup", "NVRAM file: the first boot entry is 'Windows 11 Setup'")
    check("ArchInstall" in nv["entries"].values(), "NVRAM file: ArchInstall is registered")
    if f.expected == "shim":
        check("MokNew" in nv["names"], "NVRAM file: the MOK enrollment request is pending (MokNew)")
        set_secure_boot(f, True)  # the user's firmware has Secure Boot ENABLED for the real boots that follow
    save_ckpt(f, 5 if second else 1)
    return installed


# =============================================================================================================
# Phase 2: the firmware starts Windows Setup by itself
# =============================================================================================================


def setup_done(nv: dict) -> bool:
    """SetupComplete has run: the one-time entry is gone and Windows Boot Manager is registered."""
    names = list(nv["entries"].values())
    return "Windows 11 Setup" not in names and "Windows Boot Manager" in names


def task_ran(nv: dict) -> bool:
    """The ArchInstallBootOrder task has run: ArchInstall is first in the firmware boot order."""
    return setup_done(nv) and first_entry(nv) == "ArchInstall"


def phase_windows(f: Flavor, second: bool = False) -> bool:
    log(f"E2E-{'7' if second else '2'} [{f.tag}] first boot with Secure Boot ENABLED: Windows 11 Setup must start by itself and finish by itself"
        + (" (second install)" if second else ""))
    # Nested KVM under Docker Desktop (WSL2/Hyper-V) sometimes dies with "KVM: entry failed, hardware error 0x0":
    # that freezes the guest and says nothing about the installer, so such a run is redone from checkpoint 1.
    result: dict = {"crashed": True}
    for attempt in range(1, 4):
        if not load_ckpt(f, 5 if second else 1):
            return False
        result = windows_run(f, attempt, midsetup=300 if second else None)
        if not result["crashed"]:
            break
        print(f"      attempt {attempt}: the hypervisor froze the VM (KVM entry failed): starting again from checkpoint 1",
              flush=True)
    nv = nvram(f.vars)
    print(f"      NVRAM after Windows: {describe(nv)}", flush=True)
    check(not result["crashed"], "the VM ran Windows to the end without a hypervisor crash (KVM entry failed)")
    check(result["setup_at"] is not None, "Windows Setup ran by itself and its finish script (SetupComplete) ran")
    check(result["first_at_setup"] == "Windows Boot Manager",
          "right after SetupComplete the first boot entry is still Windows Boot Manager (ArchInstall is NOT moved yet)",
          str(result["first_at_setup"]))
    check(result["shim_at"] is None,
          "Windows' automatic restart reached WINDOWS again: shim's blue screen did not appear before the first start was over")
    check(result["task_at"] is not None, "at the next Windows start the ArchInstallBootOrder task ran: ArchInstall is first in NVRAM")
    check(task_ran(nv), "NVRAM at the end: no 'Windows 11 Setup' entry, Windows Boot Manager registered, ArchInstall FIRST")
    save_ckpt(f, 6 if second else 2)
    if second:
        check_midsetup_drivers(f)
    check_windows_files(f, "after the second install" if second else "after Windows' first start", drivers=second)
    if LAPTOP:
        ntfs_rw_probe(f, "after Windows' first start and its ACPI shutdown")
    return result["ok"]


def windows_run(f: Flavor, attempt: int, midsetup: float | None = None) -> dict:
    """One run of the VM from checkpoint 1, until Windows has had time to reach its sign-in screen."""
    nv = nvram(f.vars)
    print(f"      attempt {attempt}. NVRAM at power on: {describe(nv)}  SecureBootEnable={nv.get('secure_boot_enable', b'').hex()}", flush=True)
    vm = Vm(f"{f.tag}-win", f.vars, [f.disk], reboot=True, tpm=True, tpm_dir=f.tpm, tpm_device="tpm-crb",
            disk_bus="nvme", cpu=CPU, extra=EXTRA, nic="e1000e", memory=MEMORY, cpus=CPUS, boot_index=False)
    vm.start()
    started = time.time()
    res: dict = {"ok": False, "crashed": False, "setup_at": None, "task_at": None, "shim_at": None,
                 "first_at_setup": None}
    last_shot, last_digest, last_describe = -999.0, "", ""
    same, blue, midsetup_done = 0, 0.0, False
    try:
        while True:
            elapsed = time.time() - started
            if midsetup is not None and not midsetup_done and elapsed >= midsetup and res["setup_at"] is None:
                # A consistent copy of the disk while Windows is still setting itself up: C:\\Drivers is there only now.
                midsetup_done = True
                vm.monitor("stop")
                copy_started = time.time()
                subprocess.run(["cp", "--sparse=always", str(f.disk), str(WORK / f"{f.tag}-midsetup.img")], check=True)
                vm.monitor("cont")
                print(f"      [{elapsed / 60:5.1f} min] disk copied while the VM was paused ({time.time() - copy_started:.0f} s)", flush=True)
            if "KVM: entry failed" in vm.text():
                res["crashed"] = True
                print(f"      [{elapsed / 60:5.1f} min] QEMU reports: KVM: entry failed, hardware error 0x0", flush=True)
                break
            if vm.exited():
                print("      QEMU exited by itself", flush=True)
                print(vm.tail(10))
                break
            nv = nvram(f.vars)
            text = describe(nv)
            if text != last_describe:
                print(f"      [{elapsed / 60:5.1f} min] NVRAM {text}", flush=True)
                last_describe = text
            if res["setup_at"] is None and setup_done(nv):
                res["setup_at"], res["first_at_setup"] = elapsed, first_entry(nv)
                print(f"      [{elapsed / 60:5.1f} min] SetupComplete has run; first boot entry: {res['first_at_setup']}", flush=True)
            if res["setup_at"] is not None and res["task_at"] is None and task_ran(nv):
                res["task_at"] = elapsed
                print(f"      [{elapsed / 60:5.1f} min] the ArchInstallBootOrder task has run (next Windows start): ArchInstall is first",
                      flush=True)
            if res["setup_at"] is None:
                interval = 15 if elapsed < 420 else 60
            else:
                interval = 3 if res["task_at"] is None else 15
            if elapsed - last_shot >= interval:
                last_shot = elapsed
                png, digest, blue = shot(vm, f"{f.tag}-2-win-a{attempt}-{int(elapsed):05d}s")
                if digest == last_digest:
                    png.unlink(missing_ok=True)
                    same += 1
                else:
                    last_digest, same = digest, 0
                    kind = f", {blue:.0%} EFI-blue" if blue > 0.04 else ""
                    print(f"      [{elapsed / 60:5.1f} min] screen {png.name}  disk used {disk_used_gib(f.disk):.1f} GiB{kind}",
                          flush=True)
                if blue > 0.04 and res["setup_at"] is not None and res["shim_at"] is None:
                    res["shim_at"] = elapsed
                    print(f"      [{elapsed / 60:5.1f} min] shim's blue screen appeared before Windows finished its first start", flush=True)
            if res["shim_at"] is not None and blue > 0.04 and same >= 5:
                print(f"      [{elapsed / 60:5.1f} min] stuck on shim's blue screen (nobody is there to press a key)", flush=True)
                res["ok"] = False
                break
            if res["task_at"] is not None and elapsed - res["task_at"] > WINDOWS_TAIL:
                res["ok"] = True  # Windows has had time to reach the sign-in screen (it shows a clock: wait by time)
                break
            if elapsed > WINDOWS_MINUTES * 60:
                print(f"      timeout after {WINDOWS_MINUTES} min", flush=True)
                break
            time.sleep(1)
        if not res["crashed"] and vm.proc and vm.proc.poll() is None:
            png, _, _ = shot(vm, f"{f.tag}-2-win-a{attempt}-final")
            print(f"      final screen {png.name}", flush=True)
            if res["shim_at"] is not None:  # a firmware dialog does not react to the ACPI button
                print("      stopping QEMU (the machine waits on a key press)", flush=True)
                vm.stop()
                return res
            vm.monitor("system_powerdown")
            if vm.wait_exit(180):
                print("      Windows shut down on the ACPI power button", flush=True)
            else:
                print("      Windows did not shut down on the ACPI button within 3 min: killing QEMU (like pulling the plug)",
                      flush=True)
    finally:
        vm.stop()
    return res


def check_windows_files(f: Flavor, when: str, drivers: bool = False) -> None:
    """Read the Windows disk offline (the VM is stopped): the logs of the two scripts and the scheduled task."""
    log(f"E2E-files [{f.tag}] the Windows disk, read offline, {when}")
    with offline(f.disk) as mounts:
        windows = mounts.get(2)
        if not windows:
            check(False, "the Windows partition can be mounted offline")
            return
        SHOTS.mkdir(parents=True, exist_ok=True)
        for name, expect in (("archinstall-finish.log", ("deleted the one-time entry", "done")),
                             ("archinstall-bootorder.log", ("first",))):
            path = windows / "Windows/Temp" / name
            check(path.exists(), rf"C:\Windows\Temp\{name} exists")
            if path.exists():
                text = read_text(path)
                (SHOTS / f"{f.tag}-{when.split()[0]}-{name}").write_text(text)
                print(indent(text))
                check(all(word in text for word in expect), f"{name} contains {expect}", text[-400:])
        if LAPTOP:
            finish = read_text(windows / "Windows/Temp/archinstall-finish.log") if (windows / "Windows/Temp/archinstall-finish.log").exists() else ""
            check("hibernation and Fast Startup turned off" in finish, "the finish log says 'hibernation and Fast Startup turned off'", finish[-500:])
            has_key = "a product key is stored in the firmware" in finish
            no_key = "no product key in the firmware" in finish
            check(has_key != no_key, "the finish log says either 'a product key is stored in the firmware' or 'no product key in the firmware'", finish[-500:])
            if MSDM:
                check(has_key, "with the fake MSDM table the log shows that a firmware key was found and installed")
                check(FAKE_KEY not in finish, "the firmware key itself is NOT in the finish log")
            else:
                check(no_key, "a plain QEMU VM has no firmware key: the log says so")
            for sub in ("hiberfil.sys",):
                check(not (windows / sub).exists(), rf"there is no C:\{sub}")
            for key, value in (("Session Manager\\Power", "HiberbootEnabled"), ("Power", "HibernateEnabled")):
                got = subprocess.run(["hivexget", str(windows / "Windows/System32/config/SYSTEM"),
                                      rf"\ControlSet001\Control\{key}", value], capture_output=True, text=True)
                check(got.stdout.strip() == "0", rf"registry: HKLM\SYSTEM\...\Control\{key}\{value} = 0", (got.stdout + got.stderr).strip())
        if LAPTOP:
            scripts = windows / "Windows/Setup/Scripts"
            xml = scripts / "archinstall-wifi.xml"
            check(xml.exists(), r"C:\Windows\Setup\Scripts\archinstall-wifi.xml still exists (a VM has no Wi-Fi adapter)")
            check((scripts / "archinstall-wifi.ps1").exists(), r"C:\Windows\Setup\Scripts\archinstall-wifi.ps1 exists")
            wifi_log = windows / "Windows/Temp/archinstall-wifi.log"
            check(wifi_log.exists(), r"C:\Windows\Temp\archinstall-wifi.log exists")
            wifi_text = read_text(wifi_log) if wifi_log.exists() else ""
            print(indent(wifi_text))
            (SHOTS / f"{f.tag}-{when.split()[0]}-archinstall-wifi.log").write_text(wifi_text)
            check("not added yet" in wifi_text and "trying again later" in wifi_text,
                  "the Wi-Fi log says 'not added yet ... trying again later' (nothing to join with: a VM has no Wi-Fi adapter)", wifi_text[-400:])
            task_file = windows / "Windows/System32/Tasks/ArchInstallWifi"
            task_xml = read_text(task_file) if task_file.exists() else ""
            check(task_file.exists(), "the scheduled task ArchInstallWifi exists")
            check("BootTrigger" in task_xml and "<Interval>PT15M</Interval>" in task_xml and "<Duration>P2D</Duration>" in task_xml,
                  "...with an AtStartup trigger and a repetition every 15 minutes for 2 days", task_xml[:700])
            if xml.exists():
                try:
                    control, aces = decode_dacl(os.getxattr(str(xml), "system.ntfs_acl"))
                    sids = sorted({sid for _, _, sid in aces})
                    print(f"      ACL of archinstall-wifi.xml: protected={bool(control & 0x1000)} ACEs={[(k, hex(m), sid) for k, m, sid in aces]}")
                    check(sids == ["S-1-5-18", "S-1-5-32-544"] and bool(control & 0x1000),
                          "archinstall-wifi.xml is readable only by SYSTEM (S-1-5-18) and Administrators (S-1-5-32-544), inheritance removed", str(sids))
                except OSError as exc:
                    check(False, "the ACL of archinstall-wifi.xml can be read from the ntfs-3g mount", str(exc))
            bootlog = windows / "Windows/Temp/archinstall-bootorder.log"
            texts = {"finish log": read_text(windows / "Windows/Temp/archinstall-finish.log") if (windows / "Windows/Temp/archinstall-finish.log").exists() else "",
                     "wifi log": wifi_text, "boot order log": read_text(bootlog) if bootlog.exists() else ""}
            check(all("wifi-test-pass-2026" not in text for text in texts.values()), "the Wi-Fi passphrase appears in no Windows log (finish, wifi, boot order)")
            finish_text = texts["finish log"]
            check("Wi-Fi" in finish_text or "wi-fi" in finish_text.lower(), "the finish log has its Wi-Fi section ('no Wi-Fi adapter yet: a task will add the Wi-Fi profile')", finish_text[-600:])
        if drivers:
            finish_text = read_text(windows / "Windows/Temp/archinstall-finish.log") if (windows / "Windows/Temp/archinstall-finish.log").exists() else ""
            for line in finish_text.splitlines():
                if "driver" in line.lower() or "Drivers" in line:
                    print(f"      finish log: {line[:200]}")
            check("drivers staged in C:\\Drivers" in finish_text, "the finish log has its driver section ('drivers staged in C:\\Drivers: N INF file(s), M in the Windows driver store')", finish_text[-800:])
            stored = re.search(r"(\d+) INF file\(s\), (\d+) in the Windows driver store", finish_text)
            check(bool(stored) and int(stored.group(2)) >= 1, "...and at least one of them is in the Windows driver store", stored.group(0) if stored else "no match")
            check("C:\\Drivers deleted" in finish_text and not (windows / "Drivers").exists(), "the finish script deleted C:\\Drivers (and it is gone)")
            setupapi = windows / "Windows/INF/setupapi.dev.log"
            if setupapi.exists():
                lines = [line for line in read_text(setupapi).splitlines() if "c:\\drivers" in line.lower()]
                (SHOTS / f"{f.tag}-setupapi-drivers.txt").write_text("\n".join(lines))
                for line in lines[:8]:
                    print(f"      setupapi.dev.log: {line.strip()[:200]}")
                check(bool(lines), "setupapi.dev.log shows the drivers being installed from C:\\Drivers", str(setupapi))
            else:
                check(False, "setupapi.dev.log exists", str(setupapi))
        task = windows / "Windows/System32/Tasks/ArchInstallBootOrder"
        check(not task.exists(), "the scheduled task ArchInstallBootOrder no longer exists (it unregistered itself)")
        scripts = windows / "Windows/Setup/Scripts"
        print("      Windows/Setup/Scripts:", sorted(p.name for p in scripts.glob("*")) if scripts.exists() else "(missing)")


# =============================================================================================================
# Phase 3: Secure Boot and MOK, for the shim flavor; a plain boot for the sbctl flavor
# =============================================================================================================


def enable_serial_console(f: Flavor) -> list[str]:
    """Add console=ttyS0 to the installed grub.cfg, so the host can read and drive the Arch system.
    grub.cfg is neither signed nor verified: nothing Secure Boot checks changes."""
    titles: list[str] = []
    with offline(f.disk, write=True) as mounts:
        for config in mounts[1].rglob("grub.cfg"):
            text = config.read_text()
            titles = titles or menu_titles(text)
            patched, count = re.subn(r"(?m)^(\s*linux\s.*?[^\s])\s*$", r"\1 console=ttyS0,115200", text)
            config.write_text(patched)
            print(f"      grub.cfg: {config.relative_to(mounts[1])}: {count} kernel line(s) patched", flush=True)
    print(f"      GRUB menu entries, top level: {titles}", flush=True)
    return titles


def wait_blue(vm: Vm, timeout: float, tag: str) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, _, blue = shot(vm, f"{tag}-poll")
        if blue > 0.04:  # MokManager draws blue boxes on black: the countdown box is small
            return True
        time.sleep(0.3)
    return False


def phase_mok(f: Flavor) -> bool:
    if f.expected != "shim":
        return phase_plain_boot(f)
    log("E2E-3 [shim] the restart after Windows: shim's blue MOK screen, a key within 10 s, enroll the key")
    if not load_ckpt(f, 2):
        return False
    set_secure_boot(f, True)
    nv = nvram(f.vars)
    print(f"      NVRAM at power on: {describe(nv)}", flush=True)
    check("MokNew" in nv["names"], "the MOK enrollment request is still pending (nobody has seen the blue screen yet)")
    check(first_entry(nv) == "ArchInstall", "ArchInstall is first in the boot order")
    titles = enable_serial_console(f)
    vm = Vm(f"{f.tag}-mok", f.vars, [f.disk], reboot=True, tpm=True, tpm_dir=f.tpm, tpm_device="tpm-crb",
            disk_bus="nvme", cpu=CPU, extra=EXTRA, nic="e1000e", memory=MEMORY, cpus=CPUS, boot_index=False)
    vm.start()
    try:
        started = time.time()
        seen = wait_blue(vm, 120, f.tag)
        check(seen, "the blue 'Press any key to perform MOK management' screen appears on the first boot after Windows")
        if seen:
            print(f"      the blue screen was seen {time.time() - started:.1f} s after power on; pressing a key now", flush=True)
            # The exact keystrokes of the dialog verified in the earlier firmware tests.
            plan = ("shot;key=ret;wait=1.2;shot;key=down;wait=0.5;key=ret;wait=1.2;shot;key=down;wait=0.5;key=ret;wait=1.2;"
                    "shot;key=down;wait=0.5;key=ret;wait=1.2;shot;type=" + MOK_PASSWORD + ";wait=0.5;shot;key=ret;wait=1.5;shot;key=ret")
            lv.mok_screens(vm, f"{f.tag}-3-enroll", plan)
        ok = vm.login(r"testbox login:", password="root-pw", timeout=300)
        if not ok:
            print(vm.tail(40))
            print(f"      screendump: {vm.screendump(f.tag + '-3-no-login')}")
        check(ok, "after enrolling the key, Arch boots under Secure Boot and the root password works")
        if ok:
            vm.screendump(f"{f.tag}-3-login")
            code, out = vm.run("od -An -tu1 -j4 -N1 /sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c", 30)
            check(out.strip() == "1", "SecureBoot = 1 in the firmware variable", out)
            code, out = vm.run("mokutil --sb-state 2>&1; mokutil --list-enrolled 2>&1 | grep -E 'Subject:' | head -3; "
                               "dmesg | grep -iE 'ArchInstall|secure ?boot|lockdown' | head -6; echo ---; "
                               "grep -i -E 'archinstall' /proc/keys | head -3", 60)
            print(indent(out))
            check("ArchInstall Secure Boot Key" in out, "the ArchInstall certificate is in the kernel's key list (dmesg or /proc/keys)", out)
            check("SecureBoot enabled" in out, "mokutil --sb-state: SecureBoot enabled", out)
            code, out = vm.run("sleep 5; systemctl is-enabled archinstall-mok-enroll 2>&1; "
                               "journalctl -t archinstall-mok -b --no-pager 2>&1 | tail -3", 60)
            print(indent(out))
            check("disabled" in out, "the archinstall-mok-enroll service disabled itself once the key is enrolled", out)
            if LAPTOP:
                check_laptop_arch(vm)
            poweroff(vm, "the installed Arch system")
    finally:
        vm.stop()
    nv = nvram(f.vars)
    print(f"      NVRAM after 3: {describe(nv)}", flush=True)
    check("MokNew" not in nv["names"], "the enrollment request was consumed")
    (WORK / f"{f.tag}-menu-titles.json").write_text(json.dumps(titles))
    save_ckpt(f, 3)
    return True


def phase_recover(f: Flavor) -> bool:
    """The user misses the 10 s window: shim refuses, then the recovery path (Secure Boot off + the service)."""
    if f.expected != "shim":
        return True
    log("E2E-R [shim] the user misses the 10 s window: shim's blue screen, nobody presses a key")
    if not load_ckpt(f, 2):
        return False
    set_secure_boot(f, True)
    nv = nvram(f.vars)
    print(f"      NVRAM at power on: {describe(nv)}", flush=True)
    pending = "MokNew" in nv["names"]
    print(f"      the MOK enrollment request is {'still pending' if pending else 'ALREADY GONE (consumed earlier)'}",
          flush=True)
    check(first_entry(nv) == "ArchInstall", "ArchInstall is first in the boot order")

    log("E2E-3a boot again with Secure Boot ON and press NOTHING; then OK on the error dialog")
    vm = Vm(f"{f.tag}-mok-a", f.vars, [f.disk], reboot=True, tpm=True, tpm_dir=f.tpm, tpm_device="tpm-crb",
            disk_bus="nvme", cpu=CPU, extra=EXTRA, nic="e1000e", memory=MEMORY, cpus=CPUS, boot_index=False)
    vm.start()
    blue_seen, pressed_ok, press_at = None, False, 0.0
    try:
        started, last, same = time.time(), "", 0
        while time.time() - started < 170 + 90:
            elapsed = time.time() - started
            png, digest, blue = shot(vm, f"{f.tag}-3a-nothing-{int(elapsed):03d}s")
            if digest == last:
                png.unlink(missing_ok=True)
                same += 1
            else:
                last, same = digest, 0
                kind = "blue" if blue > 0.04 else "other"
                if blue > 0.04 and blue_seen is None:
                    blue_seen = elapsed
                print(f"      t+{elapsed:5.1f}s screen {png.name} ({kind}, {blue:.0%} blue)", flush=True)
            if not pressed_ok and blue > 0.04 and same >= 6:
                # shim's error dialog does not go away by itself: it waits for a key (OK)
                png, _, _ = shot(vm, f"{f.tag}-3a-error-still-waiting")
                print(f"      t+{elapsed:5.1f}s the blue screen has not changed for ~12 s ({png.name}): pressing Enter on 'OK'", flush=True)
                vm.key("ret", 0.5)
                pressed_ok, same, press_at = True, 0, elapsed
            if pressed_ok and elapsed - press_at > 90:
                break
            time.sleep(2)
        if vm.text().strip():
            print("      serial console during the boot:")
            print(vm.tail(8))
        check(blue_seen is not None, "a blue shim screen appears on the boot with Secure Boot ON")
        check(pressed_ok, "with no key pressed shim ends on a blue error screen that waits for a key (see the screens)")
        png, _, _ = shot(vm, f"{f.tag}-3a-final")
        print(f"      final screen {png.name}", flush=True)
        if vm.proc and vm.proc.poll() is None:
            vm.monitor("system_powerdown")
            if not vm.wait_exit(180):
                print("      the system did not react to the ACPI button: killing QEMU", flush=True)
    finally:
        vm.stop()
    nv = nvram(f.vars)
    print(f"      NVRAM after 3a: {describe(nv)}", flush=True)
    check("MokNew" not in nv["names"], "no enrollment request is left pending: the key was NOT enrolled and Linux cannot start under Secure Boot yet")

    log("E2E-3b recovery path: Secure Boot OFF, boot Arch, archinstall-mok-enroll queues the key again")
    titles = enable_serial_console(f)
    set_secure_boot(f, False)
    vm = Vm(f"{f.tag}-mok-b", f.vars, [f.disk], reboot=True, tpm=True, tpm_dir=f.tpm, tpm_device="tpm-crb",
            disk_bus="nvme", cpu=CPU, extra=EXTRA, nic="e1000e", memory=MEMORY, cpus=CPUS, boot_index=False)
    vm.start()
    try:
        ok = vm.login(r"testbox login:", password="root-pw", timeout=300)
        if not ok:
            print(vm.tail(40))
            print(f"      screendump: {vm.screendump(f.tag + '-3b-no-login')}")
        check(ok, "with Secure Boot off, GRUB and Arch start and the root password works")
        if ok:
            code, out = vm.run("od -An -tu1 -j4 -N1 /sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c", 30)
            check(out.strip() == "0", "the firmware really has Secure Boot off for this boot", out)
            code, out = vm.run("journalctl -t archinstall-mok --no-pager -o short-iso 2>&1 | tail -5; "
                               "echo ---; systemctl is-enabled archinstall-mok-enroll 2>&1; "
                               "systemctl show archinstall-mok-enroll -p Result -p ExecMainStatus 2>&1; echo ---; "
                               "mokutil --list-new 2>&1 | grep -E 'SHA1|Subject:' | head -4; echo ---; "
                               "mokutil --test-key /var/lib/archinstall/secureboot/MOK.cer 2>&1", 60)
            print(indent(out))
            check("archinstall-mok" in out and "key is not enrolled yet" in out,
                  "journalctl -t archinstall-mok: the service ran and told the user what to do", out)
            check("ArchInstall Secure Boot Key" in out, "mokutil --list-new shows the ArchInstall certificate (queued again)", out)
            check("Result=success" in out, "the archinstall-mok-enroll service finished successfully", out)
            poweroff(vm, "the installed Arch system")
    finally:
        vm.stop()
    nv = nvram(f.vars)
    print(f"      NVRAM after 3b: {describe(nv)}", flush=True)
    check("MokNew" in nv["names"], "NVRAM: the enrollment request is pending again after the recovery boot")

    log("E2E-3c Secure Boot ON again, a key pressed within 10 s: enroll the key")
    set_secure_boot(f, True)
    vm = Vm(f"{f.tag}-mok-c", f.vars, [f.disk], reboot=True, tpm=True, tpm_dir=f.tpm, tpm_device="tpm-crb",
            disk_bus="nvme", cpu=CPU, extra=EXTRA, nic="e1000e", memory=MEMORY, cpus=CPUS, boot_index=False)
    vm.start()
    try:
        seen = wait_blue(vm, 120, f.tag)
        check(seen, "the blue MokManager screen appears again")
        if seen:
            # The exact keystrokes of the dialog verified in the earlier firmware test.
            plan = ("shot;key=ret;wait=1.2;shot;key=down;wait=0.5;key=ret;wait=1.2;shot;key=down;wait=0.5;key=ret;wait=1.2;"
                    "shot;key=down;wait=0.5;key=ret;wait=1.2;shot;type=" + MOK_PASSWORD + ";wait=0.5;shot;key=ret;wait=1.5;shot;key=ret")
            lv.mok_screens(vm, f"{f.tag}-3c-enroll", plan)
        ok = vm.login(r"testbox login:", password="root-pw", timeout=300)
        if not ok:
            print(vm.tail(40))
            print(f"      screendump: {vm.screendump(f.tag + '-3c-no-login')}")
        check(ok, "after enrolling the key, Arch boots under Secure Boot and the root password works")
        if ok:
            vm.screendump(f"{f.tag}-3c-login")
            code, out = vm.run("od -An -tu1 -j4 -N1 /sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c", 30)
            check(out.strip() == "1", "SecureBoot = 1 in the firmware variable", out)
            code, out = vm.run("mokutil --sb-state 2>&1; mokutil --list-enrolled 2>&1 | grep -E 'Subject:' | head -3; "
                               "dmesg | grep -iE 'ArchInstall|secure ?boot|lockdown' | head -6; echo ---; "
                               "grep -i -E 'archinstall' /proc/keys | head -3", 60)
            print(indent(out))
            check("ArchInstall Secure Boot Key" in out, "the ArchInstall certificate is in the kernel's key list (dmesg or /proc/keys)", out)
            check("SecureBoot enabled" in out, "mokutil --sb-state: SecureBoot enabled", out)
            code, out = vm.run("sleep 5; systemctl is-enabled archinstall-mok-enroll 2>&1; "
                               "journalctl -t archinstall-mok -b --no-pager 2>&1 | tail -3", 60)
            print(indent(out))
            poweroff(vm, "the installed Arch system")
    finally:
        vm.stop()
    nv = nvram(f.vars)
    print(f"      NVRAM after 3c: {describe(nv)}", flush=True)
    (WORK / f"{f.tag}-menu-titles.json").write_text(json.dumps(titles))
    save_ckpt(f, 3)
    return True


def phase_plain_boot(f: Flavor) -> bool:
    log("E2E-3 [sbctl] Windows done: Arch boots from the firmware's first entry under the sbctl keys")
    if not load_ckpt(f, 2):
        return False
    titles = enable_serial_console(f)
    vm = Vm(f"{f.tag}-arch", f.vars, [f.disk], reboot=True, tpm=True, tpm_dir=f.tpm, tpm_device="tpm-crb",
            disk_bus="nvme", cpu=CPU, extra=EXTRA, nic="e1000e", memory=MEMORY, cpus=CPUS, boot_index=False)
    vm.start()
    try:
        ok = vm.login(r"testbox login:", password="root-pw", timeout=300)
        if not ok:
            print(vm.tail(40))
            print(f"      screendump: {vm.screendump(f.tag + '-3-no-login')}")
        check(ok, "after Windows, the firmware starts GRUB and Arch under the sbctl keys (no key to enroll, no screen)")
        if ok:
            code, out = vm.run("sbctl status 2>&1", 30)
            print(indent(out))
            check("Enabled" in next((line for line in out.splitlines() if "Secure Boot" in line), ""),
                  "sbctl status: Secure Boot ENABLED", out)
            if LAPTOP:
                check_laptop_arch(vm)
            poweroff(vm, "the installed Arch system")
    finally:
        vm.stop()
    (WORK / f"{f.tag}-menu-titles.json").write_text(json.dumps(titles))
    save_ckpt(f, 3)
    return True


# =============================================================================================================
# Phase 4: Windows from the GRUB menu, Secure Boot ON
# =============================================================================================================


def phase_boot_windows(f: Flavor) -> bool:
    log(f"E2E-4 [{f.tag}] GRUB menu (Secure Boot ON) -> 'Windows 11' -> the Windows sign-in screen")
    if not load_ckpt(f, 3):
        return False
    titles = json.loads((WORK / f"{f.tag}-menu-titles.json").read_text()) if (WORK / f"{f.tag}-menu-titles.json").exists() else []
    if "Windows 11" not in titles:
        check(False, "the installed grub.cfg has a 'Windows 11' menu entry", str(titles))
        return False
    downs = titles.index("Windows 11")
    print(f"      'Windows 11' is entry #{downs + 1} of {titles}: {downs} x Down", flush=True)
    vm = Vm(f"{f.tag}-boot", f.vars, [f.disk], reboot=True, tpm=True, tpm_dir=f.tpm, tpm_device="tpm-crb",
            disk_bus="nvme", cpu=CPU, extra=EXTRA, nic="e1000e", memory=MEMORY, cpus=CPUS, boot_index=False)
    vm.start()
    reached = False
    try:
        # The keys wait in the firmware's keyboard buffer until GRUB reads them: they stop its 5 s countdown
        # and move the highlight, exactly as a user's fingers would.
        time.sleep(6)
        for _ in range(downs):
            vm.key("down", 0.4)
        started, last, still = time.time(), "", 0
        pressed = False
        while time.time() - started < 420:
            elapsed = time.time() - started
            png, digest, blue = shot(vm, f"{f.tag}-4-grub-{int(elapsed):03d}s")
            if digest == last:
                png.unlink(missing_ok=True)
                still += 1
            else:
                last, still = digest, 0
                print(f"      t+{elapsed:5.1f}s screen {png.name}", flush=True)
            if not pressed and elapsed > 22:
                vm.key("ret", 0.5)
                pressed = True
                print("      pressed Enter on the highlighted entry", flush=True)
            if pressed and elapsed > 200 and still >= 4:
                break
            time.sleep(8 if pressed else 4)
        check("Linux version" not in vm.text(), "Linux did not start: the Windows entry was the one chosen")
        reached = True
        png, _, _ = shot(vm, f"{f.tag}-4-windows-final")
        print(f"      final screen {png.name}", flush=True)
        vm.monitor("system_powerdown")
        if not vm.wait_exit(240):
            print("      Windows did not shut down on the ACPI button: killing QEMU", flush=True)
    finally:
        vm.stop()
    nv = nvram(f.vars)
    print(f"      NVRAM after 4: {describe(nv)}", flush=True)
    save_ckpt(f, 4)
    if LAPTOP:
        ntfs_rw_probe(f, "after Windows booted from the GRUB menu and its ACPI shutdown")
    return reached


# =============================================================================================================
# Phase 5: the disk, offline
# =============================================================================================================


def phase_inspect(f: Flavor) -> bool:
    log(f"E2E-5 [{f.tag}] the installed disk, read offline (VM stopped)")
    source = 4 if (WORK / f"{f.tag}-ckpt4" / "disk.img").exists() else 3
    if not load_ckpt(f, source):
        return False
    SHOTS.mkdir(parents=True, exist_ok=True)
    check_windows_files(f, "final inspection")
    with offline(f.disk) as mounts:
        check(1 in mounts and 2 in mounts, "the ESP and the Windows partition can be read offline", str(list(mounts)))
        windows, esp = mounts.get(2), mounts.get(1)
        if windows:
            finish = windows / "Windows/Temp/archinstall-finish.log"
            check(finish.exists(), r"C:\Windows\Temp\archinstall-finish.log exists")
            if finish.exists():
                text = read_text(finish)
                (SHOTS / f"{f.tag}-5-archinstall-finish.log").write_text(text)
                print(indent(text))
                check("scheduled the firmware boot order change" in text and "deleted the one-time entry" in text,
                      "the finish log says: boot order change scheduled for the next start, one-time entry deleted", text[-600:])
                check("done" in text.splitlines()[-1] if text.strip() else False, "the finish script ran to its last line ('done')", text[-300:])
            scripts = windows / "Windows/Setup/Scripts"
            print("      Windows/Setup/Scripts:", sorted(p.name for p in scripts.glob("*")) if scripts.exists() else "(missing)")
            for name in ("setuperr.log",):
                for candidate in (windows / "Windows/Panther" / name, windows / "Windows/Panther/UnattendGC" / name):
                    if candidate.exists():
                        content = read_text(candidate)
                        (SHOTS / f"{f.tag}-5-{candidate.parent.name}-{name}").write_text(content)
                        errors = [line for line in content.splitlines() if "Error" in line][:6]
                        print(f"      {candidate.relative_to(windows)}: {len(content.splitlines())} lines; first errors: {errors}")
            lab = windows / "Windows/System32/config/SYSTEM"
            print(f"      registry hive present: {lab.exists()} ({lab.stat().st_size if lab.exists() else 0} bytes)")
        if esp:
            files = sorted(str(p.relative_to(esp)) for p in esp.rglob("*") if p.is_file())
            print("      ESP files:")
            for name in files:
                if not name.startswith(("EFI/Microsoft/Boot/Fonts", "EFI/Microsoft/Boot/Resources", "EFI/Microsoft/Recovery")) \
                        and not re.match(r"EFI/Microsoft/Boot/[a-z]{2}-[A-Z]{2}/", name) and not name.startswith("grub/"):
                    print(f"        {name}")
            lower = {n.lower() for n in files}
            for wanted in ("efi/archinstall/shimx64.efi", "efi/archinstall/grubx64.efi", "efi/boot/bootx64.efi",
                           "efi/microsoft/boot/bootmgfw.efi", "efi/microsoft/boot/bcd"):
                if f.expected == "sbctl" and "shimx64" in wanted:
                    continue
                check(wanted in lower, f"ESP has {wanted}", "")
        for number, point in mounts.items():
            if number not in (1, 2, 3):
                print(f"      partition {number}: {sorted(p.name for p in point.glob('*'))[:12]}")
    table = json.loads(subprocess.run(["sfdisk", "-J", str(f.disk)], capture_output=True, text=True).stdout)["partitiontable"]
    for part in table["partitions"]:
        print(f"      partition {part['node'].rsplit('img', 1)[-1]}: {part['size'] * 512 / 2**30:7.1f} GiB  {part.get('name', '')}")
    check("WINSETUP" not in [part.get("name") for part in table["partitions"]],
          "the temporary WINSETUP partition is gone from the partition table (removed by the finish script)")
    nv = nvram(f.vars)
    names = list(nv["entries"].values())
    print(f"      NVRAM: {describe(nv)}", flush=True)
    check("Windows 11 Setup" not in names, "NVRAM: the temporary 'Windows 11 Setup' entry no longer exists")
    check(first_entry(nv) == "ArchInstall", "NVRAM: ArchInstall (GRUB) is FIRST in the boot order")
    check("Windows Boot Manager" in names, "NVRAM: Windows Boot Manager is still registered (second)")
    return True


def phase_reinstall(f: Flavor) -> bool:
    return phase_install(f, second=True)


def phase_windows2(f: Flavor) -> bool:
    return phase_windows(f, second=True)


PHASES = {"install": phase_install, "windows": phase_windows, "mok": phase_mok, "boot": phase_boot_windows,
          "reinstall": phase_reinstall, "windows2": phase_windows2,
          "inspect": phase_inspect, "recover": phase_recover}
ALL_PHASES = ("install", "windows", "mok", "boot", "inspect")  # "recover" only when asked for


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    if MSDM:
        write_msdm()
    for name in sys.argv[1:]:
        flavor, _, phase = name.partition("-")
        if flavor not in FLAVORS or phase not in ("", *PHASES):
            print(f"unknown scenario {name}")
            return 2
        for step in ([phase] if phase else list(ALL_PHASES)):
            if not PHASES[step](FLAVORS[flavor]):
                print(f"\n##### {name}: phase '{step}' could not complete; later phases are NOT run", flush=True)
                break
    failed = [what for ok, what in lv.RESULTS if not ok]
    print(f"\n##### SUMMARY: {len(lv.RESULTS) - len(failed)} ok, {len(failed)} failed")
    for what in failed:
        print(f"  FAIL  {what}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
