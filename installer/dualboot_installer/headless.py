"""Run the installation from an answers file, without the interface.

Used to test the steps in a container against a disk image, and handy for repeatable
installs. Secrets are read from the file in clear text: treat it accordingly.

The file mirrors the configuration, for example::

    {"disk": {"device": "/dev/loop0", "windows_gib": 64},
     "arch": {"username": "alice", "user_password": "...", "kernel": "linux"},
     "windows": {"computer_name": "PC", "username": "alice", "password": "..."},
     "secureboot": {"strategy": "sbctl", "touch_firmware": false}}
"""

from __future__ import annotations

import json
from pathlib import Path

from . import secureboot
from .config import InstallConfig
from .layout import default_sizes
from .plan import build_plan, can_run_for_real, execute_plan
from .runner import Runner
from .steps.context import Context
from .validate import problems


def load_config(path: Path, *, dry_run: bool, partial: bool) -> InstallConfig:
    cfg = InstallConfig(dry_run=dry_run, partial=partial)
    cfg.update_from(json.loads(path.read_text(encoding="utf-8")))
    cfg.dry_run, cfg.partial = dry_run, partial or cfg.partial

    sb = cfg.secureboot
    if not sb.resolved:
        sb.resolved = secureboot.resolve_strategy(sb.strategy, secureboot.detect())
    d = cfg.disk
    if not d.windows_gib and d.size_bytes:
        sizes = default_sizes(d.size_bytes)
        d.windows_gib, d.root_gib, d.shared_gib = sizes["windows_gib"], sizes["root_gib"], sizes["shared_gib"]
    return cfg


def run_headless(path: Path, *, confirm_erase: str | None, dry_run: bool, partial: bool,
                 log=print) -> int:
    cfg = load_config(path, dry_run=dry_run, partial=partial)
    runner = Runner(dry_run=dry_run, log=log)

    if not cfg.disk.size_bytes and not dry_run:
        cfg.disk.size_bytes = int(runner.capture(["blockdev", "--getsize64", cfg.disk.device]) or 0)
        cfg.disk.sector_size = int(runner.capture(["blockdev", "--getss", cfg.disk.device]) or 512)
        if not cfg.disk.windows_gib:
            sizes = default_sizes(cfg.disk.size_bytes)
            cfg.disk.windows_gib, cfg.disk.root_gib, cfg.disk.shared_gib = (
                sizes["windows_gib"], sizes["root_gib"], sizes["shared_gib"])

    found = problems(cfg)
    if found:
        for message in found:
            log(f"Invalid answers: {message}")
        return 2

    plan = build_plan(cfg, cfg.layout())
    if not dry_run:
        if confirm_erase != cfg.disk.device:
            log(f"Refusing to erase {cfg.disk.device}: pass --confirm-erase {cfg.disk.device} to allow it.")
            return 2
        if not can_run_for_real(plan, cfg):
            log("Refusing to start: some steps are not implemented yet. Use --partial.")
            return 2

    ok = execute_plan(plan, Context(cfg, cfg.layout(), runner),
                      lambda index, status: log(f"--- [{status}] {plan[index].title}") if status != "running" else None)
    return 0 if ok else 1
