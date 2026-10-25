#!/usr/bin/env bash
# ROLLBACK when the preset failed: put the installer's own Hyprland setup back (normal user).
# Revo Shell's install.sh copies the existing ~/.config/hypr to ~/.config/revo-shell-backup-<time>/hypr first;
# that copy is the installer's own setup (hyprland.conf with waybar and the network applet in autostart).
set -u
backup="$(ls -dt "$HOME"/.config/revo-shell-backup-* 2>/dev/null | head -n 1)"
if [ -n "$backup" ] && [ -d "$backup/hypr" ]; then
  rm -rf "$HOME/.config/hypr"
  cp -a "$backup/hypr" "$HOME/.config/hypr"
fi
rm -rf "$HOME/.config/quickshell" "$HOME/.config/rofi" "$HOME/.config/kitty"
rm -f "$HOME/.local/bin/systemsettings" "$HOME/.local/share/applications/systemsettings.desktop"
echo "RECIPE-RESULT rollback=done"
