#!/usr/bin/env bash
# Runs INSIDE the privileged builder container (see scripts/live-test.ps1).
#
# Boots the OFFICIAL Arch ISO in QEMU/KVM with OVMF and runs the installer for real in the VM,
# to check what container tests cannot: efivars, boot entries, Secure Boot keys, MOK.
#
#   live-test.sh [a-sbctl] [b-shim]        (default: both)
set -euo pipefail

SCENARIOS="${*:-a-sbctl b-shim}"
LIVE="${LIVE_WORK:-/tmp/live}"
export LIVE_WORK="$LIVE"
mkdir -p "$LIVE" "${SCREENS_DIR:-/screens}"
cd "$LIVE"

echo "##### Tools for the host side (QEMU, OVMF, virt-firmware)"
sed -i '/^NoExtract/d' /etc/pacman.conf
pacman -Sy --noconfirm --needed qemu-system-x86 edk2-ovmf virt-firmware swtpm >/dev/null 2>&1
ls -l /dev/kvm

echo "##### Kernel and initramfs of the official ISO"
bsdtar -xf /iso/arch.iso -C "$LIVE" arch/boot/x86_64/vmlinuz-linux arch/boot/x86_64/initramfs-linux.img
mv arch/boot/x86_64/vmlinuz-linux arch/boot/x86_64/initramfs-linux.img "$LIVE"/
ln -sf /iso/arch.iso "$LIVE/arch.iso"

echo "##### The installer snapshot and the scripts that run in the live system"
rm -rf snaproot && mkdir snaproot
cp -r /snap/dualboot_installer snaproot/
cp /src/scripts/integration/live-guest-*.sh snaproot/

# Same answers as scripts/integration/test-install.sh, but touch_firmware is TRUE: the real
# firmware commands (sbctl enroll-keys, efibootmgr, mokutil) run in the VM.
write_answers() { # <file> <strategy>
  cat >"snaproot/$1" <<EOF
{
  "disk": {"device": "/dev/vda", "windows_gib": 64},
  "arch": {"country": "FR", "timezone": "Europe/Paris", "locale": "fr_FR.UTF-8", "keymap": "fr",
           "hostname": "testbox", "username": "alice", "user_password": "alice-pw",
           "root_same_password": false, "root_password": "root-pw",
           "filesystem": "ext4", "kernel": "linux", "desktop": "minimal", "aur_helper": "none"},
  "windows": {"computer_name": "WINTEST", "username": "alice", "password": "win-pw"},
  "secureboot": {"strategy": "$2", "mok_password": "Abcd1234", "touch_firmware": true}
}
EOF
}
write_answers answers-sbctl.json sbctl
write_answers answers-shim.json shim
tar -C snaproot -cf snap.tar .
ls -l snap.tar

python3 /src/scripts/integration/live-vm.py $SCENARIOS
