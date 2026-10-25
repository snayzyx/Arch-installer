#!/usr/bin/env bash
# MacTahoe + MacOS-Like icon themes for the CURRENT user (normal user, no TTY, no session).
# HEAD of each default branch. Run it AFTER every application is installed: MacOS-Like composes a
# squircle tile for each app whose icon MacTahoe does not cover (it scans the installed .desktop files).
#   MACTAHOE_REF / MACOSLIKE_REF = known-good fallback commits (optional)
set -euo pipefail
WORK="$HOME/.cache/archinstall/dotfiles"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons"
mkdir -p "$WORK" "$ICONS"
fetch() { # fetch <repo> <dir> [ref]
  rm -rf "$2"; mkdir -p "$2"
  if [ -n "${3:-}" ]; then
    git -C "$2" init -q; git -C "$2" fetch -q --depth 1 "https://github.com/$1.git" "$3"; git -C "$2" checkout -q --detach FETCH_HEAD
  else
    git clone -q --depth 1 "https://github.com/$1.git" "$2"
  fi
}
fetch vinceliuice/MacTahoe-icon-theme "$WORK/MacTahoe-icon-theme" "${MACTAHOE_REF:-}"
fetch Scrimas/MacOS-Like "$WORK/MacOS-Like" "${MACOSLIKE_REF:-}"
T="$(git -C "$WORK/MacTahoe-icon-theme" rev-parse HEAD)"; M="$(git -C "$WORK/MacOS-Like" rev-parse HEAD)"
for f in "$WORK/MacTahoe-icon-theme/install.sh" "$WORK/MacOS-Like/setup.sh"; do
  [ -f "$f" ] || { echo "RECIPE-FAIL icons: $f is missing"; exit 30; }
done
grep -q -e '--dest' "$WORK/MacTahoe-icon-theme/install.sh" || { echo "RECIPE-FAIL icons: MacTahoe install.sh lost --dest at $T"; exit 31; }
grep -q -e '--source' "$WORK/MacOS-Like/setup.sh" || { echo "RECIPE-FAIL icons: MacOS-Like setup.sh lost --source at $M"; exit 32; }
( cd "$WORK/MacTahoe-icon-theme" && bash ./install.sh -d "$ICONS" -t default )
( cd "$WORK/MacOS-Like" && bash ./setup.sh --source "$ICONS/MacTahoe" )
for t in MacTahoe MacOS-Like-Light MacOS-Like-Dark; do
  [ -f "$ICONS/$t/index.theme" ] || { echo "RECIPE-FAIL icons: $ICONS/$t was not created"; exit 33; }
done
# use the dark theme: GTK apps and MacShell (macshell-start reads gtk settings.ini when QS_ICON_THEME is unset)
for v in gtk-3.0 gtk-4.0; do
  mkdir -p "$HOME/.config/$v"
  s="$HOME/.config/$v/settings.ini"
  if [ -f "$s" ] && grep -q '^gtk-icon-theme-name' "$s"; then sed -i 's/^gtk-icon-theme-name.*/gtk-icon-theme-name=MacOS-Like-Dark/' "$s"
  elif [ -f "$s" ] && grep -q '^\[Settings\]' "$s"; then sed -i '/^\[Settings\]/a gtk-icon-theme-name=MacOS-Like-Dark' "$s"
  else printf '[Settings]\ngtk-icon-theme-name=MacOS-Like-Dark\n' >> "$s"; fi
done
if [ -f "$HOME/.config/macshell/env" ] && ! grep -q '^export QS_ICON_THEME=' "$HOME/.config/macshell/env"; then
  echo 'export QS_ICON_THEME=MacOS-Like-Dark' >> "$HOME/.config/macshell/env"
fi
echo "RECIPE-RESULT preset=icons mactahoe=$T macos_like=$M"
