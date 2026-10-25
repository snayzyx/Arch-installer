#!/usr/bin/env bash
# Runs inside the builder container (see build.ps1).
# Builds the ArchInstall ISO from the stock archiso "releng" profile plus the
# overlay in /src/profile and the installer in /src/installer.
#
# Set WINDOWS_ISO to the path of a Windows 11 ISO to build the "offline" flavour,
# which carries that ISO inside the image.
set -euo pipefail

SRC=/src
OUT=/out
WORK=/tmp/archinstall-build
PROFILE="$WORK/profile"
ISOFS="$WORK/work/iso" # the tree mkarchiso turns into the ISO

WINDOWS_ISO="${WINDOWS_ISO:-}"
WINDOWS_DIR=windows # inside the ISO: /windows
WINDOWS_FILE=Windows11.iso

ISO_NAME="archinstall"
ISO_LABEL="ARCHINSTALL"
ISO_VERSION="$(date +%Y.%m.%d)"
ISO_TITLE="ArchInstall dual boot installer"
if [[ -n "$WINDOWS_ISO" ]]; then
  ISO_NAME="archinstall-offline"
  ISO_TITLE="ArchInstall dual boot installer (offline, Windows 11 included)"
fi

# Microsoft-signed shim for the "shim + MOK" Secure Boot path: Fedora's package, unmodified
# (the AUR's shim-signed uses the same file). Keep in sync with steps/bootloader.py; a test
# checks that the two agree.
SHIM_RPM="shim-x64-16.1-7.x86_64.rpm"
SHIM_URL="https://kojipkgs.fedoraproject.org/packages/shim/16.1/7/x86_64/$SHIM_RPM"
SHIM_SHA256="0a119f3488e5da27ca55c1864ee8e131afa5ecf27689385ba09b805eb00dd608"

log() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# Download (once, into the cached volume) and verify the shim, then put it on the ISO.
embed_shim() {
  local cached="/var/cache/pacman/pkg/$SHIM_RPM"
  log "Embedding the signed shim"
  [[ -f "$cached" ]] || curl -fsSL --retry 3 -o "$cached" "$SHIM_URL" || die "Cannot download $SHIM_URL"
  if ! echo "$SHIM_SHA256  $cached" | sha256sum -c - >/dev/null; then
    rm -f "$cached"
    die "The shim RPM does not match the pinned SHA-256; refusing to use it."
  fi
  install -D -m 0644 "$cached" "$PROFILE/airootfs/opt/archinstall/shim/$SHIM_RPM"
}

# Describe the Windows ISO (editions, languages, checksum) in $WORK/manifest.json.
# The installer reads that file at run time instead of mounting a 9 GB image.
describe_windows_iso() {
  [[ -f "$WINDOWS_ISO" ]] || die "Windows ISO not found: $WINDOWS_ISO"
  command -v wimlib-imagex >/dev/null && command -v python >/dev/null \
    || die "The builder image is outdated. Run build.ps1 again without -SkipImage."

  log "Checking the Windows ISO"
  local size sha256 wim mount_point="$WORK/windows-mount"
  mkdir -p "$mount_point"
  # Windows ISOs are UDF: bsdtar and xorriso only see an ISO 9660 stub with a README, so mount it.
  mount -t udf -o loop,ro "$WINDOWS_ISO" "$mount_point" 2>/dev/null \
    || mount -t iso9660 -o loop,ro "$WINDOWS_ISO" "$mount_point" \
    || die "Cannot mount $WINDOWS_ISO inside the container (UDF/ISO 9660 loop mount failed)."
  wim="$mount_point/sources/install.wim"
  [[ -f "$wim" ]] || wim="$mount_point/sources/install.esd"
  if [[ ! -f "$wim" ]]; then
    umount "$mount_point"
    die "$WINDOWS_ISO does not look like a Windows installation ISO (no sources/install.wim)."
  fi
  wimlib-imagex info "$wim" >"$WORK/wim-info.txt" || { umount "$mount_point"; die "wimlib cannot read $wim."; }
  umount "$mount_point"

  size="$(stat -c %s "$WINDOWS_ISO")"
  log "Computing the SHA-256 of the Windows ISO"
  sha256="$(sha256sum "$WINDOWS_ISO" | cut -d' ' -f1)"

  PYTHONPATH="$SRC/installer" PYTHONDONTWRITEBYTECODE=1 python -m dualboot_installer.windows_iso make-manifest \
    --file "$WINDOWS_FILE" --size "$size" --sha256 "$sha256" \
    --wim-info "$WORK/wim-info.txt" --output "$WORK/manifest.json"
  cat "$WORK/manifest.json"
}

# Put the Windows ISO into the ISO tree without copying it: a bind mount makes the
# host file appear in $ISOFS/windows, and xorriso reads it from there.
attach_windows_iso() {
  install -d "$ISOFS/$WINDOWS_DIR"
  install -m 0644 "$WORK/manifest.json" "$ISOFS/$WINDOWS_DIR/manifest.json"
  : >"$ISOFS/$WINDOWS_DIR/$WINDOWS_FILE"
  mount --bind "$WINDOWS_ISO" "$ISOFS/$WINDOWS_DIR/$WINDOWS_FILE"
  trap 'umount "$ISOFS/$WINDOWS_DIR/$WINDOWS_FILE" 2>/dev/null || true' EXIT
}

log "Preparing the archiso profile"
rm -rf "$WORK"
mkdir -p "$WORK" "$OUT"
cp -r /usr/share/archiso/configs/releng "$PROFILE"

[[ -z "$WINDOWS_ISO" ]] || describe_windows_iso

# 1. Overlay: files copied on top of the releng airootfs.
cp -a "$SRC/profile/airootfs/." "$PROFILE/airootfs/"

