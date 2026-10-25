#!/usr/bin/env bash
# Runs INSIDE the privileged builder container (see scripts/live-test.ps1 -E2e).
#
# Full end-to-end acceptance test: boots the OFFLINE ISO in QEMU/KVM with OVMF and a virtual TPM,
# runs the installer for real, lets Windows install itself, then checks the boot chain.
#
#   live-e2e.sh shim            (all phases)    live-e2e.sh shim-windows (one phase)
set -euo pipefail

LIVE="${LIVE_WORK:-/work}"
export LIVE_WORK="$LIVE"
mkdir -p "$LIVE" "${SCREENS_DIR:-/screens}"
cd "$LIVE"

echo "##### Tools for the host side (QEMU, OVMF, virt-firmware, swtpm, ntfs-3g, hivex)"
sed -i '/^NoExtract/d' /etc/pacman.conf
pacman -Sy --noconfirm --needed qemu-system-x86 edk2-ovmf virt-firmware swtpm ntfs-3g hivex >/dev/null 2>&1
ls -l /dev/kvm /dev/fuse
free -m | head -2
df -h "$LIVE" | tail -n 1

exec python3 -u /src/scripts/integration/live-e2e.py "$@"
