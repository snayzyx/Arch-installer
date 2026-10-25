# shellcheck shell=bash
# Checks of the "MacShell + MacOS-Like icons" desktop shell preset. SOURCED by test-install.sh (after the other
# checks) when DOTFILES=macshell, so it uses its helpers (check, log, shows) and its variables (CHECK = the installed
# system, mounted; LOOP; EVIDENCE_DIR; LAPTOP; FAILED). /tmp/installer.log is the installer output and
# /tmp/installer.ts.log the same with the elapsed seconds in front of every line.
#
# Part 1: the result of the normal installation (real pacstrap, real GitHub, real scripts in the real chroot).
# Part 2: the safeguards, by fault injection on the SAME mounted target (scripts/integration/dotfiles_faults.py:
#         a modified COPY of the preset, installer/ untouched).

PRESET_ID=macshell
U=alice
UH="$CHECK/home/$U"
LOGF=/tmp/installer.log
PRESET_SRC="/src/installer/dualboot_installer/presets/$PRESET_ID"
EV="${EVIDENCE_DIR:-/tmp/dotfiles-evidence}"
mkdir -p "$EV"
SUMMARY="$EV/SUMMARY.txt"
: >"$SUMMARY"
say() { printf '%s\n' "$*" | tee -a "$SUMMARY"; }
OWNER="$(awk -F: -v u="$U" '$1==u{print $3":"$4}' "$CHECK/etc/passwd")"
UID_NUM="${OWNER%%:*}"

# Hyprland --verify-config inside the chroot, as the user, on the given file. Prints the output.
verify_hypr() {
  rm -rf "$CHECK/tmp/xdg-test"; mkdir -p "$CHECK/tmp/xdg-test"
  chown "$OWNER" "$CHECK/tmp/xdg-test"; chmod 700 "$CHECK/tmp/xdg-test"
  arch-chroot "$CHECK" runuser -u "$U" -- env XDG_RUNTIME_DIR=/tmp/xdg-test HOME="/home/$U" \
    Hyprland --verify-config -c "/home/$U/.config/hypr/${1:-hyprland.lua}" 2>&1 | sed 's/\x1b\[[0-9;]*m//g' || true
}

command -v git >/dev/null 2>&1 || pacman -S --noconfirm --needed git >/dev/null 2>&1 || true  # test tool only: compares with the remote HEAD

log "MacShell preset: the installer output"
GOOD_MSG="The desktop shell MacShell + MacOS-Like icons is installed (latest version)."
check "the installer says the preset is installed (latest version)" grep -qF "$GOOD_MSG" "$LOGF"
check "no fallback to the tested versions was needed"              bash -c "! grep -q 'last tested versions' '$LOGF'"
check "no RECIPE-FAIL line"                                       bash -c "! grep -q 'RECIPE-FAIL' '$LOGF'"
check "no rollback ran"                                           bash -c "! grep -q 'rollback=done' '$LOGF'"
check "no WARNING about the desktop shell"                        bash -c "! grep -q 'WARNING: .*desktop shell' '$LOGF'"
MS_LINE="$(grep -m1 'RECIPE-RESULT preset=macshell' "$LOGF" || true)"
IC_LINE="$(grep -m1 'RECIPE-RESULT preset=icons' "$LOGF" || true)"
VF_LINE="$(grep -m1 'RECIPE-RESULT verify=' "$LOGF" || true)"
say "  $MS_LINE"; say "  $IC_LINE"; say "  $VF_LINE"
check "RECIPE-RESULT macshell: a full commit and a version"        bash -c '[[ "$1" =~ commit=[0-9a-f]{40}\ committed=.*installed_version=[0-9]+\.[0-9]+ ]]' _ "$MS_LINE"
check "RECIPE-RESULT icons: MacTahoe and MacOS-Like commits"       bash -c '[[ "$1" =~ mactahoe=[0-9a-f]{40}\ macos_like=[0-9a-f]{40} ]]' _ "$IC_LINE"
check "RECIPE-RESULT verify: config-ok"                            bash -c '[[ "$1" == *verify=config-ok* ]]' _ "$VF_LINE"
# "latest" means: the installed commit is the HEAD of the default branch at this very moment.
for pair in "0-ss/MacShell:commit=" "vinceliuice/MacTahoe-icon-theme:mactahoe=" "Scrimas/MacOS-Like:macos_like="; do
  repo="${pair%%:*}"; key="${pair#*:}"
  got="$(grep -o "${key}[0-9a-f]\{40\}" <<<"$MS_LINE $IC_LINE" | head -n 1 | cut -d= -f2 || true)"
  head="$(git ls-remote "https://github.com/$repo.git" HEAD 2>/dev/null | cut -f1 || true)"
  say "  $repo installed=${got:-?} HEAD-now=${head:-?}"
  check "$repo: the installed commit is the HEAD of the default branch" bash -c '[[ -n "$1" && "$1" == "$2" ]]' _ "$got" "$head"
