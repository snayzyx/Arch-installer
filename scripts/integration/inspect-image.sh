#!/usr/bin/env bash
# Looks inside the persistent disk image (archinstall-winimg volume) from a privileged container,
# read-only: partition table, filesystems, and what Windows left on its NTFS partition.
#
#   inspect-image.sh            overview
#   inspect-image.sh logs       also copy the Windows Setup logs to /out/win-logs and print what the
#                               answer file and the finish script changed (registry, computer name)
set -euo pipefail
IMG=/img/disk.img
MODE="${1:-overview}"
[[ -f "$IMG" ]] || { echo "no image at $IMG" >&2; exit 1; }

sed -i '/^NoExtract/d' /etc/pacman.conf
pacman -Sy --noconfirm --needed ntfs-3g gptfdisk >/dev/null 2>&1 || { echo "cannot install tools" >&2; exit 1; }

LOOP="$(losetup -r -f --show -P "$IMG")"
cleanup() { umount /tmp/m-win /tmp/m-esp /tmp/m-setup 2>/dev/null || true; losetup -d "$LOOP" 2>/dev/null || true; }
trap cleanup EXIT
for dir in /sys/class/block/"$(basename "$LOOP")"p*; do
  name="$(basename "$dir")"; rm -f "/dev/$name"
  IFS=: read -r major minor <"$dir/dev"; mknod "/dev/$name" b "$major" "$minor"
done

echo "--- partitions ---"
sgdisk -p "$LOOP" | tail -n +9
echo "--- filesystems ---"
for p in "${LOOP}"p*; do printf '%s  ' "$(basename "$p")"; blkid -o export "$p" | tr '\n' ' ' | sed 's/DEVNAME=[^ ]* //'; echo; done

mkdir -p /tmp/m-win /tmp/m-esp /tmp/m-setup
if [[ "$(blkid -s TYPE -o value "${LOOP}p2")" == ntfs ]]; then
  ntfs-3g -o ro "${LOOP}p2" /tmp/m-win
  echo "--- Windows partition (p2): top level ---"
  ls -A /tmp/m-win | head -30
  echo "--- Windows folders / Setup logs ---"
  ls -d /tmp/m-win/Windows /tmp/m-win/'$WINDOWS.~BT' /tmp/m-win/Windows/Panther 2>&1 | head || true
  if [[ "$MODE" == logs ]]; then
    if pacman -Sy --noconfirm --needed hivex >/dev/null 2>&1; then
      read_hive() {  # hive, then "key|value" pairs
        local hive="$1"; shift
        for entry in "$@"; do
          printf '%s = ' "${entry/|/ : }"; hivexget "$hive" "${entry%%|*}" "${entry##*|}" 2>&1 || echo "(missing)"
        done
      }
      echo "--- registry (SYSTEM hive) ---"
      read_hive /tmp/m-win/Windows/System32/config/SYSTEM \
        'ControlSet001\Control\TimeZoneInformation|RealTimeIsUniversal' \
        'ControlSet001\Control\TimeZoneInformation|TimeZoneKeyName' \
        'ControlSet001\Control\BitLocker|PreventDeviceEncryption' \
        'ControlSet001\Control\ComputerName\ComputerName|ComputerName'
      echo "--- registry (SOFTWARE hive) ---"
      read_hive /tmp/m-win/Windows/System32/config/SOFTWARE \
        'Policies\Microsoft\Windows\DataCollection|AllowTelemetry' \
        'Microsoft\Windows\CurrentVersion\OOBE|BypassNRO' \
        'Microsoft\Windows NT\CurrentVersion|ProductName' \
        'Microsoft\Windows NT\CurrentVersion|DisplayVersion' \
        'Microsoft\Windows NT\CurrentVersion|CurrentBuildNumber'
    else
      echo "(hivex not installable: registry not read)"
    fi
    echo "--- user profiles ---"; ls /tmp/m-win/Users
    out=/out/win-logs; rm -rf "$out"; mkdir -p "$out"
    for f in /tmp/m-win/Windows/Panther/*.log /tmp/m-win/Windows/Panther/UnattendGC/*.log \
             /tmp/m-win/'$WINDOWS.~BT'/Sources/Panther/*.log /tmp/m-win/Windows/Temp/archinstall-finish.log \
             /tmp/m-win/Windows/Setup/Scripts/*; do
      [[ -f "$f" ]] && cp "$f" "$out/" && echo "copied $(basename "$f")"
    done
  fi
else
  echo "--- partition 2 is NOT NTFS: Windows Setup never formatted it ---"
fi

if [[ "$MODE" == exec ]]; then  # inspect-image.sh exec '<command>': run it with the volumes mounted (/tmp/m-win, /tmp/m-esp)
  bash -c "${2:?exec needs a command}" || true
fi

SETUP_PART="$(ls "${LOOP}"p* | tail -n 1)"
if [[ "$(blkid -s PARTLABEL -o value "$SETUP_PART")" == "Arch root" || "$(blkid -s PARTLABEL -o value "$SETUP_PART")" == "Arch home" ]]; then
  echo "--- no WINSETUP partition left: the last partition is $(basename "$SETUP_PART") ($(blkid -s PARTLABEL -o value "$SETUP_PART")) ---"
else
  mount -o ro "$SETUP_PART" /tmp/m-setup 2>/dev/null && {
    echo "--- last partition ($(basename "$SETUP_PART")): $(blkid -s LABEL -o value "$SETUP_PART") ---"; ls /tmp/m-setup | head -20; } \
    || echo "--- last partition cannot be mounted as FAT (reformatted as NTFS?) ---"
fi
mount -o ro "${LOOP}p1" /tmp/m-esp
echo "--- ESP ---"; ls /tmp/m-esp/EFI /tmp/m-esp/EFI/Microsoft/Boot 2>&1 | head -20
