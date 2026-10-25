#!/usr/bin/env bash
# Runs INSIDE the live system (booted from the OFFLINE ISO): what the installer will see.
# usage: live-guest-e2e-state.sh <installer PYTHONPATH>
installer="${1:-/opt/archinstall}"
echo "efivars entries: $(ls /sys/firmware/efi/efivars 2>/dev/null | wc -l)"
for name in SetupMode SecureBoot; do
  file="/sys/firmware/efi/efivars/${name}-8be4df61-93ca-11d2-aa0d-00e098032b8c"
  [[ -r "$file" ]] && echo "$name byte: $(od -An -tu1 -j4 -N1 "$file" | tr -d ' ')"
done
mokutil --sb-state 2>&1 | head -2
echo "TPM devices: $(ls /sys/class/tpm 2>/dev/null | tr '\n' ' ') version: $(cat /sys/class/tpm/tpm0/tpm_version_major 2>/dev/null)"
echo "--- the Windows ISO bundled in this ISO ---"
ls -l /run/archiso/bootmnt/windows/ 2>&1 | sed 's/^/  /'
echo "--- what the installer's own code says ---"
PYTHONPATH="$installer" python3 - <<'EOF'
from dualboot_installer import secureboot, windows_iso
state = secureboot.detect()
print("  detect():", state)
print("  automatic strategy:", secureboot.resolve_strategy("auto", state))
info = windows_iso.load_manifest()
if info is None:
    print("  load_manifest(): None (no usable manifest)")
else:
    print("  embedded ISO:", info.path, "language", info.language_tag, "editions", info.editions)
    print("  WINDOWS-ISO-PATH", info.path)
EOF
echo "--- tools ---"
for tool in archinstall-launcher sbctl mokutil efibootmgr wimlib-imagex rsync sbsign ntfs-3g; do
  printf '  %-20s %s\n' "$tool" "$(command -v $tool || echo MISSING)"
done
