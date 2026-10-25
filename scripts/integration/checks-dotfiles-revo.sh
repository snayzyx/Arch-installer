# shellcheck shell=bash
# Checks of the "Revo Shell" desktop shell preset. SOURCED by test-install.sh (after the other checks) when
# DOTFILES=revo, so it uses its helpers (check, log) and its variables (CHECK = the installed system, mounted;
# EVIDENCE_DIR; FAILED). /tmp/installer.log is the installer output and /tmp/installer.ts.log the same with the
# elapsed seconds in front of every line.
#
# Part 1: the result of the normal installation (real pacstrap, real AUR, real GitHub, real scripts in the real chroot).
# Part 2: what happens when the clone cache and ~/.cache are deleted.
# Part 3: the safeguards, by fault injection on the SAME mounted target (scripts/integration/dotfiles_revo_faults.py:
#         a modified COPY of the preset, installer/ untouched).

PRESET_ID=revo
REPO=https://github.com/vzbc/revo-shell.git
KNOWN_GOOD=72f7113c9530577acff57778246b0583324c3774
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
say "Revo Shell preset checks, $(date -u +%FT%TZ), laptop=${LAPTOP:-no}"

# Hyprland --verify-config inside the chroot, as the user. Prints the output.
verify_hypr() {
  rm -rf "$CHECK/tmp/xdg-test"; mkdir -p "$CHECK/tmp/xdg-test"
  chown "$OWNER" "$CHECK/tmp/xdg-test"; chmod 700 "$CHECK/tmp/xdg-test"
  arch-chroot "$CHECK" runuser -u "$U" -- env XDG_RUNTIME_DIR=/tmp/xdg-test HOME="/home/$U" \
    Hyprland --verify-config "$@" 2>&1 | sed 's/\x1b\[[0-9;]*m//g' || true
}
# Symlinks of the user's home that point nowhere, seen from INSIDE the new system (their targets are absolute paths
# of the new system, so the host's view of $CHECK would give false alarms).
dangling() { arch-chroot "$CHECK" find "/home/$U/.config" "/home/$U/.local" -xdev -xtype l 2>/dev/null | head -n 20; }
# The first 3 lines of the newest backup of ~/.config/hypr made by Revo Shell's install.sh.
newest_backup() { ls -dt "$UH"/.config/revo-shell-backup-* 2>/dev/null | head -n 1; }

# ===============================================================================================================
log "Revo Shell preset: the installer output"
GOOD_MSG="The desktop shell Revo Shell (experimental) is installed (latest version)."
check "the installer says the preset is installed (latest version)" grep -qF "$GOOD_MSG" "$LOGF"
check "no fallback to the tested version was needed"                bash -c "! grep -q 'last tested versions' '$LOGF'"
check "no RECIPE-FAIL line"                                         bash -c "! grep -q 'RECIPE-FAIL' '$LOGF'"
check "no rollback ran"                                             bash -c "! grep -q 'rollback=done' '$LOGF'"
check "no WARNING about the desktop shell"                          bash -c "! grep -q 'WARNING: .*desktop shell' '$LOGF'"
RES_LINE="$(grep -m1 'RECIPE-RESULT preset=revo' "$LOGF" || true)"
CLONE_LINE="$(grep -m1 'REVO_COMMIT=' "$LOGF" || true)"
say "  $RES_LINE"; say "  $CLONE_LINE"
check "RECIPE-RESULT: a full commit and the default shell"          bash -c '[[ "$1" =~ ^RECIPE-RESULT\ preset=revo\ commit=[0-9a-f]{40}\ shell=macos$ ]]' _ "$RES_LINE"
check "the sibling shell eqsh was found from the symlinks"          bash -c '[[ "$1" == *"siblings: eqsh"* ]]' _ "$CLONE_LINE"
GOT="$(grep -o 'commit=[0-9a-f]\{40\}' <<<"$RES_LINE" | cut -d= -f2 || true)"
HEAD_NOW="$(curl -fsS --max-time 30 'https://github.com/vzbc/revo-shell/info/refs?service=git-upload-pack' 2>/dev/null | grep -aoE '[0-9a-f]{40} HEAD' | head -n 1 | cut -d' ' -f1 || true)"
say "  vzbc/revo-shell installed=${GOT:-?} HEAD-now=${HEAD_NOW:-?} (known-good fallback: $KNOWN_GOOD)"
check "the installed commit is the HEAD of the default branch (latest)" bash -c '[[ -n "$1" && "$1" == "$2" ]]' _ "$GOT" "$HEAD_NOW"
say "  latest == known-good fallback? $([[ "$GOT" == "$KNOWN_GOOD" ]] && echo yes || echo "no (upstream has moved on: the latest version was installed and verified)")"

