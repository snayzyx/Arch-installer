#!/usr/bin/env bash
# Integration test: runs the REAL installation steps against a disk image, inside a
# privileged Arch container, then checks the result. Nothing touches a real disk.
#
#   test-install.sh <shim|sbctl> [ext4|btrfs|xfs] [minimal|hyprland] [luks] [home] [shared]
#
# "luks" encrypts Arch Linux, "home" gives /home its own partition, "shared" keeps the last
# partition as a permanent shared NTFS-to-be partition (16 GiB here) instead of removing it.
#
# Environment (all optional, set by scripts/test-install.ps1):
#   WINDOWS_ISO       a Windows 11 ISO inside the container: Windows is staged too
#   DOWNLOAD_SOURCE=1 with WINDOWS_ISO: the installer downloads that ISO from a local HTTPS server
#   AUR_HELPER        none (default), paru or yay
#   LAPTOP=1          laptop answers: arch.laptop, a Wi-Fi network, hybrid Intel + NVIDIA graphics
#   EVIDENCE_DIR      a directory (mounted) that receives the generated Windows scripts and logs
# Run it through scripts/test-install.ps1 (Docker Desktop must be running).
set -euo pipefail

STRATEGY="${1:-shim}"
FS="${2:-ext4}"
DESKTOP="${3:-minimal}"
LUKS=""; HOME_PART=""; SHARED=""
for flag in "${@:4}"; do
  case "$flag" in luks) LUKS=1 ;; home) HOME_PART=1 ;; shared) SHARED=1 ;; *) echo "unknown option: $flag" >&2; exit 2 ;; esac
done
AUR_HELPER="${AUR_HELPER:-none}"
DOWNLOAD_SOURCE="${DOWNLOAD_SOURCE:-}"
LAPTOP="${LAPTOP:-}"
EVIDENCE_DIR="${EVIDENCE_DIR:-}"
STARTED_AT=$SECONDS
# WINDOWS_ISO (a path inside the container) turns on the full install: Windows is staged too, and
# the extra checks run. IMAGE_PATH puts the disk image on a persistent volume, to boot it later.
WINDOWS_ISO="${WINDOWS_ISO:-}"
IMAGE="${IMAGE_PATH:-/tmp/disk.img}"
PASSPHRASE="correct horse battery"

# Whatever happens, leave nothing behind: device-mapper and loop devices belong to the kernel of
# the Docker VM and outlive the container, which breaks the next run ("device already exists").
cleanup() {
  umount -R /mnt 2>/dev/null || true
  umount -R "${CHECK:-/nonexistent}" 2>/dev/null || true
  for mapping in chkhome chkroot crypthome cryptroot; do cryptsetup close "$mapping" 2>/dev/null || true; done
  if [[ -n "${LOOP:-}" ]]; then losetup -d "$LOOP" 2>/dev/null || true; fi
}
trap cleanup EXIT

log() { printf '\n##### %s\n' "$*"; }
fail() { printf '\nFAIL: %s\n' "$*" >&2; exit 1; }
FAILED=0
# check "description" command...   (the command's own error message is shown when it fails)
check() {
  local what="$1" err; shift
  if err="$("$@" 2>&1 >/dev/null)"; then printf '  ok    %s\n' "$what"
  else printf '  FAIL  %s\n' "$what"; [[ -n "$err" ]] && printf '        %s\n' "$(head -n 2 <<<"$err")"; FAILED=1; fi
}
# Two files with the same content (cmp is not installed in the Arch container image).
same() { [[ "$(sha256sum <"$1")" == "$(sha256sum <"$2")" ]]; }
# Output of a command contains a pattern.
shows() { local pattern="$1" output; shift; output="$("$@")"; grep -qE -- "$pattern" <<<"$output"; }

log "Tools for the test environment"
# The Docker image of Arch strips locale sources and docs (NoExtract), which the live ISO does not.
sed -i '/^NoExtract/d' /etc/pacman.conf
# On a real live system arch-chroot mounts the SAME devtmpfs as the host, so /dev/mapper/* (made
# by udev) is visible inside the chroot. A container's /dev is a separate tmpfs, which would hide
# the LUKS mapper from grub-probe. Binding the container's /dev reproduces the real behaviour.
sed -i 's|chroot_add_mount udev "\$1/dev" -t devtmpfs -o mode=0755,nosuid|chroot_add_mount /dev "$1/dev" --bind|' /usr/sbin/arch-chroot
grep -q 'chroot_add_mount /dev "$1/dev" --bind' /usr/sbin/arch-chroot || { echo "cannot patch arch-chroot" >&2; exit 1; }
pacman -Sy --noconfirm --needed gptfdisk dosfstools btrfs-progs xfsprogs cryptsetup reflector efibootmgr \
  sbsigntools mokutil python parted psmisc rsync wimlib >/dev/null 2>&1 || fail "cannot install the test tools"

log "Disk image"
mkdir -p "$(dirname "$IMAGE")"
rm -f "$IMAGE"
truncate -s 110G "$IMAGE"
LOOP="$(losetup -f --show -P "$IMAGE")"
echo "attached $IMAGE to $LOOP"

# No udev in a container: create the partition device nodes ourselves. `udevadm settle` is
# called by the installer right after partitioning, so it is replaced by a node creator.
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

log "Answers"
ROOT_GIB=64
[[ -n "$HOME_PART" ]] && ROOT_GIB=20
if [[ -n "$WINDOWS_ISO" ]]; then
  [[ -f "$WINDOWS_ISO" ]] || fail "the Windows ISO is not mounted in the container: $WINDOWS_ISO"
