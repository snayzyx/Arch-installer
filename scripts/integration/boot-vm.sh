#!/usr/bin/env bash
# Boot test, sourced by test-install.sh when BOOT_TEST=1.
#
# Boots the freshly installed disk image in QEMU with OVMF firmware that ENFORCES Secure Boot,
# with the keys pre-enrolled the way the real installer would have done it:
#   shim  -> Microsoft keys in the firmware db, our certificate in the MOK list
#   sbctl -> our own PK/KEK/db
# Then it proves the signatures matter with two negative controls.
#
# Expects: IMAGE, CHECK (root of the mounted install), ESP, STRATEGY, LUKS, LOOP, FAILED,
# and the helpers log() and check().

OVMF_DIR=/usr/share/edk2/x64
VM_TIMEOUT="${VM_TIMEOUT:-420}"
SHIM_LOCK_GUID=605dab50-e046-4300-abb6-3dd810dd8b23 # owner GUID used for MOK entries

# Print what matters of the serial console: Secure Boot messages and the end of the boot.
show_log_excerpt() {
  local clean="/tmp/vm-$1.clean"
  sed 's/\x1b\[[0-9;?]*[A-Za-z]//g' "$VM_LOG" | tr -d '\r' >"$clean"
  # Every pipeline ends in `|| true`: a grep with no match must not abort the script (set -e).
  echo "  --- console log ($1): $(wc -l <"$clean") lines; Secure Boot lines ---"
  { grep -iE "secure ?boot|shim|mok|sbat|lockdown|security|violation|denied" "$clean" | head -12 | sed 's/^/      /'; } || true
  echo "  --- context of GRUB policy errors ---"
  { grep -n -B3 -A3 "prohibited by secure boot policy" "$clean" | head -20 | cut -c1-120 | sed 's/^/      /'; } || true
  echo "  --- units that failed or timed out ---"
  { grep -nE "FAILED|Timed out|Dependency failed|Failed to|failed with|emergency" "$clean" | head -25 | sed 's/^/      /'; } || true
  echo "  --- last 12 lines ---"
  { tail -n 12 "$clean" | sed 's/^/      /'; } || true
}

# Boot the image once. $1 = label for the logs. Sets VM_LOG to the serial console capture.
run_vm() {
  local label="$1" vars="/tmp/vars-$1.fd"
  VM_LOG="/tmp/vm-$label.log"
  : >"$VM_LOG"

  # Fresh firmware variables with the keys of the scenario.
  cp "$OVMF_DIR/OVMF_VARS.4m.fd" "$vars"
  case "$VM_KEYS" in
    shim)
      virt-fw-vars --input "$OVMF_DIR/OVMF_VARS.4m.fd" --output "$vars" --enroll-redhat --secure-boot \
        --add-mok "$SHIM_LOCK_GUID" "$MOK_CERT" >/dev/null ;;
    shim-no-mok)
      virt-fw-vars --input "$OVMF_DIR/OVMF_VARS.4m.fd" --output "$vars" --enroll-redhat --secure-boot >/dev/null ;;
    sbctl)
      # --enroll-cert puts the certificate in PK and KEK only; the db (which GRUB and the kernel
      # are checked against) needs it too, like `sbctl enroll-keys` does.
      virt-fw-vars --input "$OVMF_DIR/OVMF_VARS.4m.fd" --output "$vars" --enroll-cert "$SBCTL_DB" \
        --add-db "$SHIM_LOCK_GUID" "$SBCTL_DB" --secure-boot >/dev/null ;;
  esac

  echo "  booting ($label), up to ${VM_TIMEOUT}s..."
  # smm=on and the secure pflash are required by the Secure Boot build of OVMF.
  timeout "$VM_TIMEOUT" qemu-system-x86_64 \
    -machine q35,smm=on -cpu max -smp 2 -m 2048 -nographic -no-reboot \
    -global driver=cfi.pflash01,property=secure,value=on \
    -drive if=pflash,format=raw,unit=0,readonly=on,file="$OVMF_DIR/OVMF_CODE.secboot.4m.fd" \
    -drive if=pflash,format=raw,unit=1,file="$vars" \
    -drive file="$IMAGE",format=raw,if=none,id=disk0 -device virtio-blk-pci,drive=disk0,bootindex=0 \
    -serial stdio -monitor none >"$VM_LOG" 2>&1 </dev/null &
  local qemu_pid=$!

  # Stop as soon as the outcome is known: a login prompt, or a clear refusal.
  local waited=0
  while kill -0 "$qemu_pid" 2>/dev/null && (( waited < VM_TIMEOUT )); do
    if grep -qE 'testbox login:' "$VM_LOG"; then break; fi
    if [[ -n "${VM_STOP_ON:-}" ]] && grep -q "$VM_STOP_ON" "$VM_LOG"; then break; fi
    if grep -qE 'Security Violation|Access Denied|bad shim signature|Verification failed|MokManager|Perform MOK management' "$VM_LOG" \
       && (( waited > 45 )); then break; fi
    sleep 3; waited=$((waited + 3))
  done
  kill "$qemu_pid" 2>/dev/null || true
  wait "$qemu_pid" 2>/dev/null || true
}

