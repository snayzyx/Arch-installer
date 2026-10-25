import base64
import re
import xml.etree.ElementTree as ET

import pytest

from dualboot_installer.config import InstallConfig
from dualboot_installer.layout import GIB
from dualboot_installer.steps.bootloader import BOOTLOADER_ID
from dualboot_installer.windows_unattend import (
    BOOT_ORDER_TASK,
    GENERIC_KEYS,
    MAX_DISKS,
    REMOVED_APPS,
    SETUP_ENTRY_TITLE,
    SETUP_LOADER,
    bootorder_script,
    build_unattend,
    encode_password,
    finish_script,
    launcher_cmd,
    setup_complete_cmd,
    specialize_commands,
    winpeshl_ini,
)

NS = {"u": "urn:schemas-microsoft-com:unattend", "wcm": "http://schemas.microsoft.com/WMIConfig/2002/State"}
GUID = "8D0E7B4E-1A2B-4C3D-9E8F-0123456789AB"


def make_config(**windows) -> InstallConfig:
    cfg = InstallConfig(dry_run=True)
    cfg.disk.device, cfg.disk.size_bytes, cfg.disk.windows_gib, cfg.disk.guid = "/dev/vda", 300 * GIB, 100, GUID
    cfg.arch.country, cfg.arch.timezone, cfg.arch.keymap = "FR", "Europe/Paris", "fr"
    cfg.arch.user_password = "arch-pw"
    w = cfg.windows
    w.language_tag, w.edition, w.computer_name, w.username, w.password = "fr-FR", "Pro", "WINTEST", "alice", "Wind0ws!"
    for key, value in windows.items():
        setattr(w, key, value)
    return cfg


def parse(cfg=None, disk=3, partition=2, index=6) -> ET.Element:
    return ET.fromstring(build_unattend(cfg or make_config(), disk_id=disk, partition_id=partition, image_index=index))


def component(root: ET.Element, pass_name: str, name: str) -> ET.Element:
    for settings in root.findall("u:settings", NS):
        if settings.get("pass") == pass_name:
            for comp in settings.findall("u:component", NS):
                if comp.get("name") == name:
                    return comp
    raise AssertionError(f"no component {name} in pass {pass_name}")


def text(element: ET.Element, path: str) -> str:
    found = element.find(path, NS)
    assert found is not None, path
    return found.text or ""


# -- the answer file -------------------------------------------------------------------------------


def test_the_answer_file_is_well_formed_xml_with_the_three_passes():
    root = parse()
    assert root.tag == "{urn:schemas-microsoft-com:unattend}unattend"
    assert [s.get("pass") for s in root.findall("u:settings", NS)] == ["windowsPE", "specialize", "oobeSystem"]


def test_setup_installs_to_the_right_disk_partition_and_image():
    setup = component(parse(disk=5, partition=2, index=6), "windowsPE", "Microsoft-Windows-Setup")
    assert text(setup, "u:ImageInstall/u:OSImage/u:InstallTo/u:DiskID") == "5"
    assert text(setup, "u:ImageInstall/u:OSImage/u:InstallTo/u:PartitionID") == "2"
    assert text(setup, "u:ImageInstall/u:OSImage/u:InstallFrom/u:MetaData/u:Key") == "/IMAGE/INDEX"
    assert text(setup, "u:ImageInstall/u:OSImage/u:InstallFrom/u:MetaData/u:Value") == "6"
    # Only the Windows partition is formatted; the disk is never wiped by Setup (we did the layout).
    assert text(setup, "u:DiskConfiguration/u:Disk/u:WillWipeDisk") == "false"
    assert text(setup, "u:DiskConfiguration/u:Disk/u:DiskID") == "5"
    modify = setup.find("u:DiskConfiguration/u:Disk/u:ModifyPartitions/u:ModifyPartition", NS)
    assert text(modify, "u:PartitionID") == "2" and text(modify, "u:Format") == "NTFS"
    assert text(modify, "u:Letter") == "C"
    assert len(setup.findall("u:DiskConfiguration/u:Disk/u:ModifyPartitions/u:ModifyPartition", NS)) == 1


