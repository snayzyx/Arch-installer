#!/usr/bin/env bash
# Checks of the Caelestia desktop shell preset (answers: arch.dotfiles = caelestia).
# Sourced by test-install.sh at the very end, so it can use check/log/shows, $CHECK (the installed system,
# mounted), /tmp/installer.log (the installer's output) and /tmp/installer.ts.log (the same, with seconds).
#
# Optional extra parts, switched on by an (empty) flag file in the evidence directory, because they take
# minutes and change the installed system:
#   RUN-IDEMPOTENCE  run `caelestia install` a second time
#   RUN-CLEAN        remove the build tools (orphans), then prove the shell still loads
#   RUN-FAULTS       fault injection through the real engine (dotfiles_caelestia_faults.py); runs last: it rolls the preset back
# shellcheck disable=SC2154  # CHECK, EVIDENCE_DIR, AUR_HELPER, ... come from test-install.sh

USER_NAME=alice
UH="$CHECK/home/$USER_NAME"
as_user() { arch-chroot "$CHECK" runuser -l "$USER_NAME" -c "$1"; }
flag() { [[ -n "${EVIDENCE_DIR:-}" && -e "$EVIDENCE_DIR/$1" ]]; }
sudoers_on() { echo "$USER_NAME ALL=(ALL) NOPASSWD: ALL" >"$CHECK/etc/sudoers.d/99-test-preset"; chmod 440 "$CHECK/etc/sudoers.d/99-test-preset"; }
sudoers_off() { rm -f "$CHECK/etc/sudoers.d/99-test-preset"; }

log "Caelestia preset: what the installer said"
RESULT_LINE="$(grep -a 'RECIPE-RESULT preset=caelestia' /tmp/installer.log | tail -n 1)"
echo "      ${RESULT_LINE:-<no RECIPE-RESULT line>}"
check "the RECIPE-RESULT line has the dots commit and the three package versions" \
  grep -aqE 'RECIPE-RESULT preset=caelestia dots=[0-9a-f]{40} caelestia-shell [^ ]+ caelestia-cli [^ ]+ quickshell-git [^ ]+' /tmp/installer.log
check "40-verify.sh reported config-ok" grep -aq 'RECIPE-RESULT verify=config-ok' /tmp/installer.log
check "the installer says the shell is installed (latest version)" grep -aq 'is installed (latest version)' /tmp/installer.log
check "no fallback to the tested versions was needed" bash -c "! grep -aq 'last tested versions' /tmp/installer.log"
check "no RECIPE-FAIL line" bash -c "! grep -aq 'RECIPE-FAIL' /tmp/installer.log"
check "no 'could not be installed' warning for the desktop shell" bash -c "! grep -aq 'desktop shell .* could not be installed' /tmp/installer.log"

log "Caelestia preset: leftovers of the build"
check "the installer left no AUR build cache (~/.cache/yay, ~/.cache/paru)" bash -c "! test -e '$UH/.cache/yay' && ! test -e '$UH/.cache/paru'"
check "the installer emptied the pacman package cache" test -z "$(ls -A "$CHECK/var/cache/pacman/pkg" 2>/dev/null)"

log "Caelestia preset: packages"
for pkg in caelestia-cli caelestia-shell quickshell-git pipewire-jack gammastep geoclue yay-bin; do
  check "package $pkg is installed" arch-chroot "$CHECK" pacman -Q "$pkg"
done
check "jack2 is NOT installed (pipewire-jack came first)" bash -c "! arch-chroot '$CHECK' pacman -Q jack2 2>/dev/null"
check "no -git shell/cli package (the dots ask for the releases)" bash -c "! arch-chroot '$CHECK' pacman -Q caelestia-shell-git 2>/dev/null"
echo "      versions: $(arch-chroot "$CHECK" pacman -Q caelestia-cli caelestia-shell quickshell-git qt6-m3shapes-git libcava pwvucontrol 2>&1 | tr '\n' ';')"

log "Caelestia preset: files"
for f in .config/hypr/hyprland.lua .config/hypr/variables.lua .config/hypr/hyprland.conf \
         .local/state/caelestia/dots-state.json .local/state/caelestia/scheme.json .local/state/caelestia/dots/manifest.toml \
         .config/foot/foot.ini .config/fish/config.fish; do
  check "~/$f exists" test -e "$UH/$f"
