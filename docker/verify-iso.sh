#!/usr/bin/env bash
# Verifies a built ArchInstall ISO. Runs inside the builder container (see scripts/rebuild-all.ps1).
#
#   verify-iso.sh /dist/<iso>                              online ISO
#   verify-iso.sh /dist/<iso> offline /windows-source.iso  offline ISO + the Windows ISO it was built from
#
# The offline check loop-mounts the image, so the container must be privileged.
set -uo pipefail
ISO="$1"; MODE="${2:-online}"; SRC_WIN="${3:-}"
FAIL=0
ok()   { printf '  ok    %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; FAIL=1; }
have() { grep -qE "$1" /tmp/files.txt; }

echo "ISO: $ISO ($(stat -c %s "$ISO") bytes)"
( cd "$(dirname "$ISO")" && sha256sum -c "$(basename "$ISO").sha256" >/dev/null 2>&1 ) \
  && ok "the .sha256 file validates" || bad "the .sha256 file does NOT validate"

mkdir -p /t && cd /t
bsdtar -xf "$ISO" arch/x86_64/airootfs.sfs 2>/dev/null || bad "cannot extract airootfs.sfs"
unsquashfs -l arch/x86_64/airootfs.sfs >/tmp/files.txt 2>/dev/null || bad "cannot list airootfs.sfs"

echo "--- shim"
have 'opt/archinstall/shim/shim-x64-16\.1-7\.x86_64\.rpm$' && ok "shim rpm embedded" || bad "shim rpm missing"
unsquashfs -d /t/shimrpm -f arch/x86_64/airootfs.sfs opt/archinstall/shim/shim-x64-16.1-7.x86_64.rpm >/dev/null 2>&1
want="$(grep -oE 'SHIM_SHA256="[0-9a-f]{64}"' /src/docker/build-iso.sh | cut -d'"' -f2)"
got="$(sha256sum /t/shimrpm/opt/archinstall/shim/shim-x64-16.1-7.x86_64.rpm 2>/dev/null | cut -d' ' -f1)"
[[ -n "$want" && "$want" == "$got" ]] && ok "shim rpm sha256 matches the pin ($got)" || bad "shim sha256 mismatch (pin '$want', image '$got')"

echo "--- installer modules"
grep -E 'opt/archinstall/dualboot_installer/.*\.(py|tcss)$' /tmp/files.txt | sed 's|squashfs-root/opt/archinstall/dualboot_installer/||' | sort | tr '\n' ' ' | fold -w 150 -s
echo
echo "    $(grep -cE 'opt/archinstall/dualboot_installer/.*\.py$' /tmp/files.txt) python modules in the image"
for m in __main__ app config plan runner layout windows_iso windows_unattend headless steps/bootloader steps/arch steps/storage steps/preflight; do
  have "dualboot_installer/$m\.py$" && ok "module $m" || bad "module $m MISSING"
done
have 'dualboot_installer/steps/windows\.py$' && ok "module steps/windows" || bad "module steps/windows MISSING"
have 'dualboot_installer/installer\.tcss$' && ok "installer.tcss" || bad "installer.tcss MISSING"
# The MOK re-enrollment service must be part of the bootloader step (it queues the key again if the
# first MokManager screen was skipped).
unsquashfs -d /t/code -f arch/x86_64/airootfs.sfs opt/archinstall/dualboot_installer/steps/bootloader.py >/dev/null 2>&1
grep -q 'archinstall-mok-enroll' /t/code/opt/archinstall/dualboot_installer/steps/bootloader.py 2>/dev/null \
  && ok "steps/bootloader.py contains 'archinstall-mok-enroll' ($(grep -c 'archinstall-mok-enroll' /t/code/opt/archinstall/dualboot_installer/steps/bootloader.py) mentions)" \
  || bad "steps/bootloader.py does NOT contain 'archinstall-mok-enroll'"
# The image carries EXACTLY the installer code of the repository (catches a stale build): every
# file is compared, line endings ignored, and the image must not hold extra files either.
rm -rf /t/code && unsquashfs -d /t/code -f arch/x86_64/airootfs.sfs opt/archinstall/dualboot_installer >/dev/null 2>&1
IMG=/t/code/opt/archinstall/dualboot_installer; REPO=/src/installer/dualboot_installer
repo_files="$(cd "$REPO" && find . -type f ! -path '*/__pycache__/*' | sort)"
image_files="$(cd "$IMG" 2>/dev/null && find . -type f ! -path '*/__pycache__/*' | sort)"
same_count=0; differ=0
while IFS= read -r f; do
  a="$(tr -d '\r' <"$IMG/$f" 2>/dev/null | sha256sum | cut -d' ' -f1)"
  b="$(tr -d '\r' <"$REPO/$f" | sha256sum | cut -d' ' -f1)"
  if [[ "$a" == "$b" && -f "$IMG/$f" ]]; then same_count=$((same_count + 1)); else differ=$((differ + 1)); bad "$f differs from the repository copy (stale build?)"; fi
done <<<"$repo_files"
extra="$(comm -13 <(echo "$repo_files") <(echo "$image_files"))"
[[ -z "$extra" ]] || bad "files in the image but not in the repository: $(echo $extra)"
(( differ == 0 )) && [[ -n "$repo_files" ]] && ok "all $same_count installer files are identical to the repository (none missing, none extra)"

# Markers of the latest fixes: they prove this image has them, not just that it matches a repo copy.
marker() {  # marker <file> <pattern> <description>
  grep -qE -- "$2" "$IMG/$1" 2>/dev/null && ok "$1: $3" || bad "$1: $3 NOT FOUND"
}
marker windows_unattend.py 'archinstall-bootorder'   "has the boot-order script ($(grep -c 'archinstall-bootorder' "$IMG/windows_unattend.py" 2>/dev/null) mentions)"
marker windows_unattend.py 'ArchInstallBootOrder'    "has the scheduled task name 'ArchInstallBootOrder'"
marker steps/arch.py       'PACSTRAP_ATTEMPTS'       "pacstrap is retried (PACSTRAP_ATTEMPTS)"
marker steps/windows.py    'archinstall-bootorder'   "stages archinstall-bootorder.ps1"
# Going back must save the answers of the screen: self.collect() has to be called inside action_back.
if awk '/def action_back/{f=1} f&&/self\.collect\(\)/{found=1} f&&/self\.wizard\.go_back\(\)/{exit} END{exit !found}' "$IMG/screens/base.py" 2>/dev/null; then
  ok "screens/base.py: action_back calls self.collect() before going back"
else
  bad "screens/base.py: action_back does NOT call self.collect()"
fi

echo "--- libraries and tools"
have 'site-packages/textual/__init__\.py$' && ok "python-textual" || bad "python-textual"
have 'site-packages/tzlocal/windows_tz\.py$' && ok "python-tzlocal" || bad "python-tzlocal"
for f in sbctl wimlib-imagex rsync mokutil efibootmgr reflector sgdisk cryptsetup sbsign sbverify grub-install mkfs.ntfs pacstrap arch-chroot bsdtar curl openssl iwctl; do
  have "squashfs-root/usr/bin/$f\$" && ok "$f" || bad "$f MISSING"
done

echo "--- launcher and boot files"
unsquashfs -d /t/rootfs -f arch/x86_64/airootfs.sfs usr/local/bin/archinstall-launcher root/.zlogin etc/motd >/dev/null 2>&1
mode="$(stat -c %a /t/rootfs/usr/local/bin/archinstall-launcher 2>/dev/null)"
[[ "$mode" == 755 ]] && ok "archinstall-launcher has mode 755" || bad "archinstall-launcher mode is '$mode'"
grep -q archinstall-launcher /t/rootfs/root/.zlogin && ok ".zlogin starts the launcher on tty1" || bad ".zlogin does not start the launcher"
bsdtar -xf "$ISO" 'loader/entries/*' 2>/dev/null; grep -h '^title' loader/entries/*.conf 2>/dev/null | sed 's/^/        /'

# Without copytoram=n, archiso copies the system to RAM and unmounts the medium when it is a USB
# stick: the bundled Windows ISO and the live-medium protection would disappear.
bsdtar -xf "$ISO" boot/grub/grub.cfg boot/grub/loopback.cfg boot/syslinux/archiso_sys-linux.cfg 2>/dev/null
# Both UEFI entries (normal and speech), GRUB, GRUB loopback (ISO file booted from a disk) and BIOS syslinux.
for entry in loader/entries/01-archiso-linux.conf loader/entries/02-archiso-speech-linux.conf \
             boot/grub/grub.cfg boot/grub/loopback.cfg boot/syslinux/archiso_sys-linux.cfg; do
  if [[ -f "$entry" ]] && grep -q 'archisobasedir=' "$entry"; then
    if grep 'archisobasedir=' "$entry" | grep -qv 'copytoram=n'; then bad "$entry has a boot line without copytoram=n"
    else ok "$entry: every archiso boot line has copytoram=n ($(grep -c 'archisobasedir=' "$entry") line(s))"; fi
  elif [[ "$entry" == boot/grub/grub.cfg ]]; then
    # archiso boots UEFI through systemd-boot; GRUB's own menu is only shipped for some boot modes.
    echo "  note  $entry is not in this image (no GRUB menu to patch)"
  else
    bad "$entry not found in the image"
  fi
done
echo "        the UEFI boot line as shipped:"
grep -h '^options' loader/entries/01-archiso-linux.conf 2>/dev/null | sed 's/^/          /'

if [[ "$MODE" == "offline" ]]; then
  echo "--- offline payload"
  want_size="$(stat -c %s "$SRC_WIN")"
  listed="$(bsdtar -tvf "$ISO" 2>/dev/null | awk '$NF ~ /(^|\/)windows\/Windows11\.iso$/ { print $5 }')"
  [[ "$listed" == "$want_size" ]] && ok "windows/Windows11.iso is listed with the exact size ($listed bytes)" \
    || bad "listed size '$listed' != source $want_size"
  bsdtar -tf "$ISO" 2>/dev/null | grep -qE '(^|/)windows/manifest\.json$' && ok "windows/manifest.json present" || bad "manifest.json missing"
  bsdtar -xOf "$ISO" windows/manifest.json 2>/dev/null | head -c 500 | sed 's/^/        /'; echo
  echo "  loop-mounting the ISO to compare the Windows file byte for byte (a few minutes)..."
  mkdir -p /m
  if mount -o loop,ro "$ISO" /m 2>/tmp/mount.err; then
    src_sum="$(sha256sum "$SRC_WIN" | cut -d' ' -f1)"
    in_sum="$(sha256sum /m/windows/Windows11.iso | cut -d' ' -f1)"
    [[ "$src_sum" == "$in_sum" ]] && ok "sha256 of /windows/Windows11.iso == the source ISO ($in_sum)" \
      || bad "sha256 differs: in the image $in_sum, source $src_sum"
    ls -l /m/windows/ | sed 's/^/        /'
    umount /m
  else
    bad "cannot loop-mount the offline ISO: $(cat /tmp/mount.err)"
  fi
fi

if (( FAIL )); then echo "SOME VERIFICATIONS FAILED"; else echo "ALL VERIFICATIONS PASSED"; fi
exit "$FAIL"
