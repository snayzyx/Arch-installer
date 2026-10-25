"""Checks across all the answers, done once before installing."""

from __future__ import annotations

from . import extras
from .config import InstallConfig
from .validators import (
    validate_hostname,
    validate_linux_username,
    validate_mok_password,
    validate_windows_computer_name,
    validate_windows_username,
)


def problems(cfg: InstallConfig) -> list[str]:
    """Every reason the installation cannot start with these answers (empty list = fine)."""
    found = list(cfg.layout().errors)
    if not cfg.disk.device:
        found.append("No target disk selected.")
    for error in (
        validate_hostname(cfg.arch.hostname),
        validate_linux_username(cfg.arch.username),
        validate_windows_computer_name(cfg.windows.computer_name),
        validate_windows_username(cfg.windows.username, cfg.windows.computer_name),
    ):
        if error:
            found.append(error)
    if not cfg.arch.user_password:
        found.append("The user password is required.")
    if not cfg.arch.effective_root_password:
        found.append("The root password is required.")
    found += extras.problems(cfg.arch)
    if cfg.arch.luks_enabled and len(cfg.arch.luks_passphrase) < 8:
        found.append("The disk encryption passphrase must be at least 8 characters.")
    if cfg.secureboot.resolved == "shim" and cfg.secureboot.touch_firmware:
        error = validate_mok_password(cfg.secureboot.mok_password, cfg.secureboot.mok_password)
        if error:
            found.append(error)
    return found
