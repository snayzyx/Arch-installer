import pytest

from dualboot_installer.config import InstallConfig
from dualboot_installer.layout import GIB
from dualboot_installer.plan import build_plan, can_run_for_real, execute_plan
from dualboot_installer.runner import Command, FileWrite, Runner
from dualboot_installer.steps.context import Context, StepError
from dualboot_installer.steps.storage import format_commands, mount_commands, partition_commands, unmount_target


def make_config(**arch) -> InstallConfig:
    cfg = InstallConfig(dry_run=True)
    cfg.disk.device = "/dev/nvme0n1"
    cfg.disk.size_bytes = 512 * GIB
    cfg.disk.windows_gib = 200
    cfg.arch.luks_passphrase = "correct horse"
    cfg.arch.username = "alice"
    cfg.arch.user_password = "s3cret-user"
    cfg.arch.root_password = "s3cret-root"
    cfg.arch.root_same_password = False
    cfg.secureboot.resolved = "shim"
    cfg.secureboot.mok_password = "Abcd1234"
    for key, value in arch.items():
        setattr(cfg.arch, key, value)
    return cfg


def argvs(commands: list[Command]) -> list[list[str]]:
    return [c.argv for c in commands]


# -- partitioning and formatting ---------------------------------------------------------


def test_partition_commands_wipe_then_create_every_partition_once():
    cfg = make_config()
    layout = cfg.layout()
    commands = partition_commands(cfg, layout)
    assert commands[0].argv[:2] == ["wipefs", "--all"]
    create = next(c for c in commands if c.argv[0] == "sgdisk" and "-n" in c.argv)
    assert create.argv.count("-n") == len(layout.partitions)
    assert create.argv[-1] == "/dev/nvme0n1"
    assert "1:2048:2099199" in create.argv  # the 1 GiB ESP starts at sector 2048
    types = [create.argv[i + 1] for i, arg in enumerate(create.argv) if arg == "-t"]
    assert types == ["1:EF00", "2:0700", "3:8304", "4:0700"]


def test_luks_changes_the_root_partition_type():
    cfg = make_config(luks_enabled=True)
    create = next(c for c in partition_commands(cfg, cfg.layout()) if "-n" in c.argv)
    assert "3:8309" in create.argv


def test_windows_partition_is_not_formatted_by_linux():
    cfg = make_config()
    commands = format_commands(cfg, cfg.layout())
    assert "/dev/nvme0n1p2" not in [c.argv[-1] for c in commands]
    assert ["mkfs.fat", "-F", "32", "-n", "ESP", "/dev/nvme0n1p1"] in argvs(commands)
    assert ["mkfs.fat", "-F", "32", "-n", "WINSETUP", "/dev/nvme0n1p4"] in argvs(commands)


def test_filesystem_choice_is_applied_to_root():
    for fs in ("ext4", "btrfs", "xfs"):
        cfg = make_config(filesystem=fs)
        commands = format_commands(cfg, cfg.layout())
        assert any(c.argv[0] == f"mkfs.{fs}" and c.argv[-1] == "/dev/nvme0n1p3" for c in commands)


def test_luks_root_is_formatted_through_the_mapper_with_a_hidden_passphrase():
    cfg = make_config(luks_enabled=True)
    commands = format_commands(cfg, cfg.layout())
    luks = [c for c in commands if c.argv[0] == "cryptsetup"]
    assert [c.argv[1] for c in luks] == ["luksFormat", "open"]
    assert all(c.secret and c.stdin == "correct horse" for c in luks)
    assert all("correct horse" not in c.display() for c in luks)
    assert any(c.argv[0] == "mkfs.ext4" and c.argv[-1] == "/dev/mapper/cryptroot" for c in commands)


def test_home_gets_its_own_filesystem():
    cfg = make_config()
    cfg.disk.home_enabled = True
    commands = format_commands(cfg, cfg.layout())
    assert any(c.argv[0] == "mkfs.ext4" and c.argv[-1] == "/dev/nvme0n1p4" for c in commands)


# -- mounting -----------------------------------------------------------------------------


def test_ext4_mounts_root_then_esp_on_boot():
    cfg = make_config()
    commands = argvs(mount_commands(cfg, cfg.layout()))
    assert commands[0] == ["mount", "/dev/nvme0n1p3", "/mnt"]
    assert commands[-1] == ["mount", "-o", "umask=0077", "/dev/nvme0n1p1", "/mnt/boot"]


