"""Windows steps: pure helpers, and the steps run in dry-run and against scripted answers."""

import pytest

from dualboot_installer.config import InstallConfig
from dualboot_installer.layout import GIB
from dualboot_installer.runner import Command, Runner
from dualboot_installer.steps import windows
from dualboot_installer.steps.context import Context, StepError
from dualboot_installer.windows_iso import parse_wimlib_info

WIM_INFO = """\
Available Images:
-----------------
Index:                  1
Name:                   Windows 11 Home
Edition ID:             Core
Languages:              en-US
Default Language:       en-US
Build:                  26300
Architecture:           x86_64

Index:                  6
Name:                   Windows 11 Pro
Edition ID:             Professional
Languages:              en-US
Default Language:       en-US
Build:                  26300
Architecture:           x86_64
"""

BOOT_WIM_INFO = """\
Available Images:
-----------------
Index:                  1
Name:                   Microsoft Windows PE (x64)
Description:            Microsoft Windows PE (x64)

Index:                  2
Name:                   Microsoft Windows Setup (x64)
Description:            Microsoft Windows Setup (x64)
"""


def make_ctx(answers=None, **windows_fields) -> Context:
    cfg = InstallConfig(dry_run=True)
    cfg.disk.device, cfg.disk.size_bytes, cfg.disk.windows_gib = "/dev/vda", 300 * GIB, 100
    w = cfg.windows
    w.iso_source, w.iso_path, w.edition, w.language_tag = "local", "/media/Win11.iso", "Pro", "fr-FR"
    for key, value in windows_fields.items():
        setattr(w, key, value)
    runner = Runner(dry_run=True, log=lambda line: None, answers=answers or {})
    return Context(cfg, cfg.layout(), runner)


def commands(ctx: Context) -> list[list[str]]:
    return [op.argv for op in ctx.runner.operations if isinstance(op, Command)]


# -- curl -sIL output -------------------------------------------------------------------------------------


def test_parse_headers_reads_the_last_response_after_redirects():
    text = ("HTTP/2 302\r\nlocation: https://cdn.example/win.iso\r\ncontent-length: 0\r\n\r\n"
            "HTTP/2 200\r\ncontent-type: application/octet-stream\r\nContent-Length: 7340032000\r\n\r\n")
    assert windows.parse_headers(text) == (200, 7340032000)


@pytest.mark.parametrize("text, expected", [
    ("", (0, 0)),
    ("HTTP/1.1 403 Forbidden\r\ncontent-length: 312\r\n\r\n", (403, 312)),
    ("HTTP/2 200\r\ncontent-type: text/html\r\n\r\n", (200, 0)),
    ("HTTP/1.1 206 Partial Content\nContent-Length: 5000000000\n\n", (206, 5000000000)),
])
def test_parse_headers_cases(text, expected):
    assert windows.parse_headers(text) == expected


# -- image selection ---------------------------------------------------------------------------------------


def test_pick_image_selects_by_edition_id_not_by_name():
    images = parse_wimlib_info(WIM_INFO)
    assert windows.pick_image(images, "Pro").index == 6
    assert windows.pick_image(images, "Home").index == 1
    assert windows.pick_image(images[:1], "Pro") is None


def test_boot_wim_setup_index_finds_the_setup_image():
    assert windows.boot_wim_setup_index(BOOT_WIM_INFO) == 2
    assert windows.boot_wim_setup_index(BOOT_WIM_INFO.replace("Setup", "Install")) == 2  # documented default
    assert windows.boot_wim_setup_index(BOOT_WIM_INFO.replace("Index:                  2", "Index:                  3")) == 3
    assert windows.boot_wim_setup_index("") == 2


def test_wim_update_script_adds_both_files_under_system32():
    script = windows.wim_update_script("/tmp/stage")
    assert script.splitlines() == [
        "add /tmp/stage/winpeshl.ini /Windows/System32/winpeshl.ini",
        "add /tmp/stage/archinstall.cmd /Windows/System32/archinstall.cmd",
    ]


def test_fat32_parts_stay_below_the_4_gib_limit():
    assert windows.FAT32_PART_MIB * 1024 * 1024 < 4 * 1024**3


# -- the preflight check -----------------------------------------------------------------------------------


