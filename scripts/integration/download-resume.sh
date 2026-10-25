#!/usr/bin/env bash
# Does the "link" source survive a connection that dies in the middle of the 9 GB download?
#
# Runs in a privileged Arch container with the Windows ISO mounted at /windows/Windows11.iso.
# An HTTPS server (nginx, Range requests supported) serves it slowly enough to be interrupted;
# its worker process is killed (SIGKILL) once part of the file has arrived. The step under test is
# the SHIPPED one (steps/windows.py fetch_iso, unmodified, only its download folder is redirected).
#
#   A1  one cut at ~1.5 GiB: the step must finish by itself, in ONE run, with the source's SHA-256
#   A3  two cuts (~1.5 GiB and ~4 GiB) in one run
#
# Results are printed as "RESULT <name>: ok|FAIL ..." lines.
set -uo pipefail

ISO=/windows/Windows11.iso
KILL_AT=$((1500 * 1024 * 1024))
KILL_AT_2=$((4000 * 1024 * 1024))
DL=/dl/archinstall-windows11.iso
mkdir -p /dl
log() { printf '\n##### %s\n' "$*"; }
result() { printf 'RESULT %s\n' "$*"; return 0; }

log "Tools and HTTPS server"
sed -i '/^NoExtract/d' /etc/pacman.conf
pacman -Sy --noconfirm --needed python wimlib psmisc >/dev/null 2>&1 || { echo "cannot install tools" >&2; exit 1; }
# shellcheck source=iso-server.sh
source <(tr -d '\r' </src/scripts/integration/iso-server.sh)
start_iso_server "$ISO" 120m || { echo "cannot start the HTTPS server" >&2; exit 1; }
echo "serving at $ISO_SERVER_URL ($(curl --version | head -n 1))"
echo "shipped curl line: $(grep -n 'retry' /src/installer/dualboot_installer/steps/windows.py | tr -s ' ' | tr '\n' ' ')"

WANT_BYTES="$(stat -c %s "$ISO")"
WANT_SHA="$(sha256sum <"$ISO" | cut -d' ' -f1)"
echo "source: $WANT_BYTES bytes, sha256 $WANT_SHA"

# Kill the nginx worker once $1 has at least $2 bytes. The master respawns a worker at once.
kill_midway() {
  local file="$1" threshold="$2" size
  while :; do
    size="$(stat -c %s "$file" 2>/dev/null || echo 0)"
    if (( size >= threshold )); then
      pkill -9 -f 'nginx: worker' && echo "[killer] server worker killed with $size bytes on disk"
      return
    fi
    sleep 0.3
  done
}

shipped_step() {  # the real fetch_iso, with the download folder redirected to /dl
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/src/installer ISO_URL="$ISO_SERVER_URL" python - <<'PY'
import os, sys
from dualboot_installer.config import InstallConfig
from dualboot_installer.runner import CommandError, Runner
from dualboot_installer.steps import windows
from dualboot_installer.steps.context import Context

windows.DOWNLOAD_TARGET = "/dl/archinstall-windows11.iso"
cfg = InstallConfig()
cfg.disk.device, cfg.disk.size_bytes, cfg.disk.windows_gib = "/dev/loop99", 110 * 1024**3, 64
cfg.windows.iso_source, cfg.windows.iso_url, cfg.windows.edition = "download", os.environ["ISO_URL"], "Pro"
ctx = Context(cfg, cfg.layout(), Runner(dry_run=False, log=lambda line: print("  installer>", line, flush=True)))
try:
    windows.fetch_iso(ctx)
except CommandError as exc:
    print("STEP_FAILED:", exc)
    sys.exit(3)
print("STEP_OK: image index", ctx.state.get("image_index"), "language", cfg.windows.language_tag)
PY
}

run_case() {  # name, then the byte thresholds at which the server is cut
  local name="$1"; shift
  log "$name: the shipped step, connection cut at: $*"
  rm -f "$DL"
  : >"$ISO_SERVER_LOG_DIR/access.log"
  local KILLS_BEFORE
  KILLS_BEFORE="$(grep -c 'exited on signal 9' "$ISO_SERVER_LOG_DIR/error.log" 2>/dev/null || true)"
  KILLS_BEFORE="${KILLS_BEFORE:-0}"
  ( for threshold in "$@"; do kill_midway "$DL" "$threshold"; done ) &
  local killer=$!
  shipped_step 2>&1 | tee "/tmp/$name.log"
  local status=${PIPESTATUS[0]}
  kill "$killer" 2>/dev/null; wait "$killer" 2>/dev/null
  local got_sha got_bytes
  got_sha="$(sha256sum <"$DL" 2>/dev/null | cut -d' ' -f1)"; got_bytes="$(stat -c %s "$DL" 2>/dev/null || echo 0)"
  echo "step exit status $status; file $got_bytes bytes, sha256 $got_sha"
  grep 'GET /Win11.iso' "$ISO_SERVER_LOG_DIR/access.log" | sed 's/^/  server log: /'
  # nginx logs a response only when it ends normally: the responses cut by the killer are not in the access
  # log. The cuts themselves are counted in the error log ("worker process ... exited on signal 9").
  local resumed cuts
  resumed="$(grep -c '" 206 ' "$ISO_SERVER_LOG_DIR/access.log")"
  cuts=$(( $(grep -c 'exited on signal 9' "$ISO_SERVER_LOG_DIR/error.log") - KILLS_BEFORE ))
  if (( status == 0 )) && [[ "$got_sha" == "$WANT_SHA" ]]; then
    result "$name the shipped step survives the cut(s) in ONE run and the SHA-256 matches the source: ok"
  else
    result "$name the shipped step survives the cut(s) in ONE run and the SHA-256 matches the source: FAIL (exit status $status, $got_bytes of $WANT_BYTES bytes)"
  fi
  if (( cuts == $# )); then
    result "$name the server connection was really cut $cuts time(s) as planned: ok"
  else
    result "$name the server connection was really cut $cuts time(s) as planned: FAIL (planned $#)"
  fi
  if (( resumed >= 1 )); then
    result "$name the transfer ended with a resumed Range response (HTTP 206): ok"
  else
    result "$name the transfer ended with a resumed Range response (HTTP 206): FAIL"
  fi
}

run_case A1 "$KILL_AT"
run_case A3 "$KILL_AT" "$KILL_AT_2"

stop_iso_server
rm -rf /dl
if [[ -n "${EVIDENCE_DIR:-}" ]]; then cp /tmp/A1.log /tmp/A3.log "$ISO_SERVER_LOG_DIR/error.log" "$EVIDENCE_DIR/" 2>/dev/null || true; fi
exit 0
