"""GRUB, Secure Boot signing and the Windows menu entry.

Two strategies (see docs/ARCHITECTURE.md):

* ``sbctl``: our own Platform Key / KEK / db are enrolled in the firmware (Microsoft's keys
  are kept so Windows and option ROMs still load). GRUB is installed without shim and signed
  with our db key; the kernel is signed with the same key. Needs the firmware in Setup Mode.
* ``shim``: Microsoft's signed shim loads GRUB, which is signed with a key we generate and
  ask the user to enroll in the MOK list on the next boot. Works with any firmware.

Everything that must be redone after an update (reinstall GRUB, sign it, sign the kernel,
regenerate the menu) lives in /usr/local/sbin/archinstall-secureboot, run by a pacman hook.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..config import InstallConfig
from ..layout import ROLE_ESP, ROLE_ROOT
from .arch import strategy_of
from .context import Context, StepError

BOOTLOADER_ID = "ArchInstall"
ESP_DIR = f"/boot/EFI/{BOOTLOADER_ID}"
SECURE_DIR = "/var/lib/archinstall/secureboot"

# Everything GRUB needs is built into its signed image: under Secure Boot it cannot load
# unsigned modules from disk. Verified against `grub-install` in the integration test.
GRUB_MODULES = (
    "all_video boot btrfs cat chain configfile cpuid echo efifwsetup efinet ext2 fat font gettext "
    "gfxmenu gfxterm gfxterm_background gzio halt help keystatus linux loadenv ls lsefi lsefimmap "
    "lsefisystab minicmd normal ntfs part_gpt part_msdos password_pbkdf2 probe read reboot regexp "
    "search search_fs_file search_fs_uuid search_label sleep smbios test true video xfs"
)

# Microsoft-signed shim, taken unmodified from Fedora's package (same source as the AUR's
# shim-signed). Pinned: the build embeds it in the ISO and the installer re-checks the hash.
SHIM_VERSION = "16.1-7"
SHIM_RPM = f"shim-x64-{SHIM_VERSION}.x86_64.rpm"
SHIM_URL = f"https://kojipkgs.fedoraproject.org/packages/shim/16.1/7/x86_64/{SHIM_RPM}"
SHIM_SHA256 = "0a119f3488e5da27ca55c1864ee8e131afa5ecf27689385ba09b805eb00dd608"
SHIM_EMBEDDED = Path("/opt/archinstall/shim") / SHIM_RPM
SHIM_DOWNLOAD = f"/tmp/{SHIM_RPM}"

MOK_CERT_PATH = f"{SECURE_DIR}/MOK.cer"
MOK_HASH_PATH = f"{SECURE_DIR}/mok.hash"

# Until the key is enrolled, queue the request again at every boot so the next restart offers the
# blue screen once more. Needs the user to be there to press a key; nothing else can do that for them.
MOK_REQUEUE_SCRIPT = r"""#!/usr/bin/env bash
# Installed by ArchInstall (shim strategy).
set -u
cert=/var/lib/archinstall/secureboot/MOK.cer
hash=/var/lib/archinstall/secureboot/mok.hash

[[ -d /sys/firmware/efi/efivars ]] || exit 0
if mokutil --test-key "$cert" 2>&1 | grep -qi "already enrolled"; then
  systemctl disable archinstall-mok-enroll.service >/dev/null 2>&1 || true
  exit 0
fi
mokutil --import "$cert" --hash-file "$hash" >/dev/null 2>&1 || exit 0
message="ArchInstall: the Secure Boot key is not enrolled yet. Restart, and when a blue screen appears \
press a key within 10 seconds, then choose: Enroll MOK, Continue, Yes, type your enrollment password, Reboot."
echo "$message" | systemd-cat -t archinstall-mok -p warning
wall "$message" 2>/dev/null || true
"""

MOK_REQUEUE_UNIT = """\
[Unit]
Description=Queue the Secure Boot key enrollment until it is done
ConditionPathExists=/sys/firmware/efi/efivars

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/archinstall-mok-enroll