def test_a_link_must_answer_200_and_be_big_enough():
    ok = "HTTP/2 200\r\ncontent-length: 7000000000\r\n\r\n"
    ctx = make_ctx(answers={"curl -sIL --max-time 30 https://dl.example/win.iso": ok},
                   iso_source="download", iso_url="https://dl.example/win.iso")
    windows.check_source(ctx)  # does not raise

    for bad in ("HTTP/2 403\r\ncontent-length: 100\r\n\r\n", "HTTP/2 200\r\ncontent-length: 5000\r\n\r\n"):
        ctx = make_ctx(answers={"curl -sIL --max-time 30 https://dl.example/win.iso": bad},
                       iso_source="download", iso_url="https://dl.example/win.iso")
        with pytest.raises(StepError, match="does not give a Windows ISO"):
            windows.check_source(ctx)


def test_a_local_iso_sets_the_language_of_the_answer_file_and_the_image_index():
    answers = {"wimlib-imagex info /tmp/archinstall-windows-iso/sources/install.wim": WIM_INFO}
    ctx = make_ctx(answers=answers)
    windows.check_source(ctx)
    assert ctx.cfg.windows.language_tag == "en-US"  # the ISO decides, whatever the wizard said
    assert ctx.state["image_index"] == 6
    ops = commands(ctx)
    assert ["mount", "-t", "udf", "-o", "loop,ro", "/media/Win11.iso", "/tmp/archinstall-windows-iso"] in ops
    assert ["umount", "/tmp/archinstall-windows-iso"] in ops  # never left mounted


def test_an_iso_without_the_chosen_edition_is_refused_before_anything_is_erased():
    answers = {"wimlib-imagex info /tmp/archinstall-windows-iso/sources/install.wim": WIM_INFO}
    ctx = make_ctx(answers=answers, edition="Home")
    ctx.runner.answers = {"wimlib-imagex info /tmp/archinstall-windows-iso/sources/install.wim":
                          WIM_INFO.split("Index:                  6")[0] + "Index: 9\nName: Windows 11 Education\n"
                          "Edition ID: Education\nDefault Language: en-US\n"}
    ctx.cfg.windows.edition = "Pro"
    with pytest.raises(StepError, match="no Windows 11 Pro image"):
        windows.check_source(ctx)


# -- fetching and staging (dry run) -------------------------------------------------------------------------


def test_a_downloaded_iso_is_fetched_with_a_resumable_download_into_the_new_system():
    ctx = make_ctx(iso_source="download", iso_url="https://dl.example/win.iso")
    windows.fetch_iso(ctx)
    curl = next(argv for argv in commands(ctx) if argv[0] == "curl")
    assert "-C" in curl and "--retry" in curl and curl[-1] == "https://dl.example/win.iso"
    assert "--retry-all-errors" in curl  # a connection dropped midway (curl exit 56) must be retried too
    assert curl[curl.index("-o") + 1] == "/mnt/var/tmp/archinstall-windows11.iso"
    assert ctx.state["windows_iso"] == "/mnt/var/tmp/archinstall-windows11.iso"


def test_a_local_or_included_iso_is_used_where_it_is():
    ctx = make_ctx(iso_source="embedded", iso_path="/run/archiso/bootmnt/windows/Windows11.iso")
    windows.fetch_iso(ctx)
    assert ctx.state["windows_iso"] == "/run/archiso/bootmnt/windows/Windows11.iso"
    assert not any(argv[0] == "curl" for argv in commands(ctx))


def test_staging_copies_everything_but_the_big_image_then_splits_it():
    ctx = make_ctx()
    ctx.state.update(windows_iso="/media/Win11.iso", image_index=6)
    windows.stage_setup(ctx)
    ops = commands(ctx)
    rsync = next(argv for argv in ops if argv[0] == "rsync")
    assert "--exclude=/sources/install.wim" in rsync and "--exclude=/sources/install.esd" in rsync
    assert rsync[-2:] == ["/tmp/archinstall-windows-iso/", "/tmp/archinstall-windows-setup/"]
    split = next(argv for argv in ops if argv[:2] == ["wimlib-imagex", "split"])
    assert split[2:] == ["/tmp/archinstall-windows-iso/sources/install.wim",
                         "/tmp/archinstall-windows-setup/sources/install.swm", "3800"]
    assert ops.index(rsync) < ops.index(split)