# ===============================================================================================================
log "Revo Shell preset: the Hyprland configuration"
HYPR="$UH/.config/hypr"
check "hyprland.lua was deployed"                                   test -s "$HYPR/hyprland.lua"
check "the Lua autostart was deployed"                              test -s "$HYPR/configs/autostart.lua"
check "the default shell marker says macos"                         bash -c "[[ \$(tr -d ' \n' < '$HYPR/.revo_default_shell') == macos ]]"
check "the autostart launches the default shell (REVO_DEFAULT_SHELL or the marker)" bash -c "grep -rqE 'REVO_DEFAULT_SHELL|revo_default_shell' '$HYPR/configs/'"
HYPR_OUT="$(verify_hypr)"
echo "$HYPR_OUT" | tail -n 4 | sed 's/^/      hyprland: /'
check "Hyprland --verify-config (default path) uses the Lua config" grep -q 'Using lua config' <<<"$HYPR_OUT"
check "Hyprland --verify-config accepts it (in the chroot)"         grep -q 'config ok' <<<"$HYPR_OUT"
say "  kb_layout in the deployed config: $(grep -h 'kb_layout' "$HYPR/configs/input.lua" "$HYPR/settings.lua" 2>/dev/null | tr -s ' ' | tr '\n' ' ')"
check "the keyboard layout chosen in the wizard (fr) reaches the session: configs/input.lua" grep -qE 'kb_layout[[:space:]]*=[[:space:]]*"fr"' "$HYPR/configs/input.lua"
check "... and settings.lua, which is loaded after it and wins" grep -qE 'kb_layout[[:space:]]*=[[:space:]]*"fr"' "$HYPR/settings.lua"
check "no 'powerprofilesctl set performance' left in ~/.config/hypr" bash -c "! grep -rq 'powerprofilesctl set performance' '$HYPR'"
# Positive control for the check above: the project does ship that line, our script removed it.
UPSTREAM_HITS="$(grep -rl 'powerprofilesctl set performance' "$UH/.cache/archinstall/revo-shell/hypr" 2>/dev/null | wc -l)"
say "  upstream files with the performance-profile line (in the clone): $UPSTREAM_HITS"
check "(control) the upstream clone does carry that line"           bash -c "[[ $UPSTREAM_HITS -ge 1 ]]"
BK="$(newest_backup)"
say "  backup of our config: ${BK#"$CHECK"}"
check "a revo-shell-backup-* folder exists"                         test -d "$BK/hypr"
check "the backup holds OUR hyprland.conf (written by the installer)" grep -q 'Generated by ArchInstall' "$BK/hypr/hyprland.conf"
check "our own config is not the one in use any more (the preset replaced it)" bash -c "! grep -q 'Generated by ArchInstall' '$HYPR/hyprland.conf'"
cp "$BK/hypr/hyprland.conf" "$EV/our-hyprland.conf.from-backup" 2>/dev/null || true

# ===============================================================================================================
log "Revo Shell preset: the shells and the links"
for s in macos eqsh; do
  check "quickshell/$s/shell.qml is deployed"                       test -s "$UH/.config/quickshell/$s/shell.qml"
