#!/usr/bin/env bash
# ROLLBACK when the preset failed: put the installer's own setup back (normal user).
# `caelestia install` copies ~/.config to ~/.config.bak before it deploys anything, and it deploys much more than
# Hyprland (fish, foot, GTK, btop, starship... files): restore the WHOLE configuration, not only ~/.config/hypr.
# The backup stays, so that a second attempt starts from the same state.
set -u
if [ -d "$HOME/.config.bak" ]; then
  rm -rf "$HOME/.config"
  cp -a "$HOME/.config.bak" "$HOME/.config"
fi
rm -rf "$HOME/.local/state/caelestia" "$HOME/.config/caelestia"
echo "RECIPE-RESULT rollback=done"