done

log "MacShell preset: the Hyprland configuration"
HYPR_LUA="$UH/.config/hypr/hyprland.lua"
check "hyprland.lua exists"                            test -s "$HYPR_LUA"
check "hyprland.conf does NOT exist (Lua wins over it)" test ! -e "$UH/.config/hypr/hyprland.conf"
check "the keyboard layout placeholder was replaced"    bash -c "! grep -q '@KB_LAYOUT@' '$HYPR_LUA'"
check "kb_layout is the chosen layout (fr)"             grep -qE 'kb_layout[[:space:]]*=[[:space:]]*"fr"' "$HYPR_LUA"
check "hyprland.lua loads macshell"                     grep -qF 'require("macshell")' "$HYPR_LUA"
check "the Liquid Glass layer rules were added"         grep -qF 'namespace = "macdock"' "$HYPR_LUA"
check "waybar is NOT started (MacShell replaces it)"    bash -c "! grep -q 'waybar' '$HYPR_LUA' '$UH/.config/hypr/macshell.lua'"
check "nm-applet is NOT started"                        bash -c "! grep -q 'nm-applet' '$HYPR_LUA' '$UH/.config/hypr/macshell.lua'"
check "hyprland.lua keeps the polkit agent (pkexec)"    grep -q 'hyprpolkitagent' "$HYPR_LUA"
check "macshell.lua autostarts the shell"               grep -q 'macshell-start' "$UH/.config/hypr/macshell.lua"
check "the previous config was saved"                   test -s "$UH/.config/hypr/hyprland.lua.macshell-backup"
HYPR_OUT="$(verify_hypr hyprland.lua)"
echo "$HYPR_OUT" | tail -n 4 | sed 's/^/      hyprland: /'
check "Hyprland --verify-config accepts the final hyprland.lua (in the chroot)" grep -q 'config ok' <<<"$HYPR_OUT"

log "MacShell preset: files"
check "MacShell is installed (shell.qml)"               test -s "$UH/.config/quickshell/MacShell/shell.qml"
check "MacLock is installed (shell.qml)"                test -s "$UH/.config/quickshell/MacLock/shell.qml"
in_chroot() { arch-chroot "$CHECK" runuser -u "$U" -- env HOME="/home/$U" "$@"; }
check "macshell-start exists and resolves (in the chroot)" in_chroot test -x "/home/$U/.local/bin/macshell-start"
check "the helper scripts were installed"               bash -c "ls '$UH'/.config/hypr-macshell/scripts/macshell-*.sh >/dev/null"
check "the environment file exists"                     test -f "$UH/.config/macshell/env"
check "the UI sounds were generated (numpy present)"    bash -c "ls '$UH'/.local/share/sounds/macshell/*.wav >/dev/null && ! grep -q 'could not generate the UI sounds' '$LOGF'"
check "state.env has the installed version"             grep -qE '^installed_version=[0-9]' "$UH/.local/share/macshell/state.env"
check "the automatic update timer was NOT installed"    bash -c "! ls '$UH'/.config/systemd/user/macshell-update.* 2>/dev/null"
for t in MacTahoe MacOS-Like-Light MacOS-Like-Dark; do
  check "icon theme $t is installed"                    test -s "$UH/.local/share/icons/$t/index.theme"
