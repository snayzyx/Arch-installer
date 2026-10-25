#!/usr/bin/env bash
# POST-INSTALL CHECK (normal user, a chroot is fine, no GPU needed): the Hyprland config must parse.
# Non-zero => run restore-default.sh and fall back to the default setup.
set -u
export XDG_RUNTIME_DIR="$(mktemp -d)"; chmod 700 "$XDG_RUNTIME_DIR"
out="$(Hyprland --verify-config -c "$HOME/.config/hypr/hyprland.lua" 2>&1)"; rc=$?
rm -rf "$XDG_RUNTIME_DIR"
echo "$out" | sed 's/\x1b\[[0-9;]*m//g' | tail -6
if echo "$out" | grep -q 'config ok' && [ "$rc" -eq 0 ]; then echo "RECIPE-RESULT verify=config-ok"; exit 0; fi
echo "RECIPE-FAIL verify: Hyprland rejects ~/.config/hypr/hyprland.lua (run restore-default.sh)"; exit 40