# Diagnostic: run GRUB commands one by one under Secure Boot (shim_lock active) and print which
# of them GRUB refuses with "prohibited by secure boot policy". Enabled with BOOT_PROBE=1.
probe_grub() {
  log "GRUB probe: which commands does Secure Boot forbid?"
  local esp_uuid; esp_uuid="$(blkid -s UUID -o value "${LOOP}p1" | tr 'A-Z' 'a-z')"
  echo "  --- the first lines of the generated grub.cfg that open files or load modules ---"
  { grep -nE "insmod|search|loadfont|load_env|source|configfile|set root|terminal|gfx" "$ESP/grub/grub.cfg" | head -40 | cut -c1-140 | sed 's/^/      /'; } || true

  mkdir -p "$ESP/EFI/Microsoft/Boot"; echo placeholder >"$ESP/EFI/Microsoft/Boot/bootmgfw.efi"
  echo 'GRUB_TERMINAL_OUTPUT=console' >>"$CHECK/etc/default/grub"
  arch-chroot "$CHECK" grub-mkconfig -o /boot/grub/grub.cfg >/dev/null 2>&1
  {
    cat <<EOF
set pager=0
echo "PROBE-START"
echo "PROBE 1: search --fs-uuid"
search --no-floppy --fs-uuid --set=probe_root $esp_uuid
echo "PROBE 1 done, root=\$probe_root"
echo "PROBE 2: test -e on a file"
if [ -e (\$probe_root)/EFI/Microsoft/Boot/bootmgfw.efi ]; then echo "PROBE 2 done: found"; else echo "PROBE 2 done: not found"; fi
echo "PROBE 3: search --file"
search --no-floppy --file --set=probe_root2 /EFI/Microsoft/Boot/bootmgfw.efi
echo "PROBE 3 done"
echo "PROBE 4: loadfont unicode"
loadfont unicode
echo "PROBE 4 done"
echo "PROBE 5: insmod of modules built into the image"
insmod part_gpt
insmod fat
insmod gzio
insmod all_video
insmod gfxterm
echo "PROBE 5 done"
echo "PROBE 6: load_env"
load_env
echo "PROBE 6 done"
echo "PROBE 7: insmod of a module that is NOT built in (png)"
insmod png
echo "PROBE 7 done"
echo "PROBE-END"
EOF
    cat "$ESP/grub/grub.cfg"
  } >"$ESP/grub/grub.cfg.new"
  mv "$ESP/grub/grub.cfg.new" "$ESP/grub/grub.cfg"

  sed -i 's|^GRUB_CMDLINE_LINUX_DEFAULT=.*|GRUB_CMDLINE_LINUX_DEFAULT="loglevel=6 console=ttyS0,115200"|' "$CHECK/etc/default/grub"
  MOK_CERT=/tmp/MOK.crt; SBCTL_DB=/tmp/db.pem
  [[ "$STRATEGY" == "shim" ]] && cp "$CHECK/var/lib/archinstall/secureboot/MOK.crt" "$MOK_CERT"
  [[ "$STRATEGY" == "sbctl" ]] && cp "$CHECK/var/lib/sbctl/keys/db/db.pem" "$SBCTL_DB"
  umount -R "$CHECK"; losetup -d "$LOOP"

  VM_TIMEOUT=240 VM_KEYS="$STRATEGY" VM_STOP_ON='PROBE-END' run_vm probe
  local clean=/tmp/vm-probe.clean
  sed 's/\x1b\[[0-9;?]*[A-Za-z]//g' /tmp/vm-probe.log | tr -d '\r' >"$clean"
  echo "  --- GRUB output between PROBE-START and PROBE-END ---"
  { sed -n '/PROBE-START/,/PROBE-END/p' "$clean" | cut -c1-140 | sed 's/^/      /'; } || true
}

