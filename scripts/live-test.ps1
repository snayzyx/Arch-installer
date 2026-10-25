<#
.SYNOPSIS
  Checks the installer's writes to REAL UEFI firmware, in a QEMU/KVM VM booted from the official
  Arch ISO (scripts\integration\live-vm.py).

.DESCRIPTION
  Needs Docker Desktop (with /dev/kvm in its VM) and the builder image (run build.ps1 once).
  Scenario a-sbctl: firmware in Setup Mode -> the installer enrolls sbctl keys -> the installed
  disk boots under Secure Boot with nothing added by hand.
  Scenario b-shim: firmware with a Platform Key -> sbctl refused -> shim + mokutil --import ->
  MokManager is driven with the keyboard. Screenshots go to dist\screens.

.PARAMETER Scenario
  a-sbctl, b-shim or both (default). a-boot only boots the disk and firmware variables left by the
  last a-sbctl run (kept in the Docker volume archinstall-live), to debug a boot without reinstalling.

.PARAMETER MokPlan
  For b-mok: the keyboard plan for MokManager, e.g. "wait=14;shot;key=ret;wait=1;shot" (steps
  wait=, shot, key=, type=, separated by ';').

.PARAMETER ExpectLogin
  For b-mok: after the plan, wait for the login prompt of the installed system and check it.

.PARAMETER MokSecureBoot
  For b-mok: boot with Secure Boot on (the variables are edited like the firmware setup would)
  or off (the variables exactly as the installer left them).

.PARAMETER Snapshot
  A folder containing dualboot_installer\ to test. Default: the installer folder of this repo.

.PARAMETER E2e
  FULL end-to-end acceptance test (scripts\integration\live-e2e.py) instead of the scenarios above:
  boots the OFFLINE ISO, runs the real installer, lets Windows 11 install itself, checks Secure Boot and
  the dual boot. Values: shim or sbctl (all phases), or one phase: shim-install, shim-windows, shim-mok,
  shim-boot, shim-inspect (each starts from the checkpoint saved by the phase before it).

.PARAMETER IsoInVolume
  For -E2e: file name of the offline ISO inside the Docker volume archinstall-live (copy it there first).
  Default: offline-dev.iso.

.PARAMETER Laptop
  For -E2e: laptop answers (arch.laptop, a Wi-Fi network) and the checks that go with them: Fast Startup and
  hibernation off, NTFS mounted read-write from Linux, Wi-Fi profile, laptop packages and services.

.PARAMETER Msdm
  For -E2e: give the virtual machine a fake MSDM table (a product key in the firmware).

.PARAMETER EvidenceDir
  For -E2e: folder of dist\screens that receives the pictures and logs (default e2e2).

.PARAMETER Tag
  For -E2e: prefix of the disk, variables and checkpoints in the Docker volume (default r2), so a run does not
  overwrite the files of another.

.PARAMETER DevInstaller
  For -E2e: run a snapshot of installer\ (see -Snapshot) instead of the installer inside the ISO.
  Only for debugging the test itself with an ISO built before the installer changes.

.EXAMPLE
  .\scripts\live-test.ps1 -Scenario a-sbctl
#>
[CmdletBinding()]
param(
  [ValidateSet('a-sbctl', 'a-boot', 'a-boot-tpm', 'a-notpm', 'a-chain', 'a-fallback', 'b-shim', 'b-mok', 'b-fallback')][string[]]$Scenario = @('a-sbctl', 'b-shim'),
  [string]$Snapshot,
  [string]$MokPlan,
  [switch]$ExpectLogin,
  [ValidateSet('on', 'off')][string]$MokSecureBoot = 'on',
  [string[]]$E2e,
  [string]$IsoInVolume = 'offline-dev.iso',
  [switch]$DevInstaller,
  [int]$MemoryMiB = 6144,
  [int]$WindowsMinutes = 90,
  [switch]$Laptop,
  [switch]$Msdm,
  [string]$EvidenceDir = 'e2e2',
  [string]$Tag = 'r2'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Snapshot) { $Snapshot = Join-Path $Root 'installer' }
$Snapshot = (Resolve-Path $Snapshot).Path
$Iso = (Resolve-Path (Join-Path $Root 'ISO\archlinux-2026.10.01-x86_64.iso')).Path
# Not under out\: IDEs and cleanup tools may empty that folder.
$Screens = Join-Path $Root 'dist\screens'
New-Item -ItemType Directory -Force -Path $Screens | Out-Null

# Native stderr must not become a PowerShell error (see build.ps1).
$previous = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$names = $Scenario -join ' '
if ($E2e) {
  $names = $E2e -join ' '
  docker run --rm --privileged --name "live-e2e-$($E2e[0])" `
    -v "${Root}:/src:ro" `
    -v "${Snapshot}:/snap:ro" `
    -v "${Screens}:/screens" `
    -v archinstall-pkgcache:/var/cache/pacman/pkg `
    -v archinstall-live:/work -e LIVE_WORK=/work -e SCREENS_DIR=/screens `
    -e "E2E_ISO=/work/$IsoInVolume" -e "E2E_DEV_INSTALLER=$(if ($DevInstaller) { '1' } else { '0' })" `
    -e "E2E_MEMORY=$MemoryMiB" -e "E2E_WINDOWS_MINUTES=$WindowsMinutes" `
    -e "E2E_LAPTOP=$(if ($Laptop) { '1' } else { '0' })" -e "E2E_MSDM=$(if ($Msdm) { '1' } else { '0' })" `
    -e "E2E_EVIDENCE=$EvidenceDir" -e "E2E_TAG=$Tag" `
    archinstall-builder `
    bash -c "tr -d '\r' < /src/scripts/integration/live-e2e.sh > /tmp/e.sh && bash /tmp/e.sh $names"
  $code = $LASTEXITCODE
  $ErrorActionPreference = $previous
  if ($code -ne 0) { throw "End-to-end test failed (exit code $code)" }
  return
}
docker run --rm --privileged `
  -v "${Root}:/src:ro" `
  -v "${Snapshot}:/snap:ro" `
  -v "${Iso}:/iso/arch.iso:ro" `
  -v "${Screens}:/screens" `
  -v archinstall-pkgcache:/var/cache/pacman/pkg `
  -v archinstall-live:/work -e LIVE_WORK=/work `
  -e "MOK_EXPECT_LOGIN=$(if ($ExpectLogin) { '1' } else { '0' })" `
  -e "MOK_PLAN=$MokPlan" -e "MOK_SB=$(if ($MokSecureBoot -eq 'on') { '1' } else { '0' })" `
  archinstall-builder `
  bash -c "tr -d '\r' < /src/scripts/integration/live-test.sh > /tmp/t.sh && bash /tmp/t.sh $names"
$code = $LASTEXITCODE
$ErrorActionPreference = $previous
if ($code -ne 0) { throw "Live firmware test failed (exit code $code)" }
