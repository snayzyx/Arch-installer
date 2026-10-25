#!/usr/bin/env bash
# Runs INSIDE the live system (the VM). The official Arch ISO lacks the tools that our own ISO
# ships through profile/packages.extra; install them the same way.
for attempt in 1 2 3 4 5; do
  # The first tries can fail while pacman's keyring is still being initialised at boot.
  if pacman -Sy --noconfirm --needed sbctl sbsigntools mokutil wimlib rsync >/tmp/pacman.log 2>&1; then
    command -v sbctl mokutil sbsign efibootmgr >/dev/null && echo PREPARE-OK
    exit 0
  fi
  sleep 20
done
tail -n 15 /tmp/pacman.log
exit 1
