# Arch-installer

**Install Arch Linux and Windows 11 side by side on one disk, from a single bootable USB stick,
with Secure Boot left enabled.**

You boot the stick, answer a few questions in a text wizard, and the installer does the rest: it
partitions the disk, installs Arch Linux with a signed GRUB, installs Windows 11 unattended from
your own ISO, and leaves you with a boot menu that lists both systems.

> [!WARNING]
> **Alpha software. The disk you choose is erased completely.**
> Everything was tested in virtual machines (QEMU/KVM with UEFI firmware, Secure Boot and a virtual
> TPM) and in containers. It has **not** been validated on a wide range of real hardware, and
> firmwares differ. Back up your data, read [docs/FIRST-INSTALL.md](docs/FIRST-INSTALL.md) before
> the first run, and prefer a spare disk, or `--partial` (Arch Linux only), for a first try.
> [docs/RESULTS.md](docs/RESULTS.md) states exactly what was and was not verified.

## Contents

- [What you get](#what-you-get)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [The wizard, screen by screen](#the-wizard-screen-by-screen)
- [Developer tools, applications and desktop shells](#developer-tools-applications-and-desktop-shells)
- [Secure Boot](#secure-boot)
- [Where the Windows ISO comes from](#where-the-windows-iso-comes-from)
- [Disk layout](#disk-layout)
- [Laptops](#laptops)
- [Command line](#command-line)
- [Troubleshooting](#troubleshooting)
- [Questions](#questions)
- [Status and testing](#status-and-testing)
- [Repository layout](#repository-layout)
- [Reporting a problem](#reporting-a-problem)
- [License and trademarks](#license-and-trademarks)

## What you get

**Arch Linux**
- Root file system of your choice (ext4, btrfs with subvolumes, xfs), optional **LUKS2** encryption,
  optional separate `/home`, swap on zram.
- Kernel: `linux`, `linux-lts` or `linux-zen`. Graphics drivers detected and pre-selected (Intel,
  AMD, NVIDIA open modules or nouveau, virtual machine guests). Microcode for your CPU.
- Either a **ready-to-use Hyprland desktop** (SDDM login, waybar, wofi, kitty, PipeWire,
  NetworkManager applet, function keys, touchpad settings) or a minimal system with no desktop.
- Optional AUR helper (`paru` or `yay`) and any extra packages you name.
- **Developer tools**, ticked by default: git, Node.js (npm, pnpm), Python (pip, pipx, virtualenv), Go, Rust,
  cmake, ninja, gdb, GitHub CLI, ripgrep, fd, fzf, tmux, neovim, httpie, and optionally Docker.
- **Applications** with the Hyprland desktop, ticked by default: **VSCodium**, **LibreWolf** and **Postman**.
- A choice of **desktop shells (dotfiles)**: MacShell + MacOS-Like icons, Revo Shell, or Caelestia, always
  downloaded in their latest version while installing. See [Developer tools, applications and desktop
  shells](#developer-tools-applications-and-desktop-shells).
- Region, locale, keyboard layout, host name, user (with `sudo`) and root password.

**Windows 11**, installed unattended by Windows Setup from **your own ISO**
- Edition (Pro or Home), language of the ISO, local administrator account, computer name, time
  zone and keyboard.
- Always applied, because a dual boot needs them: the clock fix (`RealTimeIsUniversal`), BitLocker
  / device encryption off, **Fast Startup and hibernation off** (otherwise Linux cannot write to the
  Windows disk), a small debloat, reduced telemetry, and the TPM / RAM / CPU requirement checks
  bypassed (unsupported by Microsoft).
- If the firmware holds a Windows licence key (most laptops sold with Windows do), it is used.
- The **Wi-Fi network** you used for the installation is added to Windows too (WPA2-Personal or open).

**Boot**
- A **signed GRUB** with Arch Linux as the default entry (5 seconds) and a Windows 11 entry.
- **Secure Boot keeps working**: two methods, chosen for you. See [Secure Boot](#secure-boot).
- Optional **shared NTFS partition** that both systems can read and write.

**Laptops** are detected automatically: see [Laptops](#laptops).

## How it works

1. You **build the ISO** on Windows with Docker; your Windows 11 ISO is bundled into it.
2. You write it to a USB stick and boot it. The wizard starts by itself.
3. You answer the wizard (14 screens) and confirm the summary, which names the disk to erase.
4. The installer erases the disk, installs Arch Linux and GRUB, prepares Windows Setup on a
   dedicated partition, and makes the **first restart** start Windows Setup.
5. Windows installs itself and restarts several times. A scheduled task then puts GRUB back at the
   top of the firmware boot order.
6. One more restart shows a **one-time key enrollment screen** (Secure Boot), then the boot menu.

No step needs a keyboard from you between the end of the Arch installation and that last screen.

## Requirements

| What | Detail |
|---|---|
| Computer | **UEFI** PC (legacy BIOS is not supported). A disk of **128 GiB or more**. |
| Storage mode | **AHCI** (or plain NVMe). Intel RST / VMD / "RAID" mode hides the disk from Windows Setup: the installer warns, switch it in the firmware first. |
| USB stick | **16 GB or more** (the offline ISO is about 10.7 GB). |
| Windows | A Windows 11 x64 ISO, from <https://www.microsoft.com/software-download/windows11>. |
| To build | **Windows with Docker Desktop**, PowerShell, and at least 40 GB of free disk space. |
| Network | Internet access during the installation: Arch packages are downloaded. Wi-Fi works. |

## Quick start

**1. Build the ISO.** With Docker Desktop running, in PowerShell from the repository folder:

```powershell
.\build.ps1 -WindowsIso .\ISO\Windows11_Client_x64_en-us.iso
```

This writes `dist\archinstall-offline-<date>-x86_64.iso` (about 10.7 GB) and its `.sha256`. The first
build also builds the Docker builder image and downloads packages, so it takes longer; later builds
can add `-SkipImage`. Without `-WindowsIso` you get a 1.6 GB ISO that asks for Windows by file or link
during the installation.

To build **and verify** (installer identical to the repository, pinned signed shim, Windows image
intact, boot lines) in one go:

```powershell
.\scripts\rebuild-all.ps1 -SkipImage -WindowsIso .\ISO\Windows11_Client_x64_en-us.iso
```

**2. Write it to a USB stick.** Use [Rufus](https://rufus.ie) in **DD Image mode** (not ISO mode), or
copy the file to a [Ventoy](https://www.ventoy.net) stick. On Linux or macOS:
`sudo dd if=archinstall-offline-*.iso of=/dev/sdX bs=4M status=progress conv=fsync` (check the
device name twice).

**3. Prepare the firmware.** UEFI mode, storage in AHCI, and **Secure Boot off for now**: the live
ISO itself is not signed. On a laptop, plug in the charger.

**4. Boot the stick and answer the wizard.** The summary names the disk that will be erased and asks
you to type it. When the installation ends, press **Reboot now** and remove the stick.

**5. Let Windows install** (15-30 minutes, several restarts, no keyboard needed). At the blue
key-enrollment screen you have **10 seconds** to press a key, then choose *Enroll MOK*, *Continue*,
*Yes*, type the password you picked in the wizard, and *Reboot*. Finally turn **Secure Boot back on**
in the firmware. More detail and recovery steps: [docs/FIRST-INSTALL.md](docs/FIRST-INSTALL.md).

## The wizard, screen by screen

| # | Screen | What it asks or does |
|---|---|---|
| 1 | Welcome | Checks UEFI, memory, TPM, Secure Boot state, the bundled Windows image; warns about RAID/VMD storage and battery power |
| 2 | Region | Country, which sets the time zone, locale and keyboard layout (changeable) |
| 3 | Network | Shows the connection; scans and connects to Wi-Fi |
| 4 | Disk | The target disk (the stick you booted from is refused) |
| 5 | Layout | Windows partition size, optional separate `/home`, optional shared NTFS partition |
| 6 | Accounts | Host name, user, passwords, root |
| 7 | Storage | Root file system, optional LUKS2 encryption |
| 8 | Software | Kernel, graphics drivers, desktop, AUR helper, extra packages |
| 9 | Extras | Developer tools, applications (VSCodium, LibreWolf, Postman) and a desktop shell (dotfiles) |
| 10 | Windows source | Bundled ISO, a file, or a link; edition |
| 11 | Windows account | Computer name, local account, optional separate time zone and keyboard |
| 12 | Secure Boot | Method (Automatic is right for almost everyone) and the key enrollment password |
| 13 | Summary | Everything in one place; confirm by typing the disk |
| 14 | Installation | Live progress; Esc, Ctrl+C and Ctrl+Q are ignored while it runs |

`Esc` goes back one screen and keeps what you entered.

## Developer tools, applications and desktop shells

Screen 9 offers three things, all optional:

- **Developer tools**: *Languages and build tools* (git, Node.js with npm and pnpm, Python with pip, pipx
  and virtualenv, Go, Rust, cmake, ninja, gdb), *Command line helpers* (GitHub CLI, ssh, jq, ripgrep, fd,
  fzf, tmux, neovim, httpie) and *Docker* (you join the `docker` group, which is equivalent to root access;
  off by default). They come from the Arch repositories and are installed with the system.
- **Applications** (Hyprland desktop only): VSCodium and Postman are built from the AUR on the installed
  system (`yay` is installed for that, a few minutes; a failure only prints a warning), LibreWolf comes from
  the Arch repositories.
- **Desktop shell (dotfiles)** (Hyprland desktop only), one of:

| Preset | What it is | Extra time and disk on a laptop (estimates) |
|---|---|---|
| Default Hyprland setup | waybar, wofi, kitty: plain and light | none |
| **MacShell + MacOS-Like icons** | a macOS-style menu bar, Dock and lock screen ([MacShell](https://github.com/0-ss/MacShell)) with macOS-like icons ([MacOS-Like](https://github.com/Scrimas/MacOS-Like)) | 5-10 minutes, about 1 GB |
| **Revo Shell** (experimental) | several Quickshell shells, macOS-like by default ([revo-shell](https://github.com/vzbc/revo-shell)) | 15-25 minutes, about 5 GB |
| **Caelestia** | a fluid, themed shell with bar, launcher, dashboard and lock screen ([caelestia-dots](https://github.com/caelestia-dots)) | 15-30 minutes, about 2 GB |

How the desktop shells are installed, and why:

- **Always the latest version, so only with a connection.** Nothing of them is stored in the ISO but a small
  install script each: the projects are downloaded from GitHub and the AUR while installing. The choice is
  disabled when the machine is offline.
- **Checked before anything is erased.** The installer first makes sure that every source answers; if one
  does not, it stops with "Nothing was changed".
- **Safeguards, because the latest version of a third-party project can break.** Each script checks that the
  files and options it relies on still exist. If the latest version fails, the preset is rolled back and
  retried with the versions that were tested; if that fails too, the default Hyprland setup comes back and
  the installation goes on. The installed commit is written in the installation log.
- **These are third-party projects**, under their own licenses: MacShell is MIT; MacOS-Like, MacTahoe and
  Caelestia are GPL-3.0; Revo Shell has **no license file** (it is only cloned on your machine, never
  redistributed here). MacShell contacts online services at run time (weather, location from your IP
  address, stock prices, lyrics) and imitates macOS without any link to Apple. Revo Shell loads many
  components and starts a "performance" power profile in its own configuration (removed by this installer).
- **What was checked:** each preset was installed by the real installer and then seen running on a screen in a
  virtual machine (software rendering, keyboard layout correct); the glass and blur effects, the lock screens
  and the Wi-Fi and Bluetooth panels were not exercised, and nothing was tried on a real GPU. Revo Shell prints
  warnings and shows two bars, which is why it is labelled experimental.
- Hyprland, Quickshell and these projects move fast: a preset that worked when this was written may need an
  update later.

## Secure Boot

The installed GRUB and kernel are signed, so Secure Boot can stay on. There are two methods; the
wizard's **Automatic** choice picks for you.

| Method | Needs | How it works |
|---|---|---|
| **shim + MOK** | Nothing special | GRUB and the kernel are signed with a new key. Microsoft's signed `shim` loads them. You enroll the key once, at a blue screen after a restart. Works on firmware that cannot be put in Setup Mode. |
| **sbctl** | Firmware in **Setup Mode** (Secure Boot keys cleared) **and a TPM** | The installer creates its own keys, enrolls them (keeping Microsoft's) and signs everything. No blue screen. |
| **Automatic** | | sbctl when the firmware is in Setup Mode with a TPM, shim + MOK otherwise. |

A normal laptop ships with Secure Boot keys installed, so it is **not** in Setup Mode: choose
Automatic or shim + MOK. The wizard refuses sbctl when its requirements are not met.

## Where the Windows ISO comes from

| Source | When |
|---|---|
| **Included in this installer** | You built the offline ISO. The language and editions come from it. |
| **A file you already have** | A Windows 11 ISO on a USB drive or another partition; mount it first, for example `mount /dev/sdX1 /media`, then give its path. |
| **A link you paste** | Choose the language on the Microsoft page above, copy the download link (valid about 24 hours) and paste it into the wizard. A dropped connection is retried and resumed. |

**There is no automatic download from Microsoft, on purpose.** Microsoft's download service is
built to resist automation and changes often; this project does not work around those protections.
Windows Setup uses the language of the ISO itself.

## Disk layout

The chosen disk gets a GPT:

| # | Partition | Notes |
|---|---|---|
| 1 | EFI system partition, 1 GiB, FAT32, mounted on `/boot` | GRUB, shim, kernels, Windows boot files |
| 2 | Windows (the size you pick) | Formatted by Windows Setup |
| 3 | Arch Linux root | ext4, btrfs (`@` and `@home` subvolumes) or xfs, optionally in LUKS2 |
| 4 | `/home` (optional) | Separate file system, optionally encrypted |
| last | `WINSETUP` | FAT32 holding the Windows Setup files during the installation. Reformatted as a shared NTFS partition if you asked for one, removed otherwise. |

All file systems are mounted by UUID. Swap is zram, so there is no swap partition and no
hibernation.

## Laptops

A laptop is detected from a battery or from the chassis type reported by the firmware. Then:

- **Power and hardware:** `power-profiles-daemon`, Bluetooth (`bluez`), `upower`, and the audio
  firmware most recent laptops need (`sof-firmware`). Hyprland gets the volume, microphone and
  brightness keys, tap-to-click, disable-while-typing and an automatic display scale. Hybrid
  Intel/AMD + NVIDIA graphics add `nvidia-prime`.
- **Wi-Fi:** the network you used during the installation is saved in the new system, so a laptop
  without an Ethernet port does not boot offline. Open and WPA-PSK networks are carried over;
  enterprise (802.1X) networks are not.
- **Windows:** Fast Startup and hibernation off, the firmware licence key used if there is one, and the Wi-Fi
  network of the installation added (open or WPA2-Personal; a WPA3-only or enterprise network has to be joined
  by hand once).
- **Windows drivers (Wi-Fi, network, Bluetooth):** a stock Windows ISO does not carry every Wi-Fi driver, and
  with no network Windows cannot download the missing one. Before the disk is erased, the installer reads the
  Windows that is already on it (read-only), keeps the packages that match this computer's network and
  Bluetooth adapters (up to 600 MB), and gives them to the new Windows, which installs them while it sets
  itself up. A factory Windows encrypted with BitLocker cannot be read: connect an Ethernet cable, or your
  phone by USB (tethering), once, and Windows Update installs the Wi-Fi driver. You can also bundle drivers
  in the ISO: `.\build.ps1 -DriversDir <folder of extracted driver packages>` (a folder with the `.inf`,
  `.cat` and `.sys` files; an `.exe` or `.msi` installer has to be extracted first).
- **Warnings on the first screen:** a RAID / Intel VMD storage mode (Windows Setup would not see the
  disk) and battery power.
- **Console font** scaled for 4K panels.

The installer does **not** change firmware (BIOS) settings such as Secure Boot, the storage mode or
the key database: the operating system cannot, so those stay manual.

## Command line

`python -m dualboot_installer` (on the live ISO: `archinstall-launcher`):

| Option | Effect |
|---|---|
| `--demo`, `--demo-offline` | Simulated hardware, nothing is executed; works on any OS. `--demo-offline` pretends a Windows ISO is bundled |
| `--dry-run` | Real hardware, but commands are only printed |
| `--partial` | **Skip Windows**: install Arch Linux and GRUB only; the Windows partition stays empty |
| `--answers FILE` | Run without the interface from a JSON file of answers (secrets in clear text) |
| `--confirm-erase DEVICE` | With `--answers`: the device that may be erased, typed again as a safety check |

Try the wizard on any OS without touching anything, and run the unit tests:

```powershell
cd installer
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m dualboot_installer --demo
.venv\Scripts\python -m pytest
```

## Troubleshooting

| Symptom | What to do |
|---|---|
| "The firmware is not in Setup Mode..." | You chose sbctl. Choose **Automatic** or **shim + MOK**. |
| The Network screen says "No Wi-Fi adapter found" | Check the Wi-Fi switch and `rfkill list` (Alt+F2 opens a login prompt on the live system: user `root`, empty password). Some Broadcom and Realtek chips need a driver that the live ISO does not include; use Ethernet or phone tethering. |
| Windows Setup does not start after the first restart | In the firmware boot order, "Windows 11 Setup" must come before "ArchInstall". |
| Blue screen, "Verification failed" at a boot | The key was not enrolled in time. Turn Secure Boot off, start Arch (it queues the enrollment again), restart and press a key within 10 seconds. |
| Windows Setup sees no disk | The laptop is in RAID / Intel VMD mode: switch the storage mode to AHCI. |
| Windows is installed but has no Wi-Fi | Its driver was not in the Windows ISO, and no Windows was readable on the disk to take it from (or it was BitLocker-encrypted). Connect an Ethernet cable or your phone by USB (tethering) once: Windows Update installs it. Next time, bundle it: `build.ps1 -DriversDir`. The installer's log (`C:\Windows\Temp\archinstall-finish.log`) says which drivers were given to Windows. |
| An installation step fails | The log is `/var/log/archinstall.log` on the live system; run `archinstall-launcher` to start again. |
| The Windows partition is read-only from Linux | It should not happen (Fast Startup is off). A Windows that was hibernated by hand needs a full restart. |

## Questions

**Does it work without Secure Boot?** Yes: leave Secure Boot off; everything else is the same.

**Can I keep an existing Windows?** No. The installer erases the whole disk and installs a fresh
Windows. To keep an existing system, use another disk.

**Is Windows activated?** Only if the firmware holds a Windows key (most laptops sold with
Windows do). Otherwise Windows runs unactivated until you enter a key.

**Why is the Windows ISO not downloaded for me?** See [Where the Windows ISO comes
from](#where-the-windows-iso-comes-from).

**Where are the passwords stored?** The user and root passwords are set through `chpasswd` and
never logged. The Windows password sits, in Setup's obfuscated (not encrypted) form, in answer
files on the temporary `WINSETUP` partition, which is removed or reformatted at the end of Windows
Setup. An `--answers` file holds secrets in clear text: delete it afterwards.

**Can I share the offline ISO?** No: it contains Windows. Keep it for yourself, and never publish it.

## Status and testing

The complete flow passes in virtual machines for both Secure Boot methods: the wizard, Arch Linux,
Windows 11 installed unattended, key enrollment, and both systems booting from the GRUB menu, with an
NVMe disk, Wi-Fi only and simulated laptop sensors. **362 unit and wizard tests pass.** The real
wizard was also driven key by key in VMs (hundreds of checks), and the installation steps run for
real against disk images in containers.

**Real hardware is the open point**, and so are the firmware-specific behaviours of your machine.
The full list of what was verified, of the defects found and fixed, and of what is **not** verified
is in [docs/RESULTS.md](docs/RESULTS.md). The design and its reasons are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Every test script is described in
[scripts/README.md](scripts/README.md).

## Repository layout

| Path | Purpose |
|---|---|
| `build.ps1` | Builds the ISO in a Docker container |
| `docker/` | The builder image, `build-iso.sh` (runs `mkarchiso`), `verify-iso.sh` |
| `profile/` | Overlay on archiso's `releng` profile (packages, autostart, motd, launcher) |
| `installer/` | The installer (Python + Textual) and its tests |
| `scripts/` | Integration tests and the verified rebuild; see [scripts/README.md](scripts/README.md) |
| `docs/` | [ARCHITECTURE](docs/ARCHITECTURE.md), [FIRST-INSTALL](docs/FIRST-INSTALL.md), [RESULTS](docs/RESULTS.md) |
| `ISO/` | Your local Arch / Windows ISOs (git-ignored) |
| `dist/` | Build output and test evidence (git-ignored) |

The ISO files and the commands on the live system keep the historical `archinstall-` prefix
(`archinstall-offline-<date>-x86_64.iso`, `archinstall-launcher`).

## Reporting a problem

Open an issue with: the laptop or motherboard model and firmware version, the Secure Boot method and
the options you chose, what you saw (a photo of the screen is fine), and `/var/log/archinstall.log`
from the live system (passwords are not logged).

## License and trademarks

No license has been chosen yet, so all rights are reserved until a `LICENSE` file is added.

This is an independent project. It is not affiliated with or endorsed by the Arch Linux project or
by Microsoft. "Windows" is a trademark of Microsoft; "Arch Linux" is a trademark of its project. The
project never redistributes Windows: you supply your own ISO.