def test_each_answer_file_differs_from_the_others_only_by_the_disk_number():
    cfg = make_config()
    files = [build_unattend(cfg, disk_id=n, partition_id=2, image_index=1) for n in range(MAX_DISKS)]
    assert len(set(files)) == MAX_DISKS
    normalised = {re.sub(r"<DiskID>\d+</DiskID>", "<DiskID>N</DiskID>", f) for f in files}
    assert len(normalised) == 1


def test_the_edition_picks_the_generic_key():
    for edition in ("Home", "Pro"):
        setup = component(parse(make_config(edition=edition)), "windowsPE", "Microsoft-Windows-Setup")
        assert text(setup, "u:UserData/u:ProductKey/u:Key") == GENERIC_KEYS[edition]
    assert text(component(parse(), "windowsPE", "Microsoft-Windows-Setup"), "u:UserData/u:AcceptEula") == "true"


def test_hardware_checks_are_bypassed_in_the_windows_pe_pass():
    setup = component(parse(), "windowsPE", "Microsoft-Windows-Setup")
    commands = [e.text for e in setup.findall("u:RunSynchronous/u:RunSynchronousCommand/u:Path", NS)]
    for name in ("BypassTPMCheck", "BypassSecureBootCheck", "BypassRAMCheck", "BypassCPUCheck", "BypassStorageCheck"):
        assert any(name in c and "HKLM\\SYSTEM\\Setup\\LabConfig" in c and "/d 1" in c for c in commands), name
    orders = [e.text for e in setup.findall("u:RunSynchronous/u:RunSynchronousCommand/u:Order", NS)]
    assert orders == [str(i) for i in range(1, len(orders) + 1)]


def test_the_language_comes_from_the_iso_and_the_keyboard_and_time_zone_from_the_answers():
    root = parse()
    for pass_name, name in (("windowsPE", "Microsoft-Windows-International-Core-WinPE"),
                            ("oobeSystem", "Microsoft-Windows-International-Core")):
        intl = component(root, pass_name, name)
        assert text(intl, "u:UILanguage") == "fr-FR" and text(intl, "u:SystemLocale") == "fr-FR"
        assert text(intl, "u:UserLocale") == "fr-FR" and text(intl, "u:InputLocale") == "fr-FR"
    shell = component(root, "specialize", "Microsoft-Windows-Shell-Setup")
    assert text(shell, "u:TimeZone") == "Romance Standard Time"
    assert text(component(root, "oobeSystem", "Microsoft-Windows-Shell-Setup"), "u:TimeZone") == "Romance Standard Time"


def test_a_separate_windows_keyboard_and_time_zone_are_honoured():
    cfg = make_config(custom_regional=True, timezone="America/New_York", keyboard="en-US")
    root = parse(cfg)
    assert text(component(root, "oobeSystem", "Microsoft-Windows-International-Core"), "u:InputLocale") == "en-US"
    assert text(component(root, "specialize", "Microsoft-Windows-Shell-Setup"), "u:TimeZone") == "Eastern Standard Time"


def test_the_computer_name_and_the_local_administrator_account():
    root = parse()
    assert text(component(root, "specialize", "Microsoft-Windows-Shell-Setup"), "u:ComputerName") == "WINTEST"
    shell = component(root, "oobeSystem", "Microsoft-Windows-Shell-Setup")
    account = shell.find("u:UserAccounts/u:LocalAccounts/u:LocalAccount", NS)
    assert text(account, "u:Name") == "alice" and text(account, "u:Group") == "Administrators"
    assert text(account, "u:Password/u:PlainText") == "false"
    assert text(shell, "u:OOBE/u:HideOnlineAccountScreens") == "true"  # no Microsoft account screen
    assert text(shell, "u:OOBE/u:HideLocalAccountScreen") == "true"
    assert text(shell, "u:OOBE/u:HideEULAPage") == "true"


