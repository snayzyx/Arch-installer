"""The new Windows gets the network, Wi-Fi and Bluetooth drivers of the old Windows and of the ISO."""

import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from dualboot_installer.config import InstallConfig
from dualboot_installer.runner import Command, FileWrite, Runner
from dualboot_installer.steps import windows
from dualboot_installer.steps import windows_drivers as wd
from dualboot_installer.steps.context import Context
from dualboot_installer.windows_unattend import build_unattend, finish_script

from test_plan import dry_run_everything, make_config
from test_windows_wifi import powershell_parse_errors

REPO = Path(__file__).resolve().parents[2]

LSPCI = """\
00:02.0 VGA compatible controller [0300]: Intel Corporation Iris Xe Graphics [8086:9a49] (rev 01)
00:14.0 USB controller [0c03]: Intel Corporation Tiger Lake-LP USB 3.2 Gen 2x1 xHCI Host Controller [8086:a0ed] (rev 20)
00:14.3 Network controller [0280]: Intel Corporation Wi-Fi 6 AX201 [8086:a0f0] (rev 20)
01:00.0 Ethernet controller [0200]: Realtek Semiconductor Co., Ltd. RTL8111/8168/8411 PCI Express Gigabit Ethernet Controller [10ec:8168] (rev 15)
02:00.0 Non-Volatile memory controller [0108]: Samsung Electronics Co Ltd NVMe SSD Controller [144d:a80a]
03:00.0 Wireless controller [0d11]: Example Bluetooth Radio [1234:abcd]
"""

WIFI_ID = r"PCI\VEN_8086&DEV_A0F0"
ETHERNET_ID = r"PCI\VEN_10EC&DEV_8168"


# -- hardware IDs --------------------------------------------------------------------------------------------


def test_only_network_and_wireless_controllers_become_hardware_ids():
    assert wd.pci_ids_from_lspci(LSPCI) == [WIFI_ID, ETHERNET_ID, r"PCI\VEN_1234&DEV_ABCD"]


def test_nothing_is_found_in_an_empty_lspci_output():
    assert wd.pci_ids_from_lspci("") == []


def usb_device(root: Path, name: str, vendor: str, product: str, device_class: str, interfaces=()):
    folder = root / "bus" / "usb" / "devices" / name
    folder.mkdir(parents=True)
    (folder / "idVendor").write_text(vendor + "\n")
    (folder / "idProduct").write_text(product + "\n")
    (folder / "bDeviceClass").write_text(device_class + "\n")
    for number, interface_class in enumerate(interfaces):
        # sysfs names them 1-5:1.0 (a colon, which Windows cannot create): the code only needs the file inside
        interface = folder / f"interface{number}"
        interface.mkdir()
        (interface / "bInterfaceClass").write_text(interface_class + "\n")
    (folder / "power").mkdir()  # a sub-folder that is not an interface


def test_usb_bluetooth_ethernet_and_vendor_specific_devices_are_kept_and_the_rest_is_not(tmp_path):
    usb_device(tmp_path, "1-7", "8087", "0026", "e0")  # Intel Bluetooth, device class
    usb_device(tmp_path, "1-5", "0489", "e0cd", "00", interfaces=("e0", "e0"))  # Bluetooth, interface class
    usb_device(tmp_path, "1-4", "0bda", "8176", "ff")  # a Wi-Fi dongle
    usb_device(tmp_path, "1-3", "046d", "c52b", "00", interfaces=("03", "03"))  # a keyboard receiver
    usb_device(tmp_path, "usb1", "1d6b", "0002", "09")  # a root hub
    # in the order of the devices on the bus (1-4, 1-5, 1-7)
    assert wd.usb_ids_from_sysfs(tmp_path) == [r"USB\VID_0BDA&PID_8176", r"USB\VID_0489&PID_E0CD",
                                               r"USB\VID_8087&PID_0026"]


