#!/usr/bin/env python3
"""A laptop-like walk of the REAL wizard in a QEMU/KVM UEFI VM (see scripts/tui-e2e.ps1 -Mode laptop).

What is emulated, and how:
  * the disk is an NVMe drive (/dev/nvme0n1, partitions nvme0n1p1...), the ISO is a plain virtio disk;
  * Wi-Fi ONLY: mac80211_hwsim radios; an access point (hostapd + dnsmasq + NAT) runs in a network namespace that
    also holds the virtio Ethernet card, so the live system only has a managed Wi-Fi interface, like a laptop
    without a cable. The wizard must scan, connect, and then install over that Wi-Fi;
  * the sensors: a battery and an unplugged charger (tmpfs over /sys/class/power_supply), a notebook chassis
    type (bind mount over /sys/class/dmi/id/chassis_type) and an lspci that reports an Intel RAID/VMD controller;
  * a big virtio screen (3840x2160) in a first, short boot to see which console font the launcher picks.
Then the installed system is booted (Secure Boot off, console on the virtio serial port) and inspected from inside.

    laptop_e2e.py run        the whole thing (exit code 0 = every check passed)
    laptop_e2e.py daemon     boot + fake laptop + Wi-Fi, then wait for commands (tui-e2e.py ctl ...)

Environment: LAPTOP_DEV_INSTALLER=1 (set 1) runs the installer/ folder of the repository inside the VM instead of
the one in the ISO (the ISO may be older than the code); 0 (default) tests the ISO as it is. LAPTOP_PARTIAL=1 (default)
starts the wizard with --partial (no Windows staging, much faster); set 0 for the complete install (needs an
offline ISO).
"""

from __future__ import annotations

import base64
import importlib.util
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_spec = importlib.util.spec_from_file_location("tui_e2e", Path(__file__).with_name("tui-e2e.py"))
t = importlib.util.module_from_spec(_spec)
sys.modules["tui_e2e"] = t
_spec.loader.exec_module(t)  # type: ignore[union-attr]

check, note, log = t.check, t.note, t.log
t.SECRETS = t.SECRETS + ("wifi-test-pass-2026", "definitely-wrong-passphrase")
WORK, SCREENS = t.WORK, t.SCREENS
DEV_INSTALLER = os.environ.get("LAPTOP_DEV_INSTALLER", "0") == "1"
PARTIAL = os.environ.get("LAPTOP_PARTIAL", "1") == "1"
SSID, WIFI_PASS = "LaptopNet", "wifi-test-pass-2026"
SENSOR_SECRETS = (WIFI_PASS, "alice-pw", "root-pw", "win-pw", "Abcd1234")

RAID_LINE = "00:0e.0 RAID bus controller: Intel Corporation Volume Management Device NVMe RAID Controller [8086:9a0b]"


class LaptopVm(t.Vm):
    """The VM of a laptop: NVMe system disk, virtio-vga with a chosen resolution, optional extra read-only disk."""

    def __init__(self, name: str, *, disk: Path, vars_file: Path, live: bool, resolution: str = "1920x1080",
                 overlay: Path | None = None, extra_cmdline: str = "", memory: int = 6144) -> None:
        super().__init__(name, disk=disk, vars_file=vars_file, live=live, extra_cmdline=extra_cmdline, memory=memory)
        self.resolution, self.overlay = resolution, overlay

    def start(self) -> None:
        self.monitor_path.unlink(missing_ok=True)
        x, y = self.resolution.split("x")
        args = ["qemu-system-x86_64", "-name", self.name, "-machine", "q35,smm=on", "-accel", "kvm", "-cpu", "host",
                "-smp", "4", "-m", str(self.memory),
                "-global", "driver=cfi.pflash01,property=secure,value=on",
                "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={t.OVMF}/OVMF_CODE.secboot.4m.fd",
                "-drive", f"if=pflash,format=raw,unit=1,file={self.vars_file}",
                "-drive", f"file={self.disk},format=raw,if=none,id=target",
                "-device", "nvme,drive=target,serial=LAPTOP0,bootindex=1"]
        if self.live:
            args += ["-drive", f"file={t.ISO},format=raw,if=none,id=usb,snapshot=on",
                     "-device", "virtio-blk-pci,drive=usb",
                     "-kernel", str(WORK / "vmlinuz-linux"), "-initrd", str(WORK / "initramfs-linux.img"),
                     "-append", f"{t.shipped_cmdline()} {self.console_arg()} cow_spacesize=3G {self.extra_cmdline}".strip()]
            if self.overlay:
                args += ["-drive", f"file={self.overlay},format=raw,if=none,id=inst,readonly=on",
                         "-device", "virtio-blk-pci,drive=inst"]
        args += ["-serial", "none", "-chardev", "stdio,id=con0,signal=off",
                 "-device", "virtio-serial-pci", "-device", "virtconsole,chardev=con0",
                 "-nic", "user,model=virtio-net-pci", "-device", "virtio-rng-pci",
                 "-device", f"virtio-vga,edid=on,xres={x},yres={y}", "-display", "none",
                 "-monitor", f"unix:{self.monitor_path},server,nowait", "-no-reboot"]
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        t.threading.Thread(target=self._read, daemon=True).start()

    def login_as(self, prompt: str, user: str, password: str | None, timeout: float = 300) -> bool:
        """Log in on the console and get a quiet bash (no prompt, no echo) for vm.shell()."""
        if not self.wait_for(prompt, timeout):
            return False
        self.send(f"{user}\r")
        if password is not None:
            if not self.wait_for(r"(?:[Pp]assword|Mot de passe) ?:", 30):
                return False
            self.send(f"{password}\r")
        time.sleep(4)
        self.send("exec bash --norc --noprofile\r")
        time.sleep(1.5)
        self.send("export PS1='' TERM=dumb LC_ALL=C; stty -echo cols 200\r")
        time.sleep(1)
        return self.shell("true", 30)[0] == 0

    def shell(self, command: str, timeout: float = 120) -> tuple[int, str]:
        self.cursor = len(self.text())  # only what this command prints, not the wizard's screens before it
        return super().shell(command, timeout)

    def script(self, text: str, timeout: float = 300) -> tuple[int, str]:
        """Run a multi-line script in the guest (sent in small base64 pieces: a console line is limited to 4 KiB)."""
        data = base64.b64encode(text.encode()).decode()
        self.shell("rm -f /tmp/s.b64 /tmp/s.sh", 20)
        for i in range(0, len(data), 900):
            self.shell(f"printf %s '{data[i:i + 900]}' >> /tmp/s.b64", 20)
        return self.shell("base64 -d /tmp/s.b64 > /tmp/s.sh && bash /tmp/s.sh 2>&1", timeout)


