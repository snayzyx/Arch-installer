#!/usr/bin/env bash
# Boots a disk image made by test-install.sh -WindowsIso (kept in the archinstall-winimg volume)
# in QEMU with KVM and an OVMF firmware that ENFORCES Secure Boot, and takes screenshots of the
# virtual screen into /out/screens (= <repo>\out\screens on the host). Windows has no serial
# console, so the screenshots are how the unattended Windows Setup is observed.
#
#   windows-boot.sh <label> [minutes]
#
# env: FRESH_VARS=1  start from fresh firmware variables (first boot). Otherwise the NVRAM of the
#                    previous run is reused, which is what a real machine does across reboots.
#      MEMORY=4096   guest RAM in MiB
#      SHOT_EVERY=45 seconds between screenshots
#
# While it runs, from the host:
#   docker exec <container> qmon "sendkey ret"      (QEMU monitor commands)
#   create out\screens\STOP-<label> to stop it early.
set -euo pipefail

LABEL="${1:?label}"
MINUTES="${2:-30}"
IMG=/img/disk.img
VARS=/img/vars.fd
OVMF_DIR=/usr/share/edk2/x64
SHOTS="${SHOTS_DIR:-/out/screens}"
SHIM_LOCK_GUID=605dab50-e046-4300-abb6-3dd810dd8b23
MEMORY="${MEMORY:-4096}"
SHOT_EVERY="${SHOT_EVERY:-45}"
FAST_UNTIL="${FAST_UNTIL:-150}" # seconds of frequent screenshots at the start
FAST_EVERY="${FAST_EVERY:-3}"
QEMU_CPU="${QEMU_CPU:-host}"
QEMU_DISPLAY="${QEMU_DISPLAY:--vga std}" # e.g. "-vga none -device ramfb"
QEMU_EXTRA="${QEMU_EXTRA:-}"

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

[[ -f "$IMG" ]] || { echo "no disk image at $IMG: run scripts/test-install.ps1 -WindowsIso ... -PersistImage first" >&2; exit 1; }
[[ -c /dev/kvm ]] || { echo "/dev/kvm is missing: no hardware acceleration in this Docker VM" >&2; exit 1; }

sed -i '/^NoExtract/d' /etc/pacman.conf
pacman -Sy --noconfirm --needed qemu-system-x86 edk2-ovmf virt-firmware python >/dev/null 2>&1 \
  || { echo "cannot install QEMU / OVMF" >&2; exit 1; }
mkdir -p "$SHOTS"


# Attach the disk image and create the partition device nodes (no udev in a container). The
# minor numbers of loop partitions change at every attach, so stale nodes are replaced.
attach_image() {  # attach_image <losetup options...>; sets LOOP
  LOOP="$(losetup "$@" -f --show -P "$IMG")"
  local dir name major minor
  for dir in /sys/class/block/"$(basename "$LOOP")"p*; do
    name="$(basename "$dir")"
    rm -f "/dev/$name"
    IFS=: read -r major minor <"$dir/dev"
    mknod "/dev/$name" b "$major" "$minor"
  done
}

# A helper to talk to the QEMU monitor from `docker exec`.
cat >/usr/local/bin/qmon <<'EOF'
#!/usr/bin/env python3
import socket, sys, time
s = socket.socket(socket.AF_UNIX)
s.connect("/tmp/qmon.sock")
s.settimeout(2)
try:
    s.recv(65536)
except OSError:
    pass
s.sendall((" ".join(sys.argv[1:]) + "\n").encode())
time.sleep(0.4)
try:
    print(s.recv(65536).decode(errors="replace"))
except OSError:
    pass
EOF
chmod +x /usr/local/bin/qmon

# Types a text into the guest through the monitor: qtype 'hostname' ; qtype -n 'dir' (+ Enter) ;
# qtype --fr ... when the guest has the French AZERTY keyboard.
cat >/usr/local/bin/qtype <<'EOF'
#!/usr/bin/env python3
import socket, sys, time
SHIFTED = {":": ";", '"': "'", "{": "[", "}": "]", "|": "\\", ">": ".", "<": ",", "_": "-", "*": "8", "(": "9",
           ")": "0", "$": "4", "%": "5", "&": "7", "!": "1", "@": "2", "#": "3", "^": "6", "+": "=", "?": "/", "~": "`"}
# QEMU names of the keys that are not a letter or a digit.
NAMES = {" ": "spc", "-": "minus", "=": "equal", "[": "bracket_left", "]": "bracket_right", "\\": "backslash",
         ";": "semicolon", "'": "apostrophe", "`": "grave_accent", ",": "comma", ".": "dot", "/": "slash"}
