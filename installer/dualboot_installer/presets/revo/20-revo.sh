#!/usr/bin/env bash
# Install the Revo Shell dotfiles (https://github.com/vzbc/revo-shell) for the CURRENT user (normal user, no TTY,
# no graphical session):  arch-chroot /mnt runuser -l USER -c 'bash /path/20-revo.sh'
# The packages (official and AUR) are already installed by the installer. This script downloads the HEAD of the
# default branch (never a version stored on the ISO), runs the project's own install.sh with the options that
# make it non-interactive, repairs what it gets wrong for any user but its author, and checks the result:
# install.sh's own exit code means nothing (it exits 0 when its checks fail).
#   REVO_SHELL   the shell to make the default (default: macos; ii also works)
#   REVO_REF     known-good fallback: install that commit instead of HEAD
# Exit codes: 10 the repository no longer has the expected layout, 20 download failed, 30 installed but the
# verification failed.
set -uo pipefail
REPO_URL=https://github.com/vzbc/revo-shell.git
REF="${REVO_REF:-}"
SHELL_ID="${REVO_SHELL:-macos}"
DEST="$HOME/.cache/archinstall/revo-shell"
fail() { echo "RECIPE-FAIL revo: $1"; exit "${2:-1}"; }

# 1. Early detection of a repository that was reorganised (a sparse clone with no file content: 0.7 s).
P="$(mktemp -d)"
git clone -q --depth 1 --filter=blob:none --no-checkout "$REPO_URL" "$P" 2>/dev/null || { rm -rf "$P"; fail "the repository does not answer" 20; }
for f in install.sh hypr/hyprland.lua hypr/scripts/toggle_qs_dots.sh "quickshell/$SHELL_ID/shell.qml" SystemSettings/CMakeLists.txt; do
  git -C "$P" cat-file -e "HEAD:$f" 2>/dev/null || { rm -rf "$P"; fail "$f is missing at the latest commit" 10; }
done
rm -rf "$P"

# 2. Fetch only what is used: the whole repository is 2.3 GB, these folders about 150 MB.
rm -rf "$DEST"; mkdir -p "$(dirname "$DEST")"
git clone -q --depth 1 --filter=blob:none --sparse "$REPO_URL" "$DEST" || fail "clone failed" 20
cd "$DEST" || exit 20
if [ -n "$REF" ]; then git fetch -q --depth 1 origin "$REF" && git checkout -q FETCH_HEAD || fail "checkout of $REF failed" 20; fi
git sparse-checkout set hypr rofi kitty SystemSettings "quickshell/$SHELL_ID"
# The shell "macos" is a layer of ABSOLUTE symlinks into the sibling shell eqsh, under the author's home: find the
# siblings from the links themselves rather than naming them.
SIBLINGS="$(find "quickshell/$SHELL_ID" -type l -printf '%l\n' | sed -n 's#.*/quickshell/\([^/]*\)/.*#\1#p' | grep -vx "$SHELL_ID" | sort -u | tr '\n' ' ')"
for s in $SIBLINGS; do git sparse-checkout add "quickshell/$s"; done
COMMIT="$(git rev-parse HEAD)"
echo "REVO_COMMIT=$COMMIT $(git log -1 --format=%cI) siblings: ${SIBLINGS:-none}"
# a few small wallpapers (the autostart picks one at random).
# PATCH: `git ls-tree -r -l` on a partial clone downloads EVERY missing blob to learn the sizes: here the whole
# wallpapers folder (800+ MB, 4-5 minutes) to keep six small files. GitHub's tree API gives the sizes for free.
mkdir -p "$HOME/Pictures/Wallpapers"
WTREE="$(git rev-parse HEAD:wallpapers 2>/dev/null)"
if [ -n "$WTREE" ]; then
  curl -fsS --max-time 30 "https://api.github.com/repos/vzbc/revo-shell/git/trees/$WTREE" 2>/dev/null \
    | python3 -c 'import json, sys
tree = json.load(sys.stdin).get("tree", [])
images = [e for e in tree if e.get("type") == "blob" and e["path"].lower().endswith((".png", ".jpg", ".jpeg"))]
for entry in sorted(images, key=lambda e: e["size"])[:6]:
    print(entry["path"])' 2>/dev/null \
    | while IFS= read -r name; do git show "HEAD:wallpapers/$name" > "$HOME/Pictures/Wallpapers/$name"; done
fi

