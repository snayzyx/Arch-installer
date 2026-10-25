# Architecture

This document records how the installer works and why. Each part says how far it was actually
verified; "implemented" never means "verified".

## Decisions

| Topic | Decision | Why |
|---|---|---|
| Build environment | Privileged `archlinux` Docker container running `mkarchiso` | `mkarchiso` only runs on Arch; Docker works from Windows |
| Installer | Python 3 + Textual (TUI), shipped in `/opt/archinstall` on the ISO | Complex logic (partitioning, XML generation) is easier in Python than in Bash |
| Windows image | The person installing provides it: bundled in the ISO (offline build), a local file, or a download link pasted in the wizard | Microsoft forbids redistribution, and its download service resists automation (see below) |
| Windows install method | Windows Setup (WinPE) driven by a generated answer file | More robust than applying the WIM by hand: Setup creates the boot files and handles drivers |
| Install order | Arch first, then the first restart boots Windows Setup | One pass through the wizard, and GRUB is already on the disk |
| Secure Boot | Automatic: sbctl custom keys if the firmware is in Setup Mode, otherwise shim + MOK | Works on firmware that cannot be put in Setup Mode |
| Arch installation | Online (`pacstrap` + `reflector` for the user's country) | No offline repository to maintain on the ISO |
| `fstab` | Written by the installer from UUIDs, not by `genfstab` | See [Lessons learned](#lessons-learned-from-running-it) |
| Swap | zram only | Nothing to size, no hibernation |
| Boot menu | Arch default, 5 s timeout, Windows and UEFI Firmware Settings entries | |
| Desktop | Hyprland session ready to use (SDDM, waybar, wofi, kitty, PipeWire, network applet), or minimal | |

## Boot of the live ISO

archiso `releng` profile + `profile/` overlay. `root` auto-logs in on tty1 and `.zlogin` runs
`archinstall-launcher`, which checks for UEFI and starts `python -m dualboot_installer`.

The live ISO itself is unsigned: it must be booted with Secure Boot disabled once. It was booted
in QEMU/UEFI virtual machines, also as a plain disk (the way a USB stick is seen), with the
kernel command line it ships (`copytoram=n`, so the boot medium stays mounted).

## Offline build (bundled Windows ISO)

`build.ps1 -WindowsIso <file>` builds a second flavour, `archinstall-offline-*.iso`, that carries
the Windows 11 ISO. Nothing else changes: the same installer, the same wizard. **Status: built and
verified** (`docker/verify-iso.sh`), and used for the end-to-end tests described below.

How it works (`docker/build-iso.sh`):

1. The Windows ISO is bind-mounted read-only into the container (no 9 GB copy).
2. The script **mounts it as UDF** (Windows ISOs are UDF; `bsdtar` and `xorriso` only see an ISO
   9660 stub holding a README), checks it holds `sources/install.wim` or `install.esd`, runs
   `wimlib-imagex info` and computes its SHA-256.
3. `dualboot_installer.windows_iso make-manifest` turns that into `windows/manifest.json`:
   file name, size, SHA-256 and, for each image, its index, language, build and **Edition ID**
   (`Core` = Home, `Professional` = Pro). The Edition ID is used rather than the image name
   because names are localized ("Famille" in French) while IDs are not.
4. Before `mkarchiso` runs, the ISO is bind-mounted into its ISO tree at `windows/Windows11.iso`.
   `mkarchiso` never wipes that tree, so `xorriso` picks the file up. It already uses
   `-iso-level 3`, which allows files larger than 4 GiB.
5. The image is written **inside the container**, checked with `bsdtar` (the Windows file must
   be listed with the right size), then copied to the shared output folder and re-verified with
   its SHA-256. `build.ps1` also fails if no new ISO appears in `dist\`.

The file lives in the ISO filesystem, **not** in `airootfs.sfs`: it is never compressed (it is
already compressed), never loaded in RAM, and the live system reads it from
`/run/archiso/bootmnt/windows/` on demand.

At run time `LiveSystem.embedded_windows()` reads the manifest. If its size does not match the
file, it is ignored.

Consequences to keep in mind:

- The image is about 10-11 GB; it needs a 16 GB drive and DD mode or Ventoy.
- Arch is still installed online. A fully offline install would also need a local package
  repository on the ISO; not done.
- Redistributing the image would redistribute Windows. It is for personal use only.

## The wizard

14 screens in `installer/dualboot_installer/screens/`, in this order:

`Welcome` → `Region` → `Network` → `Disk` → `Layout` → `Accounts` → `Storage` → `Software` → `Extras` →
`WindowsSource` → `WindowsAccount` → `SecureBoot` → `Summary` → `Install`

*Region comes first* because it applies the chosen keyboard layout to the live console
(`loadkeys`), so every password typed afterwards uses the right keys.

`WindowsSource` offers up to three sources, in this order (the first one is the default):

1. **Included in this installer**, only when the offline build's manifest is present. The
   language comes from the ISO (the picker is replaced by a fixed line) and the editions the ISO
   does not contain are greyed out.
2. **A file**: a path to an `.iso`. The hint says to mount the drive on `/media`: `/mnt` is where
   the new system is mounted, and the installer refuses to start if it is in use.
3. **A link**: an `https://` download link pasted by the user.

Whatever the source, the answer file uses the language found in the ISO's own metadata, so a
mismatch with the language picked in the wizard is corrected automatically.

The summary shows only the **host** of a download link: such links carry a token that works as a
password for the file.

All answers end up in one `InstallConfig` (`config.py`). Hardware access goes through the
`System` interface (`system/base.py`) with a `LiveSystem` and a simulated `DemoSystem`, which is
what makes the wizard testable on any machine.

Safety rules built into the wizard:

- The disk the installer booted from is detected (`/run/archiso/bootmnt`) and cannot be chosen.
- A real run requires typing the disk's device path on the summary screen.
- A real run refuses to start when the plan is incomplete (`plan.can_run_for_real`).
- Secrets are masked in logs (`InstallConfig.redacted()`, `Command.secret`).
- After a real, successful installation the last button reads "Reboot now" (`systemctl reboot`);
  after a dry run or a failure it reads "Exit".

`--partial` means **skip Windows**: Arch Linux and GRUB are installed, the Windows steps are not
run, and the Windows partition stays empty. The summary says so.

### Developer tools, applications and desktop shells (`Extras`, `extras.py`, `presets/`)

`extras.py` holds the data (developer tool groups, applications, the preset registry `DOTFILES`) and
small pure functions; the screen, the package lists and the installation steps all read it. The defaults
in `ArchConfig` are empty: nothing extra is installed unless the screen (or an answers file) asks, and the
screen preselects the `RECOMMENDED_*` lists on the first visit.

- **Developer tools and LibreWolf** are official packages: they join the `pacstrap` list.
- **VSCodium and Postman** are AUR packages: the AUR step installs `yay` (prebuilt `yay-bin`; `paru-bin`
  does not start against the current pacman, so `yay` is the fallback helper) and runs `yay -S` as the new
  user, with a temporary `NOPASSWD` sudoers file that is removed afterwards. Never fatal. The yay and pacman
  caches are emptied at the end.
- **Desktop shell presets** (`presets/<id>/`: shell scripts, a Lua base config, `packages-official.txt`) are
  installed by `steps/dotfiles.py`. The scripts are copied into the new system and run there as the new
  user under `timeout`. They **follow the latest commit** of each project (the owner's requirement), which
  is why the choice needs a connection and why there are three safeguards: (1) `check_sources` runs in the
  preflight, before the disk is erased (a `curl` of each repository's `info/refs` — there is no git on the
  live system — and the AUR RPC for each AUR package); (2) each script verifies that the files and
  options it relies on still exist and exits with a distinct code; a failure runs the rollback script and
  retries with the commits that were tested (`Preset.fallback_env`); (3) if that fails too, the default
  Hyprland setup is restored and the installation goes on. Each script prints a `RECIPE-RESULT` line with the
  installed commit.
- Hyprland 0.56 reads `hyprland.lua` before `hyprland.conf`. MacShell needs the Lua form, so for that preset
  the default setup is written as `hyprland.lua` (from `presets/macshell/hyprland-base.lua`, with the
  keyboard layout filled in); Caelestia and Revo Shell deploy their own `hyprland.lua` and our
  `hyprland.conf` becomes inert (it is backed up by them and restored by the rollback).
- What was learned from running the recipes: a rolling `quickshell-git` is unavoidable; `caelestia install`
  refuses `-git` shell and CLI packages (the dots' manifest names the stable ones); `pipewire-jack` must be
  installed before any AUR build or `libcava` pulls `jack2`; Revo Shell's own `install.sh` lists five package
  names that no longer exist, ships absolute symlinks into its author's home and exits 0 when its own checks
  fail, so the preset wraps it and verifies the result itself; none of these shells can start in a container
  (no GPU), so they were checked with `Hyprland --verify-config` and, for the shells, loaded on a headless
  software-rendered compositor.

### Windows and the Wi-Fi network

The network used during the installation is added to the new Windows too: `wifi_profile_xml` builds a
WPA2-Personal (or open) WLAN profile, staged with the end-of-Setup scripts; `archinstall-wifi.ps1` runs
`netsh wlan add profile ... user=all` as SYSTEM, locks the clear-text file with `icacls` (SYSTEM and
Administrators), and deletes it once added. A laptop whose Wi-Fi driver only arrives later gets a scheduled
task that retries every 15 minutes for two days (and gives up and deletes the file after three).

## Disk layout

The whole disk is erased and re-created as GPT. Sizes are whole MiB, aligned to 1 MiB. The disk
gets a fresh **GPT GUID** (`sgdisk --disk-guid`), which the Windows launcher uses to find it.

| # | Partition | Filesystem | Notes |
|---|---|---|---|
| 1 | ESP, 1 GiB | FAT32 | Mounted on `/boot`: GRUB, signed kernels, Windows boot files |
| 2 | Windows | NTFS | Left unformatted by Linux; Windows Setup formats it |
| 3 | Arch root | ext4 / btrfs / xfs, optionally in LUKS2 | Takes all remaining space unless `/home` is separate |
| 4 | Arch `/home` | same | Optional; takes the remaining space |
| last | `WINSETUP` / Shared | FAT32 then NTFS | See below |

`/boot` is not encrypted, so the signed GRUB and Secure Boot keep working with LUKS.

**The last partition.** UEFI firmware can only boot WinPE from FAT32, and `install.wim` is larger
than 4 GiB, so the Windows Setup files go on a FAT32 partition labelled `WINSETUP` (12 GiB
minimum). When Windows Setup is done, a script reformats it as the shared NTFS partition, or
deletes it if no shared partition was asked for.

## Installation steps

Defined in `plan.py`: an ordered list of steps, each a function of a `Context` (answers, layout,
runner, and a `state` dict to hand values to a later step). The runner records every command and
file written, which is what the unit tests and the dry run inspect.

| Step | Does | Verified |
|---|---|---|
| `preflight` | Tools present, `/mnt` and the disk not in use, stale encrypted volumes closed, shim downloaded and checked, Windows source checked, package names exist | Container |
| `partition`, `format`, `mount` | `sgdisk`, `mkfs.*`, LUKS2, btrfs subvolumes | Container (many scenarios); NVMe names in a VM |
| `mirrors`, `base`, `configure` | `reflector`, `pacstrap` (3 attempts), `fstab`, locale, users, zram, Hyprland, services, laptop extras, the Wi-Fi profile | Container and VM |
| `bootloader` | GRUB, Secure Boot signing, firmware boot entry, MOK queue | Container for the files; VM boots under enforced Secure Boot (shim and sbctl); writes to a **real** firmware never run |
| `aur` | Builds paru or yay (prebuilt package first, source package if it does not start); a failure only warns | Container (paru, yay) |
| `windows-iso` | Downloads the link to the new root partition (resumable), inspects the ISO | Container (HTTPS link, 9 GB, connection cut midway); bundled ISO in VMs |
| `windows-stage` | Prepares Windows Setup on `WINSETUP`, arranges the first boot | Container and VM (full unattended install, dual boot) |
| `finish` | Unmounts (with retries), closes encrypted volumes | Container |

### Windows 11

1. **Check, before anything is erased.** A download link gets a HEAD request (HTTP 200, at least
   3 GiB, the links from Microsoft last about 24 hours). A file or the bundled ISO is mounted
   (UDF), its images are read with `wimlib-imagex info`, and the chosen edition must exist.
2. **Get the ISO.** A link is downloaded with a resumable `curl` to the new Arch root partition,
   never to RAM.
3. **Stage Setup** on `WINSETUP`: copy the ISO's files, then split `install.wim` into
   `install.swm` parts under 4 GiB (a stock Setup reads them).
4. **Launcher.** `boot.wim`'s Setup image gets a `winpeshl.ini` and `archinstall.cmd`. The script
   runs `wpeinit`, looks through disks 0-15 with `diskpart` for the one whose GPT GUID matches
   (Windows numbers disks differently from Linux), finds the matching `archinstall\unattend-N.xml`
   on any drive and starts `setup.exe /unattend:`. If no disk matches it stops with a message
   and has not changed anything.
5. **Answer files**: sixteen copies, one per possible disk number. They skip the TPM, Secure Boot,
   RAM, CPU and storage checks (`LabConfig`), format partition 2 as `C:`, install the chosen
   image by index with the public generic key of the edition (this selects the edition; it does
   not activate Windows), create a local administrator account with no Microsoft account, and
   set the computer name, time zone, language and keyboard. At the `specialize` pass they apply
   `RealTimeIsUniversal`, no automatic BitLocker, reduced telemetry and consumer features.
   Passwords use the answer file's obfuscated form (`PlainText=false`), which is **not
   encryption**: the files stay on `WINSETUP` until it is wiped at the end.
6. **First boot.** A temporary firmware boot entry, *Windows 11 Setup*, points to Microsoft's
   `\EFI\BOOT\BOOTX64.EFI` on `WINSETUP`. It is created with `efibootmgr --create` (so it is
   **first in the boot order**) and also set with `--bootnext`. It must be the firmware that
   starts it: chain-loaded from GRUB, Microsoft's boot manager looks for its BCD on the ESP and
   stops with `0xc000000f` (seen in the QEMU test; an earlier design used a GRUB entry and
   `grub-reboot`, which does not work). `BootNext` alone is not enough: the test firmware (OVMF)
   ignored it, and only an entry first in the boot order was proven to start Setup. If the
   machine starts Arch Linux anyway, the firmware boot menu (F12, F11 or Esc) offers the entry.
7. **End of Windows Setup.** `SetupComplete.cmd` (copied through `sources\$OEM$`) runs a
   PowerShell script that removes the preinstalled apps, deletes the temporary boot entry, puts
   GRUB first in the firmware boot order again (`bcdedit /set {fwbootmgr} displayorder ...
   /addfirst`, the entry found by its `ArchInstall` name because `bcdedit` labels are
   translated), and either formats `WINSETUP` as the shared NTFS partition or deletes it.
   Every action leaves a line in `C:\Windows\Temp\archinstall-finish.log`.

After installation, the *Windows 11* GRUB entry chainloads `\EFI\Microsoft\Boot\bootmgfw.efi`
on the ESP, and appears only once that file exists.

### Why there is no automatic Microsoft download

Microsoft's download service is built to resist automation: it uses sessions, rate limits and
bans, and it changes often. This project does not work around those protections, and such an
integration would break regularly. The user gets the link in a browser, as any user does, or
brings the ISO. A bundled ISO (offline build) is the most reliable path.

### Secure Boot

- **sbctl** (firmware in Setup Mode **and a TPM present**): create keys, enroll them together
  with Microsoft's keys (`sbctl enroll-keys --microsoft`, so Windows and option ROMs still load),
  sign GRUB and the kernel, keep them signed through the sbctl pacman hook. GRUB is installed
  with `--disable-shim-lock`. The TPM is required: GRUB's `tpm` module registers the verifier that
  lets GRUB start the kernel under Secure Boot only when it finds a TPM; without one GRUB fails
  with `verification requested but nobody cares` (seen in a UEFI VM). The automatic choice
  therefore picks sbctl only when both conditions hold, and a forced sbctl is refused otherwise.
- **shim + MOK** (everything else): install Microsoft's signed shim (Fedora's package, pinned by
  SHA-256, embedded in the ISO at build time), sign GRUB and the kernel with a generated key,
  queue its enrollment with `mokutil --import` using the password chosen in the wizard. MokManager
  (the blue screen) **cancels the request if no key is pressed within 10 seconds**. Because the
  first restart goes straight to Windows Setup, that screen usually appears when the user is away,
  so a service on the new system (`archinstall-mok-enroll`, using a stored hash of the password,
  never the password) queues the request again at every boot until `mokutil --test-key` says the
  key is enrolled.

The password is restricted to letters and digits because MokManager reads it with the firmware's
own US keyboard layout.

A pacman hook and `/usr/local/sbin/archinstall-secureboot` reinstall GRUB, re-sign it and the
kernel, and regenerate the menu after kernel or GRUB updates.

**What was verified**

- shim + MOK: booted in QEMU with OVMF firmware that enforces Secure Boot (Microsoft keys in the
  firmware, our certificate in the MOK list, pre-enrolled with `virt-fw-vars`). The installed
  system reaches the login prompt, shim hands our key to the kernel, and the Windows entry shows
  in the menu when a Windows boot manager is present. Negative controls: an unsigned kernel does
  not boot, and without the MOK enrollment shim refuses GRUB.
- sbctl: the install and every signature are checked in a container with `sbverify`. The
  **boot under enforced Secure Boot is not verified**: the QEMU test failed in the firmware with
  `Access Denied`, before reaching GRUB. The cause is not established. The way the test
  pre-enrolls the keys is the first suspect, but that is a guess.
- Not verified at all: writes to a real firmware (`efibootmgr`, `mokutil --import`,
  `sbctl enroll-keys`); a container has no firmware.

Known limitation: with GRUB, the initramfs and `grub.cfg` are not verified by Secure Boot.

### Laptops

Detected from the hardware, no extra question: a battery in `/sys/class/power_supply` or an
SMBIOS chassis type of portable/laptop/notebook/convertible/detachable (`system/hardware.py`). An
answers file can force it (`arch.laptop`).

- **Arch.** `power-profiles-daemon` (with `python-gobject`, which its `powerprofilesctl` needs),
  `bluez`, `bluez-utils`, `upower`, `sof-firmware`; `bluetooth` and `power-profiles-daemon`
  enabled; `nvidia-prime` when an Intel or AMD GPU is selected next to NVIDIA. Hyprland gets the
  function keys (volume, microphone, brightness), tap-to-click, disable-while-typing and an
  automatic display scale.
- **Wi-Fi kept.** The network used during the installation (open or WPA-PSK) is written as a
  NetworkManager profile (mode 0600) in the new system, otherwise a laptop without Ethernet boots
  offline. 802.1X networks cannot be rebuilt from a passphrase and are not carried over.
- **Windows.** Fast Startup and hibernation are turned off (registry values at setup time plus
  `powercfg /hibernate off`): with Fast Startup, "Shut down" leaves the NTFS volume in a saved
  state, Linux then refuses to write to it and writing anyway would corrupt it. The licence key
  stored in the firmware, if any, is installed with `slmgr` (the key is never logged); the
  generic key used for the unattended setup would otherwise hide it.
- **Warnings on the first screen** (never blocking): running on battery, and a storage controller
  in Intel VMD / RAID mode that Windows Setup has no driver for (the disk would be invisible to it).
- **Console font** chosen from the framebuffer width (4K laptop panels).
- The firmware fallback path `\EFI\BOOT\BOOTX64.EFI` is installed too, for firmwares that drop
  boot entries created with `efibootmgr`.

### Lessons learned from running it

These are defects the real runs exposed. They are the reason this project insists on running
things.

- **Under shim, GRUB cannot load anything from disk.** Every module `grub.cfg` loads must be
  built into the signed image. `bli` (GRUB 2.12+) was missing and made GRUB print `prohibited by
  secure boot policy` on each boot. A probe showed the policy also refuses `loadfont` and
  `insmod` of modules that are not built in, while `search --fs-uuid`, `[ -e ]` and `load_env`
  are allowed. (`search --file` is allowed too: an earlier guess to the contrary was wrong.)
- **`genfstab` was replaced by a hand-written `fstab`.** It fell back to `/dev/loop0p1` when a
  UUID could not be resolved, and it copies the live system's active swap. Either made systemd
  wait forever for a device and drop into emergency mode.
- **Arch's `sbat.csv` lags behind GRUB.** It says generation 4 while GRUB 2.16 expects 6, and a
  low generation can be refused by shim's revocation list. The signing script uses the level GRUB
  itself reports.
- **`umount -R /mnt` failed intermittently.** `pacstrap` starts a `gpg-agent` that holds the
  mount; it needs a moment to exit after being told to, so the unmount is retried.
- **A failed attempt left `cryptroot` open** and the next run died with "device already exists".
  The preflight now closes stale volumes.
- **`locale-gen` exits 0 even when it cannot build the locale**, so the result is checked.
- **GRUB must not go first in the boot order while Windows still restarts by itself.** With
  Secure Boot on, that restart landed on shim's blue key-enrollment screen with nobody to press a
  key and Windows could not finish. A scheduled task changes the order at the next Windows start.
- **`paru-bin` from the AUR was built against another `libalpm`** than the installed pacman and
  did not start, although `makepkg` reported success: the helper is now run once to check it.
- **`curl --retry` does not retry a dropped connection** (exit 56); `--retry-all-errors` does.
- **`powerprofilesctl` needs `python-gobject`**, which the daemon's package does not depend on.
- **The Wi-Fi screen kept saying "Not connected"** after a successful connection: the check ran
  before DHCP had finished. It is repeated for a while after a connection.
- **The Windows ISO is UDF**, which `bsdtar` and `xorriso` cannot read: it is mounted.

## Testing

- `installer/tests/`: 254 unit tests (layout, validators, plan, fstab, bootloader scripts, Windows
  answer files and launcher, laptop detection and extras, data, Secure Boot detection) and wizard
  tests driven by Textual's pilot in demo mode. They never run a real installation: the tests that
  reach the install screen stub out the execution.
- `scripts/test-install.ps1`: runs the real steps against a 110 GB sparse image in a privileged
  container, mounts the result again and checks it. Options cover the Secure Boot path, the file
  system, LUKS, a separate `/home`, a shared NTFS partition, the Windows ISO (embedded or an HTTPS
  link), the AUR helper, and the laptop profile. `-BootTest` also boots the image in QEMU.
- `scripts/live-test.ps1` and `scripts/integration/live-*.py`: the whole chain in a QEMU/KVM UEFI
  VM with enforced Secure Boot, a virtual TPM and an NVMe disk: installer, Windows Setup, key
  enrollment, GRUB, Windows, Arch (`-E2e shim`, `-E2e sbctl`, `-Laptop`).
- `scripts/tui-e2e.ps1`: the real wizard driven key by key in a VM (`-Mode laptop` adds a
  Wi-Fi-only network, simulated battery and storage controller).
- **Not covered**: real hardware and real firmware (see `RESULTS.md`).

## Risks and open points

- **Real hardware.** Firmwares differ: one may ignore `BootNext`, drop boot entries, or not fall
  back to the next entry when shim refuses to start; a hardware TPM may behave differently. The
  code puts the temporary Windows Setup entry first in the boot order and installs the fallback
  path for this reason, but only OVMF was tried.
- **Writes to a real firmware** (boot entries, MOK queue, key enrollment) have only been
  exercised in a VM.
- The 10-second MOK enrollment window is short for a person; a service re-queues the request until
  it is done, and a missed window with Secure Boot on needs Secure Boot turned off once.
- NVIDIA: the open kernel modules need a Turing or newer GPU; older cards use nouveau. Hybrid
  graphics at run time are untested (only the packages install).
- The Linux console is limited in colors and has no mouse; the wizard is keyboard-only by design.
- Cosmetic: the SDDM greeter label says `us` until the first key is pressed (the layout in use is
  the right one); `root=UUID=...` appears twice on the kernel command line (same value).