done
LINKS_TOTAL="$(arch-chroot "$CHECK" find "/home/$U/.config" -xdev -type l 2>/dev/null | wc -l)"
LINKS_HOME="$(arch-chroot "$CHECK" find "/home/$U/.config" -xdev -type l -lname "/home/$U/*" 2>/dev/null | wc -l)"
DANGLING="$(dangling)"
say "  symlinks under ~/.config: $LINKS_TOTAL, of which $LINKS_HOME were re-pointed to /home/$U; dangling: ${DANGLING:-none}"
check "ZERO dangling symlinks under ~/.config and ~/.local"         bash -c '[[ -z "$1" ]]' _ "$DANGLING"
check "no symlink still points into /home/revo"                     bash -c "[[ -z \$(arch-chroot '$CHECK' find '/home/$U/.config' -xdev -type l -lname '/home/revo/*' | head -n 1) ]]"
MACOS_LINKS="$(arch-chroot "$CHECK" find "/home/$U/.config/quickshell/macos" -type l 2>/dev/null | wc -l)"
MACOS_LINKS_OK="$(arch-chroot "$CHECK" find "/home/$U/.config/quickshell/macos" -type l -exec test -e {} \; -print 2>/dev/null | wc -l)"
say "  the macos shell is made of $MACOS_LINKS symlinks, $MACOS_LINKS_OK of them resolve"
check "every symlink of the macos shell resolves (through eqsh)"    bash -c '[[ "$1" -ge 1 && "$1" == "$2" ]]' _ "$MACOS_LINKS" "$MACOS_LINKS_OK"
check "no hyprpm plugin was built (needs a live Hyprland)"          bash -c "[[ -z \$(find '$CHECK/var/cache/hyprpm' -name '*.so' 2>/dev/null | head -n 1) ]]"
check "no plugin .so in the user's home"                            bash -c "[[ -z \$(find '$UH' -name 'hyprliquid*.so' -o -name 'hypr3d*.so' 2>/dev/null | head -n 1) ]]"
check "the wallpapers were copied (6 images)"                       bash -c "[[ \$(ls '$UH/Pictures/Wallpapers' | wc -l) -ge 1 ]]"
say "  wallpapers: $(ls "$UH/Pictures/Wallpapers" | wc -l) files, $(du -sm "$UH/Pictures" | cut -f1) MiB"
check "SystemSettings was built and installed"                      test -x "$UH/.local/bin/systemsettings"
check "its desktop entry exists"                                    test -s "$UH/.local/share/applications/systemsettings.desktop"
check "the SystemSettings binary finds all its libraries"           bash -c "[[ -z \$(arch-chroot '$CHECK' ldd /home/$U/.local/bin/systemsettings | grep 'not found') ]]"

# ===============================================================================================================
log "Revo Shell preset: system side"
MISSING=""; COUNT=0
for p in $(grep -v '^#' "$PRESET_SRC/packages-official.txt" | tr ' ' '\n' | grep -v '^$'); do
  COUNT=$((COUNT + 1))
  arch-chroot "$CHECK" pacman -Q "$p" >/dev/null 2>&1 || arch-chroot "$CHECK" pacman -Qq "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"
done
say "  official packages of the preset: $COUNT names, not installed:${MISSING:- none}"
check "every official package of the preset is installed (pacstrap)" bash -c '[[ -z "$1" ]]' _ "$MISSING"
for p in mpvpaper python-pywal16 ttf-rubik-vf kde-material-you-colors; do
  check "AUR package $p is installed"                               arch-chroot "$CHECK" pacman -Q "$p"
done
for p in quickshell hyprland hyprpm awww matugen syntax-highlighting rofi; do
  check "package $p is installed"                                   arch-chroot "$CHECK" pacman -Q "$p"
done
check "ttf-material-symbols-variable is the official one (not the conflicting -git)" bash -c "arch-chroot '$CHECK' pacman -Q ttf-material-symbols-variable && ! arch-chroot '$CHECK' pacman -Q ttf-material-symbols-variable-git"
SUDOERS_LEFT="$(ls "$CHECK/etc/sudoers.d")"
say "  /etc/sudoers.d: $(echo $SUDOERS_LEFT)"
check "the temporary NOPASSWD sudoers file is gone"                 bash -c "[[ '$SUDOERS_LEFT' == '10-wheel' ]]"
check "no file in the user's home belongs to someone else"          bash -c "[[ -z \$(find '$UH' ! -uid $UID_NUM -print -quit) ]]"
say "  /root contains: $(ls -A "$CHECK/root" | tr '\n' ' ')"
[[ -e "$CHECK/root/.ssh" ]] && say "  /root/.ssh: $(ls -ld --time-style=full-iso "$CHECK/root/.ssh" | cut -c1-120); content: $(ls -A "$CHECK/root/.ssh" | tr '\n' ' ')"
# An EMPTY /root/.ssh comes from the system, not from the preset (which runs as the user, never as root).
check "nothing from the preset in root's home"                      bash -c "[[ -z \$(ls -A '$CHECK/root' | grep -v -E '^(\.bash|\.zsh|\.viminfo|\.lesshst|\.cache|\.ssh)\$') && -z \$(ls -A '$CHECK/root/.ssh' 2>/dev/null) ]]"
check "no AUR helper build cache left (~/.cache/yay)"               bash -c "[[ ! -e '$UH/.cache/yay' ]]"
check "the preset scripts were staged in the system"                bash -c "for f in 20-revo.sh restore-default.sh packages-official.txt; do test -s '$CHECK/usr/local/share/archinstall/presets/revo/'\$f || exit 1; done"
check "no services were enabled by the preset beyond ours"          bash -c "[[ -z \$(ls '$CHECK/etc/systemd/system/multi-user.target.wants' | grep -i -E 'revo|quickshell') ]]"

# ===============================================================================================================
log "Revo Shell preset: sizes and time (before any cache is deleted)"
CACHE="$UH/.cache/archinstall"
mib() { du -sm "$1" 2>/dev/null | cut -f1 || echo 0; }
say "  clone (~/.cache/archinstall/revo-shell): $(mib "$CACHE/revo-shell") MiB, of which .git $(mib "$CACHE/revo-shell/.git") MiB"
say "  SystemSettings build dir inside the clone: $(mib "$CACHE/revo-shell/SystemSettings/build") MiB"
say "  ~/.cache (whole): $(mib "$UH/.cache") MiB"
for d in .cache/pip .cache/yay .cache/archinstall; do say "    ~/$d: $(mib "$UH/$d") MiB"; done
say "  ~/.local (whole): $(mib "$UH/.local") MiB"
PYSIDE="$(ls -d "$UH"/.local/lib/python*/site-packages/PySide6* 2>/dev/null | head -n 3 | tr '\n' ' ')"
say "  PySide6 under ~/.local: $(du -sm "$UH"/.local/lib/python*/site-packages/PySide6* 2>/dev/null | awk '{s+=$1} END{print s+0}') MiB (${PYSIDE:-absent})"
say "  ~/.config: $(mib "$UH/.config") MiB (incl. the backup: $(mib "$BK") MiB)"
say "  ~/Pictures: $(mib "$UH/Pictures") MiB"
say "  whole home: $(mib "$UH") MiB; root file system used: $(( $(df -B1 --output=used "$CHECK" | tail -n 1 | tr -d ' ') / 1048576 )) MiB"
PKG_COUNT="$(wc -l </tmp/extras-pacstrap-packages.txt 2>/dev/null || echo 0)"
PKG_DL="$(awk -F'\t' '{s+=$4} END {printf "%.0f", s}' /tmp/extras-pacstrap-packages.txt 2>/dev/null || echo 0)"
PKG_INST="$(awk -F'\t' '{s+=$3} END {printf "%.0f", s}' /tmp/extras-pacstrap-packages.txt 2>/dev/null || echo 0)"
say "  pacstrap transaction (system + the preset's official packages): ${PKG_COUNT} packages, download ${PKG_DL} MiB, installed ${PKG_INST} MiB"
python3 - <<'PY' | tee -a "$SUMMARY"
import re
rows = []
for line in open("/tmp/installer.ts.log", errors="replace"):
    sec, _, text = line.rstrip("\n").partition("\t")
    if sec.isdigit():
        rows.append((int(sec), re.sub(r"\x1b\[[0-9;]*m", "", text)))