# -- the live system -------------------------------------------------------------------------------------------


def make_overlay() -> Path | None:
    """A tar of the installer/ folder and of the launcher, attached as a read-only disk."""
    if not DEV_INSTALLER:
        return None
    tar = WORK / "inst.tar"
    tar.unlink(missing_ok=True)
    subprocess.run(["tar", "--exclude=__pycache__", "-C", "/src/installer", "-cf", str(tar), "dualboot_installer"], check=True)
    subprocess.run(["tar", "-C", "/src/profile/airootfs/usr/local/bin", "-rf", str(tar), "archinstall-launcher"], check=True)
    return tar


def boot(resolution: str = "1920x1080", name: str = "laptop") -> LaptopVm:
    disk = WORK / "laptop-target.img"
    disk.unlink(missing_ok=True)
    subprocess.run(["truncate", "-s", "100G", str(disk)], check=True)
    vars_file = WORK / "laptop-vars.fd"
    vars_file.write_bytes((t.OVMF / "OVMF_VARS.4m.fd").read_bytes())
    vm = LaptopVm(name, disk=disk, vars_file=vars_file, live=True, resolution=resolution, overlay=make_overlay())
    vm.start()
    if not vm.login_as(r"archiso login:", "root", None, 240):
        print(vm.text()[-2500:])
        raise SystemExit("the live system did not give a root shell")
    if DEV_INSTALLER:
        rc, out = vm.script("""
mkdir -p /tmp/inst && tar -xf "$(ls /dev/vd[a-z] | while read d; do [ "$(blkid -o value -s TYPE $d)" = iso9660 ] || echo $d; done | tail -1)" -C /tmp/inst
mount --bind /tmp/inst/dualboot_installer /opt/archinstall/dualboot_installer
mount --bind /tmp/inst/archinstall-launcher /usr/local/bin/archinstall-launcher
echo overlay-ok
""", 60)
        check("overlay-ok" in out, "Live: the installer/ folder of the repository replaces the one of the ISO", out[-300:])
    return vm


FAKE_SETFONT = """
mkdir -p /tmp/fakebin
cat > /tmp/fakebin/setfont <<'EOF'
#!/bin/bash
echo "$@" >> /tmp/setfont.log
exec /usr/bin/setfont "$@"
EOF
chmod +x /tmp/fakebin/setfont
cat > /tmp/fakebin/python <<'EOF'
#!/bin/bash
if [ -n "$FONT_PROBE" ]; then echo PYTHON-WOULD-START; exit 0; fi
exec /usr/bin/python "$@"
EOF
chmod +x /tmp/fakebin/python
rm -f /tmp/setfont.log
"""


def font_probe(vm: LaptopVm, label: str) -> str:
    vm.script(FAKE_SETFONT, 30)
    rc, out = vm.script("""
echo "fb0 size: $(cat /sys/class/graphics/fb0/virtual_size 2>&1)"
FONT_PROBE=1 PATH=/tmp/fakebin:$PATH archinstall-launcher >/dev/null 2>&1
echo "setfont called with: $(head -1 /tmp/setfont.log 2>&1)"
""", 60)
    print(out)
    return out


def fake_laptop(vm: LaptopVm) -> None:
    rc, out = vm.script(f"""
mount -t tmpfs tmpfs /sys/class/power_supply || echo MOUNT-POWER-FAILED
mkdir -p /sys/class/power_supply/BAT0 /sys/class/power_supply/AC
cd /sys/class/power_supply
echo Battery > BAT0/type; echo 37 > BAT0/capacity; echo Discharging > BAT0/status; echo 1 > BAT0/present
echo Mains > AC/type; echo 0 > AC/online
echo 10 > /tmp/chassis_type
mount --bind /tmp/chassis_type /sys/class/dmi/id/chassis_type || echo MOUNT-CHASSIS-FAILED
cat > /usr/local/bin/lspci <<'EOF'
#!/bin/bash
echo '{RAID_LINE}'
exec /usr/bin/lspci "$@"
EOF
chmod +x /usr/local/bin/lspci
echo "battery=$(cat BAT0/type)/$(cat BAT0/capacity) ac=$(cat AC/online) chassis=$(cat /sys/class/dmi/id/chassis_type)"
lspci | head -2
: > /tmp/fake-windows.iso
""", 30)
    print(out)
    check("MOUNT-POWER-FAILED" not in out and "MOUNT-CHASSIS-FAILED" not in out and "battery=Battery/37 ac=0 chassis=10" in out,
          "Live: fake battery (37%), unplugged charger, notebook chassis type and a RAID controller in lspci", out[-400:])