def test_password_is_base64_of_utf16le_with_the_suffix_setup_expects():
    encoded = encode_password("Wind0ws!")
    assert base64.b64decode(encoded).decode("utf-16-le") == "Wind0ws!Password"
    assert base64.b64decode(encode_password("x", "AdministratorPassword")).decode("utf-16-le") == "xAdministratorPassword"
    shell = component(parse(), "oobeSystem", "Microsoft-Windows-Shell-Setup")
    assert text(shell, "u:UserAccounts/u:LocalAccounts/u:LocalAccount/u:Password/u:Value") == encoded
    assert "Wind0ws!" not in build_unattend(make_config(), disk_id=0, partition_id=2, image_index=1)


def test_the_arch_password_is_used_when_the_user_asks_for_the_same_password():
    cfg = make_config(same_password_as_arch=True, password="")
    shell = component(parse(cfg), "oobeSystem", "Microsoft-Windows-Shell-Setup")
    value = text(shell, "u:UserAccounts/u:LocalAccounts/u:LocalAccount/u:Password/u:Value")
    assert base64.b64decode(value).decode("utf-16-le") == "arch-pwPassword"


@pytest.mark.parametrize("name", ['A<B>&"x\'', "</ComputerName><x/>", "]]>"])
def test_names_cannot_inject_xml(name):
    cfg = make_config(username=name, computer_name=name)
    root = parse(cfg)  # still well-formed
    assert text(component(root, "specialize", "Microsoft-Windows-Shell-Setup"), "u:ComputerName") == name
    shell = component(root, "oobeSystem", "Microsoft-Windows-Shell-Setup")
    assert text(shell, "u:UserAccounts/u:LocalAccounts/u:LocalAccount/u:Name") == name
    assert len(root.findall(".//u:LocalAccount", NS)) == 1  # nothing extra was smuggled in


def test_the_always_applied_tweaks_are_in_the_specialize_pass():
    commands = specialize_commands()
    joined = "\n".join(commands)
    assert "RealTimeIsUniversal" in joined and "/d 1" in next(c for c in commands if "RealTimeIsUniversal" in c)
    assert "PreventDeviceEncryption" in joined
    assert "AllowTelemetry" in joined and "DisableWindowsConsumerFeatures" in joined
    assert "BypassNRO" in joined
    deployment = component(parse(), "specialize", "Microsoft-Windows-Deployment")
    paths = [e.text for e in deployment.findall("u:RunSynchronous/u:RunSynchronousCommand/u:Path", NS)]
    assert paths == commands


# -- WinPE launcher ----------------------------------------------------------------------------------


def test_launcher_finds_the_disk_by_guid_and_starts_setup_with_the_matching_answer_file():
    script = launcher_cmd("{" + GUID.lower() + "}")
    assert "\r\n" in script and "\n" not in script.replace("\r\n", "")  # CRLF only: cmd.exe is picky
    assert "wpeinit" in script
    assert f'find /i "{GUID}"' in script  # braces stripped, case-insensitive match
    assert f"for /l %%i in (0,1,{MAX_DISKS - 1})" in script
    assert "echo select disk %%i& echo detail disk" in script
    assert "\\archinstall\\unattend-%DISK%.xml" in script
    assert "setup.exe /unattend:%ANSWER%" in script
    assert ":nodisk" in script and ":noanswer" in script  # it must stop, not guess, when lost
    letters = re.search(r"for %%d in \((.*?)\) do", script).group(1).split()
    assert "X" not in letters and "C" in letters  # X: is the WinPE RAM disk


