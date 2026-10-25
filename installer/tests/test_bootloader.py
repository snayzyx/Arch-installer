import re
import shutil
import subprocess
from pathlib import Path

import pytest

from dualboot_installer.config import InstallConfig
from dualboot_installer.data.xkb import xkb_layout
from dualboot_installer.layout import GIB
from dualboot_installer.steps import arch, bootloader
from dualboot_installer.runner import Runner
from dualboot_installer.steps.arch import cpu_ucode, mkinitcpio_hooks, package_list

REPO = Path(__file__).resolve().parents[2]


def cfg_with(strategy="shim", **arch_fields) -> InstallConfig:
    cfg = InstallConfig(dry_run=True)
    cfg.disk.device, cfg.disk.size_bytes, cfg.disk.windows_gib = "/dev/vda", 300 * GIB, 64
    cfg.secureboot.resolved = strategy
    for key, value in arch_fields.items():
        setattr(cfg.arch, key, value)
    return cfg


# -- kernel command line and GRUB settings -----------------------------------------------------


def test_plain_root_is_found_by_uuid():
    assert bootloader.kernel_cmdline(cfg_with(), "AAAA-1") == "root=UUID=AAAA-1"


def test_encrypted_root_is_unlocked_by_the_initramfs():
    cmdline = bootloader.kernel_cmdline(cfg_with(luks_enabled=True), "LUKS-UUID")
    assert cmdline == "rd.luks.name=LUKS-UUID=cryptroot root=/dev/mapper/cryptroot"


def test_btrfs_root_selects_its_subvolume():
    assert bootloader.kernel_cmdline(cfg_with(filesystem="btrfs"), "U").endswith("rootflags=subvol=@")


def test_grub_defaults():
    text = bootloader.grub_defaults(cfg_with(), "U")
    assert "GRUB_TIMEOUT=5" in text
    # Not "saved": GRUB would read grubenv, which it refuses under Secure Boot with shim.
    assert "GRUB_DEFAULT=0\n" in text and "saved" not in text
    assert 'GRUB_CMDLINE_LINUX="root=UUID=U"' in text
    assert "GRUB_DISABLE_OS_PROBER=true" in text  # Windows has its own static entry


def test_the_shipped_grub_config_uses_the_text_menu_for_both_strategies():
    # The graphical menu needs loadfont/gfxterm, refused under Secure Boot with shim: it would
    # print "prohibited by secure boot policy" at every boot.
    for strategy in ("shim", "sbctl"):
        text = bootloader.grub_defaults(cfg_with(strategy), "U")
        assert "GRUB_TERMINAL_OUTPUT=console\n" in text
        assert "gfxterm" not in text.replace("GRUB_GFXMODE", "")


def test_windows_entry_only_appears_when_windows_is_present_and_chainloads_the_boot_manager():
    entry = bootloader.windows_entry("ABCD-1234")
    assert entry.startswith("#!/bin/sh\nexec tail -n +3 $0\n")
    assert "search --no-floppy --fs-uuid --set=winroot abcd-1234" in entry
    assert "if [ -e ($winroot)/EFI/Microsoft/Boot/bootmgfw.efi ]; then" in entry
    assert "chainloader ($winroot)/EFI/Microsoft/Boot/bootmgfw.efi" in entry
    assert 'menuentry "Windows 11" --class windows --class os' in entry


def test_there_is_no_grub_entry_for_the_windows_setup_media():
    # Chain-loaded from GRUB, Windows Boot Manager looks for its BCD on the ESP instead of the
    # WINSETUP partition (0xc000000f in the QEMU test), so the firmware starts Setup instead.
    entry = bootloader.windows_entry("ABCD-1234")
    assert "Setup" not in entry and "efi/boot/bootx64.efi" not in entry.lower()
    assert "grub-reboot" not in bootloader.RESIGN_SCRIPT


def test_modules_missing_from_the_signed_image_cannot_be_loaded_under_secure_boot():
    # Under shim, GRUB refuses to `insmod` anything that is not built in ("prohibited by secure
    # boot policy", seen in the QEMU boot test). The script must add the modules grub.cfg asks for.
    script = bootloader.RESIGN_SCRIPT
    assert script.count('bli.mod ]] && modules="$modules bli"') == 2  # shim and sbctl branches
    assert '--modules="$modules"' in script and '--modules="$GRUB_MODULES"' not in script
    # Every module grub.cfg loads on an ordinary boot must be in the list.
    for needed in ("part_gpt", "part_msdos", "fat", "gzio", "all_video", "chain", "search_fs_uuid", "loadenv"):
        assert needed in bootloader.GRUB_MODULES.split()


def test_fstab_uses_uuids_only_and_has_no_swap():
    cfg = cfg_with()
    text = arch.fstab_content(cfg, cfg.layout(), root_uuid="ROOT", esp_uuid="ESP", home_uuid="")
    assert "UUID=ROOT  /  ext4  rw,relatime  0  1" in text
    assert "UUID=ESP  /boot  vfat" in text and "fmask=0077" in text
    assert "/dev/" not in text and "swap" not in text


