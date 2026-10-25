<#
.SYNOPSIS
  Builds the online ISO, and the offline ISO when -WindowsIso is given, then verifies both.

.DESCRIPTION
  Runs build.ps1 for each flavour and then docker\verify-iso.sh in the builder container (the ISO
  contents, the pinned shim, the installer modules and, for the offline ISO, a byte-for-byte
  comparison of the bundled Windows ISO). The ISOs are written to .\dist.

.PARAMETER WindowsIso
  Windows 11 ISO to bundle into the offline image. Omit to build only the online ISO.

.PARAMETER SkipImage
  Pass through to build.ps1: do not rebuild the builder image.

.PARAMETER SkipVerify
  Build only.

.PARAMETER OutDir
  Where the ISOs are written and verified. Defaults to .\dist.

.PARAMETER DriversDir
  A folder of extracted Windows driver packages to bundle into both ISOs (build.ps1 -DriversDir) and to check
  against the image afterwards.

.PARAMETER OfflineOnly
  Skip the online ISO (it is already built and verified in OutDir).

.EXAMPLE
  .\scripts\rebuild-all.ps1 -SkipImage -WindowsIso .\ISO\Windows11_Client_x64_en-us.iso
#>
[CmdletBinding()]
param(
  [string]$WindowsIso,
  [switch]$SkipImage,
  [switch]$SkipVerify,
  [string]$OutDir,
  [switch]$OfflineOnly,
  [string]$DriversDir
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
if (-not $OutDir) { $OutDir = Join-Path $Root 'dist' }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$Dist = (Resolve-Path $OutDir).Path
# A hashtable, not an array: splatting an array passes '-SkipImage' as a positional value, which
# build.ps1 then took for its -OutDir (the ISO ended up in a folder named "-SkipImage").
$buildArgs = @{ OutDir = $Dist }
if ($SkipImage) { $buildArgs['SkipImage'] = $true }
if ($DriversDir) {
  $DriversDir = (Resolve-Path -LiteralPath $DriversDir).Path
  $buildArgs['DriversDir'] = $DriversDir
}

function Invoke-Verify {
  param([string]$IsoName, [string]$Mode, [string]$Source)
  $docker = @('run', '--rm', '--privileged', '-v', "${Root}:/src:ro", '-v', "${Dist}:/dist:ro")
  $sourceArg = ''
  if ($DriversDir) { $docker += @('-v', "${DriversDir}:/drivers-source:ro", '-e', 'DRIVERS_SRC=/drivers-source') }
  if ($Source) {
    $docker += @('-v', "${Source}:/windows-source.iso:ro")
    $sourceArg = '/windows-source.iso'
  }
  $previous = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  & docker @docker archinstall-builder bash -c "tr -d '\r' < /src/docker/verify-iso.sh > /tmp/v.sh && bash /tmp/v.sh /dist/$IsoName $Mode $sourceArg"
  $code = $LASTEXITCODE
  $ErrorActionPreference = $previous
  if ($code -ne 0) { throw "Verification of $IsoName failed" }
}

if (-not $OfflineOnly) {
  Write-Host '==> Online ISO' -ForegroundColor Cyan
  & (Join-Path $Root 'build.ps1') @buildArgs
  $online = Get-ChildItem $Dist -Filter 'archinstall-2*.iso' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if (-not $SkipVerify) { Invoke-Verify -IsoName $online.Name -Mode 'online' -Source '' }
}

if ($WindowsIso) {
  $WindowsIso = (Resolve-Path -LiteralPath $WindowsIso).Path
  Write-Host '==> Offline ISO' -ForegroundColor Cyan
  $offlineArgs = @{ SkipImage = $true; WindowsIso = $WindowsIso; OutDir = $Dist }
  if ($DriversDir) { $offlineArgs['DriversDir'] = $DriversDir }
  & (Join-Path $Root 'build.ps1') @offlineArgs
  $offline = Get-ChildItem $Dist -Filter 'archinstall-offline-*.iso' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if (-not $SkipVerify) { Invoke-Verify -IsoName $offline.Name -Mode 'offline' -Source $WindowsIso }
}

Write-Host '==> Done' -ForegroundColor Green
Get-ChildItem $Dist -Filter 'archinstall-*.iso' | Select-Object Name, @{n = 'SizeMiB'; e = { [math]::Round($_.Length / 1MB) } }