# The French AZERTY layout (what a test answer file with keyboard "fr" gives): where the key of each
# character is, in US key names. Letters not listed are at the same place; digits need Shift.
AZERTY = {"a": "q", "q": "a", "z": "w", "w": "z", "m": "semicolon", "&": "1", '"': "3", "'": "4", "(": "5",
          "-": "6", "_": "8", ")": "minus", "=": "equal", ",": "m", ";": "comma", ":": "dot", "!": "slash",
          ".": "shift-comma", "/": "shift-dot", "?": "shift-m", "$": "bracket_right", "*": "backslash",
          "<": "less", ">": "shift-less", "+": "shift-equal", "%": "shift-apostrophe", " ": "spc",
          "\\": "alt_r-8", "{": "alt_r-4", "}": "alt_r-equal", "[": "alt_r-5", "]": "alt_r-minus",
          "|": "alt_r-6", "@": "alt_r-0", "#": "alt_r-3", "~": "alt_r-2", "`": "alt_r-7"}
args = sys.argv[1:]
enter = french = False
while args and args[0] in ("-n", "--fr"):
    enter, french = enter or args[0] == "-n", french or args[0] == "--fr"
    args = args[1:]
text = " ".join(args)
s = socket.socket(socket.AF_UNIX)
s.connect("/tmp/qmon.sock")
s.settimeout(1)
try:
    s.recv(65536)
except OSError:
    pass
def key(name):
    s.sendall(f"sendkey {name}\n".encode())
    time.sleep(0.15)
    try:
        s.recv(65536)
    except OSError:
        pass
for ch in text:
    if french:
        base = ch.lower()
        if ch.isdigit():
            key(f"shift-{ch}")
        elif ch.isupper():
            key("shift-" + AZERTY.get(base, base))
        else:
            key(AZERTY.get(ch, ch))
    elif ch.isupper():
        key(f"shift-{ch.lower()}")
    elif ch in SHIFTED:
        key("shift-" + NAMES.get(SHIFTED[ch], SHIFTED[ch]))
    else:
        key(NAMES.get(ch, ch))
if enter:
    key("ret")
EOF
chmod +x /usr/local/bin/qtype

# QEMU can write PPM or PNG; the PNG of a window whose size just changed came out scrambled once,
# so the first minutes are also saved as PPM and converted here (Python and zlib only).
cat >/usr/local/bin/ppm2png <<'EOF'
#!/usr/bin/env python3
import struct, sys, zlib
data = open(sys.argv[1], "rb").read()
parts = data.split(maxsplit=4)
assert parts[0] == b"P6", "not a binary PPM"
width, height, _maxval = int(parts[1]), int(parts[2]), int(parts[3])
pixels = parts[4][1:] if parts[4][:1] in b"\n\r \t" and len(parts[4]) > width * height * 3 else parts[4]
pixels = pixels[-width * height * 3:]
rows = b"".join(b"\x00" + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))
def chunk(tag, body):
    return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
       + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))
open(sys.argv[2], "wb").write(png)
EOF
chmod +x /usr/local/bin/ppm2png

# --- firmware variables ---------------------------------------------------------------------
if [[ ! -f "$VARS" || -n "${FRESH_VARS:-}" ]]; then
  log "Preparing fresh firmware variables (Secure Boot on, Microsoft keys, our MOK, a shim boot entry)"
  pacman -S --noconfirm --needed gptfdisk >/dev/null 2>&1
  attach_image -r
  trap 'umount /tmp/rootmnt 2>/dev/null || true; losetup -d "$LOOP" 2>/dev/null || true' EXIT
  mkdir -p /tmp/rootmnt
  mount -o ro,noload "${LOOP}p3" /tmp/rootmnt
  cp /tmp/rootmnt/var/lib/archinstall/secureboot/MOK.crt /tmp/MOK.crt
  umount /tmp/rootmnt
  # WINSETUP is the last partition: the numbers efibootmgr would put in its boot entry.
  SETUP_N="$(ls "${LOOP}"p* | sed 's/.*p//' | sort -n | tail -n 1)"
  sgdisk -i "$SETUP_N" "$LOOP" >/tmp/setup-part.txt
  losetup -d "$LOOP"; trap - EXIT

  # The boot entry plays the part of what `efibootmgr` creates on a real machine.
  if [[ "${SB:-1}" == 0 ]]; then
    # Secure Boot off (no keys at all, the firmware stays in Setup Mode): to tell Secure Boot
    # problems from the rest.
    virt-fw-vars --input "$OVMF_DIR/OVMF_VARS.4m.fd" --output "$VARS" \
      --append-boot-filepath '\EFI\ArchInstall\shimx64.efi' >/dev/null
  else
    virt-fw-vars --input "$OVMF_DIR/OVMF_VARS.4m.fd" --output "$VARS" --enroll-redhat --secure-boot \
      --add-mok "$SHIM_LOCK_GUID" /tmp/MOK.crt --append-boot-filepath '\EFI\ArchInstall\shimx64.efi' >/dev/null
  fi

  # What the installer asks of the firmware, done here by hand because a container has no
  # firmware: `efibootmgr --create-only --disk D --part N --label "Windows 11 Setup" --loader
  # '\EFI\BOOT\BOOTX64.EFI'` followed by `efibootmgr --bootnext XXXX`. Same bytes: a UEFI load
  # option with a hard-drive node (partition N of this disk) and a file node, set as BootNext.
  if [[ -z "${NO_SETUP_ENTRY:-}" ]]; then
    virt-fw-vars --input "$VARS" --output-json /tmp/current.json >/dev/null 2>&1
    python - "$SETUP_N" <<'PY'
