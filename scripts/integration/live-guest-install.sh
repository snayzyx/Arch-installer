#!/usr/bin/env bash
# Runs INSIDE the live system: the headless installer on /dev/vda with REAL firmware writes.
# usage: live-guest-install.sh <answers.json>
answers="$1"
cd /root/t || exit 1
PYTHONUNBUFFERED=1 PYTHONPATH=/root/t python3 -m dualboot_installer \
  --answers "$answers" --partial --confirm-erase /dev/vda >/root/install.log 2>&1
rc=$?
echo "--- step markers, errors and the firmware-related lines of the installer log ---"
grep -E '^(--- \[|ERROR|WARNING|NEXT BOOT|Invalid|Refusing)|sbctl|efibootmgr|mokutil|Secure Boot keys|Setup Mode' /root/install.log \
  | cut -c1-220 | head -60
echo "--- what the real sbctl / mokutil / efibootmgr printed ---"
grep -A9 -E 'sbctl (create-keys|enroll-keys)' /root/install.log | cut -c1-200 | head -30
grep -B1 -A4 -E 'mokutil --import|efibootmgr --create' /root/install.log | cut -c1-200 | head -20
echo "--- last 12 lines ---"
tail -n 12 /root/install.log | cut -c1-220
echo "INSTALLER-EXIT $rc"