def test_a_system_without_usb_information_gives_nothing(tmp_path):
    assert wd.usb_ids_from_sysfs(tmp_path / "nowhere") == []


# -- the driver store of an old Windows ----------------------------------------------------------------------


def package(store: Path, name: str, inf_text: str, *, encoding="utf-16", inf_name="driver.inf", extra=None) -> Path:
    folder = store / name
    folder.mkdir(parents=True)
    (folder / inf_name).write_bytes(inf_text.encode(encoding))
    for file_name, size in (extra or {"driver.sys": 2000, "driver.cat": 500}).items():
        (folder / file_name).write_bytes(b"x" * size)
    return folder


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "FileRepository"
    root.mkdir()
    # Windows' own drivers: UTF-16 with a byte order mark
    package(root, "netwtw10.inf_amd64_aaa", f"[Intel]\n%Wifi%=Install, {WIFI_ID}&SUBSYS_00748086\n", inf_name="netwtw10.inf")
    # an OEM driver in ANSI, with the IDs in lower case
    package(root, "rtl.inf_amd64_bbb", r"[Realtek]" + "\n" + r"%Eth%=Install, pci\ven_10ec&dev_8168" + "\n",
            encoding="cp1252", inf_name="rtl.inf")
    # a graphics driver, not one of ours
    package(root, "nv.inf_amd64_ccc", r"%Gpu%=Install, PCI\VEN_10DE&DEV_2560" + "\n", inf_name="nv.inf")
    # an upper case extension, UTF-16 without a byte order mark
    package(root, "bt.inf_amd64_ddd", r"%Bt%=Install, USB\VID_8087&PID_0026" + "\n", encoding="utf-16-le",
            inf_name="BTUSB.INF")
    (root / "stray-file.txt").write_text("not a package")
    (root / "no-inf-here").mkdir()
    return root


def test_the_packages_that_list_one_of_our_hardware_ids_are_copied_whole(store, tmp_path):
    ids = [WIFI_ID, ETHERNET_ID, r"USB\VID_8087&PID_0026"]
    out = tmp_path / "kept"
    found = wd.harvest_from_store(store, ids, out)
    assert [p.folder for p in found] == ["bt.inf_amd64_ddd", "netwtw10.inf_amd64_aaa", "rtl.inf_amd64_bbb"]
    assert [(p.inf, p.matched) for p in found] == [("BTUSB.INF", r"USB\VID_8087&PID_0026"),
                                                  ("netwtw10.inf", WIFI_ID), ("rtl.inf", ETHERNET_ID)]
    assert all(not p.skipped and p.size >= 2500 for p in found)
    assert sorted(f.name for f in (out / "netwtw10.inf_amd64_aaa").iterdir()) == ["driver.cat", "driver.sys", "netwtw10.inf"]
    assert not (out / "nv.inf_amd64_ccc").exists()  # a graphics driver is not taken


def test_a_hardware_id_not_in_any_inf_matches_nothing(store, tmp_path):
    assert wd.harvest_from_store(store, [r"PCI\VEN_9999&DEV_0001"], tmp_path / "kept") == []
    assert not (tmp_path / "kept").exists()


def test_the_size_limit_skips_what_does_not_fit_and_says_so(store, tmp_path):
    big = package(store, "huge.inf_amd64_eee", f"{WIFI_ID}\n", inf_name="huge.inf", extra={"huge.sys": 50_000})
    found = wd.harvest_from_store(store, [WIFI_ID, ETHERNET_ID], tmp_path / "kept", cap=10_000)
    by_name = {p.folder: p for p in found}
    assert by_name[big.name].skipped.startswith("over the")
    assert not (tmp_path / "kept" / big.name).exists()
    assert not by_name["netwtw10.inf_amd64_aaa"].skipped and (tmp_path / "kept" / "netwtw10.inf_amd64_aaa").is_dir()


