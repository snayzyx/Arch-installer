#!/usr/bin/env bash
# Runs INSIDE the live system, after an install. Re-does, with the real efibootmgr, what the
# installer does on a SECOND install (steps/bootloader.py, register_boot_entry): find the
# previous "ArchInstall" entries with its regex, delete them, create the entry again.
# usage: live-guest-bootentry.sh '<loader path, e.g. \EFI\ArchInstall\shimx64.efi>'
loader="$1"
python3 - <<'EOF'
import re
import subprocess

listing = subprocess.run(["efibootmgr"], capture_output=True, text=True).stdout
print("efibootmgr (what the installer parses):")
print("\n".join("  " + line for line in listing.splitlines()))
# The installer's expression, copied from register_boot_entry.
numbers = re.findall(r"^Boot([0-9A-Fa-f]{4})\*?\s+ArchInstall\b", listing, re.MULTILINE)
print("regex finds:", numbers)
for number in numbers:
    done = subprocess.run(["efibootmgr", "--bootnum", number, "--delete-bootnum"], capture_output=True, text=True)
    print(f"delete {number}: exit {done.returncode}")
after = subprocess.run(["efibootmgr"], capture_output=True, text=True).stdout
print("ArchInstall still listed after deleting:", "ArchInstall" in after)
EOF
efibootmgr --create --disk /dev/vda --part 1 --label ArchInstall --loader "$loader" >/dev/null 2>&1
echo "create exit: $?"
efibootmgr | grep -E 'BootOrder|ArchInstall'
