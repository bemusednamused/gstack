"""Full, hash-verified ZIP backups with a real restore test.

Source is only ever read. Each destination gets <key>/<key>-<stamp>.zip plus a
manifest. Verification extracts every member into a temporary folder that this
code created, re-hashes it against the source manifest, then removes only that
temporary folder.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path

from . import scan
from .common import dir_nonempty, OpsError, Run, assert_disjoint, lp, now_stamp, read_state, sha256_file, write_state


def make_zip(src_root: Path, files: list, zip_path: Path) -> None:
    tmp = zip_path.with_suffix(".zip.partial")
    with zipfile.ZipFile(lp(tmp), "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True,
                         strict_timestamps=False) as zf:
        for f in files:
            zf.write(lp(src_root / f["rel"]), arcname=f["rel"])
    os.replace(lp(tmp), lp(zip_path))


def verify_zip(zip_path: Path, files: list, work_dir: Path) -> dict:
    """Extract every member to work_dir and compare sha256 with the manifest."""
    expected = {f["rel"]: f["sha256"] for f in files}
    ok, bad, missing = 0, [], []
    restore = work_dir / ("restore-test-" + now_stamp())
    os.makedirs(lp(restore))
    try:
        with zipfile.ZipFile(lp(zip_path)) as zf:
            names = set(zf.namelist())
            for rel, digest in expected.items():
                if rel not in names:
                    missing.append(rel)
                    continue
                target = restore / rel
                os.makedirs(lp(target.parent), exist_ok=True)
                with zf.open(rel) as src, open(lp(target), "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                if sha256_file(target) == digest:
                    ok += 1
                else:
                    bad.append(rel)
            extra = sorted(names - set(expected))
    finally:
        shutil.rmtree(lp(restore), ignore_errors=True)
    return {"verified": ok, "mismatched": bad, "missing": missing, "extra": extra,
            "passed": not bad and not missing and not extra and ok == len(expected)}


def backup_root(run: Run, cfg: dict, key: str, src_root: Path, dests: list,
                allow_recursion: bool = False) -> list:
    if not dests:
        raise OpsError("no backup destinations configured (backup_dests)")
    run.log("info", f"scanning {key} at {src_root}")
    m = scan.walk(src_root, hash_files=True)
    if m["recursion_hard"] and not allow_recursion:
        raise OpsError(f"{key} contains a recursive repeated-name chain {m['recursion_hard'][:2]}. "
                       "Refusing to back it up blind; inspect first or pass --allow-recursion.")
    if m["errors"]:
        run.log("warn", f"{len(m['errors'])} unreadable entries in {key}", sample=m["errors"][:5])
    if m["links"]:
        run.log("warn", f"{len(m['links'])} links/reparse points recorded but not followed", sample=m["links"][:5])
    stamp = now_stamp()
    results = []
    for d in dests:
        dest = Path(d)
        if not dest.exists():
            run.log("warn", f"backup destination not available, skipped: {dest}")
            continue
        assert_disjoint(src_root, dest, "backup")
        out_dir = dest / key
        os.makedirs(lp(out_dir), exist_ok=True)
        zip_path = out_dir / f"{key}-{stamp}.zip"
        run.log("info", f"writing {zip_path}")
        make_zip(src_root, m["files"], zip_path)
        manifest = {"key": key, "root": str(src_root), "created": stamp, "machine": cfg["machine"],
                    "file_count": m["file_count"], "bytes": m["bytes"], "hazards": scan.hazards(m),
                    "links": m["links"], "errors": m["errors"], "files": m["files"],
                    "zip": zip_path.name, "zip_sha256": sha256_file(zip_path)}
        man_path = out_dir / f"{key}-{stamp}.manifest.json"
        with open(lp(man_path), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=1, ensure_ascii=False)
        v = verify_zip(zip_path, m["files"], out_dir)
        rec = {"key": key, "dest": str(dest), "zip": str(zip_path), "manifest": str(man_path),
               "file_count": m["file_count"], "bytes": m["bytes"], "created": stamp,
               "verified": v["passed"], "verify": {k: (v[k] if isinstance(v[k], (bool, int)) else v[k][:20]) for k in v}}
        run.log("info" if v["passed"] else "error",
                f"{key} -> {dest}: {'VERIFIED' if v['passed'] else 'VERIFY FAILED'} ({v['verified']}/{m['file_count']})")
        results.append(rec)
    if not results:
        raise OpsError("no destination was available; nothing was backed up")
    hist = read_state(cfg, "backups.json", [])
    hist.extend(results)
    write_state(cfg, "backups.json", hist)
    return results


def latest_verified(cfg: dict, key: str, max_age_days: int = 7, min_copies: int = 1) -> list:
    """Verified backups of `key` younger than max_age_days, newest first."""
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=max_age_days)
    good = []
    for r in read_state(cfg, "backups.json", []):
        if r.get("key") != key or not r.get("verified"):
            continue
        t = _dt.datetime.strptime(r["created"], "%Y%m%dT%H%M%SZ").replace(tzinfo=_dt.timezone.utc)
        if t >= cutoff and Path(r["zip"]).exists():
            good.append(r)
    good.sort(key=lambda r: r["created"], reverse=True)
    if len({r["dest"] for r in good}) < min_copies:
        return []
    return good


def restore_zip(run: Run, zip_path: Path, into: Path) -> dict:
    """Extract a verified backup into an EMPTY folder (plan B seeding)."""
    if Path(lp(into)).exists() and dir_nonempty(into):
        raise OpsError(f"restore target is not empty: {into}")
    man = Path(str(zip_path).replace(".zip", ".manifest.json"))
    if not man.exists():
        raise OpsError(f"manifest not found next to zip: {man}")
    files = json.loads(Path(lp(man)).read_text(encoding="utf-8"))["files"]
    os.makedirs(lp(into), exist_ok=True)
    with zipfile.ZipFile(lp(zip_path)) as zf:
        for f in files:
            t = into / f["rel"]
            os.makedirs(lp(t.parent), exist_ok=True)
            with zf.open(f["rel"]) as s, open(lp(t), "wb") as d:
                shutil.copyfileobj(s, d, 1 << 20)
    bad = [f["rel"] for f in files if sha256_file(into / f["rel"]) != f["sha256"]]
    run.log("info" if not bad else "error", f"restored {len(files)} files into {into}; mismatches={len(bad)}")
    return {"restored": len(files), "mismatched": bad[:50]}


def file_digest_list(files: list) -> str:
    h = hashlib.sha256()
    for f in files:
        h.update(f"{f['rel']}\0{f.get('sha256','')}\n".encode("utf-8"))
    return h.hexdigest()
