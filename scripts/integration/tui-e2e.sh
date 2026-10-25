#!/usr/bin/env bash
# Runs INSIDE the privileged builder container (see scripts/tui-e2e.ps1).
#
# Drives the REAL interactive wizard (the Textual screens, not --answers) in a QEMU/KVM UEFI VM booted
# from the OFFLINE ISO, answers every screen like a person would, lets the install finish, presses
# "Reboot now", then inspects the disk the wizard produced.
#
#   tui-e2e.sh              the whole scenario
#   tui-e2e.sh daemon       boot the VM and wait for commands (tui-e2e.py ctl ...), to explore by hand
set -euo pipefail

WORK="${TUI_WORK:-/work}"
export TUI_WORK="$WORK" SCREENS_DIR="${SCREENS_DIR:-/screens}"
mkdir -p "$WORK" "$SCREENS_DIR"
cd "$WORK"

echo "##### Tools for the host side (QEMU, OVMF, pyte, gptfdisk, sbsigntools, btrfs-progs)"
sed -i '/^NoExtract/d' /etc/pacman.conf
pacman -Sy --noconfirm --needed qemu-system-x86 edk2-ovmf python-pyte gptfdisk sbsigntools btrfs-progs \
  libarchive e2fsprogs dosfstools virt-firmware qemu-hw-display-virtio-vga qemu-hw-display-virtio-gpu >/dev/null 2>&1
ls -l /dev/kvm
free -m | head -2
df -h "$WORK" | tail -n 1

echo "##### The offline ISO that will be tested"
[[ -f offline.iso && -f offline.iso.sha256 ]] || { echo "no offline.iso (+ .sha256) in $WORK: use scripts/tui-e2e.ps1" >&2; exit 1; }
cat offline.iso.sha256
ls -l offline.iso

echo "##### Kernel and initramfs of that ISO (booted directly, so that the console is the serial line)"
rm -rf arch vmlinuz-linux initramfs-linux.img
bsdtar -xf offline.iso -C "$WORK" arch/boot/x86_64/vmlinuz-linux arch/boot/x86_64/initramfs-linux.img
mv arch/boot/x86_64/vmlinuz-linux arch/boot/x86_64/initramfs-linux.img "$WORK"/
rm -rf arch
ls -l vmlinuz-linux initramfs-linux.img
bsdtar -xOf offline.iso loader/entries/01-archiso-linux.conf > boot-entry.conf
cat boot-entry.conf

exec python3 -u "/src/scripts/integration/${TUI_ENTRY:-tui-e2e.py}" "${@:-run}"