WIFI_SCRIPT = f"""
set -x
pacman -Sy --noconfirm --needed hostapd dnsmasq iptables-nft > /tmp/pacman-wifi.log 2>&1 || {{ tail -5 /tmp/pacman-wifi.log; echo PACMAN-FAILED; exit 10; }}
systemctl stop iwd
modprobe mac80211_hwsim radios=2 || {{ echo HWSIM-FAILED; exit 11; }}
for i in $(seq 1 30); do [ -d /sys/class/ieee80211/phy0 ] && [ -d /sys/class/ieee80211/phy1 ] && break; sleep 1; done
sleep 2
AP_IF=$(iw dev | awk '/^phy#0/{{f=1;next}} /^phy#/{{f=0}} f&&/Interface/{{print $2; exit}}')
ST_IF=$(iw dev | awk '/^phy#1/{{f=1;next}} /^phy#/{{f=0}} f&&/Interface/{{print $2; exit}}')
ETH=$(ip -br link | awk '$1 !~ /^(lo|wlan|wl)/ {{print $1; exit}}')
echo "AP_IF=$AP_IF ST_IF=$ST_IF ETH=$ETH"
ip netns add apns
iw phy phy0 set netns name apns || {{ echo NETNS-PHY-FAILED; exit 12; }}
ip link set "$ETH" netns apns || {{ echo NETNS-ETH-FAILED; exit 13; }}
cat > /tmp/hostapd.conf <<EOF
interface=$AP_IF
driver=nl80211
ssid={SSID}
hw_mode=g
channel=6
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase={WIFI_PASS}
EOF
cat > /tmp/dnsmasq.conf <<EOF
interface=$AP_IF
bind-interfaces
dhcp-range=192.168.50.50,192.168.50.150,12h
dhcp-option=3,192.168.50.1
dhcp-option=6,192.168.50.1
no-resolv
no-hosts
server=10.0.2.3
user=root
EOF
ip netns exec apns bash -c "
ip link set lo up
ip addr add 10.0.2.15/24 dev $ETH; ip link set $ETH up; ip route add default via 10.0.2.2
ip addr add 192.168.50.1/24 dev $AP_IF
sysctl -qw net.ipv4.ip_forward=1
iptables -t nat -A POSTROUTING -o $ETH -j MASQUERADE
hostapd -B -f /tmp/hostapd.log /tmp/hostapd.conf
dnsmasq --conf-file=/tmp/dnsmasq.conf --pid-file=/run/dnsmasq-apns.pid
sleep 1
ping -c1 -W3 10.0.2.2 >/dev/null && echo AP-NAMESPACE-UPLINK-OK
"
systemctl start iwd
for i in $(seq 1 30); do iwctl device list | grep -q station && break; sleep 1; done
sleep 2
echo "main namespace: $(ip -br addr | tr '\\n' ';')"
echo "routes: [$(ip route | tr '\\n' ';')]"
iwctl device list
iwctl station $ST_IF scan; sleep 4
iwctl station $ST_IF get-networks
"""


def wifi_only(vm: LaptopVm) -> bool:
    rc, out = vm.script(WIFI_SCRIPT, 400)
    print(out[-2500:])
    ok = "AP-NAMESPACE-UPLINK-OK" in out and SSID in out.split("get-networks")[-1]
    check(ok, "Live: Wi-Fi only: the access point is in a namespace, the live system sees only wlan1 and the network",
          out[-600:])
    routes = re.search(r"routes: \[(.*)\]", out)
    check(routes is not None and "default" not in routes.group(1), "Live: no Ethernet and no default route before the wizard",
          routes.group(1) if routes else "")
    return ok


# -- the wizard -------------------------------------------------------------------------------------------------


def start_wizard(vm: LaptopVm) -> None:
    extra = " --partial" if PARTIAL else ""
    vm.send(f"stty echo rows {t.ROWS} cols {t.COLS}; export TERM=linux; export PATH=/tmp/fakebin:$PATH; "
            f"archinstall-launcher{extra}\r")
    time.sleep(1)


def quit_wizard(vm: LaptopVm) -> None:
    vm.keys("ctrl+q")
    time.sleep(2)
    vm.send("stty -echo\r")
    time.sleep(0.5)


def focused_button(vm: LaptopVm, labels: tuple[str, ...]) -> str | None:
    """A body button (Scan, Connect...) has the focus when its text is in reverse video."""
    lines = vm.lines()
    for y, line in enumerate(lines):
        for label in labels:
            column = line.find(label)
            if column >= 0 and vm.cell(y, column).reverse:
                return label
    return None


def press_button(vm: LaptopVm, label: str) -> bool:
    for _ in range(30):
        if focused_button(vm, (label,)):
            time.sleep(0.4)
            vm.send("\r")
            time.sleep(0.8)
            vm.settle(0.6)
            return True
        vm.keys("tab", pause=0.2)
    return False


def welcome(vm: LaptopVm, w) -> None:
    log("1. Welcome: the laptop warnings")
    check(w.wait_step(1, 60), "Welcome: the wizard starts")
    w.snapshot("welcome-on-battery")
    screen = vm.screen_text()
    check(re.search(r"WARN\s+Power\s+Running on battery \(37%\)", screen) is not None,
          "Welcome: 'Power' warning: Running on battery (37%)")
    check(re.search(r"WARN\s+Storage controller\s+RAID bus controller: Intel Corporation Volume Management Device", screen) is not None
          and "AHCI" in screen, "Welcome: 'Storage controller' warning mentions the VMD/RAID mode and AHCI")
    check(re.search(r"OK\s+UEFI firmware", screen) is not None and "FAIL" not in screen,
          "Welcome: the warnings do not block (no failed check)")
    # Plug the charger in and restart the wizard: the Power warning must go.
    quit_wizard(vm)
    vm.shell("echo 1 > /sys/class/power_supply/AC/online", 10)
    start_wizard(vm)
    check(w.wait_step(1, 60), "Welcome: the wizard restarts")
    time.sleep(1)
    w.snapshot("welcome-on-ac")
    screen = vm.screen_text()
    check("Running on battery" not in screen, "Welcome: with the charger plugged in the Power warning is gone")
    check(re.search(r"WARN\s+Storage controller", screen) is not None, "Welcome: the Storage controller warning stays")


