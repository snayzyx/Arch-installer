"""Description of a Windows 11 ISO: which editions and language it holds.

The offline build bundles a Windows ISO into the installer image. Mounting a 9 GB image
just to read its metadata would be slow, so the build writes a small ``manifest.json``
next to it, produced from ``wimlib-imagex info`` output, and the installer reads that.

Command line (used by docker/build-iso.sh)::

    python -m dualboot_installer.windows_iso make-manifest --file Windows11.iso \\
        --size N --sha256 HEX --wim-info wim-info.txt --output manifest.json
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

# Where the live system mounts the boot medium, and the folder the build creates on it.
EMBEDDED_DIR = Path("/run/archiso/bootmnt/windows")
MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1

# Language-independent "Edition ID" -> the edition names used by the wizard.
EDITION_BY_ID = {"Core": "Home", "Professional": "Pro"}

_KEY_VALUE_RE = re.compile(r"^([A-Za-z][A-Za-z ]*?):\s*(.*)$")


@dataclass(frozen=True)
class WimImage:
    index: int
    name: str
    edition_id: str  # Core, Professional, Education, ...
    languages: tuple[str, ...]
    default_language: str
    build: str
    architecture: str

    @property
    def edition(self) -> str | None:
        """"Home" or "Pro" when this image is one the wizard offers, else None."""
        return EDITION_BY_ID.get(self.edition_id)


@dataclass(frozen=True)
class WindowsIsoInfo:
    path: Path
    size: int
    sha256: str
    images: tuple[WimImage, ...]

    def image_for(self, edition: str) -> WimImage | None:
        return next((img for img in self.images if img.edition == edition), None)

    @property
    def editions(self) -> list[str]:
        """The wizard editions present in this ISO, in wizard order."""
        return [e for e in ("Pro", "Home") if self.image_for(e)]

    @property
    def language_tag(self) -> str:
        """The language Windows will be installed in (set by the ISO, not by the user)."""
        for edition in ("Pro", "Home"):
            image = self.image_for(edition)
            if image and image.default_language:
                return image.default_language
        return next((img.default_language for img in self.images if img.default_language), "en-US")


def describe_embedded(info: WindowsIsoInfo) -> str:
    """One-line summary for the system checks."""
    editions = ", ".join(info.editions) or "no Home/Pro edition"
    return f"Included in this installer ({info.size / 1024**3:.1f} GiB, {info.language_tag}, editions: {editions})."


def parse_wimlib_info(text: str) -> list[WimImage]:
    """Parse the "Available Images" part of ``wimlib-imagex info`` output."""
    _, _, images_part = text.partition("Available Images:")
    blocks: list[dict[str, str]] = []
    for line in images_part.splitlines():
        match = _KEY_VALUE_RE.match(line.strip())
        if not match:
            continue
        key, value = match.group(1).strip(), match.group(2).strip()
        if key == "Index":
            blocks.append({})
        if blocks:
            blocks[-1][key] = value

    images = []
    for block in blocks:
        languages = tuple(re.split(r"[\s,]+", block.get("Languages", "").strip())) if block.get("Languages") else ()
        images.append(WimImage(
            index=int(block["Index"]),
            name=block.get("Name", ""),
            edition_id=block.get("Edition ID", ""),
            languages=languages,
            default_language=block.get("Default Language", ""),
            build=block.get("Build", ""),
            architecture=block.get("Architecture", ""),
        ))
    return images


def manifest_dict(file_name: str, size: int, sha256: str, images: list[WimImage]) -> dict:
    return {
        "version": MANIFEST_VERSION,
        "file": file_name,
        "size": size,
        "sha256": sha256,
        "images": [asdict(image) | {"languages": list(image.languages)} for image in images],
    }


def load_manifest(directory: Path = EMBEDDED_DIR) -> WindowsIsoInfo | None:
    """Read the manifest of the bundled ISO. None if there is none or it does not match the file."""
    try:
        data = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
        iso = directory / data["file"]
        if data["version"] != MANIFEST_VERSION or iso.stat().st_size != data["size"]:
            return None
        images = tuple(
            WimImage(
                index=int(i["index"]), name=i["name"], edition_id=i["edition_id"],
                languages=tuple(i["languages"]), default_language=i["default_language"],
                build=i["build"], architecture=i["architecture"],
            )
            for i in data["images"]
        )
        return WindowsIsoInfo(path=iso, size=int(data["size"]), sha256=str(data["sha256"]), images=images)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _make_manifest(args: argparse.Namespace) -> int:
    images = parse_wimlib_info(Path(args.wim_info).read_text(encoding="utf-8", errors="replace"))
    if not images:
        print("No Windows image found in the wimlib output.")
        return 1
    if not any(image.edition for image in images):
        print("This ISO has neither a Windows 11 Home nor a Pro image.")
        return 1
    manifest = manifest_dict(args.file, args.size, args.sha256, images)
    Path(args.output).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="windows_iso")
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("make-manifest", help="write manifest.json for a bundled Windows ISO")
    make.add_argument("--file", required=True, help="file name of the ISO inside the image")
    make.add_argument("--size", type=int, required=True)
    make.add_argument("--sha256", required=True)
    make.add_argument("--wim-info", required=True, help="saved output of `wimlib-imagex info install.wim`")
    make.add_argument("--output", required=True)
    make.set_defaults(handler=_make_manifest)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
