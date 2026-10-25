#!/usr/bin/env bash
# Runs INSIDE the privileged builder container (see scripts/preset-boot.ps1).
#
# Does a desktop shell preset really start? Installs Arch Linux with the REAL installer (the same code as the ISO)
# and the preset on a disk image, prepares that image for an unattended look (test-only autologin and a serial
# console), then boots it in QEMU/KVM with a virtual screen and photographs the Hyprland session (preset_boot.py).
#
#   preset-boot.sh <none|macshell|revo|caelestia> [install|patch|boot|all]
#
# env: RENDER_ENV   lines (KEY=value, one per line) added to /etc/environment of the installed system, to find
#                   out what software rendering needs (e.g. WLR_RENDERER_ALLOW_SOFTWARE=1)
#      BOOT_SECONDS how long the session is watched (default 180)
#      IMG_GIB      size of the sparse disk image (default 110: the installer reserves room for Windows)
set -euo pipefail

PRESET="${1:?preset id: none, macshell, revo or caelestia}"
PHASE="${2:-all}"
IMAGE="/img/$PRESET.img"
SHOTS="${SCREENS_DIR:-/screens}/$PRESET"
IMG_GIB="${IMG_GIB:-110}"
mkdir -p /img "$SHOTS"

log() { printf '\n##### [%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf '\nFAIL: %s\n' "$*" >&2; exit 1; }

LOOP=""
cleanup() {
  umount -R /mnt 2>/dev/null || true
  for mapping in cryptroot crypthome; do cryptsetup close "$mapping" 2>/dev/null || true; done
  if [[ -n "$LOOP" ]]; then losetup -d "$LOOP" 2>/dev/null || true; fi
}
trap cleanup EXIT

attach_image() {  # sets LOOP; no udev in a container: make the partition nodes ourselves
  LOOP="$(losetup -f --show -P "$IMAGE")"
  local dir name major minor
  for dir in /sys/class/block/"$(basename "$LOOP")"p*; do
    [[ -e "$dir/dev" ]] || continue
    name="$(basename "$dir")"
    rm -f "/dev/$name"
    IFS=: read -r major minor <"$dir/dev"
    mknod "/dev/$name" b "$major" "$minor"
  done
}

tools() {
  log "Tools"
  sed -i '/^NoExtract/d' /etc/pacman.conf
  # a container's /dev is a separate tmpfs: bind it like a live system does (see test-install.sh)
  sed -i 's|chroot_add_mount udev "\$1/dev" -t devtmpfs -o mode=0755,nosuid|chroot_add_mount /dev "$1/dev" --bind|' /usr/sbin/arch-chroot
  pacman -Sy --noconfirm --needed gptfdisk dosfstools reflector efibootmgr sbsigntools mokutil python parted psmisc \
    rsync qemu-system-x86 edk2-ovmf virt-firmware qemu-hw-display-virtio-vga qemu-hw-display-virtio-gpu >/dev/null 2>&1 || fail "cannot install the tools"
  cat >/usr/local/bin/udevadm <<'EOF'
#!/bin/bash
for dir in /sys/class/block/loop*p*; do
  [[ -e "$dir/dev" ]] || continue
  name="$(basename "$dir")"
  if [[ ! -e "/dev/$name" ]]; then IFS=: read -r major minor <"$dir/dev"; mknod "/dev/$name" b "$major" "$minor"; fi
done
exit 0
EOF
  chmod +x /usr/local/bin/udevadm
}

install_phase() {
  log "Disk image $IMAGE (${IMG_GIB} GiB, sparse)"
  rm -f "$IMAGE" "/img/$PRESET-vars.fd"
  truncate -s "${IMG_GIB}G" "$IMAGE"
  attach_image
  echo "attached to $LOOP"
  export LOOP PRESET
  python - >/tmp/answers.json <<'PY'
import json, os
e = os.environ
arch = {"country": "FR", "timezone": "Europe/Paris", "locale": "fr_FR.UTF-8", "keymap": "fr",
        "hostname": "testbox", "username": "alice", "user_password": "alice-pw",
        "root_same_password": False, "root_password": "root-pw", "filesystem": "ext4",
        "kernel": "linux", "desktop": "hyprland", "aur_helper": "none", "gpu_profiles": ["qemu"],
        "dev_groups": [], "apps": []}
if e["PRESET"] != "none":
    arch["dotfiles"] = e["PRESET"]
answers = {"disk": {"device": e["LOOP"], "windows_gib": 64, "root_gib": 64}, "arch": arch,
           "windows": {"computer_name": "WINTEST", "username": "alice", "password": "win-pw"},
           "secureboot": {"strategy": "shim", "mok_password": "Abcd1234", "touch_firmware": False}}
print(json.dumps(answers, indent=2))
PY
  log "Running the real installer (preset: $PRESET)"
  cd /src/installer
  set +e
  PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/src/installer python -m dualboot_installer \
    --answers /tmp/answers.json --confirm-erase "$LOOP" --partial 2>&1 | tee "$SHOTS/installer.log"
  local status=${PIPESTATUS[0]}
  set -e
  [[ $status -eq 0 ]] || fail "the installer exited with $status (see $SHOTS/installer.log)"
  echo "installer exit code 0"
  losetup -d "$LOOP"; LOOP=""
}

patch_phase() {
  log "Preparing the image for an unattended look (TEST ONLY: autologin, serial console)"
  attach_image
  local root="${LOOP}p3" esp="${LOOP}p1"
  mkdir -p /mnt
  mount "$root" /mnt
  mount "$esp" /mnt/boot
  # the session starts without typing
  install -d /mnt/etc/sddm.conf.d
  printf '[Autologin]\nUser=alice\nSession=hyprland.desktop\nRelogin=false\n' >/mnt/etc/sddm.conf.d/zz-test-autologin.conf
  # a root shell on the serial port (ttyS0) to collect evidence
  install -d /mnt/etc/systemd/system/serial-getty@ttyS0.service.d
  printf '[Service]\nExecStart=\nExecStart=-/sbin/agetty -o "-p -f -- \\\\u" --noclear --autologin root %%I $TERM\n' \
    >/mnt/etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf
  install -d /mnt/etc/systemd/system/getty.target.wants
  ln -sf /usr/lib/systemd/system/serial-getty@.service /mnt/etc/systemd/system/getty.target.wants/serial-getty@ttyS0.service
  # the kernel also talks on ttyS0 (grub.cfg is not signed and not verified: Secure Boot is off in this test)
  sed -i -E '/^[[:space:]]*linux[[:space:]]/ s|$| console=tty0 console=ttyS0,115200|' /mnt/boot/grub/grub.cfg
  grep -c 'console=ttyS0' /mnt/boot/grub/grub.cfg | sed 's/^/kernel command lines with the serial console: /'
  # what software rendering needed, found by trial (empty = nothing added)
  : >/mnt/etc/environment
  if [[ -n "${RENDER_ENV:-}" ]]; then printf '%s\n' "$RENDER_ENV" >/mnt/etc/environment; fi
  echo "/etc/environment:"; cat /mnt/etc/environment || true
  sync
  umount -R /mnt
  losetup -d "$LOOP"; LOOP=""
}

boot_phase() {
  log "Boot and look"
  [[ -c /dev/kvm ]] || fail "/dev/kvm is missing"
  BOOT_SECONDS="${BOOT_SECONDS:-180}" python3 -u /src/scripts/integration/preset_boot.py "$PRESET"
}

case "$PHASE" in
  install) tools; install_phase ;;
  patch)   tools; patch_phase ;;
  boot)    tools; boot_phase ;;
  all)     tools; install_phase; patch_phase; boot_phase ;;
  *) fail "unknown phase $PHASE" ;;
esac
