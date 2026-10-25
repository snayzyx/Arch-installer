"""Graphics driver profiles."""

from __future__ import annotations

from dataclasses import dataclass

from .system.models import GpuInfo


@dataclass(frozen=True)
class Profile:
    id: str
    label: str
    packages: tuple[str, ...]


PROFILES: dict[str, Profile] = {
    "intel": Profile("intel", "Intel graphics (mesa, vulkan-intel, intel-media-driver)",
                     ("mesa", "vulkan-intel", "intel-media-driver")),
    "amd": Profile("amd", "AMD graphics (mesa, vulkan-radeon, libva-mesa-driver)",
                   ("mesa", "vulkan-radeon", "libva-mesa-driver")),
    "nvidia-open": Profile("nvidia-open", "NVIDIA open kernel modules (RTX 20 series / GTX 16 series and newer)",
                           ("nvidia-utils", "nvidia-settings")),
    "nouveau": Profile("nouveau", "NVIDIA open-source driver (nouveau, any NVIDIA GPU)",
                       ("mesa", "vulkan-nouveau")),
    "qemu": Profile("qemu", "QEMU / KVM guest (mesa, qemu-guest-agent, spice-vdagent)",
                    ("mesa", "qemu-guest-agent", "spice-vdagent")),
    "vmware": Profile("vmware", "VMware guest (mesa, open-vm-tools)", ("mesa", "open-vm-tools")),
    "virtualbox": Profile("virtualbox", "VirtualBox guest (mesa, virtualbox-guest-utils)",
                          ("mesa", "virtualbox-guest-utils")),
}

_VENDOR_PROFILE = {
    "8086": "intel",
    "1002": "amd",
    "10de": "nvidia-open",
    "1af4": "qemu",
    "1234": "qemu",
    "1b36": "qemu",
    "15ad": "vmware",
    "80ee": "virtualbox",
}


def suggest_profiles(gpus: list[GpuInfo]) -> list[str]:
    """Profile ids matching the detected GPUs, without duplicates."""
    found = [_VENDOR_PROFILE[g.vendor_id] for g in gpus if g.vendor_id in _VENDOR_PROFILE]
    return list(dict.fromkeys(found))


def packages_for(profile_ids: list[str], kernel: str) -> list[str]:
    """Packages to install for the chosen profiles on the chosen kernel."""
    packages: list[str] = []
    for profile_id in profile_ids:
        profile = PROFILES[profile_id]
        if profile_id == "nvidia-open":
            # The prebuilt module only exists for the stock kernel; others need DKMS.
            packages += ["nvidia-open"] if kernel == "linux" else ["nvidia-open-dkms", f"{kernel}-headers"]
        packages += profile.packages
    return list(dict.fromkeys(packages))
