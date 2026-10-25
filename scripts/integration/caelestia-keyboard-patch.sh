# PROPOSED PATCH (tested by dotfiles_caelestia_faults.py, scenario 4b): the block to add to
# presets/caelestia/20-caelestia.sh, after `caelestia install` succeeded and before the expected-files loop.
#
# The Caelestia dots hard-code `kb_layout = "us"` in ~/.config/hypr/hyprland/input.lua, and hyprland.lua loads
# ~/.config/caelestia/hypr-user.lua AFTER it. That file is the person's own: the dots never overwrite it and
# `restore-default.sh` removes it with the rest of ~/.config/caelestia. The engine has to pass KB_LAYOUT
# (xkb_layout(cfg.arch.keymap), e.g. "fr") in the environment of the script.
if [ -n "${KB_LAYOUT:-}" ] && ! grep -qs 'archinstall: keyboard layout' "$HOME/.config/caelestia/hypr-user.lua"; then
  mkdir -p "$HOME/.config/caelestia"
  {
    echo '-- archinstall: keyboard layout chosen in the installer (the Caelestia dots hard-code "us")'
    echo "hl.config({ input = { kb_layout = \"$KB_LAYOUT\" } })"
  } >> "$HOME/.config/caelestia/hypr-user.lua"
fi