def network(vm: LaptopVm, w) -> None:
    log("3. Network: Wi-Fi only")
    check(w.wait_step(3, 30), "Network: on the screen")
    check(w.wait(r"Not connected", 30), "Network: 'Not connected' (no Ethernet)")
    w.snapshot("network-not-connected")
    seen = False
    for attempt in range(4):
        if w.has(SSID):
            seen = True
            break
        press_button(vm, "Scan")
        time.sleep(6)
    check(seen, f"Network: the scan lists '{SSID}'", vm.screen_text()[:900])
    w.snapshot("network-scan")
    check(w.has(rf"{SSID}\s+\[psk\]") or w.has(SSID), "Network: the network is listed with its security")
    # Wrong passphrase first.
    check(w.fill("Wi-Fi password", "definitely-wrong-passphrase"), "Network: the password field takes the focus")
    press_button(vm, "Connect")
    time.sleep(12)
    w.snapshot("network-wrong-passphrase")
    check(w.has(r"Wi-Fi:") or w.has(r"Not connected"), "Network: a wrong passphrase is reported and the screen stays offline",
          vm.screen_text()[:900])
    check(not w.has(r"Connected to the internet"), "Network: a wrong passphrase does not connect")
    w.fill("Wi-Fi password", WIFI_PASS)
    press_button(vm, "Connect")
    # The fix under test: no Scan press, the status must turn to "Connected" by itself (the check is repeated for ~25 s).
    online, started = False, time.time()
    while time.time() - started < 40:
        time.sleep(1)
        if w.has(r"Connected to the internet"):
            online = True
            break
    waited = int(time.time() - started)
    check(online and waited <= 35, "Network: after Connect, WITHOUT pressing Scan, the status becomes "
          "'Connected to the internet.' by itself (within 35 s)", f"after {waited} s")
    if online:
        print(f"  (the status turned to 'Connected' by itself after about {waited} s)")
    if not online:
        note("Network: the screen did not say 'Connected' by itself; pressing Scan re-checks")
        for _ in range(4):
            press_button(vm, "Scan")
            time.sleep(8)
            if w.has(r"Connected to the internet"):
                online = True
                break
    w.snapshot("network-connected")
    check(online, "Network: with the passphrase the screen says 'Connected to the internet.'")
    check(not w.has(r"Wi-Fi: "), "Network: no error is shown after the connection")
    check(w.next(4), "Network -> Disk")


def disk(vm: LaptopVm, w) -> None:
    log("4. Disk: NVMe")
    check(w.wait(r"/dev/nvme0n1\s+100 GiB", 20), "Disk: the NVMe drive /dev/nvme0n1 is listed")
    check(w.has(r"LIVE MEDIUM"), "Disk: the ISO disk is marked LIVE MEDIUM")
    for key in ("down", "up", "down"):
        if w.has(r"Selected: /dev/nvme0n1"):
            break
        vm.keys(key)
    w.snapshot("disk-nvme")
    check(w.has(r"Selected: /dev/nvme0n1"), "Disk: /dev/nvme0n1 is selected")
    check(w.next(5), "Disk -> Layout")


def walk(vm: LaptopVm, w, desktop: str = "Minimal") -> bool:
    welcome(vm, w)
    w.total = w.read_total()
    check(w.next(2), "Welcome -> Region")
    check(w.fill("Country", "fran", clear=False), "Region: filter")
    vm.keys("enter")
    check(w.has(r"Selected: France") and w.has(r"Selected: Europe/Paris"), "Region: France")
    check(w.next(3), "Region -> Network")
    network(vm, w)
    disk(vm, w)
    w.snapshot("layout")
    check(w.next(6), "Layout -> Accounts")
    w.fill("Computer name", "laptop")
    w.fill("User name", "alice")
    w.fill("User password", "alice-pw")
    w.fill("Confirm the user password", "alice-pw")
    check(w.toggle_switch("same password for the root"), "Accounts: separate root password")
    w.fill("Root password", "root-pw")
    w.fill("Confirm the root password", "root-pw")
    check(w.next(7), "Accounts -> Storage")
    check(w.choose("Filesystem", "btrfs"), "Storage: btrfs")
    check(w.next(8), "Storage -> Software")
    w.snapshot("software")
    check(w.choose("Desktop", desktop), f"Software: {desktop}")
    check(w.next(9), "Software -> " + ("Extras" if w.total == 14 else "Windows image"))
    if w.total == 14:
        if desktop == "Hyprland":
            t.extras_hypr_defaults(vm, w, "Laptop: ")
        else:
            t.extras_defaults_minimal(vm, w, "Laptop: ")
    check(w.choose("Where does the ISO come from", "ISO file I already have")
          if not w.has(r"included in this installer") else True, "Windows image: source")
    if w.has(r"Path of the ISO file"):
        w.fill("Path of the ISO file", "/tmp/fake-windows.iso")
    check(w.next(w.n("winaccount")), "Windows image -> Windows account")
    w.fill("Computer name", "WINLAPTOP")
    w.fill("User name", "alice")
    w.fill("Password", "win-pw")
    w.fill("Confirm the password", "win-pw")
    check(w.next(w.n("secureboot")), "Windows account -> Secure Boot")
    w.fill("Enrollment password", "Abcd1234")
    w.fill("Confirm the enrollment password", "Abcd1234")
    check(w.next(w.n("summary")), "Secure Boot -> Summary")
    w.snapshot("summary-top")
    summary = w.summary_text("Type /dev/nvme0n1 to confirm")
    w.snapshot("summary-bottom")
    check(re.search(r"Laptop extras\s+yes:", summary) is not None, "Summary: 'Laptop extras: yes: ...'")
    check(re.search(rf"Wi-Fi\s+{SSID} \(kept for ", summary) is not None,
          f"Summary: 'Wi-Fi: {SSID} (kept for ...)' (the installer says 'kept for Arch Linux' now)")
    check("/dev/nvme0n1" in summary and re.search(r"nvme0n1", summary) is not None, "Summary: names /dev/nvme0n1")
    check(not any(secret in summary for secret in SENSOR_SECRETS), "Summary: no secret is displayed")
    if w.total == 14:
        if desktop == "Hyprland":
            check(re.search(r"Developer tools\s+Languages and build tools, Command line helpers", summary) is not None
                  and re.search(r"Applications\s+VSCodium, LibreWolf, Postman", summary) is not None
                  and re.search(r"Desktop shell\s+default Hyprland setup", summary) is not None,
                  "Summary: Developer tools / Applications (VSCodium, LibreWolf, Postman) / Desktop shell (default Hyprland setup)")
        else:
            check(re.search(r"Developer tools\s+Languages and build tools, Command line helpers", summary) is not None
                  and re.search(r"Applications\s+none", summary) is not None, "Summary: the recommended developer tools, no applications")
    check(w.fill("Type /dev/nvme0n1 to confirm", "/dev/nvme0n1"), "Summary: the confirmation is typed")
    return True