def test_btrfs_uses_subvolumes_and_mounts_home_from_the_same_partition():
    cfg = make_config(filesystem="btrfs")
    commands = argvs(mount_commands(cfg, cfg.layout()))
    assert ["btrfs", "subvolume", "create", "/mnt/@"] in commands
    assert ["btrfs", "subvolume", "create", "/mnt/@home"] in commands
    assert ["mount", "-o", "subvol=@home,compress=zstd,noatime", "/dev/nvme0n1p3", "/mnt/home"] in commands
    assert commands.index(["umount", "/mnt"]) < commands.index(
        ["mount", "-o", "subvol=@,compress=zstd,noatime", "/dev/nvme0n1p3", "/mnt"])


def test_separate_home_is_mounted_and_encrypted_devices_use_the_mapper():
    cfg = make_config(luks_enabled=True)
    cfg.disk.home_enabled = True
    commands = argvs(mount_commands(cfg, cfg.layout()))
    assert ["mount", "/dev/mapper/cryptroot", "/mnt"] in commands
    assert ["mount", "/dev/mapper/crypthome", "/mnt/home"] in commands


# -- the plan --------------------------------------------------------------------------------


def test_plan_runs_preflight_first_and_windows_after_the_bootloader():
    cfg = make_config()
    plan = build_plan(cfg, cfg.layout())
    keys = [s.key for s in plan]
    assert keys[0] == "preflight" and keys[-1] == "finish"
    assert keys.index("partition") < keys.index("mount") < keys.index("base") < keys.index("bootloader")
    # The firmware entry for Windows Setup (windows-stage) comes after GRUB's own; unmounting must come last.
    assert keys.index("bootloader") < keys.index("windows-iso") < keys.index("windows-stage") < keys.index("finish")
    assert all(s.implemented for s in plan)
    assert can_run_for_real(plan, cfg)


def test_partial_mode_keeps_the_windows_steps_in_the_plan_but_skips_them():
    cfg = make_config()
    cfg.partial = True
    plan = build_plan(cfg, cfg.layout())
    assert [s.key for s in plan if not s.implemented] == ["windows-iso", "windows-stage"]
    assert all("partial" in s.note for s in plan if not s.implemented)
    assert can_run_for_real(plan, cfg)


def test_partial_dry_run_never_touches_the_windows_image():
    cfg = make_config()
    cfg.partial = True
    runner, lines = dry_run_everything(cfg)
    commands = [op.argv[0] for op in runner.operations if isinstance(op, Command)]
    assert "rsync" not in commands and "wimlib-imagex" not in commands
    assert "(skipped) Skipped: partial mode (Windows is not installed)." in lines


def dry_run_everything(cfg: InstallConfig) -> tuple[Runner, list[str]]:
    lines: list[str] = []
    runner = Runner(dry_run=True, log=lines.append)
    ok = execute_plan(build_plan(cfg, cfg.layout()), Context(cfg, cfg.layout(), runner))
    assert ok, "\n".join(lines[-15:])
    return runner, lines


def test_full_dry_run_never_prints_a_secret():
    cfg = make_config(luks_enabled=True)
    _, lines = dry_run_everything(cfg)
    text = "\n".join(lines)
    for secret in ("s3cret-user", "s3cret-root", "correct horse", "Abcd1234"):
        assert secret not in text


def test_full_dry_run_covers_the_whole_arch_install_in_order():
    runner, _ = dry_run_everything(make_config())
    commands = [op.argv for op in runner.operations if isinstance(op, Command)]
    order = [
        ["pacman", "-Sy"], ["sgdisk", "--zap-all"], ["mkfs.fat"], ["pacstrap", "-K"],
        ["arch-chroot", "/mnt", "locale-gen"], ["arch-chroot", "/mnt", "useradd"],
        ["arch-chroot", "/mnt", "/usr/local/sbin/archinstall-secureboot"], ["umount", "-R"],
    ]
    positions = []
    for prefix in order:
        positions.append(next(i for i, argv in enumerate(commands) if argv[:len(prefix)] == prefix))
    assert positions == sorted(positions)


def test_the_disk_guid_is_set_so_winpe_can_find_the_target_disk():
    cfg = make_config()
    commands = argvs(partition_commands(cfg, cfg.layout()))
    guid = ["sgdisk", f"--disk-guid={cfg.disk.guid}", "/dev/nvme0n1"]
    assert guid in commands
    assert commands.index(guid) < commands.index(["partprobe", "/dev/nvme0n1"])
    assert cfg.disk.guid == cfg.disk.guid.upper() and len(cfg.disk.guid) == 36