fi
if [[ -n "$DOWNLOAD_SOURCE" ]]; then
  [[ -n "$WINDOWS_ISO" ]] || fail "DOWNLOAD_SOURCE needs WINDOWS_ISO (the file the local HTTPS server serves)"
  # shellcheck source=iso-server.sh
  source <(tr -d '\015' </src/scripts/integration/iso-server.sh)
  start_iso_server "$WINDOWS_ISO" || fail "cannot start the HTTPS server"
  echo "serving $WINDOWS_ISO at $ISO_SERVER_URL"
  # A stand-in for curl that also records the checksum of the downloaded file (the installer
  # deletes it as soon as Windows is staged). It runs the real curl with the same arguments.
  cat >/usr/local/bin/curl <<'EOF'
#!/bin/bash
/usr/bin/curl "$@"; rc=$?
prev=""
for arg in "$@"; do
  if [[ "$prev" == "-o" && "$arg" == *archinstall-windows11.iso && -f "$arg" ]]; then
    { stat -c 'downloaded_bytes=%s' "$arg"; echo "downloaded_sha256=$(sha256sum <"$arg" | cut -d' ' -f1)"; } >/tmp/download-result.txt
  fi
  prev="$arg"
done
exit $rc
EOF
  chmod +x /usr/local/bin/curl
fi
export LOOP STRATEGY FS DESKTOP LUKS HOME_PART SHARED ROOT_GIB PASSPHRASE WINDOWS_ISO DOWNLOAD_SOURCE AUR_HELPER LAPTOP
export ISO_URL="${ISO_SERVER_URL:-}"
python - >/tmp/answers.json <<'PY'
import json, os
e = os.environ
flag = lambda name: bool(e.get(name))
disk = {"device": e["LOOP"], "windows_gib": 64, "root_gib": int(e["ROOT_GIB"]), "home_enabled": flag("HOME_PART")}
if flag("SHARED"):
    disk.update(shared_enabled=True, shared_gib=16)  # 110 GiB disk: 64 + 20 + 16 leaves 9 GiB for /home
arch = {"country": "FR", "timezone": "Europe/Paris", "locale": "fr_FR.UTF-8", "keymap": "fr",
        "hostname": "testbox", "username": "alice", "user_password": "alice-pw",
        "root_same_password": False, "root_password": "root-pw",
        "filesystem": e["FS"], "luks_enabled": flag("LUKS"), "luks_passphrase": e["PASSPHRASE"],
        "kernel": "linux", "desktop": e["DESKTOP"], "aur_helper": e["AUR_HELPER"]}
windows = {"computer_name": "WINTEST", "username": "alice", "password": "win-pw"}
if e.get("WINDOWS_ISO"):
    if flag("DOWNLOAD_SOURCE"):
        windows.update(iso_source="download", iso_url=e["ISO_URL"], edition="Pro")
    else:
        windows.update(iso_source="local", iso_path=e["WINDOWS_ISO"], edition="Pro")
answers = {"disk": disk, "arch": arch, "windows": windows,
           "secureboot": {"strategy": e["STRATEGY"], "mok_password": "Abcd1234", "touch_firmware": False}}
if flag("LAPTOP"):
    arch.update(laptop=True, gpu_profiles=["intel", "nvidia-open"])
    answers["network"] = {"wifi_ssid": "TestNet", "wifi_security": "psk", "wifi_passphrase": "wifi-test-pass-2026"}
print(json.dumps(answers, indent=2))
PY
grep -vE 'password|passphrase' /tmp/answers.json

log "Running the installer (strategy=$STRATEGY fs=$FS desktop=$DESKTOP luks=${LUKS:-no} home=${HOME_PART:-no} shared=${SHARED:-no} aur=$AUR_HELPER laptop=${LAPTOP:-no} windows=${WINDOWS_ISO:+yes}${DOWNLOAD_SOURCE:+(download)})"
cd /src/installer
INSTALLER_ARGS=(--answers /tmp/answers.json --confirm-erase "$LOOP")
[[ -n "$WINDOWS_ISO" ]] || INSTALLER_ARGS+=(--partial) # without a Windows ISO, install Arch only
INSTALLER_CMD=(python -m dualboot_installer)
set +e
PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/src/installer "${INSTALLER_CMD[@]}" "${INSTALLER_ARGS[@]}" 2>&1 | tee /tmp/installer.log
INSTALLER_STATUS=${PIPESTATUS[0]}
set -e
if [[ $INSTALLER_STATUS -ne 0 ]]; then
  echo "--- diagnostics ---"
  findmnt -R /mnt || true
  ps -eo pid,comm,args | grep -E "gpg|dirmngr|keyboxd" | grep -v grep || true
  fail "the installer reported an error (see above)"
fi

log "Mounting the result again"
CHECK=/tmp/check
mkdir -p "$CHECK"
ROOT_PART="${LOOP}p3"
ROOT_DEV="$ROOT_PART"
if [[ -n "$LUKS" ]]; then
  check "the root partition is a LUKS2 container" cryptsetup isLuks --type luks2 "$ROOT_PART"
  printf '%s' "$PASSPHRASE" | cryptsetup open --key-file=- "$ROOT_PART" chkroot || fail "wrong passphrase for the root partition"
  ROOT_DEV=/dev/mapper/chkroot
