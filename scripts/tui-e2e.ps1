<#
.SYNOPSIS
  Tests the REAL interactive wizard end to end: every screen is answered through a terminal emulator in a
  QEMU/KVM UEFI VM booted from the OFFLINE ISO (scripts\integration\tui-e2e.py).

.DESCRIPTION
  Needs Docker Desktop (with /dev/kvm in its VM) and the builder image (run build.ps1 once).
  The wizard is started in the live system, then driven over the serial console like a person would:
  valid and invalid inputs, Back navigation, Escape and Ctrl+C, the whole install, "Reboot now". The VM
  is then powered off and the installed disk is inspected (partitions, GRUB signature, fstab, accounts,
  the staged Windows Setup and its answer files).

  The offline ISO (about 10 GiB) is copied once into the Docker volume archinstall-tui and checked against
  its .sha256 file; the copy is skipped when the volume already holds that exact ISO.
  Text copies of the screens go to dist\tui-screens.

.PARAMETER Iso
  The offline ISO. Default: the newest dist\archinstall-offline-*.iso.

.PARAMETER Mode
  run (default): the whole scenario, exit code 0 when every check passed.
  extras: the Extras screen and what the installation does with it: E-A Hyprland with the recommended defaults (dev tools,
         VSCodium, LibreWolf, Postman: yay is built, AUR packages installed), E-B minimal desktop with everything unticked.
         Windows is skipped (--partial). Evidence and timings go to -ScreensDir.
  laptop: a laptop-like walk (NVMe disk, Wi-Fi only through a simulated access point, fake battery and
         sensors, 4K font probe), then the installed system is booted and inspected from inside.
  quick: only what depends on how the ISO is booted (a few minutes, no install): the boot medium stays
         mounted, the Welcome screen lists the included Windows image, the Disk screen refuses the live medium.
  daemon: boot the VM and keep it for exploring by hand (docker exec tui-e2e python3 ... ctl ...).

.PARAMETER StopAtSummary
  Run the 12 screens up to the summary, but do not start the install (a quick check of the wizard itself).

.PARAMETER RepoInstaller
  Run the installer folder of this repository inside the VM instead of the installer of the ISO (the folder is copied
  into the live system as a read-only disk and bind-mounted over /opt/archinstall/dualboot_installer). For every
  mode; use it when the ISO is older than the code. Also known as -LaptopRepoInstaller.

.PARAMETER LaptopComplete
  For -Mode laptop: install Windows too (needs an offline ISO). Default: the wizard is started with --partial.

.PARAMETER LaptopDesktop
  For -Mode laptop: Minimal (default) or Hyprland.

.PARAMETER Volume
  Docker volume that holds the ISO copy and the VM's disk (default archinstall-tui). Use another one to run
  two passes at the same time.

.PARAMETER ScreensDir
  Where the text copies of the screens go (default dist\tui-screens).

.PARAMETER Name
  Name of the container (default tui-e2e).

.PARAMETER MemoryMiB
  RAM of the VM (default 6144).

.EXAMPLE
  .\scripts\tui-e2e.ps1