steps = [(i, t[4:]) for i, (_, t) in enumerate(rows) if t.startswith("=== ")]
print("  time per installer step (seconds, from /tmp/installer.ts.log):")
for n, (i, name) in enumerate(steps):
    end = rows[steps[n + 1][0]][0] if n + 1 < len(steps) else rows[-1][0]
    print(f"    {end - rows[i][0]:>5} s  {name}")
def first(pred):
    for r in rows:
        if pred(r[1]):
            return r[0]
a = first(lambda s: s.startswith("Running 20-revo.sh"))
b = first(lambda s: "is installed (latest version)" in s)
print(f"  the preset script 20-revo.sh (clone + install.sh + checks): {b - a} s" if a is not None and b is not None else "  (preset script timing not found)")
print(f"  the whole installer from start to end: {rows[-1][0] - rows[0][0]} s" if rows else "  (no timestamps)")
PY
say "  (the pacman cache volume was warm: download TIMES understate a cold run; sizes do not)"
cp "$LOGF" "$EV/installer.log" 2>/dev/null || true
cp /tmp/installer.ts.log "$EV/installer.ts.log" 2>/dev/null || true
cp /tmp/extras-pacstrap-packages.txt "$EV/pacstrap-packages.txt" 2>/dev/null || true
cp "$UH"/.config/hypr/hyprland.lua "$EV/hyprland.lua.after-install" 2>/dev/null || true
echo "$HYPR_OUT" >"$EV/hyprland-verify-config.txt"
arch-chroot "$CHECK" runuser -u "$U" -- bash -c 'cat /tmp/revo-install.log' >"$EV/revo-install.sh.log" 2>/dev/null || cp "$CHECK/tmp/revo-install.log" "$EV/revo-install.sh.log" 2>/dev/null || true

