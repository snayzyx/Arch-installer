# Results

State of the project on 2026-10-25, after the last test session. This file separates what was
**run and observed** from what was only **written**. **Nothing here was tested on real hardware**:
every boot, install and Windows setup below ran in QEMU/KVM virtual machines (UEFI firmware with
enforced Secure Boot, a virtual TPM, an NVMe disk) or in privileged Docker containers.

The evidence folders cited below (`dist\screens\...`, `dist\tui-screens\...`) are written by the test
scripts (see [scripts/README.md](../scripts/README.md)) on the machine that runs them. `dist\` is
git-ignored: they are not part of the repository, and rerunning the scripts regenerates them.

## In one paragraph

The installer installs Arch Linux and Windows 11 side by side from one ISO, with a GRUB signed for
Secure Boot, and adapts to a laptop (power management, Bluetooth, Wi-Fi kept for the new system,
Windows Fast Startup off, warnings for RAID/VMD storage and battery power). In virtual machines the
complete flow worked: the real installer ran from the offline ISO, Windows installed itself
unattended, the Secure Boot key was enrolled, and both systems booted from the GRUB menu, with the
shim path and with the sbctl path. The defects those tests exposed were fixed and retested (see the
table below).

## Verified (observed in a test, with evidence)

| What | How it was tested | Evidence |
|---|---|---|
| 254 unit and wizard tests | `python -m pytest` on Windows (Python 3.14, Textual 8.2.8) | run before the final build |
| Arch install, many variants: shim+ext4, sbctl+xfs, shim+btrfs, shim+ext4+LUKS+`/home`, sbctl+btrfs+Hyprland, plus Windows staged with a shared NTFS partition + LUKS + `/home` + btrfs, and with xfs + `/home` + sbctl | the real install steps against a 110 GB disk image in a privileged container, 53-105 checks each, the result mounted again | `dist\screens\containers\SUMMARY.txt` |
| AUR helpers | `yay` installs and starts; `paru-bin` did not start, now falls back to the source package (works) | same folder, scenarios 3a-3d |
| Windows source = **HTTPS link** | nginx with a throw-away CA serving the real 9 GB ISO; same SHA-256 afterwards; connection cut at 1.6 GB, survived with `--retry-all-errors` | same folder, scenarios 4 and 4b |
| **Whole flow from the offline ISO, shim path** | real installer (headless) -> Windows Setup starts by itself -> finishes by itself -> MOK enrollment -> GRUB menu -> Windows lock screen -> Arch login with `SecureBoot=1` | `dist\screens\e2e2\` (59 checks) |
| **Whole flow, sbctl path** | firmware in Setup Mode + virtual TPM, keys enrolled by the installer, Windows installed and booted under those keys, Arch with Secure Boot enabled | `dist\screens\e2e2\` (50 checks) |
| **Laptop run of the whole flow** (NVMe disk, `arch.laptop`, Wi-Fi profile) | same chain; Fast Startup and hibernation off (registry, no `hiberfil.sys`), the Windows partition mounts **read-write** under Linux after a normal shutdown (it did not before), finish log, firmware product key attempted with a fake ACPI MSDM table and never logged | `dist\screens\e2e3\` (83 checks) |
| A missed 10-second MOK window | shim refuses GRUB; Secure Boot off, Arch starts and queues the key again, enrollment then works | 35 checks in `dist\screens\e2e2\` |
| **The real interactive wizard** on the offline ISO | driven key by key in a VM: 13 screens, validations, back navigation, Esc/Ctrl+C/Enter during the install, two full installs, disk inspected afterwards | `dist\tui-screens\final\` (254 checks, 0 failed) |
| **Laptop wizard walk** | NVMe disk (`nvme0n1p1`...), Wi-Fi only with no wired route (access point in a network namespace), simulated battery and RAID/VMD controller, install over Wi-Fi, installed system boots and joins the same network by itself, `Hyprland --verify-config` answers `config ok`, SDDM greeter appears | `dist\tui-screens\laptop\` (254 checks, 2 defects found and fixed) |
| SDDM keyboard layout | typing the physical keys of `azerty` logs in, `qwerty` is refused: the French layout is used | `dist\tui-screens\sddm-keyboard\` |
| **Developer tools and applications** | the real installation steps in a container and the real wizard in a VM: 22 developer packages, LibreWolf (official), `yay` built by itself, `vscodium-bin` and `postman-bin` from the AUR, Docker group and service; the AUR unreachable (the install still ends, with warnings); nothing extra by default | `dist\screens\containers\extras\` (6 scenarios, 61-111 checks), `dist	ui-screens\extras\` (745 checks over 5 passes) |
| **Desktop shell presets** (MacShell + MacOS-Like icons, Revo Shell, Caelestia) | each one through the real installer: latest version cloned, scripts run as the new user, expected files, `Hyprland --verify-config` ok, then fault injection on the real engine: the latest version fails -> rollback -> retry with the tested commits -> default setup back; unreachable source -> "Nothing was changed" before the erase | `dist\screens\containers\presets\<id>\` (151-195 checks per run) |
| **The three desktop shells on a screen** | each preset installed by the real installer, then the installed disk booted in QEMU/KVM (virtio-vga, no GL: Mesa llvmpipe is enough, no setting needed) with a test-only SDDM autologin, screenshots every 15 s: MacShell (macOS-style menu bar, Dock), Caelestia (vertical bar, wallpaper, themed terminal), Revo Shell (Finder bar, top bar, Dock); keyboard layout `fr` in all three sessions; the default setup (waybar, wofi, kitty) as a baseline | `dist\screens\preset-boot\<id>\` |
| **Windows drivers and Wi-Fi profile** | the drivers of this machine's adapters are read from the real Windows of an earlier test disk (Ethernet, Wi-Fi and Bluetooth packages found in 0.7 s, 20 MB, copied identically), staged with the real `stage_setup` under `$OEM$\$1\Drivers` with `DriverPaths` in the answer file; the Wi-Fi profile format was checked against a real Windows profile; the scripts pass the PowerShell parser | `dist\screens\containers\drivers\` |
| ISO builds | online 1.6 GB and offline 10.7 GB; contents and checksums verified, installer files identical to the repository, Windows ISO inside has the same SHA-256 as the source, shim hash pinned | `docker/verify-iso.sh` output |
| Booting the ISO as a **disk** (the USB case) | virtio disk, kernel command line as shipped | `/run/archiso/bootmnt` stays mounted |

## Defects found by testing, and their state

| Defect | Consequence | State |
|---|---|---|
| `fstab` written by `genfstab` used `/dev/...` and copied the live system's swap | first boot fell into emergency mode | fixed (hand-written, UUIDs only), verified |
| GRUB image lacked the `bli` module | "prohibited by secure boot policy" at every boot under shim | fixed, verified |
| Arch's `sbat.csv` older than the GRUB build | shim could refuse GRUB | fixed, verified |
| The signed shim corrupted inside the ISO (CRLF conversion of a binary) | shim path would fail | fixed, hash checked at build and run time |
| Windows Setup chain-loaded from GRUB fails (`0xc000000f`) | no Windows install | fixed: a firmware boot entry starts it, verified |
| `sbctl` path does not boot without a TPM | GRUB: "verification requested but nobody cares" | fixed: sbctl only with Setup Mode **and** a TPM, otherwise shim |
| The blue MOK screen drops the request after 10 s without a key | key never enrolled | mitigated: a service re-queues it at every boot; verified |
| archiso unmounts the USB stick (`copytoram=auto`) | bundled Windows ISO unreachable | fixed (`copytoram=n` + label fallback), verified |
| Back navigation lost the choice made on the screen | Summary showed another choice | fixed, verified in the VM wizard |
| `pacstrap` failed once on a slow mirror and aborted the install | install failed after the disk was erased | fixed (3 attempts), unit test; no network failure was simulated |
| GRUB moved first in the boot order too early | with Secure Boot on, Windows' last automatic restart landed on shim's blue screen | fixed (a scheduled task does it at the next start), **verified in the VM** |
| `MOK_INSTRUCTIONS` text inexact | misleading advice | fixed |
| No Wi-Fi in the installed system | a laptop without Ethernet boots offline | fixed (profile written), verified |
| Windows Fast Startup left on | the Windows partition unusable (read-only) from Linux after "Shut down" | fixed, verified (read-write mount) |
| Windows installed with a generic key hides the firmware licence | not activated on a laptop sold with Windows | implemented (`slmgr`); verified with a **fake** key only |
| `paru-bin` built against another `libalpm` | paru installed but did not start | fixed (checks, falls back to source), verified |
| `curl --retry` does not retry a dropped connection | link source aborted after the disk was erased | fixed (`--retry-all-errors`), verified |
| `powerprofilesctl` missing `python-gobject` | the command crashed | fixed, see the final acceptance below |
| Wi-Fi screen stayed on "Not connected" after connecting | Next blocked until Scan was pressed | fixed, unit test fails without the fix; see the final acceptance |
| Ctrl+Q during the installation quit the wizard | half-installed disk | fixed (ignored while installing), verified in the VM |
| Caelestia dots hard-code `kb_layout = "us"` | the layout chosen in the wizard was ignored in the Hyprland session (QWERTY for a French user) | fixed (written in the file the dots never overwrite), verified in a VM session |
| Revo Shell hard-codes `kb_layout = "us,ara"` | same | fixed, verified in a VM session |
| Revo Shell's `install.sh`: five dead package names, absolute symlinks into its author's home (the macOS shell loaded nothing for any other user), exit code 0 when its own checks fail, 860 MB of wallpapers downloaded to keep six | the preset could not work, or was slow | wrapped: our package list, symlink repair, our own verification, the wallpaper sizes from GitHub's tree API; verified |
| MacShell: after a rollback and a successful retry, waybar and the network applet stayed next to MacShell | two bars at login | fixed (markers), verified on the real engine |
| MacShell: `bluez` (the Bluetooth daemon) not installed and no service enabled on a desktop PC | its Bluetooth menu did nothing | fixed (package and `Preset.services`) |
| Caelestia: the rollback only restored `~/.config/hypr` | fish, foot, GTK... files of the dots stayed | fixed (the whole backup is restored), verified: 0 differences |
| The yay and pacman caches stayed on disk | about 2.4 GB | fixed (emptied at the end) |
| `scripts/rebuild-all.ps1` passed `-SkipImage` as the output folder | the ISO landed in a folder named `-SkipImage` and the old one was verified | fixed |

Known and left alone (cosmetic): the SDDM greeter shows `us` until the first key is pressed, and
`root=UUID=...` appears twice on the kernel command line (same value).

## Final acceptance of the delivered ISOs (version 2)

The ISOs were built with `scripts
ebuild-all.ps1` (online and offline) and verified with `docker/verify-iso.sh`:
the installer files, presets included, were identical to the repository, and the Windows image inside the
offline ISO had the same SHA-256 as its source. The tests below ran **on those files** (or, where marked, on
the same installer code installed by the same installer steps).

| Test | Result |
|---|---|
| **Headless whole flow with Windows**, offline ISO, laptop answers, Secure Boot on and key not enrolled, TPM, NVMe | **first installation: 104 checks passed**, 1 test expectation corrected (the plan has 13 steps now, all `done`). Installer exit 0 in 2.5 min; Windows installs by itself and restarts on Windows (no blue screen) before the boot-order task puts GRUB first; MOK enrollment; Arch with `SecureBoot=1`; GRUB menu to the Windows lock screen; Fast Startup and hibernation off, no `hiberfil.sys`, the Windows partition mounts read-write after a shutdown. **Wi-Fi in Windows**: the profile and its script are staged, `archinstall-wifi.log` says the profile could not be added yet (a VM has no Wi-Fi adapter: `wlansvc` is not running), the retry task `ArchInstallWifi` exists (at startup and every 15 minutes for two days), the XML is readable only by SYSTEM and Administrators, and the passphrase is in no log. `dist\screens\e2e5\` |
| The real wizard, 14 screens, from the repository installer inside the VM (the same code as the ISO) | **745 checks, 0 failed** over 5 passes: the Extras screen (first-visit selection, live AUR note, Back/Next keeps an empty choice, minimal desktop, the desktop shells enabled online and disabled offline), two full installations with Windows staged, a laptop walk over simulated Wi-Fi, a real AUR build of `yay`, VSCodium and Postman. `dist	ui-screens\extras\` |
| Real installer in containers: developer tools and applications; MacShell, Revo Shell and Caelestia | all pass, including the faults injected into the real engine (see the table above) |
| The three desktop shells on a screen in a VM | see the table above; `dist\screens\preset-boot\` |
| Unit and wizard tests | **367 passed**, lint clean |

**Second installation over the first one** (the disk then holds a real Windows with a DriverStore): 63 checks
passed, 1 test expectation replaced. The installer read the Windows driver store, kept the one package matching
the machine's network card (`net1ic64.inf`, 819 KB, matched on `PCI\VEN_8086&DEV_10D3`), staged it under
`sources\$OEM$\$1\Drivers`; all 16 answer files list `C:\Drivers` as a driver path; `C:\Drivers` was present
during Setup, Windows processed the `PnpCustomizationsNonWinPE` component, and the finish script deleted the
folder. The rest of the chain (boot order task, Fast Startup off, Wi-Fi files, no leaked passphrase) behaved as in
the first installation. **Not proven**: Windows installing a driver that its ISO does not already contain (this
network driver is in the ISO, so Windows had nothing new to install), and the second installation's own MOK,
GRUB and Arch login phases (the same phases passed for the first one). `dist\screens\e2e5\`

## NOT verified

- **Windows installing a driver it does not already have**: the harvest, the staging, `DriverPaths` and the clean-up
  are proven (see above), but the only driver in the test was already in the Windows ISO. A real Wi-Fi adapter was
  never present in a test (a VM has none): the profile import (`netsh wlan add profile`) was not seen succeeding.

- **The desktop shells beyond a first screen**: the glass and blur effects, the lock screens (MacLock, Caelestia),
  Caelestia's launcher and dashboard, Revo Shell's own keybindings, and the Wi-Fi and Bluetooth panels (the VM has
  no adapter) were not exercised; a real GPU may behave differently from Mesa's software renderer. Revo Shell
  shows warnings about imports from its `ii` shell (not deployed), a broken guide popup, and two bars: it stays
  labelled experimental. MacShell's Dock looks for `hyprpaper` for its glass effect (not installed).

- **Any real hardware.** Firmwares differ; a real firmware may ignore `BootNext` (the code also
  puts the temporary entry first in the boot order, which is what worked in the VM), may drop boot
  entries (the fallback path is installed too), may not fall back to the next entry when shim
  fails, or may behave differently with a hardware TPM.
- **A real Intel VMD / RAID controller**: only the warning text is tested, with a faked `lspci`.
  Windows Setup ran to the end on a virtual NVMe disk, but never behind such a controller.
- **A real firmware Windows key**: `slmgr` was exercised with a fake key from a fake ACPI table.
- Hardware features: Bluetooth, touchpad, function keys, brightness, suspend and hibernation,
  HiDPI scaling, a hardware TPM.
- A real Hyprland session (the configuration validates and the greeter works, the desktop itself
  was not looked at), hybrid NVIDIA graphics at run time (the packages install).
- 802.1X (enterprise) Wi-Fi is not carried over to the installed system.
- Windows past the lock screen, other Windows languages, Windows with LUKS/btrfs/xfs in a full VM
  install (the container tests stage it; the VM runs used ext4).
- Windows activation with a real licence. Without a firmware key Windows is **not activated**.

## Things to know

- **No automatic download from Microsoft**, on purpose: the offline ISO, a local file, or a link
  you paste (see the README).
- The answer files on the `WINSETUP` partition hold the Windows password in Setup's obfuscated
  form (not encryption). They are deleted when that partition is removed or reformatted at the
  end of Windows Setup.
- Recommended flow: leave **Secure Boot off** in the firmware until the Secure Boot key is
  enrolled (the live ISO is unsigned anyway), then turn it on. If Secure Boot is on and the 10
  seconds were missed, shim refuses to start GRUB: turn it off, start Arch (it queues the
  enrollment again), restart and press a key in time.
- On a laptop in **RAID / Intel VMD mode** Windows Setup cannot see the disk: set the storage
  mode to AHCI in the firmware first (the wizard warns).
- The GRUB menu may list a few extra firmware entries ("EFI Firmware Setup", "BootManagerMenuApp").
- Building `paru` from the AUR (when the prebuilt package does not start) compiles Rust: several
  minutes on a laptop. The step never stops the installation.

## Test environment notes

- Docker Desktop keeps its virtual disk on the system drive by default, and the test volumes
  (`archinstall-live`, `archinstall-winimg`, `archinstall-tui2`, `archinstall-laptop`,
  `archinstall-laptop2`) hold ISO copies and disk images of 10-100 GB each. A full drive crashes Docker:
  keep room, or move the disk image (Docker Desktop > Settings > Resources > Advanced), and delete the
  test volumes with `docker volume rm` when they are no longer needed. `archinstall-pkgcache` is the
  package cache: worth keeping, it makes builds faster.
- IDEs and cleanup tools may empty a folder named `out\`: the ISOs are built into `dist\`.
- The QEMU/KVM runs that install Windows crashed with `KVM: entry failed, hardware error 0x0` four
  times over all the runs, mostly while Docker was busy with something else. It is a nested
  virtualization problem of the test machine, not of the product: the test harness resumes from its
  checkpoint.

## Where the tests are

| Test | Command |
|---|---|
| Unit and wizard tests | `cd installer; python -m pytest` |
| Install steps against a disk image, one variant | `.\scripts\test-install.ps1 -Strategy shim -Filesystem ext4 [-Luks -SeparateHome] [-Desktop hyprland] [-WindowsIso <iso>] [-Laptop] [-AurHelper paru] [-BootTest]` |
| Whole flow in a VM | `.\scripts\live-test.ps1 -E2e shim -IsoInVolume <iso> [-Laptop]` |
| Interactive wizard in a VM | `.\scripts\tui-e2e.ps1 -Iso dist\archinstall-offline-<date>-x86_64.iso [-Mode laptop]` |
| Build and verify the ISOs | `.\scripts\rebuild-all.ps1 -SkipImage -WindowsIso .\ISO\<your Windows ISO> [-OutDir <folder>]` |