fi
if [[ "$FS" == "btrfs" ]]; then mount -o subvol=@ "$ROOT_DEV" "$CHECK"; else mount "$ROOT_DEV" "$CHECK"; fi
mount "${LOOP}p1" "$CHECK/boot"
# btrfs without a separate /home partition keeps it in the @home subvolume.
if [[ "$FS" == "btrfs" && -z "$HOME_PART" ]]; then mount -o subvol=@home "$ROOT_DEV" "$CHECK/home"; fi
if [[ -n "$HOME_PART" ]]; then
  HOME_DEV="${LOOP}p4"
  if [[ -n "$LUKS" ]]; then
    # The key file stored on the encrypted root must unlock /home: that is what crypttab relies on.
    cryptsetup open --key-file "$CHECK/etc/cryptsetup-keys.d/crypthome.key" "$HOME_DEV" chkhome || fail "the key file does not unlock /home"
    HOME_DEV=/dev/mapper/chkhome
  fi
  mount "$HOME_DEV" "$CHECK/home"
fi

log "Checking the installed system"
ESP="$CHECK/boot"
GRUB_EFI="$ESP/EFI/ArchInstall/grubx64.efi"
check "kernel installed"                    test -s "$ESP/vmlinuz-linux"
check "initramfs installed"                 test -s "$ESP/initramfs-linux.img"
check "GRUB image installed"                test -s "$GRUB_EFI"
check "fallback boot file installed"        test -s "$ESP/EFI/BOOT/BOOTX64.EFI"
check "grub.cfg generated"                  test -s "$ESP/grub/grub.cfg"
check "grub.cfg has the Windows 11 entry"   grep -q 'menuentry "Windows 11"' "$ESP/grub/grub.cfg"
# The graphical menu is refused under Secure Boot with shim, so the shipped config uses the text one.
check "shipped GRUB config uses the text menu" grep -qx "GRUB_TERMINAL_OUTPUT=console" "$CHECK/etc/default/grub"
check "grub.cfg does not load the graphical terminal" bash -c "! grep -qE '^\s*(loadfont|insmod gfxterm|terminal_output gfxterm)' '$ESP/grub/grub.cfg'"
check "fstab lists the ESP"                 grep -q "/boot" "$CHECK/etc/fstab"
# A /dev/xxx name or a swap line in fstab made the first boot test fall into emergency mode.
check "fstab uses UUIDs only"               bash -c "! grep -vE '^(#|$)' '$CHECK/etc/fstab' | grep -qvE '^UUID='"
check "fstab has no swap entry"             bash -c "! grep -qE '[[:space:]]swap[[:space:]]' '$CHECK/etc/fstab'"
check "hostname"                            grep -qx "testbox" "$CHECK/etc/hostname"
check "locale generated"                    shows 'fr_FR\.utf8' arch-chroot "$CHECK" locale -a
check "locale.conf"                         grep -qx "LANG=fr_FR.UTF-8" "$CHECK/etc/locale.conf"
check "keymap"                              grep -qx "KEYMAP=fr" "$CHECK/etc/vconsole.conf"
check "user alice exists"                   grep -q "^alice:" "$CHECK/etc/passwd"
check "user alice is a sudoer"              grep -q "^%wheel" "$CHECK/etc/sudoers.d/10-wheel"
check "alice has her own password hash"     grep -qE '^alice:\$' "$CHECK/etc/shadow"
check "root has a different password hash" bash -c "[[ \$(grep '^root:' '$CHECK/etc/shadow' | cut -d: -f2) != \$(grep '^alice:' '$CHECK/etc/shadow' | cut -d: -f2) ]]"
check "NetworkManager enabled"              test -L "$CHECK/etc/systemd/system/multi-user.target.wants/NetworkManager.service"
check "zram configured"                     test -s "$CHECK/etc/systemd/zram-generator.conf"
check "re-sign script installed"            test -x "$CHECK/usr/local/sbin/archinstall-secureboot"
check "pacman hook installed"               test -s "$CHECK/etc/pacman.d/hooks/95-archinstall-secureboot.hook"
check "secrets are not left in the log"     bash -c "! grep -rqE 'alice-pw|root-pw|$PASSPHRASE' '$CHECK/var/log' '$CHECK/root' 2>/dev/null"

case "$FS" in
  btrfs) check "fstab mounts the btrfs subvolumes" grep -q "subvol=" "$CHECK/etc/fstab"
         check "grub.cfg selects the @ subvolume"  grep -q "rootflags=subvol=@" "$ESP/grub/grub.cfg" ;;
  xfs)   check "fstab has an xfs root"             grep -qE '\sxfs\s' "$CHECK/etc/fstab" ;;
  ext4)  check "fstab has an ext4 root"            grep -qE '\sext4\s' "$CHECK/etc/fstab" ;;
esac

if [[ -n "$LUKS" ]]; then
  check "grub.cfg unlocks the root with the initramfs" grep -q "rd.luks.name=.*=cryptroot" "$ESP/grub/grub.cfg"
  check "grub.cfg root is the mapper"        grep -q "root=/dev/mapper/cryptroot" "$ESP/grub/grub.cfg"
  check "initramfs can unlock LUKS"          shows 'systemd-cryptsetup' arch-chroot "$CHECK" lsinitcpio /boot/initramfs-linux.img
  check "initramfs knows the keymap (passphrase typing)" shows 'vconsole.conf' arch-chroot "$CHECK" lsinitcpio /boot/initramfs-linux.img
  check "/boot is NOT encrypted (GRUB reads it)" bash -c "! cryptsetup isLuks '${LOOP}p1'"