import json, os, re, struct, sys, uuid
number = int(sys.argv[1])
info = open("/tmp/setup-part.txt").read()
guid = re.search(r"Partition unique GUID: ([0-9A-Fa-f-]+)", info).group(1)
first = int(re.search(r"First sector: (\d+)", info).group(1))
last = int(re.search(r"Last sector: (\d+)", info).group(1))
current = json.load(open("/tmp/current.json"))
used = [int(v["name"][4:], 16) for v in current["variables"] if re.fullmatch(r"Boot[0-9A-F]{4}", v["name"])]
slot = max(used, default=-1) + 1
# A short-form file path (the file only, no disk): the firmware looks for it on every volume.
# \bootmgfw.efi exists only on WINSETUP, where it is the same file as \EFI\BOOT\BOOTX64.EFI
# (same SHA-256), whereas \EFI\BOOT\BOOTX64.EFI also exists on the ESP. A hard-drive node (partition
# number, start, size, GUID) was tried first but OVMF never resolved it; efibootmgr writes a full path.
path = ("\\bootmgfw.efi" + "\0").encode("utf-16-le")
file_node = struct.pack("<BBH", 4, 4, 4 + len(path)) + path
device_path = file_node + bytes([0x7F, 0xFF, 4, 0])
option = struct.pack("<IH", 1, len(device_path)) + "Windows 11 Setup\0".encode("utf-16-le") + device_path
global_guid = "8be4df61-93ca-11d2-aa0d-00e098032b8c"
variables = [{"name": f"Boot{slot:04X}", "guid": global_guid, "attr": 7, "data": option.hex()}]
if os.environ.get("USE_BOOTNEXT"):
    variables.append({"name": "BootNext", "guid": global_guid, "attr": 7, "data": struct.pack("<H", slot).hex()})
else:
    # This OVMF build did not start a BootNext entry (it consumed the variable and booted the
    # next entry of BootOrder without a word), so the entry goes first in BootOrder instead.
    # Windows Setup puts its own "Windows Boot Manager" entry in front once it has installed.
    order = next(v for v in current["variables"] if v["name"] == "BootOrder")
    variables.append({**order, "data": (struct.pack("<H", slot) + bytes.fromhex(order["data"])).hex()})
new = {"version": current["version"], "variables": variables}
json.dump(new, open("/tmp/setup-entry.json", "w"))
print(f"boot entry Boot{slot:04X} -> \\bootmgfw.efi (WINSETUP is partition {number}), "
      + ("BootNext" if os.environ.get("USE_BOOTNEXT") else "first in BootOrder"))
PY
    virt-fw-vars --input "$VARS" --output "$VARS.new" --set-json /tmp/setup-entry.json >/dev/null
    mv "$VARS.new" "$VARS"
  fi
fi

# What `efibootmgr --label ArchInstall` gives the entry of a real installation: OVMF names the entry
# virt-fw-vars creates after the file, but the Windows finish script finds the GRUB entry by its name.
virt-fw-vars --input "$VARS" --output-json /tmp/before-rename.json >/dev/null 2>&1
python - <<'PY'
import json, re, struct
data = json.load(open("/tmp/before-rename.json"))
renamed = []
for v in data["variables"]:
    if not re.fullmatch(r"Boot[0-9A-F]{4}", v["name"]):
        continue
    raw = bytes.fromhex(v["data"])
    end = 6
    while raw[end:end + 2] != b"\0\0":
        end += 2
    if "shimx64" in raw[6:end].decode("utf-16-le", "replace").lower():
        v["data"] = (raw[:6] + "ArchInstall\0".encode("utf-16-le") + raw[end + 2:]).hex()
        renamed.append(v)