[Install]
WantedBy=multi-user.target
"""

# Shown at the end of the shim strategy. The 10 seconds are shim's: if no key is pressed on the
# blue screen the pending enrollment request is dropped (seen in the UEFI VM test).
MOK_INSTRUCTIONS = (
    "SECURE BOOT KEY: the first time this computer starts through GRUB/shim (with Windows: after "
    "Windows has finished its first setup and restarted itself), a blue 'MOK management' screen "
    "appears. PRESS A KEY WITHIN 10 SECONDS or the enrollment request is cancelled. Then choose "
    "'Enroll MOK', 'Continue', 'Yes', type the Secure Boot password you chose, and finally choose "
    "'Reboot'. RECOMMENDED: keep Secure Boot OFF in the firmware setup until the key is enrolled, "
    "then turn it on. If Secure Boot is already on and the 10 seconds were missed, shim refuses to "
    "start GRUB ('Verification failed'): turn Secure Boot off, start Arch Linux (it queues the "
    "enrollment again by itself, or run: sudo mokutil --import "
    f"{MOK_CERT_PATH}), restart and press a key in time, then turn Secure Boot on."
)

PACMAN_HOOK = """\
[Trigger]
Operation = Install
Operation = Upgrade
Type = Package
Target = linux
Target = linux-lts
Target = linux-zen
Target = grub

[Action]
Description = Signing the boot files for Secure Boot...
When = PostTransaction
Exec = /usr/local/sbin/archinstall-secureboot
"""

def windows_entry(esp_uuid: str) -> str:
    """/etc/grub.d/40_custom: the "Windows 11" menu entry.

    Windows Setup reuses our EFI system partition, so Windows Boot Manager sits next to GRUB.
    The partition is found by its UUID, and the entry only appears once bootmgfw.efi exists
    there. (Tested under Secure Boot with shim: `search --fs-uuid` and `[ -e ]` are allowed.)

    There is deliberately NO entry for the Windows Setup media (the WINSETUP partition): chain-
    loaded from GRUB, Windows Boot Manager looks for its BCD on the EFI system partition instead
    of its own partition ("0xc000000f ... \\EFI\\Microsoft\\Boot\\BCD", seen in the QEMU test).
    Setup is started by the firmware itself, through a one-time boot entry (steps/windows.py).
    """
    return f"""\
#!/bin/sh
exec tail -n +3 $0
# Windows 11. Shown only once Windows Boot Manager exists on the EFI system partition.
search --no-floppy --fs-uuid --set=winroot {esp_uuid.lower()}
if [ -e ($winroot)/EFI/Microsoft/Boot/bootmgfw.efi ]; then
    menuentry "Windows 11" --class windows --class os {{
        insmod part_gpt
        insmod fat
        insmod chain
        chainloader ($winroot)/EFI/Microsoft/Boot/bootmgfw.efi
    }}
fi
"""

# Reinstalls GRUB, signs what has to be signed, regenerates the menu. Idempotent.
RESIGN_SCRIPT = r"""#!/usr/bin/env bash
# Installed by ArchInstall. Run by a pacman hook after kernel or GRUB updates.
set -euo pipefail
source /etc/archinstall/secureboot.conf

esp_dir="/boot/EFI/$BOOTLOADER_ID"
grub_args=(--target=x86_64-efi --efi-directory=/boot --bootloader-id="$BOOTLOADER_ID" --no-nvram)