else
  check "grub.cfg boots the kernel from the root UUID" grep -q "root=UUID=" "$ESP/grub/grub.cfg"
fi
if [[ -n "$HOME_PART" ]]; then
  check "/home is a separate mount in fstab" grep -qE '\s/home\s' "$CHECK/etc/fstab"
  check "/home is mounted from its own partition" bash -c "[[ \$(findmnt -no SOURCE '$CHECK/home') != \$(findmnt -no SOURCE '$CHECK') ]]"
  check "alice's home directory is on it"     test -d "$CHECK/home/alice"
  [[ -n "$LUKS" ]] && check "crypttab unlocks /home with the key file" grep -q "crypthome .*crypthome.key" "$CHECK/etc/crypttab"
fi
if [[ "$DESKTOP" == "hyprland" ]]; then
  check "SDDM enabled"                      test -L "$CHECK/etc/systemd/system/display-manager.service"
  check "Hyprland config for the user"      grep -q "kb_layout = fr" "$CHECK/home/alice/.config/hypr/hyprland.conf"
  # Compare numeric ids: the test container does not know the user "alice" by name.
  check "Hyprland config owned by the user" test "$(stat -c %u "$CHECK/home/alice/.config/hypr/hyprland.conf")" = "$(awk -F: '$1=="alice"{print $3}' "$CHECK/etc/passwd")"
  check "Hyprland session is installed"     test -s "$CHECK/usr/share/wayland-sessions/hyprland.desktop"
  check "keyboard layout for the display manager" grep -q '"XkbLayout" "fr"' "$CHECK/etc/X11/xorg.conf.d/00-keyboard.conf"
fi

log "Secure Boot signatures ($STRATEGY)"
if [[ "$STRATEGY" == "shim" ]]; then
  CERT="$CHECK/var/lib/archinstall/secureboot/MOK.crt"
  check "MOK key is private (0600)"         test "$(stat -c %a "$CHECK/var/lib/archinstall/secureboot/MOK.key")" = 600
  check "GRUB is signed with the MOK key"   sbverify --cert "$CERT" "$GRUB_EFI"
  check "kernel is signed with the MOK key" sbverify --cert "$CERT" "$ESP/vmlinuz-linux"
  check "shim is on the ESP"                test -s "$ESP/EFI/ArchInstall/shimx64.efi"
  check "MokManager is on the ESP"          test -s "$ESP/EFI/ArchInstall/mmx64.efi"
  check "shim carries a Microsoft signature" shows 'Microsoft Corporation UEFI CA' sbverify --list "$ESP/EFI/ArchInstall/shimx64.efi"
  check "fallback BOOTX64.EFI is shim"      same "$ESP/EFI/BOOT/BOOTX64.EFI" "$ESP/EFI/ArchInstall/shimx64.efi"
  check "fallback grubx64.efi is the signed GRUB" same "$ESP/EFI/BOOT/grubx64.efi" "$GRUB_EFI"
  check "the MOK certificate can be enrolled (DER)" test -s "$CHECK/var/lib/archinstall/secureboot/MOK.cer"
  check "GRUB image has a SBAT section"     bash -c "objcopy -O binary --only-section=.sbat '$GRUB_EFI' /dev/stdout | grep -q '^grub,'"
  # Arch's sbat.csv says generation 4 but GRUB 2.16 expects 6; the script must use the higher one.
  check "GRUB SBAT generation is not stale" bash -c "objcopy -O binary --only-section=.sbat '$GRUB_EFI' /dev/stdout | tr -d '\\0' | grep -qE '^grub,([5-9]|[1-9][0-9])'"
else
  DB="$CHECK/var/lib/sbctl/keys/db/db.pem"
  check "sbctl keys exist"                  test -s "$DB"
  check "GRUB is signed with the db key"    sbverify --cert "$DB" "$GRUB_EFI"
  check "fallback BOOTX64.EFI is signed"    sbverify --cert "$DB" "$ESP/EFI/BOOT/BOOTX64.EFI"
  check "kernel is signed with the db key"  sbverify --cert "$DB" "$ESP/vmlinuz-linux"
  # `sbctl verify` cannot find the ESP in a container (no udev data), so check sbctl's own
  # bookkeeping instead: the files it will re-sign on updates (`sbctl sign-all`, run by its hook).
  check "sbctl will re-sign GRUB on updates"   shows 'ArchInstall/grubx64.efi' arch-chroot "$CHECK" sbctl list-files
  check "sbctl will re-sign the kernel on updates" shows 'vmlinuz-linux' arch-chroot "$CHECK" sbctl list-files
fi

log "Re-signing script is idempotent and survives a second run"
if arch-chroot "$CHECK" /usr/local/sbin/archinstall-secureboot >/tmp/resign.log 2>&1; then
  printf '  ok    second run of archinstall-secureboot\n'
else
  printf '  FAIL  second run of archinstall-secureboot\n'; tail -n 15 /tmp/resign.log; FAILED=1
fi
check "no SBAT mismatch warning from GRUB" bash -c "! grep -q 'does not match' /tmp/resign.log"
if [[ "$STRATEGY" == "shim" ]]; then
  check "kernel still has exactly one signature after the second run" bash -c "[[ \$(sbverify --list '$ESP/vmlinuz-linux' | grep -c 'signature certificates') == 1 ]]"
fi

