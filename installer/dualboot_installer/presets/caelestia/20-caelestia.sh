#!/usr/bin/env bash
# Install the Caelestia desktop shell for the CURRENT user (normal user, no TTY, no graphical session):
#   arch-chroot /mnt runuser -l USER -c 'bash /path/20-caelestia.sh'
# The AUR package caelestia-cli (the `caelestia` command) and the AUR helper yay are already installed by
# the installer. `caelestia install` then installs the shell (AUR release), the rolling quickshell-git it
# needs and the applications of the dots, and deploys the dots from the HEAD of the default branch
# (never a version stored on the ISO).
#   CAELESTIA_DOTS_REF=<sha>   known-good fallback: deploy that commit of the dots instead of HEAD.
# Exit codes: 20 caelestia missing, 21 yay missing, 22 `caelestia install` lost an option we use,
# 23 `caelestia install` failed, 24 an expected file is missing.
set -uo pipefail
export XDG_RUNTIME_DIR="$(mktemp -d)"
chmod 700 "$XDG_RUNTIME_DIR"
cleanup() { rm -rf "$XDG_RUNTIME_DIR" "${MIRROR:-}"; }
trap cleanup EXIT

command -v caelestia >/dev/null || { echo "RECIPE-FAIL caelestia: the caelestia command is missing (AUR package caelestia-cli)"; exit 20; }
command -v yay >/dev/null || { echo "RECIPE-FAIL caelestia: yay is missing"; exit 21; }
help="$(caelestia install --help 2>&1)"
for flag in --noconfirm --aur-helper --disable-components; do
  grep -q -e "$flag" <<<"$help" || { echo "RECIPE-FAIL caelestia: 'caelestia install' no longer knows $flag"; exit 22; }
done

if [ -n "${CAELESTIA_DOTS_REF:-}" ]; then
  # The CLI follows a branch of a repository (dots.url, dots.branch in ~/.config/caelestia/cli.json), not a
  # commit: build a local mirror whose branch points at the known-good commit.
  MIRROR="$(mktemp -d)/dots.git"
  git clone -q --bare https://github.com/caelestia-dots/caelestia.git "$MIRROR"
  git -C "$MIRROR" update-ref refs/heads/known-good "$CAELESTIA_DOTS_REF"
  mkdir -p "$HOME/.config/caelestia"
  printf '{"dots": {"url": "file://%s", "branch": "known-good"}}\n' "$MIRROR" > "$HOME/.config/caelestia/cli.json"
fi

caelestia install --noconfirm --aur-helper yay --disable-components firefox,qt
rc=$?
if [ -n "${CAELESTIA_DOTS_REF:-}" ]; then
  rm -f "$HOME/.config/caelestia/cli.json"  # otherwise `caelestia update` would keep following the mirror
fi
# the hyprctl reload hook fails without a session ("hook exited with 1"): it is not a failure
[ "$rc" -eq 0 ] || { echo "RECIPE-FAIL caelestia: 'caelestia install' exited with $rc"; exit 23; }

# The dots hard-code kb_layout = "us". Their hyprland.lua loads ~/.config/caelestia/hypr-user.lua after the input
# settings and never overwrites that file: put the layout chosen in the installer there.
if [ -n "${KB_LAYOUT:-}" ] && ! grep -qs 'archinstall: keyboard layout' "$HOME/.config/caelestia/hypr-user.lua"; then
  mkdir -p "$HOME/.config/caelestia"
  {
    echo '-- archinstall: keyboard layout chosen in the installer (the Caelestia dots hard-code "us")'
    echo "hl.config({ input = { kb_layout = \"$KB_LAYOUT\" } })"
  } >> "$HOME/.config/caelestia/hypr-user.lua"
fi

for f in .config/hypr/hyprland.lua .config/hypr/variables.lua .local/state/caelestia/dots-state.json \
         .local/state/caelestia/scheme.json; do
  [ -e "$HOME/$f" ] || { echo "RECIPE-FAIL caelestia: $f was not created"; exit 24; }
done
for f in /etc/xdg/quickshell/caelestia/shell.qml /usr/bin/caelestia /usr/bin/qs; do
  [ -e "$f" ] || { echo "RECIPE-FAIL caelestia: $f is missing"; exit 24; }
done
echo "RECIPE-RESULT preset=caelestia dots=$(git -C "$HOME/.local/state/caelestia/dots" rev-parse HEAD 2>/dev/null) $(pacman -Q caelestia-shell caelestia-cli quickshell-git 2>/dev/null | tr '\n' ' ')"
