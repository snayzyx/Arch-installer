#!/usr/bin/env bash
# Runs INSIDE the live system, after the install: give the installed system's GRUB and kernel a
# serial console so the host can watch it. grub.cfg is neither signed nor verified, so this does not
# change what Secure Boot checks. NO keys, NO boot entries are touched here.
set -e
mount /dev/vda3 /mnt
mount -o umask=0077 /dev/vda1 /mnt/boot
sed -i 's|^GRUB_CMDLINE_LINUX_DEFAULT=.*|GRUB_CMDLINE_LINUX_DEFAULT="loglevel=6 console=ttyS0,115200"|' /mnt/etc/default/grub
echo 'GRUB_TERMINAL_OUTPUT=console' >>/mnt/etc/default/grub
arch-chroot /mnt grub-mkconfig -o /boot/grub/grub.cfg >/dev/null 2>&1
grep -q 'console=ttyS0' /mnt/boot/grub/grub.cfg
umount -R /mnt
echo SERIAL-OK
