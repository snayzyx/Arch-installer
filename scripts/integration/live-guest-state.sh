#!/usr/bin/env bash
# Runs INSIDE the live system: what the installer's own detection sees in the firmware.
cd /root/t || exit 1
echo "efivars entries: $(ls /sys/firmware/efi/efivars 2>/dev/null | wc -l)"
for name in SetupMode SecureBoot; do
  file="/sys/firmware/efi/efivars/${name}-8be4df61-93ca-11d2-aa0d-00e098032b8c"
  [[ -r "$file" ]] && echo "$name byte: $(od -An -tu1 -j4 -N1 "$file" | tr -d ' ')"
done
mokutil --sb-state 2>&1 | head -2
PYTHONPATH=/root/t python3 - <<'EOF'
from dualboot_installer import secureboot
state = secureboot.detect()
print(f"detect(): setup_mode={state.setup_mode} secure_boot={state.enabled}")
print(f"auto strategy would be: {secureboot.resolve_strategy('auto', state)}")
EOF