if [[ -n "$WINDOWS_ISO" ]]; then
  log "Checking the staged Windows Setup"
  SETUP_PART="${LOOP}p$([[ -n "$HOME_PART" ]] && echo 5 || echo 4)"
  SETUP=/tmp/check-setup
  mkdir -p "$SETUP"
  mount -o ro "$SETUP_PART" "$SETUP"

  check "WINSETUP is a FAT32 volume with that label" bash -c "[[ \$(blkid -s LABEL -o value '$SETUP_PART') == WINSETUP && \$(blkid -s TYPE -o value '$SETUP_PART') == vfat ]]"
  check "UEFI boot manager of Windows Setup"   test -s "$SETUP/efi/boot/bootx64.efi"
  check "BCD of Windows Setup"                 test -s "$SETUP/efi/microsoft/boot/bcd"
  check "boot.wim and boot.sdi"                bash -c "test -s '$SETUP/sources/boot.wim' && test -s '$SETUP/boot/boot.sdi'"
  check "setup.exe"                            test -s "$SETUP/setup.exe"
  check "install.wim is gone (split instead)"  bash -c "! test -e '$SETUP/sources/install.wim'"
  check "install image is split in several parts" bash -c "[[ \$(ls '$SETUP'/sources/install*.swm | wc -l) -ge 2 ]]"
  check "every part is below the 4 GiB FAT32 limit" bash -c "! find '$SETUP/sources' -name 'install*.swm' -size +4194303k | grep -q ."
  check "the first part is a valid split image" shows 'Part Number: +1/' wimlib-imagex info "$SETUP/sources/install.swm"
  check "the image still has the edition we install" shows 'Windows 11 Pro' wimlib-imagex info "$SETUP/sources/install.swm"

  # The launcher and the shell definition must really be inside the Setup image of boot.wim.
  DISK_GUID="$(sgdisk -p "$LOOP" | awk '/Disk identifier/ {print $4}')"
  rm -rf /tmp/xwim && mkdir -p /tmp/xwim
  wimlib-imagex extract "$SETUP/sources/boot.wim" 2 /Windows/System32/winpeshl.ini /Windows/System32/archinstall.cmd \
    --dest-dir=/tmp/xwim >/dev/null 2>&1 || true
  PYTHONPATH=/src/installer python - "$DISK_GUID" <<'PY'