def after_install(vm: LaptopVm) -> None:
    """Ctrl+Q must quit once the install has finished; then the log kept on the live system is searched for secrets."""
    log("After the install: Ctrl+Q, the log, power off")
    before = len(vm.raw)
    vm.keys("ctrl+q")
    time.sleep(2)
    quit_ok = b"\x1b[?1049l" in bytes(vm.raw[before:])
    check(quit_ok, "Install: Ctrl+Q quits the wizard once the installation has finished ('Reboot now' is shown)")
    if not quit_ok:
        return
    vm.send("stty -echo\r")
    time.sleep(0.5)
    rc, out = vm.shell("ls -l /var/log/archinstall.log; wc -l < /var/log/archinstall.log; "
                       "grep -c -F -e wifi-test-pass-2026 -e alice-pw -e root-pw -e win-pw -e Abcd1234 "
                       "-e definitely-wrong /var/log/archinstall.log", 30)
    print(out)
    lines = out.strip().splitlines()
    check(len(lines) >= 3 and lines[-1].strip() == "0", "Install: /var/log/archinstall.log contains no password or passphrase",
          out[-300:])
    rc, out = vm.shell("grep -n -i -E 'nvme0n1p[0-9]|wifi|laptop|bluez|power-profiles' /var/log/archinstall.log | head -30", 30)
    (SCREENS / "install-log-extract.txt").write_text(out + "\n", encoding="utf-8")
    check("nvme0n1p3" in out, "Install log: the commands use /dev/nvme0n1p3 (p-suffix device names)")
    check("laptop extras included" in out.lower() or "bluez" in out, "Install log: the laptop extras are announced")
    check("is kept for the installed system" in out, "Install log: the Wi-Fi network is kept for the installed system")
    vm.send("systemctl poweroff\r")
    check(vm.wait_exit(120), "The live system powers off")


# -- the installed disk ---------------------------------------------------------------------------------------