done
check "MacOS-Like-Dark has app icons"                   bash -c "[[ \$(ls '$UH/.local/share/icons/MacOS-Like-Dark/apps/scalable' | wc -l) -gt 500 ]]"
check "GTK 3 uses MacOS-Like-Dark"                      grep -qx 'gtk-icon-theme-name=MacOS-Like-Dark' "$UH/.config/gtk-3.0/settings.ini"
check "GTK 4 uses MacOS-Like-Dark"                      grep -qx 'gtk-icon-theme-name=MacOS-Like-Dark' "$UH/.config/gtk-4.0/settings.ini"
check "MacShell uses MacOS-Like-Dark"                   grep -qx 'export QS_ICON_THEME=MacOS-Like-Dark' "$UH/.config/macshell/env"
check "the preset scripts were staged in the system"    bash -c "for f in 20-macshell.sh 30-icons.sh 40-verify.sh restore-default.sh packages-official.txt hyprland-base.lua; do test -s '$CHECK/usr/local/share/archinstall/presets/$PRESET_ID/'\$f || exit 1; done"

log "MacShell preset: system side"
MISSING_PKGS=""
for p in $(grep -v '^#' "$PRESET_SRC/packages-official.txt" | tr ' ' '\n' | grep -v '^$'); do
  arch-chroot "$CHECK" pacman -Q "$p" >/dev/null 2>&1 || MISSING_PKGS="$MISSING_PKGS $p"
done
check "every package of the preset is installed (pacstrap)" bash -c '[[ -z "$1" ]]' _ "$MISSING_PKGS"
say "  packages of the preset not installed:${MISSING_PKGS:- none}"
for p in quickshell awww hyprlock hypridle python-numpy qt6-multimedia-ffmpeg inter-font papirus-icon-theme; do
  check "package $p is installed"                        arch-chroot "$CHECK" pacman -Q "$p"
done
check "/etc/pam.d/hyprlock exists (MacLock checks the password with it)" test -s "$CHECK/etc/pam.d/hyprlock"
check "bluez is installed (the Bluetooth menu needs the daemon)"         arch-chroot "$CHECK" pacman -Q bluez
check "bluetooth.service is enabled"                     shows '^enabled$' arch-chroot "$CHECK" systemctl is-enabled bluetooth.service
check "power-profiles-daemon.service is enabled"         shows '^enabled$' arch-chroot "$CHECK" systemctl is-enabled power-profiles-daemon.service
SUDOERS_LEFT="$(ls "$CHECK/etc/sudoers.d")"
say "  /etc/sudoers.d: $(echo $SUDOERS_LEFT)"
check "the temporary NOPASSWD sudoers file is gone"      bash -c "[[ '$SUDOERS_LEFT' == '10-wheel' ]]"
check "no file in the user's home belongs to someone else" bash -c "[[ -z \$(find '$UH' ! -uid $UID_NUM -print -quit) ]]"
say "  /root contains: $(ls -A "$CHECK/root" | tr '
' ' ') (an empty .ssh comes with the openssh package, not with the preset)"
check "nothing from the preset in root's home (an empty .ssh is allowed)" bash -c "[[ -z \$(ls -A '$CHECK/root' | grep -v -E '^(\.bash|\.zsh|\.viminfo|\.lesshst|\.ssh)\$') && -z \$(ls -A '$CHECK/root/.ssh' 2>/dev/null) ]]"
check "the user's home is not world-writable"            bash -c "[[ \$(stat -c %a '$UH') =~ ^7[0-5][0-5]$ ]]"