def test_winpeshl_ini_runs_the_launcher_through_cmd():
    ini = winpeshl_ini()
    assert ini.startswith("[LaunchApps]\r\n")
    assert "cmd.exe, /c %SYSTEMDRIVE%\\Windows\\System32\\archinstall.cmd" in ini


# -- scripts copied into the installed Windows ---------------------------------------------------------


def test_setup_complete_runs_the_powershell_script_and_logs():
    cmd = setup_complete_cmd()
    assert "archinstall-finish.ps1" in cmd and "-ExecutionPolicy Bypass" in cmd
    assert "archinstall-finish.log" in cmd and cmd.endswith("exit /b 0\r\n")


def test_grub_is_put_first_by_a_task_at_the_next_start_not_during_setup_complete():
    # Found by the end-to-end test: moving GRUB first inside SetupComplete made Windows' own last
    # automatic restart land on shim's key-enrollment screen with nobody there; with Secure Boot
    # on, Windows could not finish its first start. The change is deferred to the next start.
    finish = finish_script(make_config().windows, shared_partition=False)
    assert "displayorder" not in finish.replace("fallback", "")  # not done directly in the finish script...
    assert "Register-ScheduledTask" in finish and "-AtStartup" in finish and f"'{BOOT_ORDER_TASK}'" in finish
    assert "& $bootOrderScript" in finish  # ...except as the fallback when the task cannot be created

    order = bootorder_script()
    assert "bcdedit /enum firmware /v" in order and "displayorder $grub /addfirst" in order
    assert f"$entry -match '{BOOTLOADER_ID}'" in order  # the name GRUB's firmware entry is created with
    assert f"Unregister-ScheduledTask -TaskName '{BOOT_ORDER_TASK}'" in order
    assert "is first" in order and "no firmware entry named ArchInstall" in order and "Say 'done'" in order
    assert order.index("displayorder") < order.index("Unregister-ScheduledTask")


def test_finish_script_handles_the_setup_partition():
    w = make_config().windows
    keep = finish_script(w, shared_partition=True)
    drop = finish_script(w, shared_partition=False)
    for script in (keep, drop):
        assert "FileSystemLabel -eq 'WINSETUP'" in script
        assert "Remove-AppxProvisionedPackage" in script
        for app in REMOVED_APPS:
            assert f"'{app}'" in script
    assert "if ($true)" in keep and "if ($false)" in drop
    assert "Format-Volume" in keep and "NewFileSystemLabel 'Shared'" in keep
    assert "Remove-Partition" in drop


def test_finish_script_says_what_it_did_so_that_its_log_is_never_empty():
    script = finish_script(make_config().windows, shared_partition=False)
    assert script.count("Say ") >= 8 and "function Say" in script
    for message in ("removed $name", "scheduled the firmware boot order change", "deleted the one-time entry",
                    "(WINSETUP) removed", "no WINSETUP partition found", "Say 'done'"):
        assert message in script, message
    # Every step logs; the log is what tells why a boot order or a partition was not changed.
    assert "{0:s} {1}" in script  # the timestamp format survived the f-string


def test_apps_users_expect_are_not_removed():
    for keep in ("Microsoft.WindowsStore", "Microsoft.WindowsCalculator", "Microsoft.Windows.Photos",
                 "Microsoft.WindowsNotepad", "Microsoft.Paint", "Microsoft.WindowsTerminal"):
        assert keep not in REMOVED_APPS


def test_the_one_time_setup_boot_entry_is_cleaned_up_at_the_end_of_setup():
    script = finish_script(make_config().windows, shared_partition=False)
    assert f"'{SETUP_ENTRY_TITLE}'" in script
    assert "$id = $Matches[0]" in script and "bcdedit /delete $id" in script
    assert script.index("$id = $Matches[0]") < script.index("bcdedit /delete $id")


def test_entry_constants():
    assert SETUP_ENTRY_TITLE == "Windows 11 Setup"
    assert SETUP_LOADER == "\\EFI\\BOOT\\BOOTX64.EFI"