import sys
from dualboot_installer.windows_unattend import launcher_cmd, winpeshl_ini
open("/tmp/expected.ini", "w", newline="").write(winpeshl_ini())
open("/tmp/expected.cmd", "w", newline="").write(launcher_cmd(sys.argv[1]))
PY
  check "boot.wim: winpeshl.ini is ours"       same /tmp/xwim/winpeshl.ini /tmp/expected.ini
  check "boot.wim: archinstall.cmd is ours"    same /tmp/xwim/archinstall.cmd /tmp/expected.cmd
  check "the launcher looks for this disk's GUID" grep -qi "${DISK_GUID}" /tmp/xwim/archinstall.cmd
  check "boot.wim image 1 (plain WinPE) was left alone" bash -c "! wimlib-imagex dir '$SETUP/sources/boot.wim' 1 --path=/Windows/System32 | grep -qi archinstall.cmd"

  check "16 answer files"                      bash -c "[[ \$(ls '$SETUP'/archinstall/unattend-*.xml | wc -l) -eq 16 ]]"
  check "answer file 0 targets disk 0"         grep -q "<DiskID>0</DiskID>" "$SETUP/archinstall/unattend-0.xml"
  check "answer file 7 targets disk 7"         grep -q "<DiskID>7</DiskID>" "$SETUP/archinstall/unattend-7.xml"
  check "answer file installs to partition 2"  grep -q "<PartitionID>2</PartitionID>" "$SETUP/archinstall/unattend-0.xml"
  check "answer file uses the Pro image index" bash -c "grep -A1 '/IMAGE/INDEX' '$SETUP/archinstall/unattend-0.xml' | grep -qE '<Value>[0-9]+</Value>'"
  check "answer file has the local account"    grep -q "<Name>alice</Name>" "$SETUP/archinstall/unattend-0.xml"
  check "answer file: the password is not in clear" bash -c "! grep -q 'win-pw' '$SETUP/archinstall/unattend-0.xml'"
  check "answer file: computer name"           grep -q "<ComputerName>WINTEST</ComputerName>" "$SETUP/archinstall/unattend-0.xml"
  check "answer file: English (US) from the ISO" grep -q "<UILanguage>en-US</UILanguage>" "$SETUP/archinstall/unattend-0.xml"
  check "SetupComplete.cmd"                    test -s "$SETUP/sources/\$OEM\$/\$\$/Setup/Scripts/SetupComplete.cmd"
  check "archinstall-finish.ps1"               test -s "$SETUP/sources/\$OEM\$/\$\$/Setup/Scripts/archinstall-finish.ps1"
  check "archinstall-bootorder.ps1"            test -s "$SETUP/sources/\$OEM\$/\$\$/Setup/Scripts/archinstall-bootorder.ps1"
  FINISH="$SETUP/sources/\$OEM\$/\$\$/Setup/Scripts/archinstall-finish.ps1"
  if [[ -n "$SHARED" ]]; then
    check "finish script turns the staging partition into the NTFS 'Shared' volume" bash -c "grep -q 'if (\$true)' '$FINISH' && grep -q \"NewFileSystemLabel 'Shared'\" '$FINISH'"
    check "finish script does not remove the partition (shared)" bash -c "! grep -qE 'if \(\$false\)' '$FINISH'"
  else
    check "finish script removes the staging partition" bash -c "grep -q 'if (\$false)' '$FINISH'"
  fi
  check "finish script turns Fast Startup and hibernation off" grep -q 'powercfg.exe /hibernate off' "$FINISH"
  check "finish script reads the firmware product key without logging it" bash -c "grep -q OA3xOriginalProductKey '$FINISH' && ! grep -E '^\s*Say.*firmwareKey' '$FINISH'"
  check "answer file turns Fast Startup off"   grep -q 'HiberbootEnabled' "$SETUP/archinstall/unattend-0.xml"
  # Keep the generated scripts: the PowerShell driver parses them with the real PowerShell parser.
  if [[ -n "$EVIDENCE_DIR" ]]; then
    mkdir -p "$EVIDENCE_DIR/generated"
    cp "$SETUP"/sources/\$OEM\$/\$\$/Setup/Scripts/* "$EVIDENCE_DIR/generated/"
    cp "$SETUP/archinstall/unattend-0.xml" "$SETUP/archinstall/unattend-7.xml" "$EVIDENCE_DIR/generated/"
    cp /tmp/xwim/winpeshl.ini /tmp/xwim/archinstall.cmd "$EVIDENCE_DIR/generated/" 2>/dev/null || true
    sgdisk -p "$LOOP" >"$EVIDENCE_DIR/partition-table.txt"
  fi
  umount "$SETUP"

  check "the ISO and WINSETUP are no longer mounted" bash -c "! findmnt /tmp/archinstall-windows-iso && ! findmnt /tmp/archinstall-windows-setup"
  check "grub.cfg has the Windows 11 entry"      grep -q 'menuentry "Windows 11" ' "$ESP/grub/grub.cfg"
  # Windows Setup is started by the firmware (a one-time boot entry), never through GRUB: Windows
  # Boot Manager chain-loaded from GRUB looks for its BCD on the ESP (0xc000000f, QEMU test), and
  # a grubenv makes GRUB print a Secure Boot policy error at every boot.
  check "grub.cfg has no entry for the Setup media" bash -c "! grep -qi 'Windows 11 Setup' '$ESP/grub/grub.cfg'"
  # grub-install always creates an (empty) grubenv; it must stay empty: no saved or next entry.
  check "grubenv carries no saved or next entry" bash -c "! grep -aqE '^(next_entry|saved_entry)=' '$ESP/grub/grubenv'"
  check "the Windows partition is left unformatted for Setup" bash -c "[[ -z \$(blkid -s TYPE -o value '${LOOP}p2') ]]"
fi

log "Disk layout"
EXPECTED_NAMES=(ESP Windows "Arch root")
[[ -n "$HOME_PART" ]] && EXPECTED_NAMES+=("Arch home")
EXPECTED_NAMES+=("$([[ -n "$SHARED" ]] && echo Shared || echo WINSETUP)")
COUNT="${#EXPECTED_NAMES[@]}"
LAST_GIB="$([[ -n "$SHARED" ]] && echo 16 || echo 12)"
check "the disk has exactly $COUNT partitions" bash -c "[[ \$(sgdisk -p '$LOOP' | awk '/^ +[0-9]+ /' | wc -l) -eq $COUNT ]]"
for index in "${!EXPECTED_NAMES[@]}"; do
  number=$((index + 1))
  check "partition $number is '${EXPECTED_NAMES[$index]}'" bash -c "sgdisk -i $number '$LOOP' | grep -q \"Partition name: '${EXPECTED_NAMES[$index]}'\""
done
check "the ESP has the EFI system type" bash -c "sgdisk -i 1 '$LOOP' | grep -q 'C12A7328-F81F-11D2-BA4B-00A0C93EC93B'"
check "the Windows partition is Microsoft basic data" bash -c "sgdisk -i 2 '$LOOP' | grep -q 'EBD0A0A2-B9E5-4433-87C0-68B6B72699C7'"
check "the last partition is Microsoft basic data (FAT32 now, NTFS later)" bash -c "sgdisk -i $COUNT '$LOOP' | grep -q 'EBD0A0A2-B9E5-4433-87C0-68B6B72699C7'"
check "the Windows partition is 64 GiB" bash -c "sgdisk -i 2 '$LOOP' | grep -q 'Partition size: .* (64.0 GiB)'"
check "the last partition is $LAST_GIB GiB" bash -c "sgdisk -i $COUNT '$LOOP' | grep -q 'Partition size: .* ($LAST_GIB.0 GiB)'"
check "the last partition ends at the end of the disk (nothing wasted)" bash -c "[[ \$(sgdisk -i $COUNT '$LOOP' | awk '/Last sector/ {print \$3}') -ge \$(( \$(sgdisk -p '$LOOP' | awk '/last usable sector/ {print \$NF}') - 2048 )) ]]"
if [[ -n "$HOME_PART" ]]; then
  check "/home ends before the last partition starts" bash -c "[[ \$(sgdisk -i 4 '$LOOP' | awk '/Last sector/ {print \$3}') -lt \$(sgdisk -i $COUNT '$LOOP' | awk '/First sector/ {print \$3}') ]]"
fi
check "the disk has a GUID (the Windows launcher looks for it)" bash -c "[[ -n \$(sgdisk -p '$LOOP' | awk '/Disk identifier/ {print \$4}') ]]"
check "the partition table is valid (sgdisk --verify)" bash -c "sgdisk --verify '$LOOP' | grep -q 'No problems found'"

if [[ "$AUR_HELPER" != "none" ]]; then
  log "AUR helper ($AUR_HELPER)"
  check "$AUR_HELPER runs inside the installed system" shows "$AUR_HELPER" arch-chroot "$CHECK" runuser -l alice -c "$AUR_HELPER --version"
  # Expected with the shipped step: a prebuilt -bin package that starts is kept (yay); one that does not
  # start (paru-bin links libalpm.so.15) is replaced by the source package and the -bin one is removed.
  EXPECT="${AUR_EXPECT:-$([[ "$AUR_HELPER" == paru ]] && echo source || echo bin)}"
  OWNER_PKG="$(arch-chroot "$CHECK" pacman -Qoq "/usr/bin/$AUR_HELPER" 2>/dev/null || true)"
  echo "      expected: $EXPECT; /usr/bin/$AUR_HELPER is owned by: ${OWNER_PKG:-nobody}"
  if [[ "$EXPECT" == source ]]; then
    check "the installer noticed that $AUR_HELPER-bin does not start" grep -q "WARNING: $AUR_HELPER-bin could not be installed or does not start" /tmp/installer.log
    check "the installer says the helper works (installed from $AUR_HELPER)" grep -q "The AUR helper $AUR_HELPER works (installed from $AUR_HELPER)" /tmp/installer.log
    check "$AUR_HELPER is owned by the source package $AUR_HELPER" test "$OWNER_PKG" = "$AUR_HELPER"
    check "the prebuilt $AUR_HELPER-bin package was removed" bash -c "! arch-chroot '$CHECK' pacman -Q $AUR_HELPER-bin 2>/dev/null"
    check "the $AUR_HELPER-bin-debug package was removed too" bash -c "! arch-chroot '$CHECK' pacman -Q $AUR_HELPER-bin-debug 2>/dev/null"
  else
    check "the installer says the helper works (installed from $AUR_HELPER-bin)" grep -q "The AUR helper $AUR_HELPER works (installed from $AUR_HELPER-bin)" /tmp/installer.log
    check "$AUR_HELPER is owned by the prebuilt package $AUR_HELPER-bin" test "$OWNER_PKG" = "$AUR_HELPER-bin"
    check "no fallback warning in the installer log" bash -c "! grep -q 'WARNING: $AUR_HELPER' /tmp/installer.log"
  fi
  check "the temporary NOPASSWD sudoers file is gone" bash -c "! test -e '$CHECK/etc/sudoers.d/99-archinstall-aur'"
  check "the wheel rule is the only sudoers.d file left" bash -c "[[ \$(ls '$CHECK/etc/sudoers.d' | tr '\n' ' ') == '10-wheel ' ]]"
  check "base-devel and git were installed for makepkg" bash -c "arch-chroot '$CHECK' pacman -Q base-devel git >/dev/null"
fi

if [[ -n "$LAPTOP" ]]; then
  log "Laptop extras"
  for pkg in power-profiles-daemon python-gobject bluez bluez-utils upower sof-firmware nvidia-prime nvidia-open nvidia-utils intel-media-driver vulkan-intel mesa brightnessctl; do
    check "package $pkg is installed" arch-chroot "$CHECK" pacman -Q "$pkg"
  done
  check "bluetooth.service is enabled" shows '^enabled$' arch-chroot "$CHECK" systemctl is-enabled bluetooth.service
  check "power-profiles-daemon.service is enabled" shows '^enabled$' arch-chroot "$CHECK" systemctl is-enabled power-profiles-daemon.service
  check "NetworkManager is still enabled" shows '^enabled$' arch-chroot "$CHECK" systemctl is-enabled NetworkManager.service
  check "python-gobject imports (powerprofilesctl needs it)" arch-chroot "$CHECK" python -c "import gi"
  # powerprofilesctl talks to the daemon over the SYSTEM bus. There is no systemd in a chroot, so start
  # a system bus and the daemon by hand, in the same chroot session as the client.
  PPD_OUT="$(arch-chroot "$CHECK" bash -c '
    PPD_BIN="$(sed -n "s/^ExecStart=//p" /usr/lib/systemd/system/power-profiles-daemon.service | head -n 1)"
    echo "daemon: $PPD_BIN"
    mkdir -p /run/dbus; dbus-uuidgen --ensure
    dbus-daemon --system --fork --nopidfile || echo "DBUS_START_FAILED"
    sleep 1
    $PPD_BIN >/tmp/ppd.log 2>&1 &
    PPD_PID=$!
    for i in 1 2 3 4 5 6 7 8 9 10; do busctl --system list 2>/dev/null | grep -q PowerProfiles && break; sleep 1; done
    echo "== powerprofilesctl list"; powerprofilesctl list 2>&1; echo "list exit=$?"
    echo "== powerprofilesctl get"; powerprofilesctl get 2>&1; echo "get exit=$?"
    echo "== daemon log"; cat /tmp/ppd.log
    kill $PPD_PID 2>/dev/null; pkill -x dbus-daemon 2>/dev/null; true
  ' 2>&1 || true)"
  echo "$PPD_OUT" | sed 's/^/      ppd: /' | head -n 40
  [[ -n "$EVIDENCE_DIR" ]] && echo "$PPD_OUT" >"$EVIDENCE_DIR/powerprofilesctl.txt"
  check "powerprofilesctl list runs and shows the profiles" bash -c 'grep -q "list exit=0" <<<"$1" && grep -q "balanced" <<<"$1"' _ "$PPD_OUT"
  check "powerprofilesctl get prints the active profile" bash -c 'grep -q "get exit=0" <<<"$1" && grep -qE "^(balanced|power-saver|performance)$" <<<"$1"' _ "$PPD_OUT"
  NMFILE="$CHECK/etc/NetworkManager/system-connections/TestNet.nmconnection"
  check "the Wi-Fi profile of the installation is kept" test -s "$NMFILE"
  check "the Wi-Fi profile is private (0600, root)" bash -c "[[ \$(stat -c '%a %u' '$NMFILE') == '600 0' ]]"
  check "the Wi-Fi profile has the SSID and the key" bash -c "grep -qx 'ssid=TestNet' '$NMFILE' && grep -qx 'psk=wifi-test-pass-2026' '$NMFILE' && grep -qx 'key-mgmt=wpa-psk' '$NMFILE'"
  check "the Wi-Fi profile has a valid uuid" bash -c "grep -qE '^uuid=[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\$' '$NMFILE'"
  check "the Wi-Fi passphrase is not in the installer output" bash -c "! grep -q 'wifi-test-pass-2026' /tmp/installer.log"
  check "the installer says the network is kept" bash -c "grep -q 'is kept for the installed system' /tmp/installer.log"
  HYPR="$CHECK/home/alice/.config/hypr/hyprland.conf"
  check "Hyprland config has the brightness keys" grep -q 'XF86MonBrightnessUp' "$HYPR"
  check "Hyprland config has the volume keys" grep -q 'XF86AudioRaiseVolume' "$HYPR"
  check "Hyprland config has tap-to-click" grep -q 'tap-to-click = true' "$HYPR"
  # Does the generated configuration load in the installed Hyprland? The version decides how.
  HYPR_VERSION="$(arch-chroot "$CHECK" pacman -Q hyprland 2>&1 | head -n 1 || true)"
  echo "      installed: ${HYPR_VERSION:-unknown}"
  rm -rf "$CHECK/tmp/xdg-test"; mkdir -p "$CHECK/tmp/xdg-test"
  OWNER="$(awk -F: '$1=="alice"{print $3":"$4}' "$CHECK/etc/passwd")"
  chown "$OWNER" "$CHECK/tmp/xdg-test"; chmod 700 "$CHECK/tmp/xdg-test"
  HYPR_OUT="$(arch-chroot "$CHECK" runuser -u alice -- env XDG_RUNTIME_DIR=/tmp/xdg-test HOME=/home/alice \
      Hyprland --verify-config -c /home/alice/.config/hypr/hyprland.conf 2>&1 || true)"
  echo "$HYPR_OUT" | sed 's/^/      hyprland: /' | head -n 25
  if [[ -n "$EVIDENCE_DIR" ]]; then
    echo "$HYPR_VERSION" >"$EVIDENCE_DIR/hyprland-verify-config.txt"; echo "$HYPR_OUT" >>"$EVIDENCE_DIR/hyprland-verify-config.txt"
    cp "$HYPR" "$EVIDENCE_DIR/hyprland.conf"; cp "$NMFILE" "$EVIDENCE_DIR/TestNet.nmconnection"
  fi
  check "Hyprland --verify-config accepts the generated file" grep -qiE 'config ok' <<<"$HYPR_OUT"
fi

if [[ -n "$DOWNLOAD_SOURCE" ]]; then
  log "Windows ISO downloaded from a link"
  check "the server was asked for the ISO" grep -q 'GET /Win11.iso' "$ISO_SERVER_LOG_DIR/access.log"
  check "the download was recorded" test -s /tmp/download-result.txt
  WANT_BYTES="$(stat -c %s "$WINDOWS_ISO")"
  WANT_SHA="$(sha256sum <"$WINDOWS_ISO" | cut -d' ' -f1)"
  echo "      source:     $WANT_BYTES bytes, sha256 $WANT_SHA"
  sed 's/^/      downloaded: /' /tmp/download-result.txt
  check "the downloaded file has the size of the source" grep -qx "downloaded_bytes=$WANT_BYTES" /tmp/download-result.txt
  check "the downloaded file has the checksum of the source" grep -qx "downloaded_sha256=$WANT_SHA" /tmp/download-result.txt
  check "the download was removed once Windows was staged" bash -c "! test -e '$CHECK/var/tmp/archinstall-windows11.iso'"
  [[ -n "$EVIDENCE_DIR" ]] && cp /tmp/download-result.txt "$ISO_SERVER_LOG_DIR/access.log" "$EVIDENCE_DIR/" 2>/dev/null || true
  stop_iso_server
fi

if [[ -n "$EVIDENCE_DIR" ]]; then
  cp /tmp/answers.json "$EVIDENCE_DIR/answers-redacted.json"
  sed -i -E 's/("[a-z_]*(password|passphrase)": )"[^"]*"/\1"***"/' "$EVIDENCE_DIR/answers-redacted.json"
fi
echo "duration_seconds=$((SECONDS - STARTED_AT))"

if [[ -n "${BOOT_TEST:-}" ]]; then
  # shellcheck source=boot-vm.sh
  source /src/scripts/integration/boot-vm.sh
  boot_test
fi

if (( FAILED )); then fail "some checks failed"; fi
log "ALL CHECKS PASSED (strategy=$STRATEGY fs=$FS desktop=$DESKTOP luks=${LUKS:-no} home=${HOME_PART:-no} shared=${SHARED:-no} aur=$AUR_HELPER laptop=${LAPTOP:-no} windows=${WINDOWS_ISO:+yes}${DOWNLOAD_SOURCE:+(download)})"