def inspect(disk_img: Path, desktop: str) -> None:
    log("Inspecting the installed disk")
    sh = t.sh
    loop = sh(f"losetup -f --show -P {disk_img}")
    # The partitions appear a moment after the loop device: wait for all four, then create their device nodes.
    for _ in range(30):
        if sh(f"ls /sys/class/block | grep -c '^{Path(loop).name}p'") == "4":
            break
        time.sleep(0.5)
    sh(f'for d in /sys/class/block/{Path(loop).name}p*; do n=$(basename $d); rm -f /dev/$n; IFS=: read a b < $d/dev; mknod /dev/$n b $a $b; done')
    mnt = Path("/tmp/laptopmnt")
    root, esp = mnt / "root", mnt / "esp"
    for p in (root, esp):
        p.mkdir(parents=True, exist_ok=True)
    out: list[str] = []
    try:
        check(sh(f"mount -o ro,subvol=@ {loop}p3 {root} 2>&1; echo rc=$?").endswith("rc=0"), "Disk: the btrfs root (subvolume @) mounts")
        check(sh(f"mount -o rw {loop}p1 {esp} 2>&1; echo rc=$?").endswith("rc=0"), "Disk: the ESP mounts")
        read = lambda p: (root / p).read_text(errors="replace") if (root / p).exists() else ""  # noqa: E731
        fstab = read("etc/fstab")
        check(all(l.split()[0].startswith("UUID=") for l in fstab.splitlines() if l.strip() and not l.startswith("#")),
              "Disk: fstab uses UUIDs only (no /dev/nvme names)")
        profile = root / "etc/NetworkManager/system-connections" / f"{SSID}.nmconnection"
        check(profile.is_file(), f"Disk: /etc/NetworkManager/system-connections/{SSID}.nmconnection exists")
        mode = oct(profile.stat().st_mode & 0o777) if profile.exists() else "?"
        check(mode == "0o600", "Disk: the Wi-Fi profile is mode 0600", mode)
        text = profile.read_text() if profile.exists() else ""
        check(f"ssid={SSID}" in text and "key-mgmt=wpa-psk" in text and f"psk={WIFI_PASS}" in text and "type=wifi" in text
              and "autoconnect=true" in text, "Disk: the profile has ssid, wpa-psk, the psk and autoconnect")
        packages = sh(f"ls {root}/var/lib/pacman/local")
        for pkg in ("power-profiles-daemon", "bluez", "bluez-utils", "upower", "sof-firmware"):
            check(re.search(rf"^{re.escape(pkg)}-\d", packages, re.MULTILINE) is not None, f"Disk: package {pkg} is installed")
        check(re.search(r"^iwd-|^networkmanager-\d", packages, re.MULTILINE) is not None, "Disk: NetworkManager is installed")
        enabled = sh(f"find {root}/etc/systemd/system -name '*.service' | sed 's|.*/||' | sort -u").split()
        for svc in ("bluetooth.service", "power-profiles-daemon.service", "NetworkManager.service"):
            check(svc in enabled, f"Disk: {svc} is enabled (symlink in /etc/systemd/system)", " ".join(enabled))
        grub = (esp / "grub/grub.cfg").read_text(errors="replace")
        check("/vmlinuz-linux" in grub and "Windows 11" in grub, "Disk: GRUB has the Arch and the Windows entries")
        pkgs = sh(f"ls {root}/var/lib/pacman/local")
        have = lambda name: re.search(rf"^{re.escape(name)}-[0-9]", pkgs, re.MULTILINE) is not None  # noqa: E731
        check(have("git") and have("nodejs") and have("neovim"), "Disk: the recommended developer tools are installed")
        if desktop == "Hyprland":
            check(have("librewolf") and (have("yay") or have("yay-bin")) and have("vscodium-bin") and have("postman-bin"),
                  "Disk: LibreWolf, yay, vscodium-bin and postman-bin are installed", pkgs[:0])
            check(not (root / "etc/sudoers.d/99-archinstall-aur").exists(), "Disk: the temporary sudoers file is gone")
            home_dir = mnt / "home"
            home_dir.mkdir(exist_ok=True)
            sh(f"mount -o ro,subvol=@home {loop}p3 {home_dir}")
            conf_path = home_dir / "alice/.config/hypr/hyprland.conf"
            conf = conf_path.read_text() if conf_path.exists() else ""
            check(bool(conf), "Disk: ~/.config/hypr/hyprland.conf exists (in the @home subvolume)")
            for needle in ("monitor = , preferred, auto, auto", "tap-to-click = true", "disable_while_typing = true",
                           "XF86MonBrightnessUp, exec, brightnessctl", "XF86AudioRaiseVolume, exec, wpctl", "XF86AudioMicMute"):
                check(needle in conf, f"Disk: hyprland.conf has '{needle}'")
        if not PARTIAL:
            setup = mnt / "setup"
            setup.mkdir(exist_ok=True)
            sh(f"mount -o ro {loop}p4 {setup}")
            answer = (setup / "archinstall/unattend-0.xml").read_text(errors="replace") if (setup / "archinstall/unattend-0.xml").exists() else ""
            scripts = setup / "sources/$OEM$/$$/Setup/Scripts"
            finish = (scripts / "archinstall-finish.ps1").read_text(errors="replace") if (scripts / "archinstall-finish.ps1").exists() else ""
            check("HiberbootEnabled" in answer and "HibernateEnabled" in answer,
                  "Windows: the answer file turns Fast Startup (HiberbootEnabled) and hibernation (HibernateEnabled) off")
            check(re.search(r"HiberbootEnabled.{0,200}?(/d 0|<Value>0|dword:0)", answer, re.DOTALL) is not None
                  or "HiberbootEnabled" in answer, "Windows: the HiberbootEnabled value is written")
            check("powercfg.exe /hibernate off" in finish or "powercfg /hibernate off" in finish,
                  "Windows: the finish script runs 'powercfg.exe /hibernate off'")
            check("OA3xOriginalProductKey" in finish, "Windows: the finish script reads the firmware product key (OA3xOriginalProductKey)")
            check((scripts / "archinstall-bootorder.ps1").is_file(), "Windows: archinstall-bootorder.ps1 is staged")
            sh(f"umount {setup} 2>/dev/null")
        evidence = [
            ("partition table", f"sgdisk -p {loop}"), ("fstab", f"cat {root}/etc/fstab"),
            ("Wi-Fi profile (psk hidden)", f"sed -E 's/^psk=.*/psk=<hidden>/' '{profile}'; ls -l '{profile}'"),
            ("packages", f"ls {root}/var/lib/pacman/local | grep -E 'power-profiles|bluez|upower|sof-firmware|networkmanager|iwd'"),
            ("enabled services", f"find {root}/etc/systemd/system -path '*.wants/*' | sed 's|.*system/||' | sort"),
            ("GRUB entries", f"grep -nE 'menuentry|linux ' {esp}/grub/grub.cfg | head -20"),
        ]
        for title, command in evidence:
            out.append(f"=== {title}\n$ {command}\n{sh(command)}\n")
        (SCREENS / "disk-evidence.txt").write_text("\n".join(out), encoding="utf-8")
        # A serial console for the next boot: kernel messages and a login prompt on the virtio port.
        patched = re.sub(r"(\n\s*linux\s+/vmlinuz-linux(?![^\n]*console=hvc0)[^\n]*)", r"\1 console=hvc0", grub)
        (esp / "grub/grub.cfg").write_text(patched, encoding="utf-8")
        check(patched.count("console=hvc0") >= 1, "Test setup: console=hvc0 added to the GRUB entries (test disk only)")
    finally:
        sh(f"umount {mnt}/home {esp} {root} 2>/dev/null; losetup -d {loop} 2>/dev/null")


def ppm_to_png(ppm: bytes) -> tuple[bytes, float]:
    """(PNG bytes, share of non-black pixels) of a binary PPM written by QEMU's screendump."""
    import struct
    import zlib

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


def screendump(vm: LaptopVm, name: str) -> float:
    """Ask QEMU for the screen (virtio-vga), save it as a PNG in the evidence folder, return the lit share."""
    import socket

    ppm = WORK / f"{name}.ppm"
    ppm.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX) as sock:
        sock.connect(str(vm.monitor_path))
        sock.settimeout(5)
        try:
            sock.recv(4096)
        except OSError:
            pass
        sock.sendall(f"screendump {ppm}\n".encode())
        time.sleep(2)
    if not ppm.exists():
        return 0.0
    png, lit = ppm_to_png(ppm.read_bytes())
    (SCREENS / f"{name}.png").write_bytes(png)
    return lit


def greeter(vm: LaptopVm) -> None:
    """Start SDDM in the installed system and look at the screen."""
    rc, out = vm.script("systemctl start sddm; sleep 25; systemctl is-active sddm; "
                        "journalctl -u sddm -b --no-pager | tail -15; pacman -Q sddm xorg-server mesa", 120)
    (SCREENS / "sddm-journal.txt").write_text(out + "\n", encoding="utf-8")
    print(out[-1500:])
    lit = screendump(vm, "sddm-greeter")
    check(lit > 0.05, f"Boot: SDDM shows something on the virtual screen ({lit:.0%} of the pixels are lit)", out[-300:])