done
for f in /etc/xdg/quickshell/caelestia/shell.qml /usr/bin/caelestia /usr/bin/qs /usr/bin/quickshell; do
  check "$f exists" arch-chroot "$CHECK" test -e "$f"   # in the chroot: /usr/bin/qs is a symlink to an absolute path
done
for f in 20-caelestia.sh 40-verify.sh restore-default.sh packages-official.txt; do
  check "the preset file $f was copied into the new system" test -s "$CHECK/usr/local/share/archinstall/presets/caelestia/$f"
done
check "~/.config.bak exists (the CLI saved the configuration first)" test -d "$UH/.config.bak"
check "the backup holds the installer's own hyprland.conf" grep -q 'exec-once = waybar' "$UH/.config.bak/hypr/hyprland.conf"
check "the installer's hyprland.conf is still on disk (inert: hyprland.lua is read first)" grep -q 'exec-once = waybar' "$UH/.config/hypr/hyprland.conf"

log "Caelestia preset: state and the AUR helper"
STATE="$UH/.local/state/caelestia/dots-state.json"
APPLIED="$(python -c "import json;print(json.load(open('$STATE'))['applied_rev'])" 2>/dev/null)"
CLONE_HEAD="$(as_user 'git -C ~/.local/state/caelestia/dots rev-parse HEAD' 2>/dev/null)"
echo "      applied_rev=$APPLIED clone HEAD=$CLONE_HEAD"
check "dots-state.json applied_rev is the commit of the clone" test -n "$APPLIED" -a "$APPLIED" = "$CLONE_HEAD"
check "the RECIPE-RESULT commit is the applied one" grep -aq "dots=$APPLIED" /tmp/installer.log
check "the CLI recorded yay as the AUR helper" test "$(python -c "import json;print(json.load(open('$STATE'))['aur_helper'])")" = yay
check "yay works as the user" shows 'yay v' as_user 'yay --version'
check "yay is the prebuilt yay-bin package" test "$(arch-chroot "$CHECK" pacman -Qoq /usr/bin/yay)" = yay-bin
if [[ "$AUR_HELPER" == paru ]]; then
  echo "      -AurHelper paru was asked: paru-bin / paru installed? $(arch-chroot "$CHECK" pacman -Q paru paru-bin 2>&1 | tr '\n' ';')"
  check "the preset replaced the chosen paru by yay (paru is not installed)" bash -c "! arch-chroot '$CHECK' pacman -Q paru 2>/dev/null && ! arch-chroot '$CHECK' pacman -Q paru-bin 2>/dev/null"
fi
check "the temporary NOPASSWD sudoers file is gone" bash -c "! grep -rqs NOPASSWD '$CHECK/etc/sudoers.d'"
check "no archinstall sudoers leftover" bash -c "! ls '$CHECK/etc/sudoers.d' | grep -qi 'archinstall\\|99-test'"
check "no cli.json left behind (it would pin 'caelestia update' to a mirror)" bash -c "! test -e '$UH/.config/caelestia/cli.json'"

log "Caelestia preset: the Hyprland configuration"
VERIFY="$(as_user 'export XDG_RUNTIME_DIR=$(mktemp -d); chmod 700 $XDG_RUNTIME_DIR; Hyprland --verify-config 2>&1' | sed 's/\x1b\[[0-9;]*m//g')"
echo "$VERIFY" | tail -n 4 | sed 's/^/      /'
check "Hyprland --verify-config says config ok (default path)" grep -q 'config ok' <<<"$VERIFY"
check "Hyprland reads hyprland.lua before hyprland.conf" grep -q 'Using lua config found at .*/hyprland.lua' <<<"$VERIFY"

log "Caelestia preset: time and disk"
python - <<'PY' | tee /tmp/caelestia-timings.txt
import re
rows = []
for line in open("/tmp/installer.ts.log", errors="replace"):
    stamp, _, text = line.rstrip("\n").partition("\t")
    if stamp.isdigit():
        rows.append((int(stamp), text))
def first(pred, start=0):
    return next((i for i in range(start, len(rows)) if pred(rows[i][1])), None)