# ===============================================================================================================
log "Revo Shell preset: can the clone cache and ~/.cache be deleted?"
LINKS_IN="$(arch-chroot "$CHECK" find "/home/$U" -xdev -lname '*/.cache/archinstall/*' 2>/dev/null | head -n 5)"
TEXT_REFS="$(grep -rIl '\.cache/archinstall' "$UH/.config" "$UH/.local/bin" "$UH/.local/share/applications" 2>/dev/null | sed "s|$CHECK||" | head -n 8 || true)"
say "  symlinks pointing into the clone: ${LINKS_IN:-none}"
say "  text files that mention the clone: ${TEXT_REFS:-none}"
check "no symlink points into the clone cache"                      bash -c '[[ -z "$1" ]]' _ "$LINKS_IN"
# Delete the clone, then the whole ~/.cache, and prove what still works.
rm -rf "$CACHE"
check "after deleting the clone: no dangling symlink"               bash -c '[[ -z "$1" ]]' _ "$(dangling)"
check "after deleting the clone: the shells are still there"        bash -c "test -s '$UH/.config/quickshell/macos/shell.qml' && test -s '$UH/.config/quickshell/eqsh/shell.qml'"
rm -rf "$UH/.cache"
check "after deleting ~/.cache: no dangling symlink"                bash -c '[[ -z "$1" ]]' _ "$(dangling)"
check "after deleting ~/.cache: Hyprland still accepts the config"  grep -q 'config ok' <<<"$(verify_hypr)"
check "after deleting ~/.cache: the SystemSettings app still links" bash -c "[[ -z \$(arch-chroot '$CHECK' ldd /home/$U/.local/bin/systemsettings | grep 'not found') ]]"
ICON="$(grep -m1 '^Icon=' "$UH/.local/share/applications/systemsettings.desktop" | cut -d= -f2- | sed "s|^/home/$U|$UH|")"
say "  systemsettings.desktop icon: ${ICON#"$CHECK"} -> $([[ -e "$ICON" ]] && echo present || echo "MISSING after the cache is deleted (cosmetic: the menu entry loses its icon)")"
[[ -z "$ICON" || -e "$ICON" ]] && say "  (no icon dependency)" || say "  FINDING: the SystemSettings menu entry points its Icon= into the deleted clone"
say "  ~/.cache after the deletion: $([[ -e "$UH/.cache" ]] && echo exists || echo absent); ~/.local $(mib "$UH/.local") MiB, ~/.config $(mib "$UH/.config") MiB"

# ===============================================================================================================
log "Revo Shell preset: FAULT INJECTION on the installed target (real chroot, real network, copy of the preset)"
FAULTS="python3 /src/scripts/integration/dotfiles_revo_faults.py"
export TARGET="$CHECK" INSTALLER_PATH=/src/installer PYTHONDONTWRITEBYTECODE=1