case "$STRATEGY" in
  sbctl)
    modules="$GRUB_MODULES tpm"
    [[ -f /usr/lib/grub/x86_64-efi/bli.mod ]] && modules="$modules bli"
    grub-install "${grub_args[@]}" --modules="$modules" --disable-shim-lock
    grub-install "${grub_args[@]}" --modules="$modules" --disable-shim-lock --removable
    sbctl sign -s "$esp_dir/grubx64.efi"
    sbctl sign -s /boot/EFI/BOOT/BOOTX64.EFI
    for kernel in $KERNELS; do
      sbctl sign -s "/boot/vmlinuz-$kernel"
    done
    ;;
  shim)
    # Under shim, GRUB cannot load modules from disk: every module that grub.cfg asks for with
    # `insmod` must be built into the signed image. GRUB 2.12+ asks for `bli` (systemd's boot
    # loader interface); older versions do not have it.
    modules="$GRUB_MODULES"
    [[ -f /usr/lib/grub/x86_64-efi/bli.mod ]] && modules="$modules bli"

    # Arch's sbat.csv can lag behind the GRUB build (GRUB itself warns about it). A generation
    # that is too low can be refused by shim's revocation list, so use the level GRUB expects.
    sbat_csv=/usr/share/grub/sbat.csv
    sbat_args=()
    if [[ -f "$sbat_csv" ]]; then
      probe="$(mktemp)"
      level="$(grub-mkimage -O x86_64-efi -p /boot/grub -o "$probe" normal --sbat "$sbat_csv" 2>&1 \
               | sed -n 's/.*build-time Global SBAT level \([0-9][0-9]*\).*/\1/p' | head -n 1 || true)"
      rm -f "$probe"
      if [[ -n "$level" ]]; then
        awk -F, -v OFS=, -v level="$level" '$1 == "grub" { $2 = level } { print }' "$sbat_csv" \
          > /etc/archinstall/grub-sbat.csv
        sbat_csv=/etc/archinstall/grub-sbat.csv
      fi
      sbat_args=(--sbat "$sbat_csv")
    fi
    grub-install "${grub_args[@]}" --modules="$modules" "${sbat_args[@]}"

    sign() {  # sign a file with the MOK key unless it already carries a valid signature
      sbverify --cert "$SECURE_DIR/MOK.crt" "$1" >/dev/null 2>&1 \
        || sbsign --key "$SECURE_DIR/MOK.key" --cert "$SECURE_DIR/MOK.crt" --output "$1" "$1"
    }
    sign "$esp_dir/grubx64.efi"
    for kernel in $KERNELS; do
      sign "/boot/vmlinuz-$kernel"
    done

    # Fallback path, used by the firmware when its boot entry is lost.
    install -d /boot/EFI/BOOT
    install -m 0644 "$esp_dir/shimx64.efi" /boot/EFI/BOOT/BOOTX64.EFI
    install -m 0644 "$esp_dir/mmx64.efi" /boot/EFI/BOOT/mmx64.efi
    install -m 0644 "$esp_dir/grubx64.efi" /boot/EFI/BOOT/grubx64.efi
    ;;
  *)
    echo "Unknown Secure Boot strategy: $STRATEGY" >&2
    exit 1
    ;;
esac

