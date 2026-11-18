"""CLI: python -m opskit <command> [options]

Read-only:  init-config, preflight, scan, compare, reconcile, verify, hermes-health
Writes (need --confirm): backup*, park-old, prepare-target, restore, apply-plan,
                         session-note, schedule, hermes-pin, hermes-snapshot*, hermes-stop
(* writes only to its own output folder; never touches the source)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

from . import backup, hermes_ops, preflight, scan, schedule, vault
from .common import (DEFAULT_CONFIG, IS_MAC, IS_WINDOWS, OpsError, Run, load_config, require_confirm,
                     root, today)


def cmd_init_config(a) -> int:
    path = Path(a.config) if a.config else DEFAULT_CONFIG
    if path.exists() and not a.force:
        print(f"config already exists: {path} (use --force to overwrite)")
        return 0
    home = Path.home()
    reg = preflight.read_registry(preflight.obsidian_registry_path())
    vault_paths = [v["path"] for v in reg]
    roots: dict = {}
    if IS_WINDOWS:
        machine = "main"
        old = home / "Obsidian"
        roots["rex_old"] = str(old) if old.exists() else ""
        q = sorted(glob.glob(str(home / "Obsidian-Quarantine-*")))
        roots["quarantine"] = q[-1] if q else ""
        roots["brain_target"] = str(home / a.vault_name)
        ops_home = home / "WcOps"
        hermes = {"install_dir": str(Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "hermes-agent"),
                  "home": str(home / ".hermes"), "snapshot_dir": str(home / "HermesWorkspace" / "vault-snapshot"),
                  "reports_dir": str(home / "HermesWorkspace" / "reports")}
    else:
        machine = "secondary-mac" if IS_MAC else "secondary"
        brain = next((p for p in vault_paths if Path(p).name == a.vault_name), "")
        roots["mac_brain"] = brain
        others = [p for p in vault_paths if p != brain]
        for i, p in enumerate(others):
            roots[f"mac_other_{i+1}"] = p
        ops_home = home / "WcOps"
        hermes = {}
    cfg = {"machine": machine, "vault_name": a.vault_name, "ops_home": str(ops_home), "roots": roots,
           "backup_dests": schedule.default_backup_dests(), "backup_max_age_days": 7,
           "import_dir": f"_Imported/REX-{today()}", "session_notes_dir": "Cowork Sessions",
           "preflight_skip_roots": ["quarantine"],
           "known_writers": {"windows_tasks": ["ClaudeVaultDailyCommit", "ObsidianTopicLinker",
                                               "ObsidianWeeklyBackup", "ObsidianWeeklyOpsSummary"],
                             "mac_launchagents": list(a.known_launchagent)},
           "reconcile_sources": [{"label": "rex-outer", "root_key": "rex_old", "exclude": ["Obsidian"]},
                                 {"label": "rex-inner", "root_key": "rex_old", "subdir": "Obsidian"}],
           "hermes": hermes, "schedule": {"task_names": schedule.task_names()}}
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(cfg, indent=2, ensure_ascii=False))
    print(f"\nwrote {path}. Review backup_dests and roots before the next step.")
    return 0


def _run(cfg, name):
    return Run(cfg, name)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="opskit")
    p.add_argument("--config")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init-config"); s.add_argument("--vault-name", required=True); s.add_argument("--force", action="store_true")
    s.add_argument("--known-launchagent", action="append", default=[], help="LaunchAgent label known to write to the vault")
    s = sub.add_parser("preflight"); s.add_argument("--hash", action="store_true")
    s = sub.add_parser("scan"); s.add_argument("target", help="root key or path")
    s = sub.add_parser("backup"); s.add_argument("keys", nargs="+"); s.add_argument("--allow-recursion", action="store_true")
    s = sub.add_parser("compare"); s.add_argument("left"); s.add_argument("right"); s.add_argument("--include-config", action="store_true")
    s = sub.add_parser("park-old"); s.add_argument("--key", default="rex_old"); s.add_argument("--confirm", action="store_true")
    s = sub.add_parser("prepare-target"); s.add_argument("--confirm", action="store_true")
    s = sub.add_parser("restore"); s.add_argument("zip"); s.add_argument("into"); s.add_argument("--confirm", action="store_true")
    sub.add_parser("reconcile")
    s = sub.add_parser("apply-plan"); s.add_argument("plan"); s.add_argument("--confirm", action="store_true")
    s = sub.add_parser("verify"); s.add_argument("--plan")
    s = sub.add_parser("session-note"); s.add_argument("title"); s.add_argument("body_file"); s.add_argument("--confirm", action="store_true")
    s = sub.add_parser("schedule"); s.add_argument("--only", nargs="*", default=[]); s.add_argument("--confirm", action="store_true")
    sub.add_parser("hermes-health")
    s = sub.add_parser("hermes-pin"); s.add_argument("--confirm", action="store_true")
    sub.add_parser("hermes-snapshot")
    sub.add_parser("hermes-stop")
    a = p.parse_args(argv)

    if a.cmd == "init-config":
        return cmd_init_config(a)
    cfg = load_config(a.config)
    run = _run(cfg, a.cmd)
    try:
        summary = dispatch(a, cfg, run)
        bad = [f for f in run.findings if f["level"] in ("error", "stop")]
        run.finish("ATTENTION" if bad else "OK", summary)
        return 2 if bad else 0
    except OpsError as e:
        run.log("stop", str(e))
        run.finish("STOPPED", {"reason": str(e)})
        return 3


def _path_or_key(cfg, s: str) -> str:
    return str(root(cfg, s)) if s in (cfg.get("roots") or {}) else s


def dispatch(a, cfg, run) -> dict:
    if a.cmd == "preflight":
        r = preflight.run_preflight(run, cfg, hash_files=a.hash)
        return {"obsidian_running": r["obsidian_running"], "roots": {k: v["hazards"] for k, v in r["roots"].items()}}
    if a.cmd == "scan":
        m = scan.walk(Path(_path_or_key(cfg, a.target)))
        run.write_json("manifest.json", m)
        run.write_text("scan.md", scan.summary_md(a.target, m))
        return {"file_count": m["file_count"], "hazards": scan.hazards(m)}
    if a.cmd == "backup":
        out = []
        for k in a.keys:
            out += backup.backup_root(run, cfg, k, root(cfg, k), cfg.get("backup_dests", []), a.allow_recursion)
        run.write_json("backups.json", out)
        return {"backups": [{"key": r["key"], "dest": r["dest"], "verified": r["verified"]} for r in out]}
    if a.cmd == "compare":
        L = scan.load_manifest_or_scan(_path_or_key(cfg, a.left))
        R = scan.load_manifest_or_scan(_path_or_key(cfg, a.right))
        c = vault.compare(L, R, include_config=a.include_config)
        run.write_json("compare.json", c)
        run.write_text("compare.md", vault.compare_md(c, a.left, a.right))
        rec = {"left_hazards": scan.hazards(L) if "recursion_hard" in L else None,
               "right_hazards": scan.hazards(R) if "recursion_hard" in R else None}
        for side, m in (("left", L), ("right", R)):
            if m.get("recursion_hard"):
                run.log("stop", f"{side} side contains a recursive folder chain")
        return {"identical": c["identical"], "different": len(c["different"]), "left_only": len(c["left_only"]),
                "right_only": len(c["right_only"]), **rec}
    if a.cmd == "park-old":
        require_confirm(a, "park-old")
        return {"parked_to": str(vault.park_old(run, cfg, a.key))}
    if a.cmd == "prepare-target":
        require_confirm(a, "prepare-target")
        return {"target": str(vault.prepare_target(run, cfg))}
    if a.cmd == "restore":
        require_confirm(a, "restore")
        return backup.restore_zip(run, Path(a.zip), Path(a.into))
    if a.cmd == "reconcile":
        base_root = root(cfg, "brain_target")
        baseline = scan.walk(base_root)
        if baseline["recursion_hard"]:
            raise OpsError("live vault contains a recursive folder chain; resolve before importing anything")
        sources = []
        for s in cfg.get("reconcile_sources", []):
            r = root(cfg, s["root_key"])
            if s.get("subdir"):
                r = r / s["subdir"]
            m = scan.walk(r, exclude_prefixes=s.get("exclude", []))
            sources.append({"label": s["label"], "root": r, "manifest": m})
        plan = vault.build_plan(baseline, base_root, sources, cfg.get("import_dir", f"_Imported/REX-{today()}"))
        pj = run.write_json("plan.json", plan)
        run.write_text("plan.md", vault.plan_md(plan))
        run.log("info", f"plan written: {pj}")
        return {"plan": str(pj), "counts": plan["counts"], "copy_count": plan["copy_count"]}
    if a.cmd == "apply-plan":
        require_confirm(a, "apply-plan")
        plan = vault.load_plan(a.plan)
        res = vault.apply_plan(run, plan, root(cfg, "brain_target"))
        run.write_json("applied.json", res)
        return res
    if a.cmd == "verify":
        plan = vault.load_plan(a.plan) if a.plan else None
        res = vault.verify_vault(run, cfg, root(cfg, "brain_target"), plan)
        run.write_json("verify.json", res)
        return res
    if a.cmd == "session-note":
        require_confirm(a, "session-note")
        body = Path(a.body_file).read_text(encoding="utf-8")
        p = vault.write_session_note(cfg, root(cfg, "brain_target"), a.title, body)
        run.log("info", f"session note written: {p}")
        return {"note": str(p)}
    if a.cmd == "schedule":
        require_confirm(a, "schedule")
        return schedule.install(run, cfg, a.only)
    if a.cmd == "hermes-health":
        res = hermes_ops.health(run, cfg)
        run.write_json("hermes-health.json", res)
        md = hermes_ops.health_md(res)
        run.write_text("hermes-health.md", md)
        rep = (cfg.get("hermes") or {}).get("reports_dir")
        if rep:
            Path(rep).mkdir(parents=True, exist_ok=True)
            (Path(rep) / "hermes-health-latest.md").write_text(md, encoding="utf-8")
        if not res["passed"]:
            run.log("error", "Hermes health check did not pass")
        return {"passed": res["passed"]}
    if a.cmd == "hermes-pin":
        require_confirm(a, "hermes-pin")
        return hermes_ops.pin(run, cfg)
    if a.cmd == "hermes-snapshot":
        return hermes_ops.refresh_snapshot(run, cfg, root(cfg, "brain_target"))
    if a.cmd == "hermes-stop":
        res = hermes_ops.stop_all(run, cfg)
        return {k: v.get("rc") for k, v in res.items()}
    raise OpsError(f"unknown command {a.cmd}")


if __name__ == "__main__":
    sys.exit(main())