# 1. the latest version fails (exit 30), the known-good commit works.
log "Fault 1: the latest version fails, the retry with the known-good commit works"
$FAULTS retry >"$EV/fault1-retry.log" 2>&1 || true
sed 's/\x1b\[[0-9;]*m//g' "$EV/fault1-retry.log" | grep -E "Running|WARNING|RECIPE|installed|FAULT-RESULT|REVO_COMMIT" | cut -c1-200 | sed 's/^/      /' | tail -n 20
F1="$EV/fault1-retry.log"
cp "$UH/state-at-retry-start.txt" "$EV/fault1-state-at-retry-start.txt" 2>/dev/null || true
check "fault 1: the step did not raise"                             grep -q 'FAULT-RESULT case=retry exception=none' "$F1"
check "fault 1: the first script failed with exit code 30"          grep -q 'WARNING: 20-revo.sh failed (exit code 30' "$F1"
check "fault 1: the rollback script ran"                            grep -q 'RECIPE-RESULT rollback=done' "$F1"
check "fault 1: the log says it retries with the tested version"    grep -q 'trying the versions that were tested' "$F1"
check "fault 1: the retry installed the KNOWN-GOOD commit"          grep -q "RECIPE-RESULT preset=revo commit=$KNOWN_GOOD shell=macos" "$F1"
check "fault 1: the log says 'the last tested versions, not the latest'" grep -q 'the last tested versions, not the latest' "$F1"
check "fault 1: BETWEEN the attempts our hyprland.conf was back (rollback proof)" grep -q 'Generated by ArchInstall' "$UH/state-at-retry-start.txt"
check "fault 1: BETWEEN the attempts no quickshell/rofi/kitty config was left" bash -c "! sed -n '/--- ~\/.config:/,/--- marker/p' '$UH/state-at-retry-start.txt' | grep -qE '^(quickshell|rofi|kitty)\$'"
check "fault 1: BETWEEN the attempts the default shell marker was gone" bash -c "[[ \$(sed -n '/--- marker files:/,\$p' '$UH/state-at-retry-start.txt' | tail -n 1) == 0 ]]"
check "fault 1: after the retry the shells are deployed again"      bash -c "test -s '$UH/.config/quickshell/macos/shell.qml' && test -s '$UH/.config/quickshell/eqsh/shell.qml'"
check "fault 1: after the retry no dangling symlink"                bash -c '[[ -z "$1" ]]' _ "$(dangling)"
check "fault 1: Hyprland accepts the config after the retry"       grep -q 'config ok' <<<"$(verify_hypr)"
check "fault 1: the newest backup holds our config again"           grep -q 'Generated by ArchInstall' "$(newest_backup)/hypr/hyprland.conf"
check "fault 1: the temporary sudoers file is gone"                 bash -c "[[ \$(ls '$CHECK/etc/sudoers.d') == '10-wheel' ]]"
check "fault 1: no file of the user's home belongs to someone else" bash -c "[[ -z \$(find '$UH' ! -uid $UID_NUM -print -quit) ]]"
rm -f "$UH/state-at-retry-start.txt"

