"""Secure Boot state detection and strategy selection."""

from __future__ import annotations

from pathlib import Path

from .system.models import SecureBootState

EFIVARS = Path("/sys/firmware/efi/efivars")
TPM_DEVICE = Path("/sys/class/tpm/tpm0")
EFI_GLOBAL_GUID = "8be4df61-93ca-11d2-aa0d-00e098032b8c"

STRATEGY_LABELS = {
    "sbctl": "Custom keys with sbctl (enrolled automatically, Microsoft keys kept)",
    "shim": "shim + MOK (enroll a key on the next boot)",
}


def _read_efi_flag(name: str, efivars: Path) -> bool | None:
    try:
        data = (efivars / f"{name}-{EFI_GLOBAL_GUID}").read_bytes()
    except OSError:
        return None
    # 4 bytes of attributes, then the value.
    return data[4] == 1 if len(data) >= 5 else None


def detect(efivars: Path = EFIVARS, tpm: Path = TPM_DEVICE) -> SecureBootState:
    return SecureBootState(
        enabled=_read_efi_flag("SecureBoot", efivars),
        setup_mode=_read_efi_flag("SetupMode", efivars),
        has_tpm=tpm.exists(),
    )


def resolve_strategy(choice: str, state: SecureBootState) -> str:
    """Turn the user's choice ("auto", "sbctl" or "shim") into a concrete strategy.

    sbctl needs two things: the firmware in Setup Mode (no Platform Key installed, so that
    it can enroll its keys) and a TPM (GRUB without shim cannot boot Linux under Secure Boot
    otherwise). Everywhere else shim + MOK works, without touching the firmware's own keys.
    """
    if choice in ("sbctl", "shim"):
        return choice
    return "sbctl" if state.setup_mode and state.has_tpm else "shim"


def sbctl_problem(state: SecureBootState) -> str | None:
    """Why the sbctl strategy cannot work on this machine, or None."""
    if not state.has_tpm:
        return ("GRUB without shim only boots Linux under Secure Boot when the machine has a TPM, and none "
                "was found. Enable the firmware TPM (fTPM / PTT) in the firmware setup, or use shim + MOK.")
    if state.setup_mode is False:
        return ("The firmware is not in Setup Mode, so the Secure Boot keys cannot be enrolled. Clear the "
                "Secure Boot keys in the firmware setup first, or use shim + MOK.")
    return None
