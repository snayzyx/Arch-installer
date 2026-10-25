<#
.SYNOPSIS
  Runs the real installation steps against a virtual disk inside Docker and checks the result.

.DESCRIPTION
  Needs Docker Desktop and the builder image (run build.ps1 once). Nothing touches a real disk:
  the installer works on a 110 GB sparse image attached as a loop device inside the container.
  The first run downloads Arch packages into the shared pacman cache volume.

.PARAMETER Strategy
  Secure Boot path to test: shim (shim + MOK) or sbctl (own keys).

.PARAMETER Filesystem
  Root filesystem: ext4, btrfs or xfs.

.PARAMETER Desktop
  minimal (fast) or hyprland (installs the whole desktop).

.PARAMETER Luks
  Encrypt Arch Linux with LUKS2.

.PARAMETER SeparateHome
  Give /home its own partition.

.PARAMETER WindowsIso
  A Windows 11 ISO to install too (full install, without --partial). It is mounted read-only
  in the container, never copied. Adds the checks of the staged Windows Setup.

.PARAMETER SharedPartition
  Keep the last partition as the permanent shared NTFS partition (16 GiB in the test) instead of
  removing it after Windows Setup. Needs a Windows ISO to check the finish script.

.PARAMETER AurHelper
  none (default), paru or yay: the AUR helper is built for real (network access to the AUR).

.PARAMETER DevGroups
  Comma-separated developer tool groups (languages, cli, docker): answers file key arch.dev_groups.

.PARAMETER DriversFrom
  A folder of extracted driver packages (for instance the one windows-drivers-proof.ps1 harvested) handed to the
  installer as if it were bundled in the ISO (build.ps1 -DriversDir). With -WindowsIso, the checks verify that they
  are staged under sources\$OEM$\$1\Drivers and that the answer files list C:\Drivers.

.PARAMETER Dotfiles
  A desktop shell preset (macshell or caelestia): the installer downloads the latest version from GitHub and the
  AUR. Needs network access and the Hyprland desktop. Its checks are in scripts\integration\checks-dotfiles-<id>.sh.

.PARAMETER Apps
  Comma-separated desktop applications (vscodium, librewolf, postman): answers file key arch.apps.
  They are only installed with -Desktop hyprland; vscodium and postman are built from the AUR.

.PARAMETER AurBreak
  Failure path: the AUR is made unreachable for the installed system (a bogus hosts entry added once the
  installer has written /etc/hosts). The installation must still end with exit code 0 and warnings.

.PARAMETER AurBreakEarly
  Like -AurBreak, but the AUR is already unreachable when the installer starts, so the check made before
  anything is erased sees it too (it must only warn: the AUR applications are optional extras).

.PARAMETER DownloadSource
  With -WindowsIso: the installer downloads that ISO from an HTTPS server (nginx with a throw-away
  CA) started inside the container, the way a pasted Microsoft link works, then stages it.

.PARAMETER Laptop
  Laptop answers: arch.laptop, a Wi-Fi network to keep, hybrid Intel + NVIDIA graphics. Checks
  the extra packages, the services, the Wi-Fi profile and `Hyprland --verify-config`.

.PARAMETER Name
  Name of the Docker container (default: archinstall-test-<random>), so that runs can be told apart.

.PARAMETER EvidenceDir
  Where the log, the generated Windows scripts and the partition table are kept. The generated
  PowerShell scripts are then parsed with the PowerShell parser of this machine.

.PARAMETER PersistImage
  Keep the 110 GB disk image in the Docker volume archinstall-winimg (as /img/disk.img) so that
  scripts/integration/windows-boot.sh can boot it afterwards. Replaced at every run.

.PARAMETER BootTest
  After the install, boot the disk in QEMU with Secure Boot enforced (OVMF) and check that it
  reaches the login prompt, then that an unsigned kernel is refused. Slow: no hardware acceleration.

.EXAMPLE
  .\scripts\test-install.ps1 -Strategy shim
