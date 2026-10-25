#!/usr/bin/env bash
# Install MacShell + MacLock for the CURRENT user (run as the normal user, no TTY, no graphical session:
#   arch-chroot /mnt runuser -l USER -c 'bash /path/20-macshell.sh').
# Tracks the HEAD of the default branch (never a version stored on the ISO).
#   MACSHELL_REF=<sha>   known-good fallback: install that commit instead of HEAD.
# Requires ~/.config/hypr/hyprland.lua to exist (MacShell needs a Lua config, see RECIPE.md).
set -euo pipefail
SRC="$HOME/.cache/archinstall/dotfiles/MacShell"
rm -rf "$SRC"; mkdir -p "$SRC"
if [ -n "${MACSHELL_REF:-}" ]; then
  git -C "$SRC" init -q
  git -C "$SRC" fetch -q --depth 1 https://github.com/0-ss/MacShell.git "$MACSHELL_REF"
  git -C "$SRC" checkout -q --detach FETCH_HEAD
else
  git clone -q --depth 1 https://github.com/0-ss/MacShell.git "$SRC"
fi
COMMIT="$(git -C "$SRC" rev-parse HEAD)"
# early detection of an incompatible HEAD: the files and the flags this recipe relies on must still exist
for f in install.sh MacShell/shell.qml MacLock/shell.qml hypr-macshell/scripts; do
  [ -e "$SRC/$f" ] || { echo "RECIPE-FAIL macshell: $f is missing at $COMMIT"; exit 20; }
done
for flag in --from-dir --no-deps --no-services --no-start --no-verify --no-auto-update --yes; do
  grep -q -e "$flag" "$SRC/install.sh" || { echo "RECIPE-FAIL macshell: install.sh no longer knows $flag at $COMMIT"; exit 21; }
done
[ -f "$HOME/.config/hypr/hyprland.lua" ] || { echo "RECIPE-FAIL macshell: no ~/.config/hypr/hyprland.lua"; exit 22; }
# a previous attempt that was rolled back put the default autostart (waybar, nm-applet) back: MacShell replaces
# both, so take that block out again before installing (restore-default.sh marks it)
sed -i '/^-- >>> ArchInstall default autostart/,/^-- <<< ArchInstall default autostart/d' "$HOME/.config/hypr/hyprland.lua"
bash "$SRC/install.sh" --from-dir "$SRC" --yes --no-deps --no-services --no-start --no-verify --no-auto-update
# what must exist afterwards
for f in .config/quickshell/MacShell/shell.qml .config/quickshell/MacLock/shell.qml .local/bin/macshell-start \
         .config/hypr/macshell.lua .config/macshell/env; do
  [ -e "$HOME/$f" ] || { echo "RECIPE-FAIL macshell: $f was not created by install.sh at $COMMIT"; exit 23; }
done
grep -q 'require("macshell")' "$HOME/.config/hypr/hyprland.lua" \
  || { echo "RECIPE-FAIL macshell: hyprland.lua does not load macshell at $COMMIT"; exit 24; }
echo "RECIPE-RESULT preset=macshell commit=$COMMIT committed=$(git -C "$SRC" log -1 --format=%cI) installed_version=$(sed -n 's/^installed_version=//p' "$HOME/.local/share/macshell/state.env")"