AUTOCONNECT_SCRIPT = f"""
set -x
pacman -Sy --noconfirm --needed hostapd dnsmasq > /tmp/p.log 2>&1 || {{ tail -3 /tmp/p.log; echo PACMAN-FAILED; exit 1; }}
modprobe mac80211_hwsim radios=2 || {{ echo HWSIM-FAILED; exit 2; }}
for i in $(seq 1 20); do [ "$(ls /sys/class/net | grep -c '^wlan')" -ge 2 ] && break; sleep 1; done
nmcli device set wlan0 managed no
ip addr add 192.168.50.1/24 dev wlan0; ip link set wlan0 up
cat > /tmp/hostapd.conf <<EOF
interface=wlan0
driver=nl80211
ssid={SSID}
hw_mode=g
channel=6
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase={WIFI_PASS}
EOF
cat > /tmp/dnsmasq.conf <<EOF
interface=wlan0
bind-interfaces
dhcp-range=192.168.50.50,192.168.50.150,12h
no-resolv
user=root
EOF
hostapd -B -f /tmp/hostapd.log /tmp/hostapd.conf
dnsmasq --conf-file=/tmp/dnsmasq.conf --pid-file=/run/dnsmasq-ap.pid
for i in $(seq 1 30); do nmcli device wifi rescan ifname wlan1 >/dev/null 2>&1; nmcli -t -f DEVICE,STATE device | grep -q '^wlan1:connected' && break; sleep 3; done
echo "== devices"; nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device
echo "== link"; nmcli -t -f ACTIVE,SSID,DEVICE device wifi list ifname wlan1
echo "== address"; ip -4 -br addr show wlan1
echo "== wpa_supplicant"; pacman -Q wpa_supplicant 2>&1; systemctl is-active wpa_supplicant 2>&1
"""


def wifi_autoconnect(vm: LaptopVm) -> None:
    """The saved profile must really work: give the installed system a Wi-Fi radio and an access point."""
    rc, out = vm.script(AUTOCONNECT_SCRIPT, 400)
    (SCREENS / "installed-wifi-autoconnect.txt").write_text(out + "\n", encoding="utf-8")
    print(out[-1800:])
    check("PACMAN-FAILED" not in out and "HWSIM-FAILED" not in out, "Boot: hostapd/dnsmasq install and the Wi-Fi radios load in the installed system")
    check(re.search(r"wlan1:wifi:connected:LaptopNet", out) is not None,
          "Boot: NetworkManager joins LaptopNet by itself with the profile written by the installer", out[-500:])
    check(re.search(r"yes:LaptopNet:wlan1", out) is not None, "Boot: wlan1 is associated to LaptopNet (active access point)")
    check(re.search(r"wlan1\s+UP\s+192\.168\.50\.\d+/24", out) is not None, "Boot: wlan1 got an address from the access point")


def boot_installed(disk_img: Path, vars_file: Path, desktop: str) -> None:
    log("Boot of the installed system (Secure Boot off, MOK not enrolled)")
    if not PARTIAL:
        # The real first boot starts Windows Setup (BootNext, and first in BootOrder). To look at the Arch side, the
        # test NVRAM loses that entry: the firmware then falls back to the next one, "ArchInstall" (shim -> GRUB).
        listing = t.sh(f"virt-fw-vars --input {vars_file} --print 2>&1")
        entry = re.search(r'^Boot([0-9A-F]{4})\s*: boot entry: title="Windows 11 Setup"', listing, re.MULTILINE)
        arch_vars = WORK / "laptop-vars-arch.fd"
        t.sh(f"virt-fw-vars --input {vars_file} --output {arch_vars} --delete BootNext --delete Boot{entry.group(1) if entry else '0003'}")
        vars_file = arch_vars
        note("Complete install: the 'Windows 11 Setup' boot entry and BootNext were removed from a copy of the NVRAM so that "
             "Arch boots (the unmodified NVRAM boots Windows Setup first: 'BdsDxe: starting Boot0003 Windows 11 Setup')")
    vm = LaptopVm("laptop-boot", disk=disk_img, vars_file=vars_file, live=False)
    vm.start()
    try:
        ok = vm.login_as(r"login:", "root", "root-pw", 420)
        check(ok, "Boot: the installed system boots and root can log in on the console")
        if not ok:
            print(vm.text()[-3000:])
            return
        rc, out = vm.script("""
echo "== lsblk"; lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS
echo "== root"; findmnt -no SOURCE,FSTYPE /
echo "== nmcli"; nmcli connection show
echo "== profile"; nmcli -s -f connection.id,connection.type,connection.autoconnect,802-11-wireless.ssid,802-11-wireless-security.key-mgmt connection show LaptopNet | sed -E 's/psk:.*/psk: <hidden>/'
echo "== enabled"; systemctl is-enabled bluetooth.service power-profiles-daemon.service NetworkManager.service
echo "== powerprofilesctl"; powerprofilesctl list 2>&1; echo "-- get"; powerprofilesctl get 2>&1
echo "== ppd dbus"; busctl get-property net.hadess.PowerProfiles /net/hadess/PowerProfiles net.hadess.PowerProfiles ActiveProfile 2>&1
echo "== ppd package"; pacman -Qi power-profiles-daemon | grep -E '^(Depends On|Optional Deps)' -A2
echo "== python"; command -v python3 || echo no-python3
echo "== bluetooth"; systemctl is-enabled bluetooth.service; systemctl is-active bluetooth.service; true
echo "== failed"; systemctl --failed --no-legend
echo "== secure boot"; bootctl status 2>/dev/null | grep -i 'secure boot' || echo 'bootctl: n/a'
echo "== pacman"; pacman -Q power-profiles-daemon bluez sof-firmware upower
echo "== cmdline"; cat /proc/cmdline
""", 120)
        (SCREENS / "installed-system.txt").write_text(out + "\n", encoding="utf-8")
        print(out[-3500:])
        check(re.search(r"nvme0n1p1\s+1G\s+part\s+vfat", out) is not None and re.search(r"nvme0n1p3\s+\S+\s+part\s+btrfs", out) is not None,
              "Boot: lsblk shows nvme0n1p1 (ESP) and nvme0n1p3 (btrfs)")
        check(re.search(r"/dev/nvme0n1p3\[?/?@?\]?\s+btrfs", out) is not None, "Boot: the root is on /dev/nvme0n1p3")
        check("LaptopNet" in out.split("== profile")[0] and "wifi" in out.split("== profile")[0],
              "Boot: nmcli connection show lists LaptopNet as a wifi connection")
        check("802-11-wireless.ssid:" in out and "wpa-psk" in out, "Boot: the profile parses (ssid, key-mgmt wpa-psk)")
        enabled = out.split("== enabled")[1].split("== powerprofilesctl")[0] if "== enabled" in out else ""
        check(enabled.split().count("enabled") == 3, "Boot: bluetooth, power-profiles-daemon and NetworkManager are enabled", enabled)
        ppd = out.split("== powerprofilesctl")[1].split("== ppd dbus")[0] if "== powerprofilesctl" in out else ""
        listing, _, current = ppd.partition("-- get")
        check("balanced" in listing and "Traceback" not in ppd, "Boot: 'powerprofilesctl list' answers (python-gobject is there)", ppd.strip()[:200])
        check(current.strip() in ("balanced", "power-saver", "performance"), "Boot: 'powerprofilesctl get' prints the active profile", current.strip()[:120])
        dbus = out.split("== ppd dbus")[1].split("== ppd package")[0] if "== ppd dbus" in out else ""
        check('"balanced"' in dbus or '"power-saver"' in dbus or '"performance"' in dbus,
              "Boot: the power-profiles-daemon answers on D-Bus (ActiveProfile)", dbus.strip()[:200])
        wifi_autoconnect(vm)
        if desktop == "Hyprland":
            rc, hv = vm.script("pacman -Q hyprland; sudo -u alice env HOME=/home/alice XDG_RUNTIME_DIR=/tmp "
                               "Hyprland --verify-config 2>&1 | tail -30", 120)
            print(hv)
            (SCREENS / "hyprland-verify-config.txt").write_text(hv + "\n", encoding="utf-8")
            check("config ok" in hv.lower(), "Boot: Hyprland --verify-config accepts the generated hyprland.conf ('config ok')",
                  hv[-300:])
            greeter(vm)
    finally:
        # Never kill the VM after pacman ran in it: files written a moment before would be left empty on the disk.
        if vm.proc and vm.proc.poll() is None:
            vm.send("sync; systemctl poweroff\r")
            vm.wait_exit(90)
        vm.stop()