# 3. Deploy with the project's own installer. It refuses to run without --shells and without a terminal; it backs the
#    existing ~/.config/hypr up to ~/.config/revo-shell-backup-<time>/ first. No hyprpm plugins: they need a running
#    Hyprland and are third-party native code loaded into the compositor.
mkdir -p "$HOME/.config/hypr"
[ -e "$HOME/.config/hypr/hyprland.conf" ] || printf '# placeholder\n' > "$HOME/.config/hypr/hyprland.conf"
LIST="$SHELL_ID$(for s in $SIBLINGS; do printf ',%s' "$s"; done)"
HYPRPM_TIMEOUT=60 ./install.sh --shells "$LIST" </dev/null > /tmp/revo-install.log 2>&1
echo "install.sh finished (its exit code is not trusted); log: /tmp/revo-install.log"
grep -a "^\[install\] \(shells\|backup\|default shell\|rewrote\)" /tmp/revo-install.log | cut -c1-140

# 4. Repair the absolute links that point into another user's home (install.sh skips symlinks).
find "$HOME/.config" -type l -lname '/home/*' 2>/dev/null | while read -r link; do
  target="$(readlink "$link")"
  [[ "$target" == "$HOME/"* ]] && continue
  ln -sfn "$HOME/${target#/home/*/}" "$link"
done

# 4b. PATCH: the project hard-codes its author's keyboard (kb_layout = "us,ara", Alt+Shift to switch) in
#     configs/input.lua and in settings.lua (which wins over it). Use the layout chosen in the installer, which it wrote
#     to /etc/X11/xorg.conf.d/00-keyboard.conf before this step.
KB="${KB_LAYOUT:-$(sed -n 's/.*Option "XkbLayout" "\([^"]*\)".*/\1/p' /etc/X11/xorg.conf.d/00-keyboard.conf 2>/dev/null | head -n 1)}"
if [ -n "$KB" ]; then
  for f in "$HOME/.config/hypr/configs/input.lua" "$HOME/.config/hypr/settings.lua"; do
    [ -f "$f" ] && sed -i -E "s/(kb_layout[[:space:]]*=[[:space:]]*)\"[^\"]*\"/\1\"$KB\"/" "$f"
  done
  echo "keyboard layout set to $KB"
fi

# 4c. PATCH: the menu entry of the SystemSettings app points its Icon= into the clone (deleted to save space later):
#     keep a copy of the icon next to the user's other icons.
DESK="$HOME/.local/share/applications/systemsettings.desktop"
ICON="$(sed -n 's/^Icon=//p' "$DESK" 2>/dev/null | head -n 1)"
if [ -n "$ICON" ] && [ -f "$ICON" ]; then
  mkdir -p "$HOME/.local/share/icons"
  cp "$ICON" "$HOME/.local/share/icons/systemsettings.png" && sed -i "s|^Icon=.*|Icon=$HOME/.local/share/icons/systemsettings.png|" "$DESK"
fi

# 5. The deployed autostart forces the "performance" power profile at every login: a battery drain on a laptop.
grep -rl 'powerprofilesctl set performance' "$HOME/.config/hypr" 2>/dev/null | while read -r file; do
  sed -i '/powerprofilesctl set performance/d' "$file"
done

# 6. Verify the result (not install.sh's exit code).
bad=()
[ -f "$HOME/.config/hypr/hyprland.lua" ] || bad+=("hyprland.lua missing")
[ "$(tr -d ' \n' < "$HOME/.config/hypr/.revo_default_shell" 2>/dev/null)" = "$SHELL_ID" ] || bad+=("the default shell marker is not $SHELL_ID")
[ -f "$HOME/.config/quickshell/$SHELL_ID/shell.qml" ] || bad+=("shell.qml missing")
[ "$(find "$HOME/.config/quickshell" "$HOME/.config/hypr" -xtype l | wc -l)" = 0 ] || bad+=("dangling symlinks")
grep -q "default shell → $SHELL_ID" /tmp/revo-install.log || bad+=("install.sh did not set the default shell")
XR="$(mktemp -d)"; chmod 700 "$XR"
out="$(XDG_RUNTIME_DIR="$XR" timeout 60 Hyprland --verify-config 2>&1 | sed 's/\x1b\[[0-9;]*m//g')"
rm -rf "$XR"
echo "$out" | grep -q "config ok" || bad+=("Hyprland --verify-config: $(echo "$out" | tail -3 | tr '\n' ' ')")
[ -x "$HOME/.local/bin/systemsettings" ] || echo "note: the SystemSettings app was not built (the shells work without it)"
if [ "${#bad[@]}" -gt 0 ]; then
  echo "RECIPE-FAIL revo: installed but the verification failed: ${bad[*]}"
  exit 30
fi
echo "RECIPE-RESULT preset=revo commit=$COMMIT shell=$SHELL_ID"