#>
[CmdletBinding()]
param(
  [string]$Iso,
  [ValidateSet('run', 'quick', 'daemon', 'laptop', 'extras')][string]$Mode = 'run',
  [switch]$StopAtSummary,
  [int]$MemoryMiB = 6144,
  [string]$Volume = 'archinstall-tui',
  [string]$ScreensDir,
  [string]$Name,
  [Alias('LaptopRepoInstaller')][switch]$RepoInstaller,
  [switch]$UseVolumeIso,
  [switch]$LaptopComplete,
  [string]$LaptopDesktop = 'Minimal'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Dist = Join-Path $Root 'dist'
# Not under out\: IDEs and cleanup tools may empty that folder.
$Screens = if ($ScreensDir) { $ScreensDir } else { Join-Path $Dist 'tui-screens' }
New-Item -ItemType Directory -Force -Path $Screens | Out-Null

# Native stderr must not become a PowerShell error (see build.ps1).
$previous = $ErrorActionPreference
$ErrorActionPreference = 'Continue'

if ($UseVolumeIso) {
  # The volume already holds a verified copy (it was checked when it was put there): no ISO file needed on the host.
  Write-Host "ISO: the copy already in the Docker volume $Volume"
  docker run --rm -v "${Volume}:/work" archinstall-builder bash -c "test -f /work/offline.iso && cat /work/offline.iso.sha256"
  if ($LASTEXITCODE -ne 0) { $ErrorActionPreference = $previous; throw "The volume $Volume holds no verified ISO copy" }
} else {
  if (-not $Iso) {
    $newest = Get-ChildItem -Path $Dist -Filter 'archinstall-offline-*.iso' -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $newest) { throw "No offline ISO in $Dist. Build one first: .uild.ps1 -WindowsIso <path>" }
    $Iso = $newest.FullName
  }
  $Iso = (Resolve-Path $Iso).Path
  $IsoDir = Split-Path -Parent $Iso
  $IsoName = Split-Path -Leaf $Iso
  $HashFile = "$Iso.sha256"
  if (-not (Test-Path $HashFile)) { throw "$HashFile is missing: refusing to test an ISO that cannot be verified." }
  $Expected = ((Get-Content $HashFile -TotalCount 1) -split '\s+')[0].ToLower()
  if ($Expected -notmatch '^[0-9a-f]{64}$') { throw "$HashFile does not contain a SHA-256." }
  Write-Host "ISO: $Iso (written $((Get-Item $Iso).LastWriteTime))"
  Write-Host "Expected SHA-256: $Expected"

  Write-Host "Putting the ISO in the Docker volume $Volume (skipped when it is already there) and verifying it..."
  docker run --rm -v "${Root}:/src:ro" -v "${IsoDir}:/iso:ro" -v "${Volume}:/work" `
    -e "EXPECTED=$Expected" -e "ISO_NAME=$IsoName" archinstall-builder `
    bash -c "tr -d '\r' < /src/scripts/integration/tui-e2e-iso.sh > /tmp/c.sh && bash /tmp/c.sh"
  if ($LASTEXITCODE -ne 0) { $ErrorActionPreference = $previous; throw "Could not put a verified copy of the ISO in the volume" }
}

$name = if ($Name) { $Name } elseif ($Mode -eq 'daemon') { 'tui-e2e-dev' } else { 'tui-e2e' }
$arguments = if ($Mode -in 'laptop', 'extras') { 'run' } else { $Mode }
$entry = switch ($Mode) { 'laptop' { 'laptop_e2e.py' } 'extras' { 'extras_e2e.py' } default { 'tui-e2e.py' } }
docker run --rm --privileged --name $name `
  -v "${Root}:/src:ro" `
  -v "${Screens}:/screens" `
  -v archinstall-pkgcache:/var/cache/pacman/pkg `
  -v "${Volume}:/work" -e TUI_WORK=/work -e SCREENS_DIR=/screens `
  -e "TUI_MEMORY=$MemoryMiB" -e "TUI_STOP_AT_SUMMARY=$(if ($StopAtSummary) { '1' } else { '0' })" `
  -e "TUI_ENTRY=$entry" -e "LAPTOP_DEV_INSTALLER=$(if ($RepoInstaller) { '1' } else { '0' })" -e "TUI_REPO_INSTALLER=$(if ($RepoInstaller) { '1' } else { '0' })" `
  -e "LAPTOP_PARTIAL=$(if ($LaptopComplete) { '0' } else { '1' })" -e "LAPTOP_DESKTOP=$LaptopDesktop" `
  archinstall-builder `
  bash -c "tr -d '\r' < /src/scripts/integration/tui-e2e.sh > /tmp/t.sh && bash /tmp/t.sh $arguments"
$code = $LASTEXITCODE
$ErrorActionPreference = $previous
if ($code -ne 0) { throw "The interactive wizard test failed (exit code $code). Screens: $Screens" }
Write-Host "All checks passed. Screens: $Screens"