# 2. Extra packages for the live environment.
grep -vE '^\s*(#|$)' "$SRC/profile/packages.extra" >> "$PROFILE/packages.x86_64"

# 3. The installer itself.
install -d "$PROFILE/airootfs/opt/archinstall"
cp -a "$SRC/installer/dualboot_installer" "$PROFILE/airootfs/opt/archinstall/"
find "$PROFILE/airootfs/opt/archinstall" -name '__pycache__' -type d -prune -exec rm -rf {} +

# 4. Files edited on Windows may carry CRLF line endings; scripts would break. Only TEXT files
# are touched (grep -I skips binaries): this used to rewrite the shim RPM too, which changed its
# bytes, so the installer rejected it on the live ISO.
find "$PROFILE/airootfs" -type f -exec sh -c \
  'for f; do if grep -Iq . "$f"; then sed -i "s/\r\$//" "$f"; fi; done' sh {} +

# The binary payload goes in after the normalization, and is checked once more right before the
# image is built, so a corrupted copy can never ship.
embed_shim
echo "$SHIM_SHA256  $PROFILE/airootfs/opt/archinstall/shim/$SHIM_RPM" | sha256sum -c - >/dev/null \
  || die "The shim RPM in the image tree does not match the pinned SHA-256."

# 5. Branding and permissions.
sed -i \
  -e "s|^iso_name=.*|iso_name=\"$ISO_NAME\"|" \
  -e "s|^iso_label=.*|iso_label=\"$ISO_LABEL\"|" \
  -e "s|^iso_publisher=.*|iso_publisher=\"ArchInstall\"|" \
  -e "s|^iso_application=.*|iso_application=\"$ISO_TITLE\"|" \
  -e "s|^iso_version=.*|iso_version=\"$ISO_VERSION\"|" \
  "$PROFILE/profiledef.sh"

cat >> "$PROFILE/profiledef.sh" <<'EOF'

file_permissions+=(
  ["/usr/local/bin/archinstall-launcher"]="0:0:755"
)
EOF

grep -rlE 'Arch Linux install medium' "$PROFILE/efiboot" "$PROFILE/syslinux" "$PROFILE/grub" 2>/dev/null \
  | xargs -r sed -i "s/Arch Linux install medium/$ISO_TITLE/g"

# copytoram=auto (archiso's default) copies the system to RAM and then UNMOUNTS the boot medium
# when the medium is not an optical drive (a USB stick) and enough RAM is free. The Windows ISO of
# the offline build lives on that medium, and the installer finds the live medium through its mount
# to protect it from being erased: both would be lost. Found by the interactive end-to-end test;
# earlier VM tests booted from a virtual CD (exempt from copytoram=auto) and could not see it.
# The network (PXE) boot menu is not part of this project and is left as it is.
find "$PROFILE/efiboot" "$PROFILE/syslinux" "$PROFILE/grub" -type f ! -name 'archiso_pxe-linux.cfg' \
  -exec grep -lE 'archisobasedir=' {} + | xargs -r sed -i 's/archisobasedir=%INSTALL_DIR%/& copytoram=n/'
# A boot line that was missed would silently bring the bug back on a USB stick: fail the build instead.
if find "$PROFILE/efiboot" "$PROFILE/syslinux" "$PROFILE/grub" -type f ! -name 'archiso_pxe-linux.cfg' \
     -exec grep -hE 'archisobasedir=' {} + | grep -v 'copytoram=n' | grep -q .; then
  die "A boot menu line still lacks copytoram=n; the sed in build-iso.sh did not match it."
fi

[[ -z "$WINDOWS_ISO" ]] || attach_windows_iso

# 6. Build. The image is written inside the container first: a 10 GB file written straight
# to a folder shared with Windows was seen to vanish. It is copied and re-verified afterwards.
BUILD_OUT=/tmp/archinstall-out
log "Running mkarchiso (this downloads packages on the first run)"
mkarchiso -v -w "$WORK/work" -o "$BUILD_OUT" "$PROFILE"

ISO_NAME_FULL="${ISO_NAME}-${ISO_VERSION}-x86_64.iso"
ISO_BUILT="$BUILD_OUT/$ISO_NAME_FULL"
[[ -f "$ISO_BUILT" ]] || die "mkarchiso did not produce $ISO_BUILT"

if [[ -n "$WINDOWS_ISO" ]]; then
  log "Checking that the Windows ISO is inside the image"
  expected="$(stat -c %s "$WINDOWS_ISO")"
  # `|| true`: bsdtar can exit non-zero on exotic ISOs while still listing correctly.
  found="$(bsdtar -tvf "$ISO_BUILT" 2>/dev/null | awk '$NF ~ /(^|\/)windows\/Windows11\.iso$/ { print $5 }' || true)"
  echo "expected $expected bytes, image lists: ${found:-<nothing>}"
  [[ "$found" == "$expected" ]] || die "The Windows ISO is missing or truncated inside the image."
fi

log "Computing the checksum"
( cd "$BUILD_OUT" && sha256sum "$ISO_NAME_FULL" > "$ISO_NAME_FULL.sha256" )

log "Copying the ISO to the output folder"
cp -- "$BUILD_OUT/$ISO_NAME_FULL" "$BUILD_OUT/$ISO_NAME_FULL.sha256" "$OUT/"
sync
[[ "$(stat -c %s "$OUT/$ISO_NAME_FULL")" == "$(stat -c %s "$ISO_BUILT")" ]] \
  || die "The copy in the output folder has the wrong size."
( cd "$OUT" && sha256sum -c "$ISO_NAME_FULL.sha256" ) || die "The copy in the output folder is corrupted."

log "Done: $OUT/$ISO_NAME_FULL"
du -h "$OUT/$ISO_NAME_FULL"