boot_test() {
  log "Boot test in QEMU with Secure Boot enforced ($STRATEGY)"
  pacman -S --noconfirm --needed qemu-system-x86 edk2-ovmf virt-firmware >/dev/null 2>&1 \
    || { echo "  FAIL  cannot install QEMU / OVMF / virt-firmware"; FAILED=1; return; }

  if [[ -n "$LUKS" ]]; then
    echo "  skipped: the passphrase prompt needs a keyboard (covered by the install checks)"; return
  fi

  # Serial console for the kernel, so the log shows how far the boot got. grub.cfg is not
  # signed or verified, so editing it does not change what is being tested.
  sed -i 's|^GRUB_CMDLINE_LINUX_DEFAULT=.*|GRUB_CMDLINE_LINUX_DEFAULT="loglevel=6 console=ttyS0,115200"|' "$CHECK/etc/default/grub"
  # The shipped /etc/default/grub already uses the text menu (GRUB_TERMINAL_OUTPUT=console, because
  # the graphical one is refused under Secure Boot), so it shows on the serial console as is: this
  # test runs the real configuration. The install checks verify that line is in the shipped file.
  # A stand-in for Windows Boot Manager, so the Windows entry has to show up in the menu.
  mkdir -p "$ESP/EFI/Microsoft/Boot"
  echo "placeholder" >"$ESP/EFI/Microsoft/Boot/bootmgfw.efi"
  arch-chroot "$CHECK" grub-mkconfig -o /boot/grub/grub.cfg >/dev/null 2>&1

  if [[ -n "${BOOT_PROBE:-}" ]]; then
    probe_grub
    return 0
  fi

  # Keep copies of what the scenarios need, then release the disk image for QEMU.
  MOK_CERT=/tmp/MOK.crt; SBCTL_DB=/tmp/db.pem
  [[ "$STRATEGY" == "shim" ]] && cp "$CHECK/var/lib/archinstall/secureboot/MOK.crt" "$MOK_CERT"
  [[ "$STRATEGY" == "sbctl" ]] && cp "$CHECK/var/lib/sbctl/keys/db/db.pem" "$SBCTL_DB"
  KERNEL_VERSION="$(ls "$CHECK/usr/lib/modules" | head -n 1)"
  cp "$CHECK/usr/lib/modules/$KERNEL_VERSION/vmlinuz" /tmp/vmlinuz-unsigned
  cp "$ESP/vmlinuz-linux" /tmp/vmlinuz-signed
  umount -R "$CHECK"
  losetup -d "$LOOP"

  # --- 1. the real thing --------------------------------------------------------------
  VM_TIMEOUT=600 VM_KEYS="$STRATEGY" run_vm good
  show_log_excerpt good
  check "GRUB loaded the kernel from the ESP"       grep -q "BOOT_IMAGE=/vmlinuz-linux" "$VM_LOG"
  check "the Windows 11 entry is in the GRUB menu"  grep -q "Windows 11" "$VM_LOG"
  check "Linux boots under Secure Boot"             grep -q "Linux version" "$VM_LOG"
  check "shim handed our MOK key to the kernel"     grep -q "Loaded X.509 cert 'ArchInstall Secure Boot Key" "$VM_LOG"
  check "systemd reaches the login prompt"          grep -q "testbox login:" "$VM_LOG"
  check "no GRUB secure boot policy error"          bash -c "! grep -q 'prohibited by secure boot policy' '$VM_LOG'"
  [[ -n "${BOOT_SKIP_NEGATIVE:-}" ]] && return 0

  # --- 2. negative controls: Secure Boot must really be enforced ----------------------------
  # Put the kernel back to its unsigned original: the boot must be refused.
  local loop; loop="$(losetup -f --show -P "$IMAGE")"
  for n in "${loop}"p*; do
    [[ -e "$n" ]] || { name="$(basename "$n")"; IFS=: read -r a b <"/sys/class/block/$name/dev"; mknod "$n" b "$a" "$b"; }
  done
  mkdir -p /tmp/espmnt && mount "${loop}p1" /tmp/espmnt
  cp /tmp/vmlinuz-unsigned /tmp/espmnt/vmlinuz-linux
  umount /tmp/espmnt
  VM_KEYS="$STRATEGY" run_vm unsigned-kernel
  check "NEGATIVE: an unsigned kernel does not boot"  bash -c "! grep -q 'Linux version' '$VM_LOG'"
  check "NEGATIVE: ...and nothing reaches the login prompt" bash -c "! grep -q 'testbox login:' '$VM_LOG'"
  mount "${loop}p1" /tmp/espmnt && cp /tmp/vmlinuz-signed /tmp/espmnt/vmlinuz-linux && umount /tmp/espmnt
  losetup -d "$loop"

  if [[ "$STRATEGY" == "shim" ]]; then
    # Without the MOK enrollment, shim must refuse to run our GRUB.
    VM_KEYS="shim-no-mok" run_vm no-mok
    check "NEGATIVE: without MOK enrollment the boot is refused" bash -c "! grep -q 'Linux version' '$VM_LOG'"
    check "NEGATIVE: ...and shim says why" grep -qE 'Security Violation|Verification failed|MokManager|MOK management|Failed to open' "$VM_LOG"
  fi
}
