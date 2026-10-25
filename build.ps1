<#
.SYNOPSIS
  Builds the ArchInstall ISO inside a privileged Arch Linux Docker container.

.DESCRIPTION
  Requires Docker Desktop to be running. The ISO is written to .\dist.
  The pacman package cache lives in a named Docker volume, so only the first
  build downloads everything.

  With -WindowsIso, the Windows 11 ISO is bundled into the image ("offline"
  build): the installer then uses it instead of downloading Windows from
  Microsoft. The result is about 10 GB, so it needs a USB drive of 16 GB or more.

.PARAMETER SkipImage
  Do not rebuild the builder image (faster when only the profile or the
  installer changed).

.PARAMETER OutDir
  Where the ISO is written. Defaults to .\dist next to this script.

.PARAMETER WindowsIso
  Path of a Windows 11 installation ISO (x64) to bundle into the image.

.EXAMPLE
  .\build.ps1

.EXAMPLE
  .\build.ps1 -WindowsIso .\ISO\Windows11_Client_x64_en-us.iso
#>
[CmdletBinding()]
param(
  [switch]$SkipImage,
  [string]$OutDir,
  [string]$WindowsIso
)

$ErrorActionPreference = 'Stop'

# $PSScriptRoot is empty inside a parameter default in Windows PowerShell 5.1.
# The folder is 'dist', not 'out': 'out' is the default build-output folder of IntelliJ and other
# IDEs, which may clean it (see Find-RecycledIso below).
if (-not $OutDir) { $OutDir = Join-Path $PSScriptRoot 'dist' }

# Docker writes its progress to stderr. Windows PowerShell 5.1 turns native stderr into
# terminating errors under 'Stop' when the output is captured or redirected, so native
# commands run with 'Continue' and are judged by their exit code only.
function Invoke-Native {
  param([scriptblock]$Command, [string]$Failure)
  $previous = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try { & $Command } finally { $ErrorActionPreference = $previous }
  if ($LASTEXITCODE -ne 0) { throw $Failure }
}

# An IDE or a cleanup tool can send a freshly built ISO to the Windows Recycle Bin. Say so, with
# the time, when that is why the ISO is missing.
function Find-RecycledIso {
  param([string]$Pattern, [datetime]$Since)
  try {
    $bin = (New-Object -ComObject Shell.Application).Namespace(0xA)
    foreach ($item in $bin.Items()) {
      if ($item.Name -like $Pattern) {
        $when = $item.ExtendedProperty('System.Recycle.DateDeleted')
        if ($when -and ([datetime]$when).ToLocalTime() -ge $Since) {
          return ('{0}, deleted from {1} at {2}' -f $item.Name, $item.ExtendedProperty('System.Recycle.DeletedFrom'), $when)
        }
      }
    }
  } catch { }
  return $null
}

function Assert-Docker {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker CLI not found. Install Docker Desktop first.'
  }
  Invoke-Native { docker info *> $null } 'The Docker daemon is not running. Start Docker Desktop and try again.'
}

# Extra `docker run` arguments for the offline build.
$OfflineArgs = @()
if ($WindowsIso) {
  if (-not (Test-Path -LiteralPath $WindowsIso -PathType Leaf)) {
    throw "Windows ISO not found: $WindowsIso"
  }
  $WindowsIso = (Resolve-Path -LiteralPath $WindowsIso).Path
  if ([IO.Path]::GetExtension($WindowsIso).ToLower() -ne '.iso') {
    throw "The Windows image must be an .iso file: $WindowsIso"
  }
  $IsoGiB = (Get-Item -LiteralPath $WindowsIso).Length / 1GB
  if ($IsoGiB -lt 3) {
    throw ('{0} is only {1:N1} GiB: that is not a Windows 11 installation ISO.' -f $WindowsIso, $IsoGiB)
  }
  Write-Host ('Offline build: bundling {0} ({1:N1} GiB). The resulting ISO will be about {2:N0} GiB.' -f `
      (Split-Path $WindowsIso -Leaf), $IsoGiB, ($IsoGiB + 1.7)) -ForegroundColor Yellow
  $OfflineArgs = @('-v', "${WindowsIso}:/windows/Windows11.iso:ro", '-e', 'WINDOWS_ISO=/windows/Windows11.iso')
}

Assert-Docker

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path
$Image = 'archinstall-builder'

if (-not $SkipImage) {
  Write-Host '==> Building the builder image' -ForegroundColor Cyan
  Invoke-Native {
    docker build -t $Image -f (Join-Path $PSScriptRoot 'docker\Dockerfile') (Join-Path $PSScriptRoot 'docker')
  } 'docker build failed'
}

$BuildStart = Get-Date
Write-Host '==> Building the ISO' -ForegroundColor Cyan
# --privileged: mkarchiso needs mount/chroot capabilities (and the offline build a bind mount).
# The script is piped through `tr` so CRLF checkouts do not break bash.
Invoke-Native {
  docker run --rm --privileged `
    @OfflineArgs `
    -v "${PSScriptRoot}:/src:ro" `
    -v "${OutDir}:/out" `
    -v archinstall-pkgcache:/var/cache/pacman/pkg `
    $Image `
    bash -c "tr -d '\r' < /src/docker/build-iso.sh > /tmp/build-iso.sh && bash /tmp/build-iso.sh"
} 'ISO build failed'

$Expected = if ($WindowsIso) { 'archinstall-offline-*.iso' } else { 'archinstall-2*.iso' }
function Get-BuiltIso {
  Get-ChildItem $OutDir -Filter $Expected -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -ge $BuildStart } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
}
$Built = Get-BuiltIso
if ($Built) {
  # Look again a few seconds later: a program deleting the file would not do it instantly.
  Start-Sleep -Seconds 5
  $Built = Get-BuiltIso
}
if (-not $Built) {
  $recycled = Find-RecycledIso -Pattern $Expected -Since $BuildStart
  if ($recycled) {
    throw ("The ISO was built, then moved to the Recycle Bin by another program: $recycled. " +
           "Restore it from the Recycle Bin, and find what cleans '$OutDir' (an IDE 'clean'/'rebuild', a cleanup tool).")
  }
  throw "The build said it succeeded, but no new ISO ($Expected) is in $OutDir."
}

Write-Host "==> ISO written to $OutDir" -ForegroundColor Green
$Built | Select-Object Name, @{n = 'SizeMiB'; e = { [math]::Round($_.Length / 1MB) } }

if ($WindowsIso) {
  Write-Host 'This ISO contains Windows 11: keep it for your own use and do not share it.' -ForegroundColor Yellow
  Write-Host 'Write it to a USB drive of 16 GB or more in DD mode (Rufus) or copy it to a Ventoy drive.' -ForegroundColor Yellow
}
