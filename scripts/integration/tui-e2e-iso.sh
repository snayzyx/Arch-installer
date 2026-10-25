#!/usr/bin/env bash
# Runs INSIDE the builder container (see scripts/tui-e2e.ps1): puts the offline ISO in the Docker volume
# mounted on /work and checks it against the expected SHA-256. Skips the copy when the volume already holds
# exactly that ISO.   Needs EXPECTED (SHA-256) and ISO_NAME (file name inside /iso).
set -euo pipefail
cd /work
if [[ -f offline.iso && -f offline.iso.sha256 ]] && grep -q "^$EXPECTED " offline.iso.sha256 \
   && [[ "$(stat -c %s offline.iso)" == "$(stat -c %s "/iso/$ISO_NAME")" ]]; then
  echo "the volume already holds this ISO"
else
  rm -f offline.iso offline.iso.sha256 offline.iso.part
  cp "/iso/$ISO_NAME" offline.iso.part
  actual="$(sha256sum offline.iso.part | cut -d' ' -f1)"
  if [[ "$actual" != "$EXPECTED" ]]; then
    echo "SHA-256 MISMATCH: expected $EXPECTED, got $actual" >&2
    rm -f offline.iso.part
    exit 1
  fi
  mv offline.iso.part offline.iso
  echo "$actual  offline.iso" > offline.iso.sha256
  echo "copied and verified"
fi