def test_full_dry_run_stages_windows_after_installing_grub():
    runner, _ = dry_run_everything(make_config())
    commands = [op.argv for op in runner.operations if isinstance(op, Command)]
    order = [
        ["arch-chroot", "/mnt", "/usr/local/sbin/archinstall-secureboot"],
        ["mount", "/dev/nvme0n1p4", "/tmp/archinstall-windows-setup"],  # the WINSETUP partition
        ["rsync", "-rt"],
        ["wimlib-imagex", "split"],
        ["wimlib-imagex", "update"],
        ["umount", "/tmp/archinstall-windows-setup"],
        ["efibootmgr", "--create", "--disk", "/dev/nvme0n1", "--part", "4"],  # the Setup entry (part 4)
        ["efibootmgr", "--bootnext"],
        ["umount", "-R", "/mnt"],
    ]
    positions = [next(i for i, argv in enumerate(commands) if argv[:len(prefix)] == prefix) for prefix in order]
    assert positions == sorted(positions)


def test_windows_setup_is_started_by_the_firmware_once_not_by_grub():
    # Chain-loaded from GRUB, Windows Boot Manager looks for its BCD on the ESP and fails with
    # 0xc000000f (seen in the QEMU test): the firmware must start it, with a one-time entry.
    runner, _ = dry_run_everything(make_config())
    commands = [op.argv for op in runner.operations if isinstance(op, Command)]
    create = next(a for a in commands if a[:2] == ["efibootmgr", "--create"] and "Windows 11 Setup" in a)
    assert create[create.index("--disk") + 1] == "/dev/nvme0n1"
    assert create[create.index("--part") + 1] == "4"  # WINSETUP
    assert create[create.index("--label") + 1] == "Windows 11 Setup"
    assert create[create.index("--loader") + 1] == "\\EFI\\BOOT\\BOOTX64.EFI"
    assert not any("grub-reboot" in a or "grub-set-default" in a for a in commands)


def test_the_setup_entry_is_first_in_the_boot_order_and_also_boot_next():
    # BootNext alone was ignored by the test firmware (OVMF); an entry first in the boot order is
    # what was proven to start Windows Setup. The end-of-setup script deletes the temporary entry.
    runner, _ = dry_run_everything(make_config())
    commands = [op.argv for op in runner.operations if isinstance(op, Command)]
    setup = [a for a in commands if a[:2] == ["efibootmgr", "--create"] and "Windows 11 Setup" in a]
    assert setup and "--create-only" not in setup[0]  # plain --create: placed first in BootOrder
    assert any(a[:2] == ["efibootmgr", "--bootnext"] for a in commands)


def test_without_firmware_access_no_setup_entry_is_created():
    cfg = make_config()
    cfg.secureboot.touch_firmware = False
    runner, lines = dry_run_everything(cfg)
    commands = [op.argv for op in runner.operations if isinstance(op, Command)]
    assert not any(a[:1] == ["efibootmgr"] for a in commands)
    assert any("touch_firmware is off" in line for line in lines)


def test_windows_answer_files_and_scripts_are_written_to_the_setup_partition():
    cfg = make_config()
    runner, _ = dry_run_everything(cfg)
    written = {op.path: op for op in runner.operations if isinstance(op, FileWrite)}
    root = "/tmp/archinstall-windows-setup"
    answers = [p for p in written if p.startswith(f"{root}/archinstall/unattend-")]
    assert len(answers) == 16
    assert all(written[p].secret for p in answers)  # they hold the Windows password
    assert f"{root}/sources/$OEM$/$$/Setup/Scripts/SetupComplete.cmd" in written
    assert f"{root}/sources/$OEM$/$$/Setup/Scripts/archinstall-finish.ps1" in written
    launcher = written["/tmp/archinstall-windows-stage/archinstall.cmd"].content
    assert cfg.disk.guid in launcher  # the launcher looks for exactly the GUID given to the disk
    update = next(op for op in runner.operations if isinstance(op, Command) and op.argv[:2] == ["wimlib-imagex", "update"])
    assert "winpeshl.ini" in update.stdin and "archinstall.cmd" in update.stdin


def test_the_windows_password_never_reaches_the_log():
    cfg = make_config()
    cfg.windows.username, cfg.windows.password, cfg.windows.computer_name = "alice", "W1ndows-Pw!", "WINTEST"
    runner, lines = dry_run_everything(cfg)
    text = "\n".join(lines)
    assert "W1ndows-Pw!" not in text
    from dualboot_installer.windows_unattend import encode_password
    assert encode_password("W1ndows-Pw!") not in text


