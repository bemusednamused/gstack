"""Windows Task Scheduler wiring for the recurring opskit jobs (main machine only)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .common import IS_WINDOWS, PKG_ROOT, OpsError, Run, lp, run_cmd

# name, schtasks schedule args, opskit args
JOBS = [
    ("WcOps_VaultBackup", ["/SC", "WEEKLY", "/D", "SUN", "/ST", "02:17"], ["backup", "brain_target"]),
    ("WcOps_HermesSnapshot", ["/SC", "DAILY", "/ST", "05:43"], ["hermes-snapshot"]),
    ("WcOps_HermesHealth", ["/SC", "DAILY", "/ST", "06:11"], ["hermes-health"]),
]


def wrapper_text(python: str, repo: Path, ops_home: Path, name: str, args: list) -> str:
    log = ops_home / "logs" / f"{name}.log"
    return ("@echo off\r\n"
            f'cd /d "{repo}"\r\n'
            f'echo ==== %DATE% %TIME% >> "{log}"\r\n'
            f'"{python}" -m opskit {" ".join(args)} >> "{log}" 2>&1\r\n')


def install(run: Run, cfg: dict, only: list) -> dict:
    if not IS_WINDOWS:
        raise OpsError("scheduling is implemented for the Windows main machine only")
    ops_home = Path(cfg["ops_home"])
    (ops_home / "bin").mkdir(parents=True, exist_ok=True)
    (ops_home / "logs").mkdir(parents=True, exist_ok=True)
    res = {}
    for name, sched, args in JOBS:
        if only and name not in only:
            continue
        cmd_path = ops_home / "bin" / f"{name}.cmd"
        Path(lp(cmd_path)).write_text(wrapper_text(sys.executable, PKG_ROOT, ops_home, name, args), encoding="utf-8")
        r = run_cmd(["schtasks", "/Create", "/F", "/TN", name, *sched, "/TR", f'"{cmd_path}"'])
        res[name] = {"rc": r["rc"], "out": (r["stdout"] + r["stderr"]).strip()[:300]}
        run.log("info" if r["rc"] == 0 else "error", f"schedule {name}: rc={r['rc']}")
    return res


def task_names() -> list:
    return [n for n, _, _ in JOBS]


def default_backup_dests() -> list:
    """Fixed/removable drives other than the system drive, for init-config."""
    if not IS_WINDOWS:
        vols = Path("/Volumes")
        return [str(v / "OpsBackups") for v in sorted(vols.iterdir())] if vols.exists() else []
    sysdrive = os.environ.get("SystemDrive", "C:").upper()
    out = []
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        if f"{letter}:" == sysdrive:
            continue
        if os.path.exists(f"{letter}:\\"):
            out.append(f"{letter}:\\OpsBackups")
    return out