grub-mkconfig -o /boot/grub/grub.cfg
"""


# -- pure helpers (unit tested) -----------------------------------------------------


def kernel_cmdline(cfg: InstallConfig, root_uuid: str) -> str:
    """GRUB_CMDLINE_LINUX: how the initramfs finds the root filesystem."""
    parts: list[str] = []
    if cfg.arch.luks_enabled:
        parts += [f"rd.luks.name={root_uuid}=cryptroot", "root=/dev/mapper/cryptroot"]
    else:
        parts.append(f"root=UUID={root_uuid}")
    if cfg.arch.filesystem == "btrfs":
        parts.append("rootflags=subvol=@")
    return " ".join(parts)


def grub_defaults(cfg: InstallConfig, root_uuid: str) -> str:
    return (
        # Not "saved": that makes GRUB read /boot/grub/grubenv, which it refuses to do under
        # Secure Boot with shim ("prohibited by secure boot policy" at every boot).
        "GRUB_DEFAULT=0\n"
        "GRUB_TIMEOUT=5\n"
        "GRUB_TIMEOUT_STYLE=menu\n"
        'GRUB_DISTRIBUTOR="Arch"\n'
        'GRUB_CMDLINE_LINUX_DEFAULT="loglevel=3"\n'
        f'GRUB_CMDLINE_LINUX="{kernel_cmdline(cfg, root_uuid)}"\n'
        'GRUB_PRELOAD_MODULES="part_gpt part_msdos"\n'
        # Text menu. The graphical one needs `loadfont`/`gfxterm`, which GRUB refuses to load under
        # Secure Boot with shim ("prohibited by secure boot policy", seen in the QEMU boot test)
        # and would print that error at every boot. The text menu works everywhere, so it is also
        # used with sbctl.
        "GRUB_TERMINAL_OUTPUT=console\n"
        "GRUB_DISABLE_RECOVERY=true\n"
        # Windows is installed after Arch, so os-prober would find nothing; the menu entry
        # is written by hand in /etc/grub.d/40_custom instead.
        "GRUB_DISABLE_OS_PROBER=true\n"
    )


def secureboot_conf(cfg: InstallConfig) -> str:
    return (
        f"STRATEGY={strategy_of(cfg)}\n"
        f"BOOTLOADER_ID={BOOTLOADER_ID}\n"
        f'GRUB_MODULES="{GRUB_MODULES}"\n'
        f'KERNELS="{cfg.arch.kernel}"\n'
        f"SECURE_DIR={SECURE_DIR}\n"
    )


def shim_files_dir(root: str) -> str:
    return f"{root}/usr/lib/efi/shim/{SHIM_VERSION}/EFI/fedora"


# -- shim ---------------------------------------------------------------------------


def fetch_shim(ctx: Context) -> str:
    """Return the path of the verified shim RPM: the one on the ISO, or a fresh download."""
    path = str(SHIM_EMBEDDED) if SHIM_EMBEDDED.is_file() else SHIM_DOWNLOAD
    if path == SHIM_DOWNLOAD and not Path(path).is_file():
        ctx.run("curl", "-fsSL", "--retry", "3", "-o", path, SHIM_URL)
    ctx.run("sha256sum", "-c", "-", stdin=f"{SHIM_SHA256}  {path}\n")
    return path


def prepare_shim(ctx: Context, rpm: str) -> None:
    work = "/tmp/archinstall-shim"
    ctx.run("rm", "-rf", work)
    ctx.run("mkdir", "-p", work)
    ctx.run("bsdtar", "-xf", rpm, "-C", work)

    esp_dir = ctx.target_path(ESP_DIR)
    ctx.run("install", "-d", esp_dir)
    for name in ("shimx64.efi", "mmx64.efi"):
        ctx.run("install", "-m", "0644", f"{shim_files_dir(work)}/{name}", f"{esp_dir}/{name}")

    # The key that will sign GRUB and the kernels, and its certificate for the MOK list.
    ctx.chroot("install", "-d", "-m", "0700", SECURE_DIR)
    ctx.chroot("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{SECURE_DIR}/MOK.key",
               "-new", "-x509", "-sha256", "-days", "3650", "-subj", "/CN=ArchInstall Secure Boot Key/",
               "-out", f"{SECURE_DIR}/MOK.crt")
    ctx.chroot("openssl", "x509", "-outform", "DER", "-in", f"{SECURE_DIR}/MOK.crt",
               "-out", f"{SECURE_DIR}/MOK.cer")
    ctx.chroot("chmod", "0600", f"{SECURE_DIR}/MOK.key")


def store_mok_password_hash(ctx: Context) -> None:
    """Keep the hash (never the password) of the enrollment password, root-only.

    The firmware's MOK screen cancels the request when nobody presses a key within 10 seconds, and
    here the first restart goes straight to Windows Setup, so the screen usually appears when the
    user is away. The hash lets a service on the new system queue the request again, until done.
    """
    password_hash = ctx.capture("openssl", "passwd", "-6", "-stdin", stdin=ctx.cfg.secureboot.mok_password)
    if not password_hash and not ctx.runner.dry_run:
        raise StepError("Could not hash the Secure Boot enrollment password.")
    ctx.write(MOK_HASH_PATH, (password_hash or "<hash>") + "\n", 0o600, secret=True)


def install_mok_service(ctx: Context) -> None:
    ctx.write("/usr/local/sbin/archinstall-mok-enroll", MOK_REQUEUE_SCRIPT, 0o755)
    ctx.write("/etc/systemd/system/archinstall-mok-enroll.service", MOK_REQUEUE_UNIT)
    ctx.chroot("systemctl", "enable", "archinstall-mok-enroll.service")


def enroll_mok(ctx: Context) -> None:
    """Queue the certificate for enrollment: MokManager asks for the password on the next boot."""
    ctx.run("mokutil", "--import", ctx.target_path(MOK_CERT_PATH), "--hash-file", ctx.target_path(MOK_HASH_PATH))


# -- sbctl --------------------------------------------------------------------------


def prepare_sbctl(ctx: Context) -> None:
    ctx.chroot("sbctl", "create-keys")
    if ctx.cfg.secureboot.touch_firmware:
        # -m keeps Microsoft's certificates: Windows and many option ROMs are signed with them.
        ctx.chroot("sbctl", "enroll-keys", "--microsoft")
    else:
        ctx.log("Skipping the key enrollment in the firmware (touch_firmware is off).")


# -- firmware boot entry ------------------------------------------------------------


def register_boot_entry(ctx: Context, loader: str) -> None:
    if not ctx.cfg.secureboot.touch_firmware:
        ctx.log("Skipping the firmware boot entry (touch_firmware is off).")
        return
    listing = ctx.capture("efibootmgr")
    for number in re.findall(rf"^Boot([0-9A-Fa-f]{{4}})\*?\s+{BOOTLOADER_ID}\b", listing, re.MULTILINE):
        ctx.run("efibootmgr", "--bootnum", number, "--delete-bootnum")
    esp = ctx.layout.get(ROLE_ESP)
    assert esp
    returncode = ctx.run("efibootmgr", "--create", "--disk", ctx.cfg.disk.device, "--part", str(esp.number),
                         "--label", BOOTLOADER_ID, "--loader", loader, check=False)
    if returncode:
        ctx.log("WARNING: the firmware boot entry could not be created. The firmware will use the "
                "fallback path \\EFI\\BOOT\\BOOTX64.EFI; check the boot order in the firmware setup.")


# -- the step -------------------------------------------------------------------------


def install_bootloader(ctx: Context) -> None:
    cfg = ctx.cfg
    strategy = strategy_of(cfg)
    root_uuid = ctx.uuid_of(ctx.partition(ROLE_ROOT), "<root-uuid>")
    esp_uuid = ctx.uuid_of(ctx.partition(ROLE_ESP), "<esp-uuid>")

    ctx.write("/etc/default/grub", grub_defaults(cfg, root_uuid))
    ctx.write("/etc/grub.d/40_custom", windows_entry(esp_uuid), 0o755)
    ctx.write("/etc/archinstall/secureboot.conf", secureboot_conf(cfg))
    ctx.write("/usr/local/sbin/archinstall-secureboot", RESIGN_SCRIPT, 0o755)
    ctx.write("/etc/pacman.d/hooks/95-archinstall-secureboot.hook", PACMAN_HOOK)

    if strategy == "sbctl":
        prepare_sbctl(ctx)
        loader = rf"\EFI\{BOOTLOADER_ID}\grubx64.efi"
    else:
        prepare_shim(ctx, fetch_shim(ctx))
        loader = rf"\EFI\{BOOTLOADER_ID}\shimx64.efi"

    ctx.chroot("/usr/local/sbin/archinstall-secureboot")
    register_boot_entry(ctx, loader)

    if strategy == "shim":
        store_mok_password_hash(ctx)
        install_mok_service(ctx)
        if cfg.secureboot.touch_firmware:
            enroll_mok(ctx)
        ctx.log(MOK_INSTRUCTIONS)
    else:
        ctx.log("Secure Boot keys are enrolled. Enable Secure Boot in the firmware setup to use them.")
