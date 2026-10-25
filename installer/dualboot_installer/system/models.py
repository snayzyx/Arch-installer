"""Plain data returned by the System implementations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str
    blocking: bool = False  # a failed blocking check stops the installation


@dataclass(frozen=True)
class DiskInfo:
    path: str  # /dev/nvme0n1
    size_bytes: int
    model: str
    transport: str  # nvme, sata, usb, ...
    removable: bool
    sector_size: int
    partition_table: str  # gpt, dos or "" when blank
    is_live_medium: bool = False


@dataclass(frozen=True)
class GpuInfo:
    vendor_id: str  # lowercase hex, e.g. "10de"
    name: str


@dataclass(frozen=True)
class Country:
    code: str
    name: str


@dataclass(frozen=True)
class WifiNetwork:
    ssid: str
    security: str  # open | psk | 8021x
    signal: int  # 0-4 stars


@dataclass(frozen=True)
class SecureBootState:
    enabled: bool | None  # None = unknown
    setup_mode: bool | None
    # GRUB without shim only boots Linux under Secure Boot when its `tpm` module finds a TPM
    # (verified in a UEFI VM: without one it fails with "verification requested but nobody cares").
    has_tpm: bool = True
