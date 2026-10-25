# Scripts

Everything here **tests or builds** the project; none of it is needed to build the ISO (that is
`build.ps1` at the root) or to install. The scripts exist so that every claim in
[docs/RESULTS.md](../docs/RESULTS.md) can be reproduced.

They run on **Windows with Docker Desktop**. The tests that boot virtual machines also need
`/dev/kvm` inside Docker's Linux VM (Docker Desktop on WSL2 with nested virtualization) and the
builder image (run `.\build.ps1` once). Tests that install Windows need **your own Windows 11
ISO**; it is mounted read-only and never copied into the repository. Plan for 100+ GB of free disk
space: the tests keep disk images in Docker volumes.

Run everything from the repository root in PowerShell. Every `.ps1` has built-in help:
`Get-Help .\scripts\test-install.ps1 -Full`.

## Entry points (PowerShell)

| Script | What it does | Time |
|---|---|---|
| `rebuild-all.ps1` | Builds the online ISO and, with `-WindowsIso`, the offline ISO, then verifies both with `docker/verify-iso.sh` (installer files identical to the repository, pinned shim, Windows image intact, boot lines). `-OutDir`, `-OfflineOnly`, `-SkipVerify`. | 15-35 min |
| `test-install.ps1` | Runs the **real installation steps** against a 110 GB sparse disk image in a privileged container, mounts the result again and checks it. Options: `-Strategy shim\|sbctl`, `-Filesystem`, `-Desktop`, `-Luks`, `-SeparateHome`, `-SharedPartition`, `-AurHelper`, `-DownloadSource`, `-Laptop`, `-WindowsIso`, `-BootTest`. | 2-5 min each |
| `live-test.ps1` | Tests the installer's writes to **real UEFI firmware** in a QEMU/KVM VM (OVMF, virtual TPM). Without `-E2e`: the official Arch ISO and the Secure Boot scenarios. With `-E2e shim\|sbctl`: the **whole chain** from the offline ISO (installer, Windows Setup, key enrollment, GRUB, Windows, Arch); `-Laptop` adds the laptop answers and checks. | 45-60 min for `-E2e` |
| `tui-e2e.ps1` | Drives the **real wizard** through a terminal emulator in a VM, screen by screen, then inspects the installed disk. `-Mode laptop` emulates a laptop (NVMe disk, Wi-Fi only, simulated battery and RAID/VMD controller). `-Mode quick` only checks how the ISO boots. | 5-40 min |
| `download-resume-test.ps1` | Serves the Windows ISO over HTTPS and cuts the connection midway, to check the "link" source survives it. | 10 min |

Evidence (screenshots, logs) goes to `dist\screens`, `dist\tui-screens` and `out\` (all git-ignored).

## `integration/` (run inside the privileged builder container)

The PowerShell scripts start these; you rarely run them by hand.

| Group | Files |
|---|---|
| Install steps against a disk image | `test-install.sh` (the checks), `boot-vm.sh` (the Secure Boot boot test it sources), `inspect-image.sh`, `windows-boot.sh` (boots a disk with a staged Windows and takes screenshots) |
| The "link" Windows source | `iso-server.sh` (HTTPS server with a throw-away CA, supports Range), `download-resume.sh` |
| Real-firmware VM tests | `live-test.sh`, `live-vm.py` (QEMU/OVMF driver), `live-guest-*.sh` (small helpers run inside the live system) |
| Whole chain with Windows | `live-e2e.sh`, `live-e2e.py` |
| The wizard in a VM | `tui-e2e.sh`, `tui-e2e.py` (terminal emulator driver), `tui-e2e-iso.sh`, `laptop_e2e.py` (laptop walk), `sddm_keyboard.py` (checks the keyboard layout of the login screen) |

## Related, outside this folder

- `build.ps1`: builds the ISO (see the main README).
- `docker/build-iso.sh`: the build, run inside the builder container.
- `docker/verify-iso.sh`: the checks of `rebuild-all.ps1`.
- `docker/smoke-live.py`: boots a built ISO in QEMU (UEFI, serial, or as a USB-like disk) and takes
  screenshots; run it by hand inside the builder container.
- `installer/tests/`: the unit and wizard tests (`cd installer; python -m pytest`), no Docker needed.

## Safety

No script writes to a physical disk. Disk images are files attached as loop devices inside
containers, or virtual disks of QEMU VMs. The containers are privileged because `mkarchiso`,
`mount` and loop devices need it; they only touch what they create. If a test is interrupted and
leaves a stale loop device or device-mapper name (`cryptroot`, `crypthome`), remove exactly that one;
the installer's preflight also closes stale `cryptroot`/`crypthome` volumes by itself.