def test_an_inf_is_read_in_every_encoding_windows_uses(tmp_path):
    for encoding, expected in (("utf-16", True), ("utf-16-le", True), ("utf-8-sig", True), ("cp1252", True)):
        path = tmp_path / f"{encoding}.inf"
        path.write_bytes(f"; \u00e9\u00e8 test\n{WIFI_ID}\n".encode(encoding, errors="replace"))
        assert (WIFI_ID in wd.read_inf(path).upper()) is expected, encoding


# -- the step ------------------------------------------------------------------------------------------------


class FakeRunner(Runner):
    """Records the commands; `outputs` answers capture(), `fail` lists substrings of commands that exit 1."""

    def __init__(self, *, outputs=None, fail=(), dry_run=False):
        super().__init__(dry_run=dry_run, log=self._remember)
        self.lines: list[str] = []
        self.outputs = outputs or {}
        self.fail = list(fail)

    def _remember(self, line: str) -> None:
        self.lines.append(line)

    def run(self, command: Command) -> int:
        self.operations.append(command)
        text = " ".join(command.argv)
        return 1 if any(marker in text for marker in self.fail) else 0

    def capture(self, argv, stdin=None) -> str:
        self.operations.append(Command(argv))
        text = " ".join(argv)
        for marker, answer in self.outputs.items():
            if marker in text:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return ""

    def write_file(self, op) -> None:
        self.operations.append(op)


LSBLK_NTFS = "/dev/vda1 part\n/dev/vda2 part\n/dev/vda3 part\n"  # lsblk lists the partitions...
FS_NTFS = {"TYPE /dev/vda1": "vfat\n", "TYPE /dev/vda2": "ntfs\n", "TYPE /dev/vda3": "ntfs\n"}  # ...blkid names them


@pytest.fixture
def old_windows(tmp_path, monkeypatch, store):
    """A mounted 'old Windows' the fake runner pretends to have mounted at <mount>/1."""
    mount = tmp_path / "mnt"
    target = mount / "1" / "Windows" / "System32" / "DriverStore"
    target.mkdir(parents=True)
    shutil.move(str(store), str(target / "FileRepository"))
    monkeypatch.setattr(wd, "OLD_WINDOWS_MOUNT", str(mount))
    monkeypatch.setattr(wd, "HARVEST_DIR", tmp_path / "harvest")
    monkeypatch.setattr(wd, "usb_ids_from_sysfs", lambda *a, **k: [r"USB\VID_8087&PID_0026"])
    return mount


def make_ctx(runner: Runner, **cfg_fields) -> Context:
    cfg = InstallConfig()
    cfg.disk.device, cfg.disk.size_bytes, cfg.disk.windows_gib = "/dev/vda", 300 * 1024**3, 100
    for key, value in cfg_fields.items():
        setattr(cfg, key, value)
    return Context(cfg, cfg.layout(), runner)


def commands(runner: Runner) -> list[str]:
    return [" ".join(op.argv) for op in runner.operations if isinstance(op, Command)]


def test_the_drivers_of_the_old_windows_are_kept_before_the_erase(old_windows):
    runner = FakeRunner(outputs={"lspci": LSPCI, "lsblk": LSBLK_NTFS, **FS_NTFS})
    ctx = make_ctx(runner)
    wd.harvest_drivers(ctx)
    kept = {entry["folder"] for entry in ctx.state["drivers_harvested"]}
    assert kept == {"bt.inf_amd64_ddd", "netwtw10.inf_amd64_aaa", "rtl.inf_amd64_bbb"}
    assert (wd.HARVEST_DIR / "netwtw10.inf_amd64_aaa" / "netwtw10.inf").is_file()
    assert any("Kept 3 driver package(s)" in line and "netwtw10.inf_amd64_aaa" in line for line in runner.lines)
    assert wd.driver_sources() == [wd.HARVEST_DIR]


