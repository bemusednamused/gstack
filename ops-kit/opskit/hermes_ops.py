"""Hermes Agent operations on the main machine: health, version pin and drift,
security-setting audit, read-only vault snapshot for the agent, kill switch.

This module never changes Hermes configuration itself. Configuration changes go
through Hermes' own CLI (`hermes config set ...`), run by the operator session
after approval, so Hermes stays the source of truth for its own config.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path
from typing import Optional

from . import scan
from .common import (dir_nonempty, IS_WINDOWS, OpsError, Run, assert_disjoint, lp, read_state, redact_mapping,
                     run_cmd, sha256_file, write_state)

# What "safe for unattended, read-only use" means for approvals (see Hermes security docs).
REQUIRED_APPROVALS = {"cron_mode": "deny", "single_query_mode": "deny", "unattended_mode": "deny"}
ACCEPTABLE_MODES = ("manual", "smart")
SNAPSHOT_EXCLUDE = (".git", ".trash", ".obsidian")


def hermes_exe(cfg: dict) -> str:
    h = (cfg.get("hermes") or {}).get("exe") or shutil.which("hermes")
    if not h:
        raise OpsError("hermes executable not found on PATH; set hermes.exe in config.local.json")
    return h


def hermes_home(cfg: dict) -> Path:
    return Path((cfg.get("hermes") or {}).get("home") or (Path.home() / ".hermes"))


def install_dir(cfg: dict) -> Optional[Path]:
    d = (cfg.get("hermes") or {}).get("install_dir")
    return Path(d) if d else None


# ---------------------------------------------------------------- config parsing (stdlib)

def parse_approvals_block(yaml_text: str) -> dict:
    """Minimal, dependency-free read of the top-level `approvals:` mapping."""
    out: dict = {}
    in_block = False
    for line in yaml_text.splitlines():
        if re.match(r"^approvals:\s*(#.*)?$", line):
            in_block = True
            continue
        if in_block:
            if line and not line[0].isspace() and not line.lstrip().startswith("#"):
                break
            m = re.match(r"^\s{2}([A-Za-z_]+):\s*([^#\n]*)", line)
            if m:
                out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def audit_approvals(approvals: dict) -> list:
    issues = []
    mode = approvals.get("mode", "smart")
    if mode not in ACCEPTABLE_MODES:
        issues.append(f"approvals.mode is '{mode}' (expected one of {ACCEPTABLE_MODES}; 'off' = YOLO)")
    for k, want in REQUIRED_APPROVALS.items():
        got = approvals.get(k, "deny")  # documented default is deny
        if got != want:
            issues.append(f"approvals.{k} is '{got}', expected '{want}'")
    return issues


# ---------------------------------------------------------------- health

def git_state(d: Path) -> dict:
    head = run_cmd(["git", "-C", str(d), "rev-parse", "HEAD"])
    desc = run_cmd(["git", "-C", str(d), "describe", "--tags", "--always"])
    dirty = run_cmd(["git", "-C", str(d), "status", "--porcelain"])
    return {"head": head["stdout"].strip(), "describe": desc["stdout"].strip(),
            "dirty_files": len([ln for ln in dirty["stdout"].splitlines() if ln.strip()])}


def health(run: Run, cfg: dict) -> dict:
    exe = hermes_exe(cfg)
    env = dict(os.environ, HERMES_NONINTERACTIVE="1")
    checks = {
        "version": [exe, "--version"],
        "doctor": [exe, "doctor"],
        "status": [exe, "status"],
        "gateway_status": [exe, "gateway", "status"],
        "cron_list": [exe, "cron", "list"],
    }
    res: dict = {"exe": exe, "checks": {}}
    for name, args in checks.items():
        r = run_cmd(args, timeout=240, env=env)
        res["checks"][name] = {"rc": r["rc"], "stdout_tail": r["stdout"][-4000:], "stderr_tail": r["stderr"][-2000:]}
        run.write_text(f"hermes-{name}.txt", f"$ {' '.join(args)}\nrc={r['rc']}\n\n{r['stdout']}\n--- stderr ---\n{r['stderr']}")
        run.log("info" if r["rc"] == 0 else "warn", f"hermes {name}: rc={r['rc']}")
    d = install_dir(cfg)
    if d and d.exists():
        res["git"] = git_state(d)
        pin = read_state(cfg, "hermes-pin.json", None)
        res["pinned"] = pin
        if pin and pin.get("head") != res["git"]["head"]:
            run.log("stop", "Hermes install drifted from the approved pin",
                    pinned=pin.get("head"), current=res["git"]["head"])
        if res["git"]["dirty_files"]:
            run.log("warn", f"Hermes checkout has {res['git']['dirty_files']} locally modified files")
    cfg_path = hermes_home(cfg) / "config.yaml"
    if cfg_path.exists():
        text = cfg_path.read_text(encoding="utf-8", errors="replace")
        appr = parse_approvals_block(text)
        res["approvals"] = redact_mapping(appr)
        res["approval_issues"] = audit_approvals(appr)
        for i in res["approval_issues"]:
            run.log("warn", i)
        res["config_sha256"] = sha256_file(cfg_path)
    else:
        run.log("warn", f"Hermes config not found at {cfg_path}")
    snap = (cfg.get("hermes") or {}).get("snapshot_dir")
    if snap:
        meta = read_state(cfg, "hermes-snapshot.json", None)
        res["snapshot"] = meta
        if not meta:
            run.log("warn", "no vault snapshot yet; run hermes-snapshot")
    res["passed"] = (res["checks"]["version"]["rc"] == 0 and res["checks"]["doctor"]["rc"] == 0
                     and not res.get("approval_issues"))
    return res


def health_md(res: dict) -> str:
    lines = ["# Hermes health", "", f"- Executable: `{res['exe']}`", f"- Overall: **{'PASS' if res['passed'] else 'ATTENTION'}**"]
    for k, v in res["checks"].items():
        lines.append(f"- {k}: rc={v['rc']}")
    if res.get("git"):
        lines.append(f"- Install: {res['git']} pinned={bool(res.get('pinned'))}")
    if "approvals" in res:
        lines.append(f"- approvals: {res['approvals']}")
        lines += [f"  - ISSUE: {i}" for i in res.get("approval_issues", [])]
    if res.get("snapshot"):
        s = res["snapshot"]
        lines.append(f"- Vault snapshot: {s.get('file_count')} files at {s.get('refreshed')}")
    return "\n".join(lines) + "\n"


def pin(run: Run, cfg: dict) -> dict:
    d = install_dir(cfg)
    if not d or not d.exists():
        raise OpsError("hermes.install_dir not set or missing")
    g = git_state(d)
    ver = run_cmd([hermes_exe(cfg), "--version"])["stdout"].strip()
    rec = {"head": g["head"], "describe": g["describe"], "version": ver, "approved_by_run": run.id}
    write_state(cfg, "hermes-pin.json", rec)
    run.log("info", f"pinned Hermes at {g['describe']} ({g['head'][:12]})")
    return rec


# ---------------------------------------------------------------- read-only snapshot

def _make_writable(p: str) -> None:
    try:
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def refresh_snapshot(run: Run, cfg: dict, vault: Path) -> dict:
    """One-way copy of the vault into the agent's read-only snapshot folder.

    The snapshot folder is owned by this tool: files there that are no longer in the
    vault are removed, but only inside the snapshot folder, and only after the
    disjointness guard proves the snapshot is not inside the vault (or vice versa).
    """
    snap = Path((cfg.get("hermes") or {}).get("snapshot_dir") or "")
    if not str(snap):
        raise OpsError("hermes.snapshot_dir not set")
    assert_disjoint(vault, snap, "hermes snapshot")
    marker = snap / ".opskit-snapshot"
    if Path(lp(snap)).exists() and dir_nonempty(snap) and not Path(lp(marker)).exists():
        raise OpsError(f"{snap} is not empty and is not an opskit snapshot; refusing to manage it")
    os.makedirs(lp(snap), exist_ok=True)
    Path(lp(marker)).write_text("managed by opskit hermes-snapshot; do not edit\n", encoding="utf-8")
    m = scan.walk(vault, hash_files=False, exclude_prefixes=SNAPSHOT_EXCLUDE)
    if m["recursion_hard"]:
        raise OpsError("vault has a recursive folder chain; snapshot refused")
    want = {f["rel"]: f for f in m["files"]}
    have = scan.walk(snap, hash_files=False, exclude_prefixes=(".opskit-snapshot",))
    have_idx = {f["rel"]: f for f in have["files"]}
    copied = removed = 0
    for rel, f in want.items():
        h = have_idx.get(rel)
        if h and h["size"] == f["size"] and h["mtime"] == f["mtime"]:
            continue
        dst = snap / rel
        os.makedirs(lp(dst.parent), exist_ok=True)
        if Path(lp(dst)).exists():
            _make_writable(lp(dst))
        shutil.copy2(lp(vault / rel), lp(dst))
        os.chmod(lp(dst), stat.S_IREAD)
        copied += 1
    for rel in set(have_idx) - set(want):
        p = snap / rel
        _make_writable(lp(p))
        os.remove(lp(p))
        removed += 1
    meta = {"vault": str(vault), "snapshot": str(snap), "file_count": len(want), "copied": copied,
            "removed": removed, "refreshed": run.id.split("-")[0]}
    write_state(cfg, "hermes-snapshot.json", meta)
    run.log("info", f"snapshot: {len(want)} files, {copied} copied, {removed} removed")
    return meta


# ---------------------------------------------------------------- kill switch

def stop_all(run: Run, cfg: dict) -> dict:
    res = {"gateway_stop": run_cmd([hermes_exe(cfg), "gateway", "stop"], timeout=120)}
    if IS_WINDOWS:
        for t in (cfg.get("schedule") or {}).get("task_names", []):
            res[t] = run_cmd(["schtasks", "/Change", "/TN", t, "/DISABLE"])
    run.log("info", "kill switch: gateway stop requested; opskit scheduled tasks disabled",
            rc={k: v.get("rc") for k, v in res.items()})
    return res