def phase(name, a, b):
    i = first(a); j = first(b, i or 0) if i is not None else None
    print(f"  {name:<52} " + (f"{rows[j][0] - rows[i][0]:>5} s  (t={rows[i][0]}..{rows[j][0]})" if i is not None and j is not None else "(not seen)"))
phase("AUR step: yay-bin", lambda s: "yay-bin.git" in s and s.startswith("$ "), lambda s: "The AUR helper yay works" in s)
phase("AUR step: caelestia-cli", lambda s: s.startswith("$ ") and "caelestia-cli" in s and "yay -S" in s,
      lambda s: "caelestia-cli installed from the AUR" in s or "WARNING: caelestia-cli" in s)
phase("preset: 20-caelestia.sh (caelestia install)", lambda s: s.startswith("Running 20-caelestia.sh"), lambda s: s.startswith("Running 40-verify.sh"))
phase("preset: 40-verify.sh", lambda s: s.startswith("Running 40-verify.sh"), lambda s: "is installed (latest version)" in s or "tested versions" in s)
phase("preset: the whole step", lambda s: s.startswith("Running 20-caelestia.sh"), lambda s: "is installed (latest version)" in s or "tested versions" in s)
print(f"  {'installer total':<52} {rows[-1][0] - rows[0][0] if rows else 0:>5} s")
PY
echo "  disk (MiB): ~/.cache/yay=$(du -sm "$UH/.cache/yay" 2>/dev/null | cut -f1 || true)  quickshell=$(du -sm "$CHECK/usr/bin/quickshell" | cut -f1)  /etc/xdg/quickshell=$(du -sm "$CHECK/etc/xdg/quickshell" | cut -f1)  dots clone=$(du -sm "$UH/.local/state/caelestia" | cut -f1)  ~/.config.bak=$(du -sm "$UH/.config.bak" | cut -f1)" | tee -a /tmp/caelestia-timings.txt
echo "  disk used on the installed root file system: $(( $(df -B1 --output=used "$CHECK" | tail -n 1 | tr -d ' ') / 1048576 )) MiB" | tee -a /tmp/caelestia-timings.txt
echo "  build-time packages left installed: $(arch-chroot "$CHECK" pacman -Qq rust clang cmake ninja meson cargo 2>/dev/null | tr '\n' ' ')" | tee -a /tmp/caelestia-timings.txt
echo "  orphans (installed as dependencies, now required by nothing): $(arch-chroot "$CHECK" pacman -Qdtq 2>/dev/null | wc -l) packages" | tee -a /tmp/caelestia-timings.txt
[[ -n "${EVIDENCE_DIR:-}" ]] && cp /tmp/caelestia-timings.txt "$EVIDENCE_DIR/" 2>/dev/null

if flag RUN-IDEMPOTENCE; then
  log "Caelestia preset: a second 'caelestia install' (idempotence)"
  sudoers_on
  T0=$SECONDS
  as_user 'export XDG_RUNTIME_DIR=$(mktemp -d); chmod 700 $XDG_RUNTIME_DIR; caelestia install --noconfirm --aur-helper yay --disable-components firefox,qt' >/tmp/second-install.log 2>&1
  RC=$?
  sudoers_off
  echo "      second run: exit $RC in $((SECONDS - T0)) s"; sed 's/\x1b\[[0-9;]*m//g' /tmp/second-install.log | grep -aE 'backup|Back up|Fatal|Error|hook exited' | head -n 6 | sed 's/^/      /'
  check "the second run exits 0" test "$RC" -eq 0
  check "the second run kept the existing backup" grep -aq 'Not creating backup' /tmp/second-install.log
  check "the backup still holds the installer's hyprland.conf" grep -q 'exec-once = waybar' "$UH/.config.bak/hypr/hyprland.conf"
  check "applied_rev is still the commit of the clone" test "$(python -c "import json;print(json.load(open('$STATE'))['applied_rev'])")" = "$(as_user 'git -C ~/.local/state/caelestia/dots rev-parse HEAD' 2>/dev/null)"
  [[ -n "${EVIDENCE_DIR:-}" ]] && cp /tmp/second-install.log "$EVIDENCE_DIR/" 2>/dev/null
fi

