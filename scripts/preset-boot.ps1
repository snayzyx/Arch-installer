<#
.SYNOPSIS
  Does a desktop shell preset really start? Installs it with the real installer, boots the result in a VM and
  photographs the Hyprland session.

.DESCRIPTION
  Needs Docker Desktop (with /dev/kvm in its VM) and the builder image (run build.ps1 once). Inside a privileged
  container: the REAL installer (the code of the ISO, run from this repository) installs Arch Linux, Hyprland and
  the preset on a 110 GB sparse disk image (kept in the Docker volume boot-img); the image is prepared for an
  unattended look (a TEST-ONLY autologin and a serial console); then it is booted in QEMU/KVM with a virtio screen
  and screenshots, logs and `hyprctl` answers are saved to dist\screens\preset-boot\<preset>\.
  Secure Boot is off in this test (the key enrollment is not what is looked at). Nothing touches a real disk.

.PARAMETER Preset
  none (the default setup, the baseline), macshell, revo or caelestia.

.PARAMETER Phase
  all (default), or one of install, patch, boot: rerun only that part on the image already in the volume.

.PARAMETER RenderEnv
  Lines KEY=value (separated by ';') written to /etc/environment of the installed system, to find out what
  software rendering needs, e.g. 'WLR_RENDERER_ALLOW_SOFTWARE=1;LIBGL_ALWAYS_SOFTWARE=1'.

.PARAMETER Seconds
  How long the session is watched after power-on (default 180).

.PARAMETER MaxDockerGiB
  The other jobs of this machine share the 15.5 GB of Docker's VM. Before it starts, this script waits (up to
  30 minutes) until everything running in Docker uses at most this much (default 6.8: a 4 GB VM then fits under
  11 GB in total). Every container it starts is also capped (7 GB to install, 5 GB to boot), so that the
  host page cache of a big disk image cannot squeeze the others.

.PARAMETER Keys
  QEMU monitor key names sent once the session is up, e.g. 'meta_l-q meta_l-r' (default meta_l-q).

.EXAMPLE
  .\scripts\preset-boot.ps1 -Preset none
#>
param(
  [Parameter(Mandatory)][ValidateSet('none', 'macshell', 'revo', 'caelestia')][string]$Preset,
  [ValidateSet('all', 'install', 'patch', 'boot')][string]$Phase = 'all',
  [string]$RenderEnv = '',
  [int]$Seconds = 180,
  [string]$Keys = 'meta_l-q',
  [double]$MaxDockerGiB = 6.8,
  [string]$Volume = 'boot-img'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Screens = Join-Path $Root 'dist\screens\preset-boot'
New-Item -ItemType Directory -Force -Path $Screens | Out-Null
$previous = $ErrorActionPreference
$ErrorActionPreference = 'Continue'

function Get-DockerMemoryGiB {
  $total = 0.0
  foreach ($line in (docker stats --no-stream --format '{{.MemUsage}}' 2>$null)) {
    if ($line -match '^([\d.]+)\s*(GiB|MiB|KiB|B)') {
      $value = [double]::Parse($Matches[1], [Globalization.CultureInfo]::InvariantCulture)
      switch ($Matches[2]) { 'GiB' { $total += $value } 'MiB' { $total += $value / 1024 } 'KiB' { $total += $value / 1048576 } }
    }
  }
  $total
}
$waited = 0
while ((Get-DockerMemoryGiB) -gt $MaxDockerGiB -and $waited -lt 1800) {
  Write-Host ("Docker uses {0:N1} GiB (more than {1}): waiting for the other jobs ({2} s so far)" -f (Get-DockerMemoryGiB), $MaxDockerGiB, $waited)
  Start-Sleep -Seconds 30
  $waited += 30
}
$cap = if ($Phase -eq 'boot') { '5g' } else { '7g' }
docker run --rm --privileged --name "boot-$Preset" --memory $cap --memory-swap $cap `
  -v "${Root}:/src:ro" -v "${Screens}:/screens" -v "${Volume}:/img" `
  -v archinstall-pkgcache:/var/cache/pacman/pkg `
  -e SCREENS_DIR=/screens -e "BOOT_SECONDS=$Seconds" -e "KEYS=$Keys" -e "RENDER_ENV=$($RenderEnv -replace ';', "`n")" `
  archinstall-builder `
  bash -c "tr -d '\r' < /src/scripts/integration/preset-boot.sh > /tmp/p.sh && bash /tmp/p.sh $Preset $Phase"
$code = $LASTEXITCODE
$ErrorActionPreference = $previous
if ($code -ne 0) { throw "preset-boot failed for $Preset (exit code $code). Evidence: $Screens\$Preset" }
Write-Host "Done. Screenshots and logs: $Screens\$Preset"