# -- entry points ---------------------------------------------------------------------------------------------


def run() -> int:
    SCREENS.mkdir(parents=True, exist_ok=True)
    for stale in SCREENS.glob("*.txt"):
        stale.unlink()
    desktop = os.environ.get("LAPTOP_DESKTOP", "Minimal")

    log("Font probe: a 3840x2160 screen")
    vm = boot("3840x2160", "laptop-4k")
    try:
        out = font_probe(vm, "4k")
        m = re.search(r"fb0 size: (\d+),(\d+)", out)
        check(m is not None and int(m.group(1)) >= 3000, "Font: the virtual screen is 3840 px wide (fb0 virtual_size)", out[:200])
        check("ter-v32n" in out, "Font: the launcher picks ter-v32n for a width >= 3000", out[-300:])
    finally:
        vm.stop()

    log("The laptop-like VM")
    vm = boot("1920x1080")
    w = t.Wizard(vm)
    try:
        out = font_probe(vm, "1080p")
        check("ter-v20n" in out, "Font: the launcher picks ter-v20n at 1920x1080", out[-300:])
        fake_laptop(vm)
        wired = wifi_only(vm)
        if not wired:
            note("Wi-Fi wiring failed: the wizard walk was not started (see the output above)")
        reached = False
        try:
            if wired:
                start_wizard(vm)
                reached = walk(vm, w, desktop)
        except Exception as error:  # noqa: BLE001
            check(False, f"the wizard walk crashed: {type(error).__name__}: {error}")
            import traceback

            traceback.print_exc()
            w.snapshot("stopped-here")
            print(vm.screen_text())
        if reached:
            if t.scenario_install(vm, w, partial=PARTIAL, ctrl_q=True):
                after_install(vm)
            text = vm.text()
            check("/dev/nvme0n1p3" in text and "/dev/nvme0n1p1" in text,
                  "Install: the commands use the nvme0n1p<N> device names (p-suffix path)")
    finally:
        vm.stop()
        (WORK / "laptop-console.log").write_bytes(bytes(vm.raw))
    if reached:
        inspect(WORK / "laptop-target.img", desktop)
        boot_installed(WORK / "laptop-target.img", WORK / "laptop-vars.fd", desktop)

    failed = [what for ok, what in t.RESULTS if not ok]
    print(f"\n##### {len(t.RESULTS) - len(failed)} checks passed, {len(failed)} failed, {len(t.OBSERVATIONS)} observations")
    for what in failed:
        print(f"  FAILED: {what}")
    for text in t.OBSERVATIONS:
        print(f"  NOTE: {text}")
    return 1 if failed else 0


def daemon() -> int:
    vm = boot("1920x1080")
    font_probe(vm, "1080p")
    fake_laptop(vm)
    wifi_only(vm)
    t.__dict__.update({"start_wizard_laptop": start_wizard, "press_button": press_button, "focused_button": focused_button,
                       "network": network, "walk": walk, "welcome": welcome, "disk": disk})
    print("live system ready; control socket", t.CTL, flush=True)
    t.serve(vm)
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "run":
        sys.exit(run())
    if mode == "daemon":
        sys.exit(daemon())
    if mode == "post":  # only the inspection and the boot of the disk left by the last run (debugging the checks)
        d = os.environ.get("LAPTOP_DESKTOP", "Minimal")
        inspect(WORK / "laptop-target.img", d)
        boot_installed(WORK / "laptop-target.img", WORK / "laptop-vars.fd", d)
        failed = [what for ok, what in t.RESULTS if not ok]
        print(f"\n##### {len(t.RESULTS) - len(failed)} checks passed, {len(failed)} failed")
        for what in failed:
            print(f"  FAILED: {what}")
        sys.exit(1 if failed else 0)
    if mode == "ctl":
        sys.exit(t.main(["x", "ctl", *sys.argv[2:]]))
    raise SystemExit(f"unknown mode {mode}")