def test_fstab_for_btrfs_uses_subvolumes_and_for_a_separate_home_its_own_partition():
    cfg = cfg_with(filesystem="btrfs")
    text = arch.fstab_content(cfg, cfg.layout(), root_uuid="ROOT", esp_uuid="ESP", home_uuid="")
    assert "UUID=ROOT  /  btrfs  rw,noatime,compress=zstd,subvol=/@  0  0" in text
    assert "UUID=ROOT  /home  btrfs  rw,noatime,compress=zstd,subvol=/@home  0  0" in text

    cfg.disk.home_enabled, cfg.disk.root_gib, cfg.disk.windows_gib = True, 20, 64
    cfg.arch.filesystem = "ext4"
    text = arch.fstab_content(cfg, cfg.layout(), root_uuid="ROOT", esp_uuid="ESP", home_uuid="HOME")
    assert "UUID=HOME  /home  ext4  rw,relatime  0  2" in text


def test_secureboot_conf_is_sourceable_shell():
    text = bootloader.secureboot_conf(cfg_with("sbctl", kernel="linux-lts"))
    assert "STRATEGY=sbctl" in text and 'KERNELS="linux-lts"' in text
    assert f'GRUB_MODULES="{bootloader.GRUB_MODULES}"' in text


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_generated_shell_scripts_have_valid_syntax(tmp_path):
    # 40_custom is not checked: after its two shell lines `tail` hands the rest to GRUB's own language.
    for name, content in (("resign.sh", bootloader.RESIGN_SCRIPT),
                          ("conf.sh", bootloader.secureboot_conf(cfg_with()))):
        # Through stdin: a Windows path would not survive the trip to a POSIX bash.
        result = subprocess.run(["bash", "-n"], input=content.encode(), capture_output=True)
        assert result.returncode == 0, f"{name}: {result.stderr.decode()}"


def test_resign_script_handles_both_strategies_and_signs_kernel_and_grub():
    script = bootloader.RESIGN_SCRIPT
    assert "sbctl)" in script and "shim)" in script
    assert script.count("--disable-shim-lock") == 2  # sbctl only: the shim path needs shim_lock
    assert 'sbctl sign -s "$esp_dir/grubx64.efi"' in script
    assert 'sign "/boot/vmlinuz-$kernel"' in script and 'sbctl sign -s "/boot/vmlinuz-$kernel"' in script
    assert "grub-mkconfig -o /boot/grub/grub.cfg" in script
    assert "--sbat" in script
    assert "build-time Global SBAT level" in script  # Arch's sbat.csv can be older than the GRUB build