#>
[CmdletBinding()]
param(
  [ValidateSet('shim', 'sbctl')][string]$Strategy = 'shim',
  [ValidateSet('ext4', 'btrfs', 'xfs')][string]$Filesystem = 'ext4',
  [ValidateSet('minimal', 'hyprland')][string]$Desktop = 'minimal',
  [switch]$Luks,
  [switch]$SeparateHome,
  [switch]$BootTest,
  [switch]$SkipNegativeBootTests,
  [switch]$ProbeGrub,
  [string]$WindowsIso,
  [switch]$PersistImage,
  [switch]$SharedPartition,
  [ValidateSet('none', 'paru', 'yay')][string]$AurHelper = 'none',
  [switch]$DownloadSource,
  [switch]$Laptop,
  [string]$DevGroups = '',
  [string]$Apps = '',
  [string]$Dotfiles = '',
  [string]$DriversFrom = '',
  [switch]$AurBreak,
  [switch]$AurBreakEarly,
  [string]$Name,
  [string]$EvidenceDir
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Out = Join-Path $Root 'out'
New-Item -ItemType Directory -Force -Path $Out | Out-Null

$boot = if ($BootTest) { '1' } else { '' }
$skipNegative = if ($SkipNegativeBootTests) { '1' } else { '' }
$probe = if ($ProbeGrub) { '1' } else { '' }
$options = @()
if ($Luks) { $options += 'luks' }
if ($SeparateHome) { $options += 'home' }
if ($SharedPartition) { $options += 'shared' }
$optionText = $options -join ' '

# Extra `docker run` arguments: the Windows ISO (read-only bind mount) and the persistent image.
$extra = @()
if ($WindowsIso) {
  if (-not (Test-Path -LiteralPath $WindowsIso -PathType Leaf)) { throw "Windows ISO not found: $WindowsIso" }
  $WindowsIso = (Resolve-Path -LiteralPath $WindowsIso).Path
  $extra += @('-v', "${WindowsIso}:/windows/Windows11.iso:ro", '-e', 'WINDOWS_ISO=/windows/Windows11.iso')
}
if ($PersistImage) {
  $extra += @('-v', 'archinstall-winimg:/img', '-e', 'IMAGE_PATH=/img/disk.img')
}
if ($DownloadSource -and -not $WindowsIso) { throw '-DownloadSource needs -WindowsIso (the file the local HTTPS server serves).' }
if ($SharedPartition -and -not $WindowsIso) { Write-Warning '-SharedPartition without -WindowsIso only checks the partition layout.' }
if ($DownloadSource) { $extra += @('-e', 'DOWNLOAD_SOURCE=1') }
if ($Laptop) { $extra += @('-e', 'LAPTOP=1') }
if ($DevGroups) { $extra += @('-e', "DEV_GROUPS=$DevGroups") }
if ($Apps) { $extra += @('-e', "APPS=$Apps") }
if ($Dotfiles) { $extra += @('-e', "DOTFILES=$Dotfiles") }
if ($DriversFrom) {
  if (-not (Test-Path -LiteralPath $DriversFrom -PathType Container)) { throw "Drivers folder not found: $DriversFrom" }
  $DriversFrom = (Resolve-Path -LiteralPath $DriversFrom).Path
  $extra += @('-v', "${DriversFrom}:/drivers-from:ro", '-e', 'DRIVERS_FROM=/drivers-from')
}
if ($AurBreakEarly) { $extra += @('-e', 'AUR_BREAK=early') } elseif ($AurBreak) { $extra += @('-e', 'AUR_BREAK=1') }
$extra += @('-e', "AUR_HELPER=$AurHelper")
if (-not $Name) { $Name = 'archinstall-test-' + (Get-Random -Maximum 99999) }
$extra += @('--name', $Name)
if ($EvidenceDir) {
  New-Item -ItemType Directory -Force -Path $EvidenceDir | Out-Null
  $EvidenceDir = (Resolve-Path -LiteralPath $EvidenceDir).Path
  $extra += @('-v', "${EvidenceDir}:/evidence", '-e', 'EVIDENCE_DIR=/evidence')
}
$started = Get-Date

# Native stderr must not become a PowerShell error (see build.ps1).
$previous = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$inner = "set -o pipefail; tr -d '\r' < /src/scripts/integration/test-install.sh > /tmp/t.sh && bash /tmp/t.sh $Strategy $Filesystem $Desktop $optionText 2>&1"
if ($EvidenceDir) { $inner += ' | tee /evidence/run.log' }
docker run --rm --privileged `
  @extra `
  -e "BOOT_TEST=$boot" `
  -e "BOOT_SKIP_NEGATIVE=$skipNegative" `
  -e "BOOT_PROBE=$probe" `
  -v "${Root}:/src:ro" `
  -v "${Out}:/out" `
  -v archinstall-pkgcache:/var/cache/pacman/pkg `
  archinstall-builder `
  bash -c $inner
$code = $LASTEXITCODE
$ErrorActionPreference = $previous
$seconds = [int]((Get-Date) - $started).TotalSeconds

# The Windows scripts the installer generated, parsed by the real PowerShell parser: a syntax
# error there would only show up in the middle of Windows Setup.
$syntaxFailures = 0
if ($EvidenceDir -and (Test-Path (Join-Path $EvidenceDir 'generated'))) {
  Write-Host "`n##### PowerShell syntax of the generated scripts"
  foreach ($file in Get-ChildItem (Join-Path $EvidenceDir 'generated') -Filter *.ps1) {
    $tokens = $null; $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$errors)
    if ($errors -and $errors.Count) {
      $syntaxFailures++
      Write-Host "  FAIL  $($file.Name)"
      $errors | ForEach-Object { Write-Host "        $($_.Message) (line $($_.Extent.StartLineNumber))" }
    } else {
      Write-Host "  ok    $($file.Name) parses without errors"
    }
  }
  foreach ($file in Get-ChildItem (Join-Path $EvidenceDir 'generated') -Filter unattend-*.xml) {
    try { [void][xml](Get-Content -LiteralPath $file.FullName -Raw); Write-Host "  ok    $($file.Name) is well-formed XML" }
    catch { $syntaxFailures++; Write-Host "  FAIL  $($file.Name): $($_.Exception.Message)" }
  }
}
Write-Host ("`nscenario: strategy=$Strategy fs=$Filesystem desktop=$Desktop options=[$optionText] aur=$AurHelper " +
            "laptop=$($Laptop.IsPresent) dev=[$DevGroups] apps=[$Apps] aurbreak=$($AurBreak.IsPresent)/$($AurBreakEarly.IsPresent) " +
            "windows=$([bool]$WindowsIso) download=$($DownloadSource.IsPresent); " +
            "exit code $code; PowerShell syntax failures $syntaxFailures; took $seconds s")
if ($EvidenceDir) { "exit_code=$code`nsyntax_failures=$syntaxFailures`nseconds=$seconds" | Set-Content -LiteralPath (Join-Path $EvidenceDir 'result.txt') -Encoding ascii }
if ($code -ne 0) { throw "Integration test failed (exit code $code)" }
if ($syntaxFailures) { throw "$syntaxFailures generated file(s) do not parse" }