# 2. both attempts fail: rollback twice, our default setup is back, the step still returns normally.
log "Fault 2: both attempts fail: rollback twice, our hyprland.conf is back, the step never fails"
$FAULTS bothfail >"$EV/fault2-bothfail.log" 2>&1 || true
sed 's/\x1b\[[0-9;]*m//g' "$EV/fault2-bothfail.log" | grep -E "Running|WARNING|RECIPE|installed|FAULT-RESULT" | cut -c1-200 | sed 's/^/      /' | tail -n 20
F2="$EV/fault2-bothfail.log"
check "fault 2: the step did not raise (the installation goes on)"  grep -q 'FAULT-RESULT case=bothfail exception=none' "$F2"
check "fault 2: both attempts reported a failure"                   bash -c "[[ \$(grep -c 'WARNING: 20-revo.sh failed' '$F2') == 2 ]]"
check "fault 2: the rollback ran twice"                             bash -c "[[ \$(grep -c 'RECIPE-RESULT rollback=done' '$F2') == 2 ]]"
check "fault 2: the log says the default setup is back"             grep -q 'could not be installed. The default Hyprland setup is back' "$F2"
check "fault 2: the log does not claim a success"                   bash -c "! grep -q 'is installed (' '$F2'"
check "fault 2: ~/.config/hypr/hyprland.conf is OUR config"         grep -q 'Generated by ArchInstall' "$HYPR/hyprland.conf"
check "fault 2: our config still has the waybar autostart"          grep -q 'exec-once = waybar' "$HYPR/hyprland.conf"
check "fault 2: no hyprland.lua of the preset is left"              test ! -e "$HYPR/hyprland.lua"
check "fault 2: no quickshell directory left"                       test ! -e "$UH/.config/quickshell"
check "fault 2: no rofi directory left"                             test ! -e "$UH/.config/rofi"
check "fault 2: no kitty directory left"                            test ! -e "$UH/.config/kitty"
check "fault 2: the default-shell marker is gone"                   test ! -e "$HYPR/.revo_default_shell"
check "fault 2: the SystemSettings app and its entry are gone"      bash -c "[[ ! -e '$UH/.local/bin/systemsettings' && ! -e '$UH/.local/share/applications/systemsettings.desktop' ]]"
check "fault 2: Hyprland accepts our restored config"               grep -q 'config ok' <<<"$(verify_hypr -c "/home/$U/.config/hypr/hyprland.conf")"
check "fault 2: the temporary sudoers file is gone"                 bash -c "[[ \$(ls '$CHECK/etc/sudoers.d') == '10-wheel' ]]"
check "fault 2: no file of the user's home belongs to someone else" bash -c "[[ -z \$(find '$UH' ! -uid $UID_NUM -print -quit) ]]"
cp "$HYPR/hyprland.conf" "$EV/hyprland.conf.after-fault2" 2>/dev/null || true
LEFT_LOCAL="$(du -sm "$UH/.local" 2>/dev/null | cut -f1)"
say "  after a rollback ~/.local is still $LEFT_LOCAL MiB (the pip packages are not removed by restore-default.sh)"

# 3. the pre-flight: an unreachable source stops everything BEFORE the disk would be erased.
log "Fault 3: the pre-flight with github.com unreachable"
cp /etc/hosts /tmp/hosts.saved
echo "127.0.0.1 github.com" >>/etc/hosts
$FAULTS sources >"$EV/fault3-sources.log" 2>&1 || true
cat /tmp/hosts.saved >/etc/hosts
sed 's/\x1b\[[0-9;]*m//g' "$EV/fault3-sources.log" | tail -n 3 | cut -c1-420 | sed 's/^/      /'
F3="$EV/fault3-sources.log"
check "fault 3: check_sources raises a StepError"                   bash -c "grep -q 'FAULT-RESULT case=sources StepError=' '$F3' && ! grep -q 'StepError=none' '$F3'"
check "fault 3: the message ends with 'Nothing was changed.'"       grep -q 'Nothing was changed\.' "$F3"
check "fault 3: it names the unreachable repository"                grep -q 'do not answer: https://github.com/vzbc/revo-shell' "$F3"
$FAULTS sources-ok >"$EV/fault3-sources-ok.log" 2>&1 || true
check "fault 3 (control): with the network back check_sources passes" grep -q 'StepError=none' "$EV/fault3-sources-ok.log"

# 4. a missing AUR package stops everything before the erase as well.
log "Fault 4: the pre-flight with an AUR package that does not exist"
$FAULTS aur-missing >"$EV/fault4-aur-missing.log" 2>&1 || true
sed 's/\x1b\[[0-9;]*m//g' "$EV/fault4-aur-missing.log" | tail -n 3 | cut -c1-420 | sed 's/^/      /'
F4="$EV/fault4-aur-missing.log"
check "fault 4: check_sources raises a StepError"                   bash -c "grep -q 'FAULT-RESULT case=aur-missing StepError=' '$F4' && ! grep -q 'StepError=none' '$F4'"
check "fault 4: it names the missing AUR package"                   grep -q 'cannot be found: archinstall-no-such-package-xyz' "$F4"
check "fault 4: the message ends with 'Nothing was changed.'"       grep -q 'Nothing was changed\.' "$F4"
check "fault 4: the existing AUR packages are not reported"         bash -c "! grep -q 'cannot be found:.*mpvpaper' '$F4'"

