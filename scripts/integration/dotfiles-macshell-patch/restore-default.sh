#!/usr/bin/env bash
# ROLLBACK when the preset failed: put the installer's own Hyprland setup back (normal user).
# The default setup starts waybar and the network applet; MacShell replaces them, so the Lua base
# written for MacShell leaves them out and this script adds them back.
set -u
H="$HOME/.config/hypr"
[ -f "$H/hyprland.lua.macshell-backup" ] && mv -f "$H/hyprland.lua.macshell-backup" "$H/hyprland.lua"
rm -f "$H/macshell.lua"
rm -rf "$HOME/.config/quickshell/MacShell" "$HOME/.config/quickshell/MacLock" "$HOME"/.local/bin/macshell-*
if [ -f "$H/hyprland.lua" ] && ! grep -q 'waybar' "$H/hyprland.lua"; then
  cat >> "$H/hyprland.lua" <<'LUA'

-- >>> ArchInstall default autostart (added by a rollback) >>>
hl.on("hyprland.start", function()
    hl.exec_cmd("waybar")
    hl.exec_cmd("nm-applet --indicator")
end)
-- <<< ArchInstall default autostart <<<
LUA
fi
echo "RECIPE-RESULT rollback=done"