def test_mok_enrollment_is_queued_again_until_it_is_done():
    # The blue MOK screen drops the request after 10 s without a key, and the first restart goes
    # to Windows Setup, so the user is usually away. A service keeps re-queueing it.
    script = bootloader.MOK_REQUEUE_SCRIPT
    assert 'mokutil --import "$cert" --hash-file "$hash"' in script
    assert "already enrolled" in script and "systemctl disable archinstall-mok-enroll.service" in script
    assert "/sys/firmware/efi/efivars" in script  # does nothing when not booted in UEFI mode
    assert "WantedBy=multi-user.target" in bootloader.MOK_REQUEUE_UNIT
    assert "ExecStart=/usr/local/sbin/archinstall-mok-enroll" in bootloader.MOK_REQUEUE_UNIT


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_mok_requeue_script_has_valid_syntax():
    result = subprocess.run(["bash", "-n"], input=bootloader.MOK_REQUEUE_SCRIPT.encode(), capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_the_shim_strategy_stores_only_a_hash_of_the_password_and_enables_the_service():
    from dualboot_installer.runner import Command, FileWrite, Runner
    from dualboot_installer.steps.context import Context

    cfg = cfg_with("shim")
    cfg.secureboot.mok_password = "Abcd1234"
    runner = Runner(dry_run=True, log=lambda line: None, answers={})
    ctx = Context(cfg, cfg.layout(), runner)
    bootloader.store_mok_password_hash(ctx)
    bootloader.install_mok_service(ctx)

    writes = {op.path: op for op in runner.operations if isinstance(op, FileWrite)}
    stored = writes["/mnt/var/lib/archinstall/secureboot/mok.hash"]
    assert stored.mode == 0o600 and stored.secret
    assert "Abcd1234" not in stored.content  # a hash (or its placeholder), never the password
    assert writes["/mnt/usr/local/sbin/archinstall-mok-enroll"].mode == 0o755
    assert ["arch-chroot", "/mnt", "systemctl", "enable", "archinstall-mok-enroll.service"] in [
        op.argv for op in runner.operations if isinstance(op, Command)]


class PacstrapRunner(Runner):
    """A runner whose pacstrap fails a given number of times first."""

    def __init__(self, failures: int) -> None:
        super().__init__(dry_run=True, log=lambda line: None)
        self.failures = failures
        self.pacstrap_calls = 0

    def run(self, command) -> int:
        self.operations.append(command)
        if command.argv[:1] == ["pacstrap"]:
            self.pacstrap_calls += 1
            return 1 if self.pacstrap_calls <= self.failures else 0
        return 0


def test_pacstrap_is_retried_after_a_flaky_mirror():
    from dualboot_installer.steps.context import Context

    cfg = cfg_with()
    runner = PacstrapRunner(failures=2)
    arch.install_base(Context(cfg, cfg.layout(), runner))
    assert runner.pacstrap_calls == 3


def test_pacstrap_gives_up_after_three_failures_with_a_clear_message():
    from dualboot_installer.steps.context import Context, StepError

    cfg = cfg_with()
    runner = PacstrapRunner(failures=99)
    with pytest.raises(StepError, match="failed 3 times"):
        arch.install_base(Context(cfg, cfg.layout(), runner))
    assert runner.pacstrap_calls == 3


def test_shim_installs_mokutil_for_the_requeue_service():
    assert "mokutil" in package_list(cfg_with("shim"), [])
    assert "mokutil" not in package_list(cfg_with("sbctl"), [])


def test_pacman_hook_covers_every_kernel_and_grub():
    for target in ("linux", "linux-lts", "linux-zen", "grub"):
        assert f"Target = {target}\n" in bootloader.PACMAN_HOOK
    assert "When = PostTransaction" in bootloader.PACMAN_HOOK


# -- shim ------------------------------------------------------------------------------------------


def test_shim_pin_is_consistent_between_the_installer_and_the_iso_build():
    build_script = (REPO / "docker" / "build-iso.sh").read_text(encoding="utf-8")
    assert f'SHIM_RPM="{bootloader.SHIM_RPM}"' in build_script
    assert f'SHIM_SHA256="{bootloader.SHIM_SHA256}"' in build_script
    assert bootloader.SHIM_URL.removesuffix(bootloader.SHIM_RPM) in build_script  # same download folder
    assert re.fullmatch(r"[0-9a-f]{64}", bootloader.SHIM_SHA256)
    assert bootloader.SHIM_URL.endswith(bootloader.SHIM_RPM)
    assert bootloader.shim_files_dir("/tmp/x") == "/tmp/x/usr/lib/efi/shim/16.1-7/EFI/fedora"


# -- packages and initramfs ----------------------------------------------------------------------


def test_package_list_depends_on_the_choices():
    base = package_list(cfg_with(), ["intel-ucode"])
    assert {"base", "linux", "linux-firmware", "intel-ucode", "grub", "efibootmgr", "networkmanager"} <= set(base)
    assert "sbsigntools" in base and "sbctl" not in base
    assert "hyprland" in base and "sddm" in base
    assert len(base) == len(set(base))

    sbctl = package_list(cfg_with("sbctl", filesystem="btrfs", luks_enabled=True, desktop="minimal",
                                  aur_helper="paru", extra_packages=["htop"]), [])
    assert {"sbctl", "btrfs-progs", "cryptsetup", "base-devel", "git", "htop"} <= set(sbctl)
    assert "hyprland" not in sbctl and "sbsigntools" not in sbctl


def test_nvidia_on_a_non_stock_kernel_pulls_dkms_and_headers():
    packages = package_list(cfg_with(kernel="linux-zen", gpu_profiles=["nvidia-open"]), [])
    assert "nvidia-open-dkms" in packages and "linux-zen-headers" in packages


def test_initramfs_hooks_unlock_luks_before_mounting_root():
    plain = mkinitcpio_hooks(cfg_with())
    encrypted = mkinitcpio_hooks(cfg_with(luks_enabled=True))
    assert "sd-encrypt" not in plain
    hooks = re.search(r"\((.*)\)", encrypted).group(1).split()
    assert hooks.index("sd-vconsole") < hooks.index("sd-encrypt") < hooks.index("filesystems")
    assert hooks[:2] == ["base", "systemd"]


def test_microcode_follows_the_cpu_vendor():
    assert cpu_ucode("vendor_id : GenuineIntel") == ["intel-ucode"]
    assert cpu_ucode("vendor_id : AuthenticAMD") == ["amd-ucode"]
    assert cpu_ucode("vendor_id : Unknown") == []


def test_xkb_layout_for_console_keymaps():
    assert [xkb_layout(k) for k in ("us", "fr", "uk", "de-latin1", "br-abnt2", "cf", "jp106", "")] == [
        "us", "fr", "gb", "de", "br", "ca", "jp", "us"]


def test_hyprland_config_uses_the_chosen_layout():
    assert "kb_layout = gb" in arch.hyprland_config("uk")
