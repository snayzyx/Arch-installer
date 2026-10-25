"""Entry point: ``python -m dualboot_installer``."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="archinstall-launcher",
        description="ArchInstall: Arch Linux + Windows 11 dual boot installer.",
    )
    parser.add_argument("--demo", action="store_true",
                        help="use simulated hardware (implies --dry-run); works on any OS")
    parser.add_argument("--demo-offline", action="store_true",
                        help="like --demo, but simulates an offline build with a bundled Windows ISO")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the commands instead of running them; no disk is modified")
    parser.add_argument("--partial", action="store_true",
                        help="install Arch Linux and GRUB only: the Windows steps are not implemented yet "
                             "and are skipped, so the Windows partition stays empty")
    parser.add_argument("--answers", type=Path, metavar="FILE",
                        help="install without the interface, from a JSON answers file (clear-text secrets)")
    parser.add_argument("--confirm-erase", metavar="DEVICE",
                        help="with --answers: the device that may be erased, typed again as a safety check")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    demo = args.demo or args.demo_offline
    dry_run = args.dry_run or demo
    if not dry_run and hasattr(os, "geteuid") and os.geteuid() != 0:
        print("The installer must run as root.", file=sys.stderr)
        return 1

    if args.answers:
        from .headless import run_headless

        return run_headless(args.answers, confirm_erase=args.confirm_erase, dry_run=dry_run,
                            partial=args.partial)

    from .app import InstallerApp
    from .system import get_system

    InstallerApp(get_system(demo=demo, demo_offline=args.demo_offline), dry_run=dry_run,
                 partial=args.partial).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