def test_the_old_windows_is_only_ever_mounted_read_only_and_always_unmounted(old_windows):
    runner = FakeRunner(outputs={"lspci": LSPCI, "lsblk": LSBLK_NTFS, **FS_NTFS})
    wd.harvest_drivers(make_ctx(runner))
    mounts = [c for c in commands(runner) if c.startswith("mount ")]
    assert len(mounts) == 2  # two NTFS partitions, each tried with ntfs-3g
    assert all("-o ro," in c and " rw" not in c and "remount" not in c for c in mounts)
    assert len([c for c in commands(runner) if c.startswith("umount ")]) == 2  # the second one has no store
    assert any("no Windows driver store" in line for line in runner.lines)


def test_a_bitlocker_volume_is_skipped_with_the_manual_fallback(old_windows):
    runner = FakeRunner(outputs={"lspci": LSPCI, "lsblk": "/dev/vda2 part\n", "TYPE /dev/vda2": "BitLocker\n"})
    ctx = make_ctx(runner)
    wd.harvest_drivers(ctx)
    assert not any(c.startswith("mount ") for c in commands(runner))
    assert any("encrypted with BitLocker" in line and "tethering" in line for line in runner.lines)
    assert "drivers_harvested" not in ctx.state


def test_a_volume_that_cannot_be_mounted_is_skipped_not_fatal(old_windows):
    runner = FakeRunner(outputs={"lspci": LSPCI, "lsblk": LSBLK_NTFS, **FS_NTFS}, fail=["mount -t"])
    ctx = make_ctx(runner)
    wd.harvest_drivers(ctx)  # does not raise
    assert any("cannot be read" in line for line in runner.lines)
    assert "drivers_harvested" not in ctx.state


def test_a_disk_without_ntfs_has_no_windows_to_take_drivers_from(old_windows):
    runner = FakeRunner(outputs={"lspci": LSPCI, "lsblk": "/dev/vda1 part\n/dev/vda2 part\n",
                                 "TYPE /dev/vda1": "vfat\n", "TYPE /dev/vda2": "ext4\n"})
    wd.harvest_drivers(make_ctx(runner))
    assert any("no existing Windows" in line for line in runner.lines)


def test_without_any_network_adapter_nothing_is_looked_up(old_windows, monkeypatch):
    monkeypatch.setattr(wd, "usb_ids_from_sysfs", lambda *a, **k: [])
    runner = FakeRunner(outputs={"lspci": "00:02.0 VGA compatible controller [0300]: Intel [8086:9a49]\n"})
    wd.harvest_drivers(make_ctx(runner))
    assert not any(c.startswith("mount ") or c.startswith("lsblk") for c in commands(runner))


def test_an_unexpected_error_is_a_warning_never_a_failure(old_windows):
    runner = FakeRunner(outputs={"lspci": OSError("lspci exploded")})
    wd.harvest_drivers(make_ctx(runner))  # does not raise
    assert any("WARNING" in line and "lspci exploded" in line for line in runner.lines)


def test_partial_mode_and_dry_runs_touch_nothing(old_windows):
    runner = FakeRunner(dry_run=True)
    wd.harvest_drivers(make_ctx(runner))
    assert runner.operations == [] and any("Would look for" in line for line in runner.lines)
    runner = FakeRunner()
    wd.harvest_drivers(make_ctx(runner, partial=True))
    assert runner.operations == [] and runner.lines == []


def test_the_preflight_of_a_full_install_looks_for_drivers_and_a_partial_one_does_not():
    runner, lines = dry_run_everything(make_config())
    assert any("Would look for the Wi-Fi, network and Bluetooth drivers" in line for line in lines)
    partial = make_config()
    partial.partial = True
    runner, lines = dry_run_everything(partial)
    assert not any("Would look for the Wi-Fi, network and Bluetooth drivers" in line for line in lines)


# -- giving them to Windows ----------------------------------------------------------------------------------

NS = {"u": "urn:schemas-microsoft-com:unattend"}
SETUP = "/tmp/archinstall-windows-setup"


