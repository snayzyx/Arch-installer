# Sourced by test-install.sh inside the Windows section, while the WINSETUP partition is mounted on $SETUP:
# what the installer staged for the new Windows' network drivers.
DRIVERS_STAGED="$SETUP/sources/\$OEM\$/\$1/Drivers"
FINISH_PS1="$SETUP/sources/\$OEM\$/\$\$/Setup/Scripts/archinstall-finish.ps1"
if [[ -n "${DRIVERS_FROM:-}" ]]; then
  log "Windows drivers staged from $DRIVERS_FROM"
  # rsync, not diff: the builder image has no diffutils. -c compares contents, -n changes nothing, -i lists differences.
  check "the driver packages are in sources/\$OEM\$/\$1/Drivers (identical to the source)" bash -c \
    "[[ -z \"\$(rsync -rcni --delete '$DRIVERS_FROM/' '$DRIVERS_STAGED/')\" ]]"
  check "at least one driver package was staged" bash -c "[[ \$(ls '$DRIVERS_STAGED' | wc -l) -ge 1 ]]"
  check "an INF file is among them" bash -c "find '$DRIVERS_STAGED' -iname '*.inf' | grep -q ."
  for n in 0 7; do
    check "answer file $n lists C:\Drivers as a driver path" grep -qF '<Path>C:\Drivers</Path>' "$SETUP/archinstall/unattend-$n.xml"
    check "answer file $n puts it in the specialize pass (Microsoft-Windows-PnpCustomizationsNonWinPE)" \
      python /src/scripts/integration/check_driver_paths.py "$SETUP/archinstall/unattend-$n.xml"
  done
  check "the finish script reports and deletes C:\Drivers" bash -c "grep -q 'Get-WindowsDriver' '$FINISH_PS1' && grep -q \"Remove-Item -Path 'C:.Drivers'\" '$FINISH_PS1'"
  check "the installer said how many driver packages go to Windows" grep -q "driver package(s) go to Windows" /tmp/installer.log
  check "the RAM copy of the harvested drivers was freed" bash -c "! test -e /tmp/archinstall-windows-drivers"
else
  check "no driver was staged (nothing to add)" bash -c "! test -e '$DRIVERS_STAGED'"
  check "the answer files are unchanged (no driver path)" bash -c "! grep -q PnpCustomizations '$SETUP'/archinstall/unattend-*.xml"
  check "the finish script has no driver section" bash -c "! grep -q 'C:.Drivers' '$FINISH_PS1'"
fi