log "MacShell preset: the clone cache"
CACHE="$UH/.cache/archinstall"
CACHE_MIB="$(du -sm "$CACHE" 2>/dev/null | cut -f1 || echo 0)"
say "  ~/.cache/archinstall: ${CACHE_MIB} MiB ($(find "$CACHE" -type f | wc -l) files)"
du -sm "$CACHE"/dotfiles/* 2>/dev/null | sed 's|'"$CHECK"'||; s/^/    /' | tee -a "$SUMMARY"
LINKS_IN="$(find "$UH" -xdev -lname '*/.cache/archinstall/*' 2>/dev/null | head -n 5)"
TEXT_REFS="$(grep -rIl '\.cache/archinstall' "$UH/.config" "$UH/.local/bin" "$UH/.local/share/macshell/bin" 2>/dev/null | head -n 5 || true)"
say "  symlinks pointing into the cache: ${LINKS_IN:-none}"
say "  text files that mention the cache: ${TEXT_REFS:-none}"
check "no symlink points into the clone cache"           bash -c '[[ -z "$1" ]]' _ "$LINKS_IN"
check "no installed file mentions the clone cache"       bash -c '[[ -z "$1" ]]' _ "$TEXT_REFS"
# Delete it and prove nothing depends on it.
rm -rf "$CACHE"
BROKEN="$(in_chroot find "/home/$U" -xdev -xtype l 2>/dev/null | head -n 5 || true)"
check "deleting the cache leaves no dangling symlink"    bash -c '[[ -z "$1" ]]' _ "$BROKEN"
check "after deleting the cache: macshell-start still resolves" in_chroot test -x "/home/$U/.local/bin/macshell-start"
check "after deleting the cache: Hyprland still accepts the config" grep -q 'config ok' <<<"$(verify_hypr hyprland.lua)"
check "after deleting the cache: MacShell's own tool still resolves" in_chroot test -x "/home/$U/.local/bin/macshell-install"

log "MacShell preset: time and space"
python3 - <<'PY' | tee -a "$SUMMARY"
import re
rows = []
for line in open("/tmp/installer.ts.log", errors="replace"):
    sec, _, text = line.rstrip("\n").partition("\t")
    if sec.isdigit():
        rows.append((int(sec), re.sub(r"\x1b\[[0-9;]*m", "", text)))
def first(pred, start=0):
    for i in range(start, len(rows)):
        if pred(rows[i][1]):
            return i
    return None
def phase(name, a, b):
    i = first(a)
    j = first(b, i or 0) if i is not None else None
    print(f"  {name:<46} " + ("(not seen)" if j is None else f"{rows[j][0] - rows[i][0]:>5} s"))
phase("pacstrap (whole system incl. the preset's packages)", lambda s: s.startswith("$ pacstrap -K /mnt"), lambda s: s.startswith("> write /mnt/etc/fstab"))
phase("20-macshell.sh (clone + install.sh)", lambda s: s.startswith("Running 20-macshell.sh"), lambda s: s.startswith("Running 30-icons.sh"))
phase("30-icons.sh (clones + MacTahoe + MacOS-Like)", lambda s: s.startswith("Running 30-icons.sh"), lambda s: s.startswith("Running 40-verify.sh"))
phase("40-verify.sh", lambda s: s.startswith("Running 40-verify.sh"), lambda s: "is installed (latest version)" in s)
phase("the whole preset step (scripts)", lambda s: s.startswith("Running 20-macshell.sh"), lambda s: "is installed (latest version)" in s)
print(f"  {'the installer from start to end':<46} {rows[-1][0] - rows[0][0]:>5} s" if rows else "  (no timestamps)")
PY
PKG_COUNT="$(wc -l </tmp/extras-pacstrap-packages.txt 2>/dev/null || echo 0)"
PKG_DL="$(awk -F'\t' '{s+=$4} END {printf "%.0f", s}' /tmp/extras-pacstrap-packages.txt 2>/dev/null || echo 0)"
PKG_INST="$(awk -F'\t' '{s+=$3} END {printf "%.0f", s}' /tmp/extras-pacstrap-packages.txt 2>/dev/null || echo 0)"
say "  pacstrap transaction: ${PKG_COUNT} packages, download ${PKG_DL} MiB, installed ${PKG_INST} MiB"
say "  root file system used: $(( $(df -B1 --output=used "$CHECK" | tail -n 1 | tr -d ' ') / 1048576 )) MiB"
for d in .config .local/share/icons .local/share/macshell .local/share/sounds .cache; do
  say "  ~/$d: $(du -sm "$UH/$d" 2>/dev/null | cut -f1 || echo 0) MiB"
done
say "  (the pacman cache volume was warm: the download TIMES understate a cold run; the sizes do not)"

cp "$LOGF" "$EV/installer.log" 2>/dev/null || true
cp /tmp/installer.ts.log "$EV/installer.ts.log" 2>/dev/null || true
cp "$UH/.config/hypr/hyprland.lua" "$EV/hyprland.lua.after-install" 2>/dev/null || true
cp "$UH/.config/hypr/macshell.lua" "$EV/macshell.lua" 2>/dev/null || true
echo "$HYPR_OUT" >"$EV/hyprland-verify-config.txt"

# ---------------------------------------------------------------------------------------------------------------
log "MacShell preset: FAULT INJECTION on the installed target (real chroot, real network, copy of the preset)"
FAULTS="python3 /src/scripts/integration/dotfiles_faults.py"
export PRESET="$PRESET_ID" TARGET="$CHECK" INSTALLER_PATH=/src/installer PYTHONDONTWRITEBYTECODE=1

# 1. the latest version fails (exit 21), the tested commits work.
log "Fault 1: the latest version fails, the retry with the tested versions works"
$FAULTS retry >"$EV/fault1-retry.log" 2>&1 || true
sed 's/\x1b\[[0-9;]*m//g' "$EV/fault1-retry.log" | grep -E "Running|WARNING|RECIPE|installed|FAULT-RESULT" | cut -c1-200 | sed 's/^/      /' | tail -n 20
F1="$EV/fault1-retry.log"
check "fault 1: the step did not raise"                          grep -q 'FAULT-RESULT case=retry exception=none' "$F1"
check "fault 1: the first script failed with exit code 21"       grep -q 'WARNING: 20-macshell.sh failed (exit code 21' "$F1"
check "fault 1: the rollback script ran"                         grep -q 'RECIPE-RESULT rollback=done' "$F1"
check "fault 1: the log says it retries with the tested versions" grep -q 'trying the versions that were tested' "$F1"
check "fault 1: the retry installed the KNOWN-GOOD commit"       grep -q 'RECIPE-RESULT preset=macshell commit=1bdb51870b621100fd2709f66c8f8e00ab17b9c4' "$F1"
check "fault 1: the log says 'the last tested versions, not the latest'" grep -q 'the last tested versions, not the latest' "$F1"
check "fault 1: MacShell is installed after the retry"           test -s "$UH/.config/quickshell/MacShell/shell.qml"
check "fault 1: hyprland.lua loads macshell once"                bash -c "[[ \$(grep -c 'require(\"macshell\")' '$HYPR_LUA') == 1 ]]"
check "fault 1: Hyprland accepts the config after the retry"     grep -q 'config ok' <<<"$(verify_hypr hyprland.lua)"
check "fault 1: the temporary sudoers file is gone"              bash -c "[[ \$(ls '$CHECK/etc/sudoers.d') == '10-wheel' ]]"
cp "$HYPR_LUA" "$EV/hyprland.lua.after-fault1"
# What the rollback adds to the default setup (waybar + nm-applet) must not survive a SUCCESSFUL retry:
# MacShell has its own bar and its own network menu, and waybar would be drawn over the menu bar.
check "fault 1: no waybar left in the config after a successful retry" bash -c "! grep -q 'waybar' '$HYPR_LUA'"
check "fault 1: no nm-applet left in the config after a successful retry" bash -c "! grep -q 'nm-applet' '$HYPR_LUA'"

# 2. both attempts fail: rollback twice, the default setup is back, the step still returns normally.
log "Fault 2: both attempts fail: rollback, default setup back, the step never fails"
$FAULTS bothfail >"$EV/fault2-bothfail.log" 2>&1 || true
sed 's/\x1b\[[0-9;]*m//g' "$EV/fault2-bothfail.log" | grep -E "Running|WARNING|RECIPE|installed|FAULT-RESULT" | cut -c1-200 | sed 's/^/      /' | tail -n 20
F2="$EV/fault2-bothfail.log"
check "fault 2: the step did not raise (exit code 0 of the step)" grep -q 'FAULT-RESULT case=bothfail exception=none' "$F2"
check "fault 2: both attempts reported a failure"                bash -c "[[ \$(grep -c 'WARNING: 20-macshell.sh failed' '$F2') == 2 ]]"
check "fault 2: the rollback ran twice"                          bash -c "[[ \$(grep -c 'RECIPE-RESULT rollback=done' '$F2') == 2 ]]"
check "fault 2: the log says the default setup is back"          grep -q 'could not be installed. The default Hyprland setup is back' "$F2"
check "fault 2: the log does not claim a success"                bash -c "! grep -q 'is installed (' '$F2'"
check "fault 2: MacShell files are gone"                         bash -c "[[ ! -e '$UH/.config/quickshell/MacShell' && ! -e '$UH/.config/quickshell/MacLock' && ! -e '$UH/.local/bin/macshell-start' && ! -e '$UH/.config/hypr/macshell.lua' ]]"
check "fault 2: hyprland.lua is the default setup again: waybar" grep -q 'hl.exec_cmd("waybar")' "$HYPR_LUA"
check "fault 2: hyprland.lua is the default setup again: nm-applet" grep -q 'nm-applet --indicator' "$HYPR_LUA"
check "fault 2: hyprland.lua no longer loads macshell"           bash -c "! grep -q 'macshell' '$HYPR_LUA'"
check "fault 2: kb_layout is still the chosen one"               grep -qE 'kb_layout[[:space:]]*=[[:space:]]*"fr"' "$HYPR_LUA"
check "fault 2: Hyprland accepts the restored config"            grep -q 'config ok' <<<"$(verify_hypr hyprland.lua)"
check "fault 2: the temporary sudoers file is gone"              bash -c "[[ \$(ls '$CHECK/etc/sudoers.d') == '10-wheel' ]]"
check "fault 2: no file of the user's home belongs to someone else" bash -c "[[ -z \$(find '$UH' ! -uid $UID_NUM -print -quit) ]]"
cp "$HYPR_LUA" "$EV/hyprland.lua.after-fault2"

# 3. the pre-flight: an unreachable source stops everything BEFORE the disk would be erased.
log "Fault 3: the pre-flight with github.com unreachable"
cp /etc/hosts /tmp/hosts.saved
echo "127.0.0.1 github.com" >>/etc/hosts
$FAULTS sources >"$EV/fault3-sources.log" 2>&1 || true
cat /tmp/hosts.saved >/etc/hosts
sed 's/\x1b\[[0-9;]*m//g' "$EV/fault3-sources.log" | tail -n 3 | cut -c1-400 | sed 's/^/      /'
F3="$EV/fault3-sources.log"
check "fault 3: check_sources raises a StepError"                bash -c "grep -q 'FAULT-RESULT case=sources StepError=' '$F3' && ! grep -q 'StepError=none' '$F3'"
check "fault 3: the message ends with 'Nothing was changed.'"    grep -q 'Nothing was changed\.' "$F3"
check "fault 3: it names the unreachable repositories"           grep -q 'do not answer: https://github.com/0-ss/MacShell' "$F3"
$FAULTS sources-ok >"$EV/fault3-sources-ok.log" 2>&1 || true
check "fault 3 (control): with the network back, check_sources passes" grep -q 'StepError=none' "$EV/fault3-sources-ok.log"

# 4. the retry leaves the autostart of the default setup in place: a proposed patch, proven on a COPY of the preset.
log "Proposed patch: the rollback must not leave waybar/nm-applet behind a successful retry"
PATCH_DIR=/tmp/preset-patch; rm -rf "$PATCH_DIR"; mkdir -p "$PATCH_DIR"
# a clean start: the base config of the preset, nothing else (fault 2 left the unmarked default autostart behind)
sed "s/@KB_LAYOUT@/fr/" "$PRESET_SRC/hyprland-base.lua" >"$HYPR_LUA"; chown "$OWNER" "$HYPR_LUA"
rm -f "$UH/.config/hypr/hyprland.lua.macshell-backup" "$UH/.config/hypr/macshell.lua"
cp "/src/scripts/integration/dotfiles-macshell-patch/"* "$PATCH_DIR/" 2>/dev/null || true
if ls "$PATCH_DIR"/*.sh >/dev/null 2>&1; then
  PRESET_PATCH_DIR="$PATCH_DIR" $FAULTS retry >"$EV/fault4-patched-retry.log" 2>&1 || true
  sed 's/\x1b\[[0-9;]*m//g' "$EV/fault4-patched-retry.log" | grep -E "patched file|RECIPE|installed|FAULT-RESULT" | cut -c1-200 | sed 's/^/      /' | tail -n 12
  check "patch: the retry with the patched scripts succeeds"       grep -q 'the last tested versions, not the latest' "$EV/fault4-patched-retry.log"
  check "patch: no waybar left after the successful retry"         bash -c "! grep -q 'waybar' '$HYPR_LUA'"
  check "patch: no nm-applet left after the successful retry"      bash -c "! grep -q 'nm-applet' '$HYPR_LUA'"
  check "patch: hyprland.lua loads macshell once"                  bash -c "[[ \$(grep -c 'require(\"macshell\")' '$HYPR_LUA') == 1 ]]"
  check "patch: Hyprland accepts the patched result"               grep -q 'config ok' <<<"$(verify_hypr hyprland.lua)"
  cp "$HYPR_LUA" "$EV/hyprland.lua.after-patched-retry"
fi
say ""
say "checks failed so far: $FAILED (0 = none)"
cp /tmp/installer.log "$EV/installer.log" 2>/dev/null || true