def answer_file(**kwargs) -> ET.Element:
    text = build_unattend(InstallConfig(), disk_id=0, partition_id=2, image_index=1, **kwargs)
    return ET.fromstring(text.split("?>", 1)[1])


def test_the_answer_file_lists_c_drivers_only_when_there_are_drivers():
    with_drivers = answer_file(drivers=True)
    path = with_drivers.find("u:settings[@pass='specialize']/u:component[@name='Microsoft-Windows-PnpCustomizationsNonWinPE']"
                             "/u:DriverPaths/u:PathAndCredentials/u:Path", NS)
    assert path is not None and path.text == "C:\\Drivers"
    assert "PnpCustomizations" not in build_unattend(InstallConfig(), disk_id=0, partition_id=2, image_index=1)
    assert answer_file().find(".//u:DriverPaths", NS) is None


def test_the_answer_file_without_drivers_is_exactly_what_it_was():
    plain = build_unattend(InstallConfig(), disk_id=0, partition_id=2, image_index=1)
    assert plain == build_unattend(InstallConfig(), disk_id=0, partition_id=2, image_index=1, drivers=False)
    with_component = build_unattend(InstallConfig(), disk_id=0, partition_id=2, image_index=1, drivers=True)
    assert with_component.replace(wd_component(), "") == plain  # the only difference is the component


def wd_component() -> str:
    from dualboot_installer.windows_unattend import drivers_component

    return drivers_component()


def staged(monkeypatch, tmp_path, *, harvested=True, bundled=False):
    from test_windows_steps import make_ctx as windows_ctx

    sources = []
    if harvested:
        (tmp_path / "harvest" / "netwtw10.inf_amd64_aaa").mkdir(parents=True)
        sources.append(tmp_path / "harvest")
    if bundled:
        (tmp_path / "bundled" / "intel-ax211").mkdir(parents=True)
        sources.append(tmp_path / "bundled")
    monkeypatch.setattr(windows, "driver_sources", lambda: sources)
    monkeypatch.setattr(wd, "_package_folders", lambda directory: sorted(p for p in Path(directory).iterdir() if p.is_dir()))
    ctx = windows_ctx()
    ctx.state.update(windows_iso="/media/Win11.iso", image_index=1)
    windows.stage_setup(ctx)
    files = {op.path: op for op in ctx.runner.operations if isinstance(op, FileWrite)}
    return ctx, files


def test_staging_copies_the_driver_folders_into_oem_dollar_1_and_tells_the_answer_files(monkeypatch, tmp_path):
    ctx, files = staged(monkeypatch, tmp_path, bundled=True)
    ops = [op.argv for op in ctx.runner.operations if isinstance(op, Command)]
    destination = f"{SETUP}/sources/$OEM$/$1/Drivers/"
    rsyncs = [argv for argv in ops if argv[0] == "rsync" and argv[-1] == destination]
    assert [argv[-2] for argv in rsyncs] == [f"{tmp_path / 'harvest'}/", f"{tmp_path / 'bundled'}/"]
    assert ["mkdir", "-p", destination.rstrip("/")] in ops
    answers = [op for path, op in files.items() if "unattend-0.xml" in path]
    assert answers and "C:\\Drivers" in answers[0].content
    finish = files[f"{SETUP}/sources/$OEM$/$$/Setup/Scripts/archinstall-finish.ps1"].content
    assert "Get-WindowsDriver" in finish and "Remove-Item -Path 'C:\\Drivers'" in finish
    assert ["rm", "-rf", str(wd.HARVEST_DIR)] in ops  # the RAM disk is freed after the copy


def test_staging_without_any_driver_changes_nothing(monkeypatch, tmp_path):
    ctx, files = staged(monkeypatch, tmp_path, harvested=False, bundled=False)
    ops = [op.argv for op in ctx.runner.operations if isinstance(op, Command)]
    assert not any("Drivers" in " ".join(argv) for argv in ops)
    answers = [op for path, op in files.items() if "unattend-" in path]
    assert answers and all("PnpCustomizations" not in op.content for op in answers)
    finish = files[f"{SETUP}/sources/$OEM$/$$/Setup/Scripts/archinstall-finish.ps1"].content
    assert "Get-WindowsDriver" not in finish


