"""Command execution with dry-run support and streamed output."""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class CommandError(RuntimeError):
    def __init__(self, command: "Command", returncode: int) -> None:
        super().__init__(f"Command failed with exit code {returncode}: {command.display()}")
        self.command = command
        self.returncode = returncode


@dataclass
class Command:
    argv: list[str]
    stdin: str | None = None  # text written to the command's standard input
    secret: bool = False  # never print stdin (passphrases)
    check: bool = True
    quiet: bool = False  # only show the output if the command fails

    def display(self) -> str:
        text = shlex.join(self.argv)
        if self.stdin is not None:
            text += "   < (secret from stdin)" if self.secret else "   < (stdin)"
        return text


@dataclass
class FileWrite:
    path: str
    content: str
    mode: int = 0o644
    secret: bool = False  # never print the content


Operation = Command | FileWrite


class Runner:
    """Runs commands and writes files, reporting everything through ``log``.

    Every operation is also kept in ``operations`` so tests and the dry-run preview can
    inspect exactly what a step does. In dry-run mode nothing touches the machine and
    ``capture`` returns the canned answer from ``answers`` (keyed by the shell-quoted
    command line), or an empty string.
    """

    def __init__(self, *, dry_run: bool, log: Callable[[str], None],
                 answers: dict[str, str] | None = None) -> None:
        self.dry_run = dry_run
        self.log = log
        self.answers = answers or {}
        self.operations: list[Operation] = []

    def run(self, command: Command) -> int:
        self.operations.append(command)
        self.log(f"$ {command.display()}")
        if self.dry_run:
            return 0

        process = subprocess.Popen(
            command.argv,
            stdin=subprocess.PIPE if command.stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        if command.stdin is not None and process.stdin is not None:
            process.stdin.write(command.stdin)
            process.stdin.close()
        assert process.stdout is not None
        held: list[str] = []
        for line in process.stdout:
            if command.quiet:
                held.append(line.rstrip("\n"))
            else:
                self.log(line.rstrip("\n"))
        returncode = process.wait()
        if returncode != 0:
            for line in held:
                self.log(line)
            if command.check:
                raise CommandError(command, returncode)
        return returncode

    def capture(self, argv: list[str], stdin: str | None = None) -> str:
        """Run a command and return its standard output (empty on failure; canned in dry-run)."""
        command = Command(argv, stdin=stdin, secret=stdin is not None)
        key = shlex.join(argv)
        self.operations.append(command)
        self.log(f"$ {command.display()}")
        if self.dry_run:
            return self.answers.get(key, "")
        try:
            result = subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)
        except OSError:
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    def write_file(self, op: FileWrite) -> None:
        self.operations.append(op)
        what = "secret content" if op.secret else f"{len(op.content)} bytes"
        self.log(f"> write {op.path}  (mode {op.mode:o}, {what})")
        if self.dry_run:
            return
        path = Path(op.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create with the final mode so a secret is never readable by others, even briefly.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, op.mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(op.content)
        try:
            os.chmod(path, op.mode)
        except OSError:
            # FAT (the ESP, the Windows setup partition) has no Unix modes and refuses chmod.
            if op.secret:
                self.log(f"note: {op.path} is on a filesystem without permissions.")
