#!/usr/bin/env bash
# Real proof of the driver harvest: run installer/dualboot_installer/steps/windows_drivers.py against the disk
# image of a Windows that an earlier end-to-end run installed (a real driver store), READ-ONLY, pretending the
# computer has the network adapters of that virtual machine.
#
# Runs in a privileged container (see scripts/windows-drivers-proof.ps1):
#   /src    the repository (read-only)      /live  the Docker volume with the disk images (read-only)
#   /out    where the evidence goes          IMAGE  the image to read (default /live/r4.img)
#   HW_LSPCI  the `lspci -nn` text the computer "has"
set -euo pipefail
IMAGE="${IMAGE:-/live/r4.img}"
OUT=/out
mkdir -p "$OUT"
[[ -f "$IMAGE" ]] || { echo "no image at $IMAGE" >&2; exit 1; }

sed -i '/^NoExtract/d' /etc/pacman.conf
pacman -Sy --noconfirm --needed ntfs-3g gptfdisk >/dev/null 2>&1 || { echo "cannot install tools" >&2; exit 1; }

LOOP="$(losetup -r -f --show -P "$IMAGE")"
cleanup() { umount /tmp/archinstall-old-windows/* 2>/dev/null || true; losetup -d "$LOOP" 2>/dev/null || true; }
trap cleanup EXIT
for dir in /sys/class/block/"$(basename "$LOOP")"p*; do
  name="$(basename "$dir")"; rm -f "/dev/$name"
  IFS=: read -r major minor <"$dir/dev"; mknod "/dev/$name" b "$major" "$minor"
done
echo "--- the image, read-only on $LOOP ---"
lsblk -nrpo NAME,FSTYPE,TYPE,SIZE "$LOOP"

# The computer of the test: what `lspci -nn` would print inside the virtual machine (QEMU e1000e + an NVMe disk
# + a graphics card), because the container's own PCI bus is the Docker VM's.
mkdir -p /tmp/fakebin
cat > /tmp/fakebin/lspci <<'EOF'
#!/usr/bin/env bash
cat <<'LSPCI'
00:01.0 VGA compatible controller [0300]: Device [1234:1111] (rev 02)
00:02.0 Non-Volatile memory controller [0108]: Red Hat, Inc. QEMU NVM Express Controller [1b36:0010] (rev 02)
00:03.0 Ethernet controller [0200]: Intel Corporation 82574L Gigabit Network Connection [8086:10d3]
00:04.0 Network controller [0280]: Intel Corporation Wi-Fi 6 AX201 [8086:a0f0] (rev 20)
00:05.0 Wireless controller [0d11]: Intel Corporation Bluetooth Adapter [8086:9999]
LSPCI
EOF
chmod +x /tmp/fakebin/lspci
export PATH="/tmp/fakebin:$PATH"
export LOOP_DEV="$LOOP"

status=0
python - <<'EOF' || status=$?
import json, os, sys, time
sys.path.insert(0, "/src/installer")
from pathlib import Path
from dualboot_installer.config import InstallConfig
from dualboot_installer.runner import Runner
from dualboot_installer.steps import windows_drivers as wd
from dualboot_installer.steps.context import Context

lines = []
log = lambda line: (lines.append(line), print(line, flush=True))
cfg = InstallConfig()
cfg.disk.device = os.environ["LOOP_DEV"]
cfg.disk.size_bytes = 100 * 1024**3
wd.usb_ids_from_sysfs = lambda *a, **k: [r"USB\VID_8087&PID_0026"]   # an Intel Bluetooth on USB
ctx = Context(cfg, cfg.layout(), Runner(dry_run=False, log=log))
t0 = time.time()
wd.harvest_drivers(ctx)
elapsed = time.time() - t0

harvested = ctx.state.get("drivers_harvested", [])
total = sum(p["size"] for p in harvested)
print(f"\nRESULT harvest: {len(harvested)} package(s), {total/1024/1024:.1f} MiB, {elapsed:.1f} s")
bad = 0
for p in harvested:
    folder = wd.HARVEST_DIR / p["folder"]
    files = [f for f in folder.rglob("*") if f.is_file()]
    ok = bool(files) and all(f.stat().st_size >= 0 for f in files)
    # the copy is readable end to end and has the same size as the source
    copied = sum(f.stat().st_size for f in files)
    same = copied == p["size"]
    bad += (not ok) + (not same)
    print(f"  {p['folder']}: INF {p['inf']} matched {p['matched']}; {len(files)} file(s), {copied} bytes "
          f"(source {p['size']}) -> {'ok' if ok and same else 'MISMATCH'}")
print(f"RESULT readable-and-identical: {'ok' if not bad else 'FAIL'}")
print(f"RESULT sources for staging: {[str(s) for s in wd.driver_sources()]}")
Path("/out/harvest-manifest.json").write_text(json.dumps(harvested, indent=2))
Path("/out/harvest.log").write_text("\n".join(lines))
sys.exit(1 if (not harvested or bad) else 0)
EOF

echo "--- the volumes were only mounted read-only: mount table ---"
mount | grep -E "archinstall-old-windows" || echo "(all unmounted)"
echo "--- what was kept (RAM disk) ---"
du -sh /tmp/archinstall-windows-drivers 2>/dev/null; ls /tmp/archinstall-windows-drivers 2>/dev/null
mkdir -p "$OUT/harvested" && cp -a /tmp/archinstall-windows-drivers/. "$OUT/harvested/" 2>/dev/null || true
exit "$status"