def test_the_finish_script_with_drivers_is_valid_powershell():
    cfg = InstallConfig()
    assert powershell_parse_errors(finish_script(cfg.windows, shared_partition=False, drivers=True)) == []
    assert powershell_parse_errors(finish_script(cfg.windows, shared_partition=True, wifi=True, drivers=True)) == []


def test_bundled_drivers_are_counted_for_the_summary(monkeypatch, tmp_path):
    from dualboot_installer.screens.summary import describe_drivers

    monkeypatch.setattr(wd, "BUNDLED_DIR", tmp_path / "none")
    monkeypatch.setattr("dualboot_installer.screens.summary.bundled_driver_count", wd.bundled_driver_count)
    assert "kept" in describe_drivers() and "bundled" not in describe_drivers()
    (tmp_path / "bundled" / "a").mkdir(parents=True)
    (tmp_path / "bundled" / "b").mkdir()
    monkeypatch.setattr(wd, "BUNDLED_DIR", tmp_path / "bundled")
    assert describe_drivers().endswith("plus 2 bundled in this installer")


# -- building the ISO with a drivers folder -------------------------------------------------------------------


def test_build_options_are_plumbed_from_the_scripts_to_the_container():
    build = (REPO / "build.ps1").read_text(encoding="utf-8")
    assert "[string]$DriversDir" in build and "/drivers-source:ro" in build and "DRIVERS_DIR=/drivers-source" in build
    assert "holds no .inf file" in build  # an empty folder is refused before a 20 minute build
    rebuild = (REPO / "scripts" / "rebuild-all.ps1").read_text(encoding="utf-8")
    assert "$DriversDir" in rebuild and "DRIVERS_SRC=/drivers-source" in rebuild
    script = (REPO / "docker" / "build-iso.sh").read_text(encoding="utf-8")
    assert 'DRIVERS_DIR="${DRIVERS_DIR:-}"' in script and "attach_drivers" in script
    assert '"$ISOFS/$WINDOWS_DIR/drivers"' in script  # /windows/drivers on the ISO, where the installer looks
    verify = (REPO / "docker" / "verify-iso.sh").read_text(encoding="utf-8")
    assert "DRIVERS_SRC" in verify and "rsync -rcni" in verify


def test_the_installer_looks_for_bundled_drivers_where_the_build_puts_them():
    assert wd.BUNDLED_DIR.as_posix() == "/run/archiso/bootmnt/windows/drivers"


@pytest.mark.parametrize("script", ["docker/build-iso.sh", "docker/verify-iso.sh"])
def test_the_container_scripts_are_valid_bash(script):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash on this machine")
    text = (REPO / script).read_text(encoding="utf-8").replace("\r\n", "\n")
    result = subprocess.run([bash, "-n"], input=text.encode("utf-8"), capture_output=True)  # bytes: no CRLF added
    if result.returncode != 0 and b"\x00" in result.stdout:
        # Windows' bash.exe is the WSL launcher: when WSL itself fails (service timeout...) it prints a UTF-16
        # error on stdout, which says nothing about the script.
        pytest.skip("the WSL launcher failed: " + result.stdout.decode("utf-16-le", errors="replace").strip()[:100])
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


@pytest.mark.parametrize("script", ["build.ps1", "scripts/rebuild-all.ps1"])
def test_the_build_scripts_are_valid_powershell(script):
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        pytest.skip("no PowerShell on this machine")
    command = ("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
               f"'{REPO / script}', [ref]$null, [ref]$e); $e | ForEach-Object {{ $_.Message }}")
    result = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True)
    assert result.stdout.strip() == ""