def test_staging_mounts_then_unmounts_both_and_makes_windows_setup_the_next_boot():
    ctx = make_ctx()
    ctx.state.update(windows_iso="/media/Win11.iso", image_index=1)
    windows.stage_setup(ctx)
    ops = commands(ctx)
    assert ["umount", "/tmp/archinstall-windows-setup"] in ops and ["umount", "/tmp/archinstall-windows-iso"] in ops
    assert ops.index(["sync"]) < ops.index(["umount", "/tmp/archinstall-windows-setup"])
    assert ops[-1][:2] == ["efibootmgr", "--bootnext"]  # the very last thing it does
    # The last partition (WINSETUP) is the one that gets mounted for the Setup files.
    last = ctx.layout.partitions[-1].number
    assert ["mount", f"/dev/vda{last}", "/tmp/archinstall-windows-setup"] in ops


CREATED = """BootCurrent: 0000
Timeout: 1 seconds
BootOrder: 0000,0001
Boot0000* ArchInstall\tHD(1,GPT,aaaa,0x800,0x200000)/File(\\EFI\\ArchInstall\\shimx64.efi)
Boot0001* UEFI OS\tHD(1,GPT,aaaa,0x800,0x200000)/File(\\EFI\\BOOT\\BOOTX64.EFI)
Boot0004  Windows 11 Setup\tHD(4,GPT,bbbb,0x1000,0x2000)/File(\\EFI\\BOOT\\BOOTX64.EFI)
"""


def firmware_ctx(**answers):
    ctx = make_ctx()
    ctx.state.update(windows_iso="/media/Win11.iso", image_index=1)
    create = ("efibootmgr --create --disk /dev/vda --part {part} --label 'Windows 11 Setup' "
              "--loader '\\EFI\\BOOT\\BOOTX64.EFI'")
    ctx.runner.answers = {create.format(part=ctx.layout.partitions[-1].number): CREATED, **answers}
    return ctx


def test_the_one_time_boot_entry_number_is_read_from_efibootmgr_and_used_for_bootnext():
    ctx = firmware_ctx()
    windows.schedule_setup_boot(ctx)
    ops = commands(ctx)
    assert ["efibootmgr", "--bootnext", "0004"] in ops
    create = next(a for a in ops if a[:2] == ["efibootmgr", "--create"])
    assert "--create-only" not in create
    assert create[create.index("--part") + 1] == str(ctx.layout.partitions[-1].number)


def test_a_boot_entry_left_by_an_earlier_attempt_is_removed_first():
    ctx = firmware_ctx(efibootmgr="Boot0007* Windows 11 Setup\tHD(4,GPT,cc,0x1,0x2)\nBoot0000* ArchInstall\tHD()\n")
    windows.schedule_setup_boot(ctx)
    ops = commands(ctx)
    assert ["efibootmgr", "--bootnum", "0007", "--delete-bootnum"] in ops
    assert ["efibootmgr", "--bootnum", "0000", "--delete-bootnum"] not in ops  # ours (GRUB) stays
    assert ops.index(["efibootmgr", "--bootnum", "0007", "--delete-bootnum"]) < ops.index(
        next(a for a in ops if a[:2] == ["efibootmgr", "--create"]))


def test_a_refused_boot_entry_stops_the_installation_with_a_clear_message():
    ctx = firmware_ctx()
    ctx.runner.dry_run = False  # as in a real run: capture() returning nothing means efibootmgr failed
    ctx.runner.capture = lambda argv, stdin=None: ""  # type: ignore[method-assign]
    with pytest.raises(StepError, match="efibootmgr failed"):
        windows.schedule_setup_boot(ctx)


def test_staging_a_downloaded_iso_deletes_it_afterwards():
    ctx = make_ctx()
    ctx.state.update(windows_iso=windows.DOWNLOAD_TARGET, image_index=1)
    windows.stage_setup(ctx)
    assert ["rm", "-f", windows.DOWNLOAD_TARGET] in commands(ctx)


def test_staging_a_user_iso_never_deletes_it():
    ctx = make_ctx()
    ctx.state.update(windows_iso="/media/Win11.iso", image_index=1)
    windows.stage_setup(ctx)
    assert not any(argv[:1] == ["rm"] and "/media/Win11.iso" in argv for argv in commands(ctx))