if renamed:
    json.dump({"version": data["version"], "variables": renamed}, open("/tmp/rename.json", "w"))
    print("named the shim boot entry ArchInstall:", ", ".join(v["name"] for v in renamed))
PY
if [[ -f /tmp/rename.json ]]; then
  virt-fw-vars --input "$VARS" --output "$VARS.new" --set-json /tmp/rename.json >/dev/null && mv "$VARS.new" "$VARS"
fi

# --- run -------------------------------------------------------------------------------------
# A machine identity of its own. QEMU's default (zero UUID, empty serial number) is shared by every
# VM that ever used it, and Windows OOBE asks Microsoft's Autopilot service about it: a profile
# registered for that identity by somebody else renamed the test machine ("W-%SERIAL%" -> "W-").
[[ -s /img/smbios-uuid ]] || cat /proc/sys/kernel/random/uuid >/img/smbios-uuid
VM_UUID="$(cat /img/smbios-uuid)"

log "Starting QEMU with KVM ($MEMORY MiB, 4 vCPUs), label '$LABEL', UUID $VM_UUID, for up to $MINUTES minutes"
rm -f /tmp/qmon.sock
qemu-system-x86_64 \
  -accel kvm -machine q35,smm=on -cpu "$QEMU_CPU" -smp 4 -m "$MEMORY" \
  -uuid "$VM_UUID" -smbios "type=1,manufacturer=ArchInstall,product=Test machine,serial=AI-${VM_UUID:0:8}" \
  -global driver=cfi.pflash01,property=secure,value=on \
  -drive if=pflash,format=raw,unit=0,readonly=on,file="$OVMF_DIR/OVMF_CODE.secboot.4m.fd" \
  -drive if=pflash,format=raw,unit=1,file="$VARS" \
  -drive file="$IMG",format=raw,if=none,id=disk0,cache=unsafe \
  -device ich9-ahci,id=ahci -device ide-hd,drive=disk0,bus=ahci.0,bootindex=0 \
  -device qemu-xhci -device usb-kbd -device usb-tablet \
  -nic user,model=e1000e \
  $QEMU_DISPLAY $QEMU_EXTRA -display none -serial file:/tmp/serial.log \
  -monitor unix:/tmp/qmon.sock,server,nowait &
QEMU_PID=$!
sleep 3
kill -0 "$QEMU_PID" 2>/dev/null || { echo "QEMU did not start" >&2; exit 1; }

START=$SECONDS
n=0
while kill -0 "$QEMU_PID" 2>/dev/null; do
  elapsed=$((SECONDS - START))
  (( elapsed >= MINUTES * 60 )) && { log "time limit reached"; break; }
  [[ -e "$SHOTS/STOP-$LABEL" ]] && { log "stop file found"; break; }
  stem="$SHOTS/$LABEL-$(printf '%03d' "$n")-$(printf '%02dm%02ds' $((elapsed / 60)) $((elapsed % 60)))"
  if (( elapsed < FAST_UNTIL )); then
    # Early phase (GRUB, Windows PE): frequent shots, as PPM converted by hand.
    qmon "screendump /tmp/shot.ppm -f ppm" >/dev/null 2>&1 || true
    ppm2png /tmp/shot.ppm "$stem.png" 2>/dev/null || true
    interval="$FAST_EVERY"
  else
    qmon "screendump $stem.png -f png" >/dev/null 2>&1 || true
    interval="$SHOT_EVERY"
  fi
  n=$((n + 1))
  sleep "$interval"
done

if kill -0 "$QEMU_PID" 2>/dev/null; then
  log "powering the VM off"
  qmon "system_powerdown" >/dev/null 2>&1 || true
  sleep 5
  qmon "quit" >/dev/null 2>&1 || kill "$QEMU_PID" 2>/dev/null || true
fi
wait "$QEMU_PID" 2>/dev/null || true
log "QEMU stopped after $((SECONDS - START)) s; $n screenshots in $SHOTS"

# What the firmware ended up with (boot order and entries): handy to see who is first.
virt-fw-vars --input "$VARS" --output-json /tmp/vars.json >/dev/null 2>&1 || true
python - <<'PY' || true
import json
try:
    data = json.load(open("/tmp/vars.json"))
except Exception as exc:
    raise SystemExit(f"cannot read the variables: {exc}")
variables = data.get("variables", [])
for v in variables:
    if v.get("name") in ("BootOrder",) or (v.get("name", "").startswith("Boot0") and len(v["name"]) == 8):
        print(v["name"], v.get("data", "")[:120])
PY
cp /tmp/serial.log "$SHOTS/$LABEL-serial.log" 2>/dev/null || true
