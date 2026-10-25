import json
import subprocess
import sys
from pathlib import Path

from dualboot_installer import windows_iso
from dualboot_installer.windows_iso import load_manifest, parse_wimlib_info

# Trimmed output of `wimlib-imagex info install.wim` for a multi-edition Windows 11 ISO.
WIM_INFO = """\
WIM Information:
----------------
Path:           /mnt/sources/install.wim
GUID:           0x1234
Version:        68864
Image Count:    3
Compression:    LZX
Chunk Size:     32768 bytes
Part Number:    1/1
Boot Index:     0
Size:           5000000000 bytes
Attributes:     Relative path junction

Available Images:
-----------------
Index:                  1
Name:                   Windows 11 Home
Description:            Windows 11 Home
Display Name:           Windows 11 Home
Directory Count:        23346
Architecture:           x86_64
Edition ID:             Core
Installation Type:      Client
Languages:              en-US
Default Language:       en-US
Major Version:          10
Minor Version:          0
Build:                  26300
Flags:                  Core

Index:                  2
Name:                   Windows 11 Home N
Architecture:           x86_64
Edition ID:             CoreN
Languages:              en-US
Default Language:       en-US
Build:                  26300

Index:                  3
Name:                   Windows 11 Pro
Description:            Windows 11 Pro
Architecture:           x86_64
Edition ID:             Professional
Installation Type:      Client
Languages:              en-US
Default Language:       en-US
Build:                  26300
Flags:                  Professional
"""

# A French ISO: the image names are localized, the Edition IDs are not.
WIM_INFO_FR = WIM_INFO.replace("Windows 11 Home\n", "Windows 11 Famille\n").replace("en-US", "fr-FR")


def test_parse_wimlib_info():
    images = parse_wimlib_info(WIM_INFO)
    assert [(i.index, i.name, i.edition_id) for i in images] == [
        (1, "Windows 11 Home", "Core"), (2, "Windows 11 Home N", "CoreN"), (3, "Windows 11 Pro", "Professional")]
    assert images[0].languages == ("en-US",) and images[0].default_language == "en-US"
    assert images[0].build == "26300" and images[0].architecture == "x86_64"
    assert [i.edition for i in images] == ["Home", None, "Pro"]


def test_header_fields_before_the_image_list_are_ignored():
    assert len(parse_wimlib_info(WIM_INFO)) == 3
    assert parse_wimlib_info("nothing useful here") == []


def test_editions_are_detected_by_id_not_by_localized_name():
    images = parse_wimlib_info(WIM_INFO_FR)
    assert images[0].name == "Windows 11 Famille" and images[0].edition == "Home"
    assert images[0].default_language == "fr-FR"


def build_manifest_with_cli(tmp_path: Path, wim_info: str = WIM_INFO, size: int = 10) -> Path:
    (tmp_path / "wim-info.txt").write_text(wim_info, encoding="utf-8")
    (tmp_path / "Windows11.iso").write_bytes(b"x" * size)
    result = subprocess.run(
        [sys.executable, "-m", "dualboot_installer.windows_iso", "make-manifest",
         "--file", "Windows11.iso", "--size", str(size), "--sha256", "ab" * 32,
         "--wim-info", str(tmp_path / "wim-info.txt"), "--output", str(tmp_path / "manifest.json")],
        capture_output=True, text=True, cwd=Path(windows_iso.__file__).parent.parent,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return tmp_path


def test_manifest_roundtrip_through_the_build_command(tmp_path):
    directory = build_manifest_with_cli(tmp_path)
    info = load_manifest(directory)

    assert info is not None
    assert info.path == directory / "Windows11.iso" and info.size == 10 and info.sha256 == "ab" * 32
    assert info.editions == ["Pro", "Home"]
    assert info.image_for("Pro").index == 3 and info.image_for("Home").index == 1
    assert info.language_tag == "en-US"
    assert json.loads((directory / "manifest.json").read_text())["version"] == 1


def test_language_comes_from_the_iso(tmp_path):
    info = load_manifest(build_manifest_with_cli(tmp_path, WIM_INFO_FR))
    assert info.language_tag == "fr-FR"


def test_manifest_is_ignored_when_the_iso_does_not_match(tmp_path):
    directory = build_manifest_with_cli(tmp_path)
    (directory / "Windows11.iso").write_bytes(b"truncated")
    assert load_manifest(directory) is None

    (directory / "Windows11.iso").unlink()
    assert load_manifest(directory) is None


def test_no_manifest_means_no_embedded_iso(tmp_path):
    assert load_manifest(tmp_path) is None
    (tmp_path / "manifest.json").write_text("not json")
    assert load_manifest(tmp_path) is None


def test_build_command_rejects_an_iso_without_home_or_pro(tmp_path):
    only_education = WIM_INFO.replace("Core", "Education").replace("Professional", "EducationN")
    (tmp_path / "wim-info.txt").write_text(only_education, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "dualboot_installer.windows_iso", "make-manifest",
         "--file", "Windows11.iso", "--size", "1", "--sha256", "00",
         "--wim-info", str(tmp_path / "wim-info.txt"), "--output", str(tmp_path / "manifest.json")],
        capture_output=True, text=True, cwd=Path(windows_iso.__file__).parent.parent,
    )
    assert result.returncode == 1
    assert not (tmp_path / "manifest.json").exists()
