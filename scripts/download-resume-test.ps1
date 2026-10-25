<#
.SYNOPSIS
  Tests the "link" Windows source against a connection that dies in the middle of the download.

.DESCRIPTION
  Serves a Windows ISO over HTTPS from inside a privileged container (nginx, a throw-away CA), kills the
  server midway and checks that the installer's download step (curl with --retry-all-errors)
  still finishes in one run with an identical SHA-256. Nothing touches a real disk. See scripts/integration/download-resume.sh.

.PARAMETER WindowsIso
  The Windows 11 ISO to serve (mounted read-only, never copied).

.PARAMETER EvidenceDir
  Where the log is kept.

.EXAMPLE
  .\scripts\download-resume-test.ps1 -WindowsIso .\ISO\Windows11_Client_x64_en-us_26300_9457.iso
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory)][string]$WindowsIso,
  [string]$EvidenceDir,
  [string]$Name = 'archinstall-resume-test'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath $WindowsIso -PathType Leaf)) { throw "Windows ISO not found: $WindowsIso" }
$WindowsIso = (Resolve-Path -LiteralPath $WindowsIso).Path
$extra = @()
$inner = "set -o pipefail; tr -d '\r' < /src/scripts/integration/download-resume.sh > /tmp/r.sh && bash /tmp/r.sh 2>&1"
if ($EvidenceDir) {
  New-Item -ItemType Directory -Force -Path $EvidenceDir | Out-Null
  $EvidenceDir = (Resolve-Path -LiteralPath $EvidenceDir).Path
  $extra += @('-v', "${EvidenceDir}:/evidence", '-e', 'EVIDENCE_DIR=/evidence')
  $inner += ' | tee /evidence/run.log'
}

$previous = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
docker run --rm --privileged --name $Name @extra `
  -v "${WindowsIso}:/windows/Windows11.iso:ro" `
  -v "${Root}:/src:ro" `
  -v archinstall-pkgcache:/var/cache/pacman/pkg `
  archinstall-builder `
  bash -c $inner
$code = $LASTEXITCODE
$ErrorActionPreference = $previous
if ($code -ne 0) { throw "download-resume test failed (exit code $code)" }
