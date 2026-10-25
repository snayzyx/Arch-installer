"""What a step needs to do its work: the answers, the layout and a runner."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import InstallConfig
from ..layout import Layout, partition_path
from ..runner import Command, FileWrite, Runner

TARGET = "/mnt"


class StepError(RuntimeError):
    """A step cannot continue; the message is shown to the user as is."""


@dataclass
class Context:
    cfg: InstallConfig
    layout: Layout
    runner: Runner
    target: str = TARGET
    # Values one step hands to a later one (e.g. the Windows ISO path and the image index).
    state: dict[str, object] = field(default_factory=dict)

    # -- running things ------------------------------------------------------

    def log(self, message: str) -> None:
        self.runner.log(message)

    def run(self, *argv: str, stdin: str | None = None, secret: bool = False, check: bool = True,
            quiet: bool = False) -> int:
        """Run a command; returns its exit code (non-zero only when ``check`` is False)."""
        return self.runner.run(Command(list(argv), stdin=stdin, secret=secret, check=check, quiet=quiet))

    def capture(self, *argv: str, stdin: str | None = None) -> str:
        return self.runner.capture(list(argv), stdin=stdin)

    def chroot(self, *argv: str, stdin: str | None = None, secret: bool = False, check: bool = True) -> int:
        """Run a command inside the new system."""
        return self.run("arch-chroot", self.target, *argv, stdin=stdin, secret=secret, check=check)

    # -- files in the new system ---------------------------------------------

    def target_path(self, path: str) -> str:
        return f"{self.target}/{path.lstrip('/')}"

    def write(self, path: str, content: str, mode: int = 0o644, *, secret: bool = False) -> None:
        """Write a file at ``path`` inside the new system."""
        self.runner.write_file(FileWrite(self.target_path(path), content, mode, secret))

    def write_host(self, path: str, content: str, mode: int = 0o644, *, secret: bool = False) -> None:
        """Write a file at an absolute path of the live system (not inside the new system)."""
        self.runner.write_file(FileWrite(path, content, mode, secret))

    # -- the disk --------------------------------------------------------------

    def partition(self, role: str) -> str:
        """Device path of the partition with this role (e.g. /dev/nvme0n1p3)."""
        part = self.layout.get(role)
        if part is None:
            raise StepError(f"The disk layout has no '{role}' partition.")
        return partition_path(self.cfg.disk.device, part.number)

    def uuid_of(self, device: str, placeholder: str) -> str:
        """Filesystem or LUKS UUID of a device. In dry-run the placeholder is returned."""
        found = self.capture("blkid", "-s", "UUID", "-o", "value", device)
        if found:
            return found
        if self.runner.dry_run:
            return placeholder
        raise StepError(f"Cannot read the UUID of {device}.")