if flag RUN-CLEAN; then
  log "Caelestia preset: what can be cleaned afterwards"
  SIZE_BEFORE="$(df -B1 --output=used "$CHECK" | tail -n 1 | tr -d ' ')"
  if [[ -d "$UH/.cache/yay" ]]; then
    YAY_MB="$(du -sm "$UH/.cache/yay" | cut -f1)"
    rm -rf "$UH/.cache/yay"
    echo "      ~/.cache/yay removed now: $YAY_MB MiB"
  else
    echo "      ~/.cache/yay is already gone: the installer removes it after the AUR builds"
  fi
  echo "      orphans before: $(arch-chroot "$CHECK" pacman -Qdtq | tr '\n' ' ')"
  python - "$CHECK" <<'PY'
import subprocess, sys
root = sys.argv[1]
names = subprocess.run(["arch-chroot", root, "pacman", "-Qdtq"], capture_output=True, text=True).stdout.split()
if names:
    out = subprocess.run(["arch-chroot", root, "pacman", "-Qi", *names], capture_output=True, text=True, env={"LC_ALL": "C", "PATH": "/usr/bin"}).stdout
    total = 0.0
    for line in out.splitlines():
        if line.startswith("Installed Size"):
            v, u = line.split(":")[1].split()
            total += float(v) * {"B": 1/1048576, "KiB": 1/1024, "MiB": 1, "GiB": 1024}[u]
    print(f"      orphans total installed size: {total:.0f} MiB ({len(names)} packages)")
PY
  for round in 1 2 3 4; do
    ORPH="$(arch-chroot "$CHECK" pacman -Qdtq 2>/dev/null | tr '\n' ' ' || true)"   # pacman exits 1 when there is none
    [[ -z "$ORPH" ]] && break
    # shellcheck disable=SC2086
    arch-chroot "$CHECK" pacman -Rns --noconfirm $ORPH >/tmp/orphan-removal-$round.log 2>&1 || { echo "      removal round $round failed"; tail -n 3 /tmp/orphan-removal-$round.log | sed 's/^/        /'; break; }
  done
  SIZE_AFTER="$(df -B1 --output=used "$CHECK" | tail -n 1 | tr -d ' ')"
  echo "      freed in total: $(( (SIZE_BEFORE - SIZE_AFTER) / 1048576 )) MiB"
  check "no orphan left" test -z "$(arch-chroot "$CHECK" pacman -Qdtq 2>/dev/null)"
  check "quickshell still has all its libraries" bash -c "! arch-chroot '$CHECK' ldd /usr/bin/quickshell | grep -q 'not found'"
  check "the caelestia command still runs" shows 'usage|caelestia' as_user 'caelestia --help 2>&1'
  check "Hyprland still validates the configuration" shows 'config ok' as_user 'export XDG_RUNTIME_DIR=$(mktemp -d); chmod 700 $XDG_RUNTIME_DIR; Hyprland --verify-config 2>&1'
  check "the shell still loads its QML (offscreen: fails only at the missing PanelWindow)" \
    shows 'PanelWindow backend|Configuration Loaded' as_user 'export XDG_RUNTIME_DIR=$(mktemp -d); chmod 700 $XDG_RUNTIME_DIR; QT_QPA_PLATFORM=offscreen timeout 20 dbus-run-session -- qs -c caelestia -n 2>&1 | head -n 40'
  echo "      build tools still installed: $(arch-chroot "$CHECK" pacman -Qq rust clang cmake ninja meson 2>/dev/null | tr '\n' ' ')"
fi

if flag RUN-FAULTS; then
  log "Caelestia preset: fault injection through the real engine"
  # shellcheck disable=SC2016
  PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/src/installer python /src/scripts/integration/dotfiles_caelestia_faults.py "$CHECK" "$USER_NAME" 2>&1 \
    | tee /tmp/caelestia-faults.log
  FAULT_STATUS=${PIPESTATUS[0]}
  check "the fault injection script finished (exit code 0 = every expectation held)" test "$FAULT_STATUS" -eq 0
  [[ -n "${EVIDENCE_DIR:-}" ]] && cp /tmp/caelestia-faults.log "$EVIDENCE_DIR/" 2>/dev/null
fi