def test_users_get_their_passwords_through_stdin_only():
    runner, _ = dry_run_everything(make_config())
    chpasswd = next(op for op in runner.operations if isinstance(op, Command) and "chpasswd" in op.argv)
    assert chpasswd.secret and chpasswd.stdin == "alice:s3cret-user\nroot:s3cret-root\n"
    assert not any("s3cret" in " ".join(op.argv) for op in runner.operations if isinstance(op, Command))


def test_files_are_written_into_the_new_system():
    runner, _ = dry_run_everything(make_config(hostname="mybox", locale="fr_FR.UTF-8", keymap="fr"))
    written = {op.path: op for op in runner.operations if isinstance(op, FileWrite)}
    assert written["/mnt/etc/hostname"].content == "mybox\n"
    assert written["/mnt/etc/locale.conf"].content == "LANG=fr_FR.UTF-8\n"
    assert written["/mnt/etc/vconsole.conf"].content == "KEYMAP=fr\n"
    assert "fr_FR.UTF-8 UTF-8" in written["/mnt/etc/locale.gen"].content
    assert written["/mnt/etc/sudoers.d/10-wheel"].mode == 0o440
    assert "/boot" in written["/mnt/etc/fstab"].content and "genfstab" not in str(runner.operations)
    assert "kb_layout = fr" in written["/mnt/home/alice/.config/hypr/hyprland.conf"].content


class UmountRunner(Runner):
    """A runner whose `umount -R` fails a given number of times first."""

    def __init__(self, failures: int) -> None:
        super().__init__(dry_run=True, log=lambda line: None)
        self.failures = failures
        self.umount_calls = 0

    def run(self, command: Command) -> int:
        self.operations.append(command)
        if command.argv[:2] == ["umount", "-R"]:
            self.umount_calls += 1
            return 1 if self.umount_calls <= self.failures else 0
        return 0


def test_unmount_retries_while_the_gpg_agent_is_still_shutting_down():
    cfg = make_config()
    runner = UmountRunner(failures=3)
    unmount_target(Context(cfg, cfg.layout(), runner))
    assert runner.umount_calls == 4
    assert any(op.argv[0] == "gpgconf" for op in runner.operations if isinstance(op, Command))


def test_unmount_gives_up_with_a_diagnostic_when_the_mount_stays_busy():
    cfg = make_config()
    runner = UmountRunner(failures=999)
    with pytest.raises(StepError, match="still using it"):
        unmount_target(Context(cfg, cfg.layout(), runner))
    assert any(op.argv[0] == "fuser" for op in runner.operations if isinstance(op, Command))


def test_stale_encrypted_volumes_from_an_earlier_attempt_are_the_ones_the_installer_uses():
    from dualboot_installer.steps.preflight import STALE_MAPPINGS
    from dualboot_installer.steps.storage import CRYPT_HOME, CRYPT_ROOT

    assert set(STALE_MAPPINGS) == {CRYPT_ROOT, CRYPT_HOME}


def test_dry_run_runner_does_not_run_or_write_anything(tmp_path):
    runner = Runner(dry_run=True, log=lambda line: None)
    target = tmp_path / "must-not-exist"
    runner.write_file(FileWrite(str(target), "x"))
    assert runner.run(Command(["definitely-not-a-real-binary", "--wipe"])) == 0
    assert not target.exists()


def test_runner_writes_files_with_the_requested_mode(tmp_path):
    runner = Runner(dry_run=False, log=lambda line: None)
    target = tmp_path / "sub" / "secret.key"
    runner.write_file(FileWrite(str(target), "data", 0o600, secret=True))
    assert target.read_text() == "data"
    if hasattr(target, "stat") and not __import__("sys").platform.startswith("win"):
        assert target.stat().st_mode & 0o777 == 0o600


def test_quiet_command_hides_its_output_unless_it_fails():
    import sys

    lines: list[str] = []
    runner = Runner(dry_run=False, log=lines.append)
    # The output is built at run time so it does not appear in the logged command line.
    ok = Command([sys.executable, "-c", "print('NO' + 'ISE')"], quiet=True)
    runner.run(ok)
    assert "NOISE" not in "\n".join(lines)

    lines.clear()
    bad = Command([sys.executable, "-c", "print('WH' + 'Y'); raise SystemExit(3)"], quiet=True, check=False)
    assert runner.run(bad) == 3
    assert "WHY" in "\n".join(lines)
