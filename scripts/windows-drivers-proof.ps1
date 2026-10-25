<#
.SYNOPSIS
  Proves, on a real Windows driver store, that the installer finds and keeps the network, Wi-Fi and Bluetooth
  drivers of the Windows that is already on the disk.

.DESCRIPTION
  Needs Docker Desktop, the builder image (run build.ps1 once) and the disk image of a Windows installed by an
  earlier end-to-end run (scripts\live-test.ps1 -E2e ...), kept in the Docker volume archinstall-live.
  The image is attached READ-ONLY to a loop device inside a privileged container; the installer's own function
  (steps/windows_drivers.py harvest_drivers) runs against it with the hardware IDs of the virtual machine (an
  Intel 82574L Ethernet adapter, a Wi-Fi 6 AX201, a Bluetooth adapter). Nothing is written to the image.
  The packages found are copied to <EvidenceDir>\harvested for the staging test (test-install.ps1 -DriversFrom).

.PARAMETER Image
  File name of the disk image inside the volume archinstall-live (default r4.img).

.PARAMETER EvidenceDir
  Where the log, the manifest and the harvested packages go (default dist\screens\containers\drivers\proof).

.EXAMPLE
  .\scripts\windows-drivers-proof.ps1
#>
[CmdletBinding()]
param(
  [string]$Image = 'r4.img',
  [string]$EvidenceDir
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
if (-not $EvidenceDir) { $EvidenceDir = Join-Path $Root 'dist\screens\containers\drivers\proof' }
New-Item -ItemType Directory -Force -Path $EvidenceDir | Out-Null
$EvidenceDir = (Resolve-Path $EvidenceDir).Path

$previous = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
docker run --rm --privileged --name "drivers-proof-$(Get-Random -Maximum 9999)" `
  -e "IMAGE=/live/$Image" `
  -v archinstall-live:/live:ro `
  -v "${Root}:/src:ro" `
  -v "${EvidenceDir}:/out" `
  archinstall-builder bash -c "tr -d '\r' < /src/scripts/integration/windows-drivers-proof.sh > /tmp/proof.sh && bash /tmp/proof.sh" 2>&1 |
  Tee-Object -FilePath (Join-Path $EvidenceDir 'run.log')
$code = $LASTEXITCODE
$ErrorActionPreference = $previous
if ($code -ne 0) { throw "The driver harvest proof failed (exit code $code); see $EvidenceDir\run.log" }
Write-Host "Evidence in $EvidenceDir" -ForegroundColor Green