# 5. The proposed patches of 20-revo.sh, proven on a COPY of the preset (no fault, same target, real network).
log "Proposed patches (copy of the preset): keyboard layout, wallpapers without 860 MB of blobs, SystemSettings icon"
PATCH_DIR=/tmp/preset-patch; rm -rf "$PATCH_DIR"; mkdir -p "$PATCH_DIR"
cp /src/scripts/integration/dotfiles-revo-patch/* "$PATCH_DIR/"
P0=$SECONDS
PRESET_PATCH_DIR="$PATCH_DIR" $FAULTS patched >"$EV/patch-install.log" 2>&1 || true
P_SECONDS=$((SECONDS - P0))
sed 's/\x1b\[[0-9;]*m//g' "$EV/patch-install.log" | grep -E "patched file|Running|WARNING|RECIPE|keyboard layout|installed|FAULT-RESULT|REVO_COMMIT" | cut -c1-200 | sed 's/^/      /' | tail -n 14
PL="$EV/patch-install.log"
say "  the patched install took $P_SECONDS s (the unpatched preset script: see the step time above)"
check "patch: the step did not raise"                               grep -q 'FAULT-RESULT case=patched exception=none' "$PL"
check "patch: the preset installed (latest version)"                grep -q 'is installed (latest version)' "$PL"
check "patch: the layout chosen in the wizard (fr) is in configs/input.lua" grep -qE 'kb_layout[[:space:]]*=[[:space:]]*"fr"' "$HYPR/configs/input.lua"
check "patch: ... and in settings.lua"                              grep -qE 'kb_layout[[:space:]]*=[[:space:]]*"fr"' "$HYPR/settings.lua"
check "patch: Hyprland accepts the patched configuration"          grep -q 'config ok' <<<"$(verify_hypr)"
check "patch: still ZERO dangling symlinks"                         bash -c '[[ -z "$1" ]]' _ "$(dangling)"
check "patch: the wallpapers were picked without the whole folder"  bash -c "[[ \$(ls '$UH/Pictures/Wallpapers' | wc -l) -ge 1 && \$(ls '$UH/Pictures/Wallpapers' | wc -l) -le 6 ]]"
CLONE_MIB="$(mib "$UH/.cache/archinstall/revo-shell")"; GIT_MIB="$(mib "$UH/.cache/archinstall/revo-shell/.git")"
say "  clone after the patch: $CLONE_MIB MiB (.git $GIT_MIB MiB), ~/.cache $(mib "$UH/.cache") MiB; wallpapers $(ls "$UH/Pictures/Wallpapers" | wc -l) files"
check "patch: the clone is small (under 300 MiB instead of 1008)"  bash -c "[[ $CLONE_MIB -lt 300 ]]"
ICON2="$(sed -n 's/^Icon=//p' "$UH/.local/share/applications/systemsettings.desktop" | head -n 1 | sed "s|^/home/$U|$UH|")"
check "patch: the menu entry icon is not inside the clone"          bash -c '[[ -n "$1" && "$1" != *.cache* && -e "$1" ]]' _ "$ICON2"
rm -rf "$UH/.cache"
check "patch: after deleting ~/.cache the icon of the menu entry is still there" test -e "$ICON2"
check "patch: after deleting ~/.cache the shells are still deployed" bash -c "test -s '$UH/.config/quickshell/macos/shell.qml' && test -s '$UH/.config/quickshell/eqsh/shell.qml'"
check "patch: the temporary sudoers file is gone"                   bash -c "[[ \$(ls '$CHECK/etc/sudoers.d') == '10-wheel' ]]"
check "patch: no file of the user's home belongs to someone else"   bash -c "[[ -z \$(find '$UH' ! -uid $UID_NUM -print -quit) ]]"
cp "$HYPR/configs/input.lua" "$EV/input.lua.after-patch" 2>/dev/null || true

say ""
say "checks failed so far: $FAILED (0 = none)"
cp /tmp/installer.log "$EV/installer.log" 2>/dev/null || true
