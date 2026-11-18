"""Read-only preflight: who can write to the vault, what Obsidian thinks exists,
and whether any configured root carries recursion / link / long-path hazards."""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path

from . import scan
from .common import IS_MAC, IS_WINDOWS, Run, obsidian_running, process_names, run_cmd

WRITER_KEYWORDS = ("obsidian", "vault", "claude", "hermes", "backup", "robocopy", "rsync",
                   "dropbox", "brain", "ditto")
SYNC_CLIENT_PROCS = ("dropbox", "googledrivefs", "google drive", "onedrive", "box", "icloud")
CLOUD_PATH_MARKERS = ("dropbox", "onedrive", "google drive", "googledrive", "cloudstorage",
                      "icloud", "mobile documents")


# ---------------------------------------------------------------- parsers (unit-tested)

def parse_schtasks_csv(text: str) -> list:
    """Parse `schtasks /query /fo csv /v`. Header rows repeat per folder; drop them."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    header = rows[0]
    out = []
    for r in rows[1:]:
        if r == header or len(r) != len(header):
            continue
        out.append(dict(zip(header, r)))
    return out


def task_field(row: dict, *names: str) -> str:
    for n in names:
        if n in row:
            return row[n]
    return ""


def interesting_tasks(rows: list, known: list) -> list:
    out, seen = [], set()
    known_l = [k.lower() for k in known]
    for r in rows:
        name = task_field(r, "TaskName")
        action = task_field(r, "Task To Run")
        state = task_field(r, "Scheduled Task State")
        status = task_field(r, "Status")
        blob = f"{name} {action}".lower()
        is_known = any(k in name.lower() for k in known_l)
        if not (is_known or any(k in blob for k in WRITER_KEYWORDS)):
            continue
        key = (name, action)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "action": action, "state": state, "status": status,
                    "known_writer": is_known,
                    "enabled": state.strip().lower() == "enabled"})
    return out


def parse_core_plugins(text: str) -> dict:
    """core-plugins.json is a list in old Obsidian versions and a {name: bool} object in
    newer ones. A list-only parser reports Sync 'disabled' on the new format, which is a
    likely explanation for the earlier manual-vs-automated Sync discrepancy."""
    data = json.loads(text)
    if isinstance(data, list):
        return {"format": "list", "sync": "sync" in data}
    if isinstance(data, dict):
        return {"format": "object", "sync": bool(data.get("sync", False))}
    return {"format": type(data).__name__, "sync": None}


def obsidian_registry_path() -> Path:
    if IS_WINDOWS:
        return Path(os.environ.get("APPDATA", "")) / "obsidian" / "obsidian.json"
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"
    return Path.home() / ".config" / "obsidian" / "obsidian.json"


def read_registry(path: Path) -> list:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    vaults = []
    for vid, v in (data.get("vaults") or {}).items():
        p = v.get("path", "")
        vaults.append({"id": vid, "path": p, "open": bool(v.get("open")), "exists": Path(p).exists()})
    return vaults


def vault_sync_flags(vault: Path) -> dict:
    cp = vault / ".obsidian" / "core-plugins.json"
    res = {"core_plugins": None}
    if cp.exists():
        try:
            res["core_plugins"] = parse_core_plugins(cp.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            res["core_plugins"] = {"error": str(e)}
    sj = vault / ".obsidian" / "sync.json"
    if sj.exists():
        try:
            d = json.loads(sj.read_text(encoding="utf-8"))
            # Report only non-secret, structural keys.
            res["sync_json_keys"] = sorted(d.keys()) if isinstance(d, dict) else None
        except (ValueError, OSError) as e:
            res["sync_json_keys"] = {"error": str(e)}
    return res


def under_cloud_folder(p: str) -> bool:
    s = p.lower()
    return any(m in s for m in CLOUD_PATH_MARKERS)


# ---------------------------------------------------------------- collectors

def windows_writers(cfg: dict) -> dict:
    known = cfg.get("known_writers", {}).get("windows_tasks", [])
    r = run_cmd(["schtasks", "/query", "/fo", "csv", "/v"], timeout=180)
    tasks = interesting_tasks(parse_schtasks_csv(r["stdout"]), known)
    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    run_keys = run_cmd(["reg", "query", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"])
    return {"tasks": tasks, "schtasks_rc": r["rc"],
            "startup_items": sorted(os.listdir(startup)) if startup.exists() else [],
            "run_keys": [ln.strip() for ln in run_keys["stdout"].splitlines() if "REG_" in ln]}


def mac_writers(cfg: dict) -> dict:
    known = cfg.get("known_writers", {}).get("mac_launchagents", [])
    uid = os.getuid()
    listed = run_cmd(["launchctl", "list"])["stdout"].splitlines()
    disabled = run_cmd(["launchctl", "print-disabled", f"gui/{uid}"])["stdout"]
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents = []
    if agents_dir.exists():
        for p in sorted(agents_dir.glob("*.plist")):
            try:
                body = p.read_text(encoding="utf-8", errors="replace").lower()
            except OSError:
                body = ""
            if any(k in p.name.lower() or k in body for k in WRITER_KEYWORDS):
                label = p.stem
                agents.append({"plist": p.name, "loaded": any(label in ln for ln in listed),
                               "disabled": f'"{label}" => disabled' in disabled
                               or f'"{label}" => true' in disabled})
    known_state = []
    for label in known:
        known_state.append({"label": label, "loaded": any(label in ln for ln in listed),
                            "disabled": f'"{label}" => disabled' in disabled
                            or f'"{label}" => true' in disabled})
    cron = run_cmd(["crontab", "-l"])
    return {"launch_agents": agents, "known": known_state,
            "crontab": [ln for ln in cron["stdout"].splitlines() if ln.strip() and not ln.startswith("#")]}


def run_preflight(run: Run, cfg: dict, hash_files: bool = False) -> dict:
    res: dict = {"machine": cfg["machine"]}
    res["obsidian_running"] = obsidian_running()
    procs = [p for p in process_names() if any(k in p.lower() for k in SYNC_CLIENT_PROCS)]
    res["sync_clients_running"] = sorted(set(procs))
    res["writers"] = windows_writers(cfg) if IS_WINDOWS else (mac_writers(cfg) if IS_MAC else {})
    reg = read_registry(obsidian_registry_path())
    for v in reg:
        if v["exists"]:
            v.update(vault_sync_flags(Path(v["path"])))
        v["under_cloud_folder"] = under_cloud_folder(v["path"])
    res["obsidian_registry"] = reg
    res["roots"] = {}
    md = [f"# Preflight on {cfg['machine']}", "",
          f"- Obsidian running: **{res['obsidian_running']}**",
          f"- Cloud sync clients running: {res['sync_clients_running'] or 'none'}", ""]
    md.append("## Obsidian registered vaults")
    for v in reg:
        md.append(f"- `{v['path']}` open={v['open']} exists={v['exists']} "
                  f"cloud_folder={v['under_cloud_folder']} core_plugins={v.get('core_plugins')}")
    md.append("")
    md.append("## Possible writers")
    w = res["writers"]
    for t in w.get("tasks", []):
        flag = "ENABLED" if t["enabled"] else t["state"]
        md.append(f"- task `{t['name']}` [{flag}] -> `{t['action'][:160]}`")
        if t["known_writer"] and t["enabled"]:
            run.log("stop", f"known vault writer task is ENABLED: {t['name']}")
    for a in w.get("launch_agents", []) + w.get("known", []):
        md.append(f"- launch agent {a}")
        if a.get("loaded"):
            run.log("warn", f"launch agent loaded: {a}")
    for item in w.get("startup_items", []):
        md.append(f"- startup item `{item}`")
    for item in w.get("run_keys", []):
        md.append(f"- Run key `{item}`")
    for item in w.get("crontab", []):
        md.append(f"- crontab `{item}`")
    md.append("")
    md.append("## Roots")
    for key, path in (cfg.get("roots") or {}).items():
        if not path or not Path(path).exists():
            md.append(f"- {key}: not present (`{path}`)")
            continue
        if key in cfg.get("preflight_skip_roots", []):
            md.append(f"- {key}: skipped by config (`{path}`)")
            continue
        m = scan.walk(Path(path), hash_files=hash_files)
        res["roots"][key] = {"file_count": m["file_count"], "bytes": m["bytes"], "hazards": scan.hazards(m),
                             "recursion_hard": m["recursion_hard"][:20], "links": m["links"][:20],
                             "under_cloud_folder": under_cloud_folder(path)}
        md.append(scan.summary_md(key, m))
        if m["recursion_hard"]:
            run.log("warn", f"{key}: recursive repeated-name chain found", sample=m["recursion_hard"][:3])
        if under_cloud_folder(path):
            run.log("warn", f"{key}: root sits inside a cloud-sync folder; Obsidian Sync + another sync = conflicts")
    run.write_json("preflight.json", res)
    run.write_text("preflight.md", "\n".join(md) + "\n")
    return res
