"""Vault clean-up operations: compare, park the old root, prepare the new vault,
reconcile old content into a reviewable import plan, apply it, verify.

Write rules (enforced here, not just documented):
  * nothing is ever deleted;
  * the old root is only renamed (parked), never modified;
  * imports are copied into <vault>/<import_dir>/..., never over an existing file;
  * every copy is re-hashed after writing.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
from pathlib import Path
from typing import Optional

from . import backup, scan
from .common import (dir_nonempty, OpsError, Run, assert_disjoint, is_within, lp, obsidian_running, read_state,
                     root, sha256_file, today, write_state)

# Paths Obsidian Sync does not carry, or that are per-device. Compared separately.
DEFAULT_IGNORE = (".git/", ".trash/", ".obsidian/workspace.json", ".obsidian/workspace-mobile.json",
                  ".obsidian/workspaces.json", ".DS_Store")


def _ignored(rel: str, ignore: tuple) -> bool:
    base = rel.rsplit("/", 1)[-1]
    for pat in ignore:
        if pat.endswith("/"):
            if rel.startswith(pat):
                return True
        elif rel == pat or base == pat:
            return True
    return False


# ---------------------------------------------------------------- compare

def compare(left: dict, right: dict, ignore: tuple = DEFAULT_IGNORE, include_config: bool = False) -> dict:
    ign = ignore if include_config else ignore + (".obsidian/",)
    L = {r: f for r, f in scan.index_by_rel(left).items() if not _ignored(r, ign)}
    R = {r: f for r, f in scan.index_by_rel(right).items() if not _ignored(r, ign)}
    same = sorted(r for r in L.keys() & R.keys() if L[r].get("sha256") == R[r].get("sha256"))
    diff = sorted(r for r in L.keys() & R.keys() if L[r].get("sha256") != R[r].get("sha256"))
    lo, ro = sorted(L.keys() - R.keys()), sorted(R.keys() - L.keys())

    def by_ext(rels):
        c: dict = {}
        for r in rels:
            ext = os.path.splitext(r)[1].lower() or "(none)"
            c[ext] = c.get(ext, 0) + 1
        return dict(sorted(c.items(), key=lambda kv: -kv[1]))

    return {"identical": len(same), "different": diff, "left_only": lo, "right_only": ro,
            "left_only_by_ext": by_ext(lo), "right_only_by_ext": by_ext(ro),
            "left_total": len(L), "right_total": len(R)}


def compare_md(c: dict, lname: str, rname: str, limit: int = 60) -> str:
    out = [f"# Compare: {lname} vs {rname}", "",
           f"- {lname}: {c['left_total']} files, {rname}: {c['right_total']} files",
           f"- identical: {c['identical']}, different: {len(c['different'])}, "
           f"{lname}-only: {len(c['left_only'])}, {rname}-only: {len(c['right_only'])}",
           f"- {lname}-only by extension: {c['left_only_by_ext']}",
           f"- {rname}-only by extension: {c['right_only_by_ext']}", ""]
    for title, items in (("Different", c["different"]), (f"{lname}-only", c["left_only"]),
                         (f"{rname}-only", c["right_only"])):
        out.append(f"## {title} ({len(items)})")
        out += [f"- `{r}`" for r in items[:limit]]
        if len(items) > limit:
            out.append(f"- ... {len(items) - limit} more (see compare.json)")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- park + prepare

def park_old(run: Run, cfg: dict, key: str = "rex_old") -> Path:
    src = root(cfg, key)
    if obsidian_running():
        raise OpsError("Obsidian is running. Quit Obsidian completely, then re-run.")
    if not backup.latest_verified(cfg, key, max_age_days=cfg.get("backup_max_age_days", 7)):
        raise OpsError(f"no verified backup of '{key}' in the last few days. Run: python -m opskit backup {key}")
    target = Path(f"{src}-OLD-{today()}")
    if Path(lp(target)).exists():
        raise OpsError(f"park target already exists: {target}")
    os.rename(lp(src), lp(target))
    renames = read_state(cfg, "renames.json", {})
    renames[str(cfg["roots"][key])] = str(target)
    write_state(cfg, "renames.json", renames)
    run.log("info", f"parked {src} -> {target} (renamed only; contents untouched)")
    return target


def prepare_target(run: Run, cfg: dict, key: str = "brain_target") -> Path:
    t = Path(cfg["roots"][key])
    for other_key, other in (cfg.get("roots") or {}).items():
        if other and other_key != key:
            assert_disjoint(t, other, f"vault target vs {other_key}")
    if Path(lp(t)).exists():
        if dir_nonempty(t):
            run.log("info", f"target exists and is not empty (already synced?): {t}")
            return t
        run.log("info", f"target exists and is empty: {t}")
        return t
    os.makedirs(lp(t))
    run.log("info", f"created empty vault folder {t}")
    return t


# ---------------------------------------------------------------- reconcile

def split_frontmatter(b: bytes) -> bytes:
    """Body of a Markdown file without a leading YAML frontmatter block, newline-normalised."""
    t = b.replace(b"\r\n", b"\n")
    if t.startswith(b"---\n"):
        end = t.find(b"\n---", 4)
        if end != -1:
            nl = t.find(b"\n", end + 4)
            t = t[nl + 1:] if nl != -1 else b""
    return t.strip()


def classify_source_file(rel: str, f: dict, base_idx: dict, base_hashes: set, planned_hashes: set,
                         src_root: Path, base_root: Optional[Path], recursion_prefixes: list) -> str:
    if any(rel == p or rel.startswith(p + "/") for p in recursion_prefixes):
        return "skip-recursion"
    if rel.startswith(".git/"):
        return "skip-git"
    if rel.startswith(".obsidian/"):
        return "skip-config"
    if rel.startswith(".trash/"):
        return "skip-trash"
    if rel.rsplit("/", 1)[-1] in (".DS_Store", "Thumbs.db", "desktop.ini"):
        return "skip-os-junk"
    h = f.get("sha256")
    if rel in base_idx and base_idx[rel].get("sha256") == h:
        return "identical"
    if h in base_hashes:
        return "duplicate-elsewhere"
    if h in planned_hashes:
        return "duplicate-in-sources"
    if rel in base_idx:
        if rel.lower().endswith(".md") and base_root is not None and f.get("size", 0) < 5_000_000:
            try:
                a = Path(lp(src_root / rel)).read_bytes()
                b = Path(lp(base_root / rel)).read_bytes()
                if split_frontmatter(a) == split_frontmatter(b):
                    return "conflict-frontmatter-only"
            except OSError:
                pass
        return "conflict"
    if rel.split("/", 1)[0].startswith("."):
        return "review-hidden"
    return "new"


def build_plan(baseline: dict, base_root: Optional[Path], sources: list, import_dir: str) -> dict:
    """sources: [{label, root(Path), manifest(dict)}] in priority order."""
    base_idx = scan.index_by_rel(baseline)
    base_hashes = {f.get("sha256") for f in baseline["files"]}
    planned: set = set()
    items = []
    for s in sources:
        rec_prefixes = [r["rel"] for r in s["manifest"].get("recursion_hard", [])]
        for f in s["manifest"]["files"]:
            rel = f["rel"]
            cls = classify_source_file(rel, f, base_idx, base_hashes, planned, s["root"], base_root,
                                       rec_prefixes)
            action, dest = "none", None
            if cls == "new":
                action, dest = "copy", f"{import_dir}/{s['label']}/{rel}"
            elif cls == "conflict":
                action, dest = "copy", f"{import_dir}/{s['label']}/_conflicts/{rel}"
            if action == "copy":
                planned.add(f.get("sha256"))
            items.append({"source": s["label"], "src_root": str(s["root"]), "rel": rel,
                          "sha256": f.get("sha256"), "size": f.get("size"), "class": cls,
                          "action": action, "dest_rel": dest,
                          "baseline_path": str(base_root / rel) if (base_root and cls.startswith("conflict")) else None})
    counts: dict = {}
    for it in items:
        counts[it["class"]] = counts.get(it["class"], 0) + 1
    return {"created": _dt.datetime.now(_dt.timezone.utc).isoformat(), "baseline_root": str(base_root),
            "import_dir": import_dir, "counts": counts,
            "copy_count": sum(1 for i in items if i["action"] == "copy"),
            "copy_bytes": sum(i["size"] or 0 for i in items if i["action"] == "copy"), "items": items}


def plan_md(plan: dict, limit: int = 150) -> str:
    out = ["# Import plan", "", f"- Baseline (live vault): `{plan['baseline_root']}`",
           f"- Imports land under: `{plan['import_dir']}/<source>/...` (conflicting versions under `_conflicts/`)",
           f"- Files to copy: {plan['copy_count']} ({plan['copy_bytes']:,} bytes)",
           f"- Classification: {plan['counts']}", "",
           "Nothing in the live vault is overwritten. Old roots are not modified.", ""]
    for cls in ("conflict", "new", "review-hidden", "conflict-frontmatter-only", "skip-recursion"):
        rows = [i for i in plan["items"] if i["class"] == cls]
        if not rows:
            continue
        out.append(f"## {cls} ({len(rows)})")
        for i in rows[:limit]:
            extra = f"  \n  baseline: `{i['baseline_path']}`" if i.get("baseline_path") else ""
            out.append(f"- [{i['source']}] `{i['rel']}` ({i['size']} B){extra}")
        if len(rows) > limit:
            out.append(f"- ... {len(rows) - limit} more in plan.json")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- apply + verify

def apply_plan(run: Run, plan: dict, vault_root: Path) -> dict:
    if str(vault_root) != plan["baseline_root"]:
        raise OpsError(f"plan was built for {plan['baseline_root']}, not {vault_root}")
    copied, skipped, failed = [], [], []
    for it in plan["items"]:
        if it["action"] != "copy":
            continue
        src = Path(it["src_root"]) / it["rel"]
        dst = vault_root / it["dest_rel"]
        if not is_within(dst, vault_root / plan["import_dir"]):
            failed.append({"rel": it["rel"], "error": "destination escapes import dir"})
            continue
        assert_disjoint(Path(it["src_root"]), vault_root, "apply-plan")
        if Path(lp(dst)).exists():
            if sha256_file(dst) == it["sha256"]:
                skipped.append(it["dest_rel"])
            else:
                failed.append({"rel": it["dest_rel"], "error": "destination exists with different content; not overwritten"})
            continue
        if sha256_file(src) != it["sha256"]:
            failed.append({"rel": it["rel"], "error": "source changed since plan was built"})
            continue
        os.makedirs(lp(dst.parent), exist_ok=True)
        shutil.copy2(lp(src), lp(dst))
        if sha256_file(dst) != it["sha256"]:
            failed.append({"rel": it["dest_rel"], "error": "post-copy hash mismatch"})
            continue
        copied.append(it["dest_rel"])
    res = {"copied": len(copied), "already_present": len(skipped), "failed": failed}
    run.log("info" if not failed else "error",
            f"apply-plan: copied {len(copied)}, already present {len(skipped)}, failed {len(failed)}")
    return res


def verify_vault(run: Run, cfg: dict, vault_root: Path, plan: Optional[dict]) -> dict:
    m = scan.walk(vault_root, hash_files=True)
    res = {"file_count": m["file_count"], "hazards": scan.hazards(m), "imports_missing": [],
           "imports_mismatch": []}
    if m["recursion_hard"] or m["recursion_warn"]:
        run.log("stop", "repeated-name folder chain present in live vault", sample=(m["recursion_hard"] + m["recursion_warn"])[:5])
    if m["links"]:
        run.log("warn", "links/junctions present in live vault", sample=m["links"][:5])
    if plan:
        idx = scan.index_by_rel(m)
        for it in plan["items"]:
            if it["action"] != "copy":
                continue
            got = idx.get(it["dest_rel"])
            if got is None:
                res["imports_missing"].append(it["dest_rel"])
            elif got.get("sha256") != it["sha256"]:
                res["imports_mismatch"].append(it["dest_rel"])
        if res["imports_missing"] or res["imports_mismatch"]:
            run.log("error", "planned imports missing or changed", missing=len(res["imports_missing"]),
                    mismatch=len(res["imports_mismatch"]))
    res["passed"] = not (m["recursion_hard"] or m["recursion_warn"] or res["imports_missing"]
                         or res["imports_mismatch"])
    return res


def write_session_note(cfg: dict, vault_root: Path, title: str, body: str) -> Path:
    folder = vault_root / cfg.get("session_notes_dir", "Cowork Sessions")
    os.makedirs(lp(folder), exist_ok=True)
    name = f"{title} {_dt.date.today().isoformat()}.md"
    p = folder / name
    n = 2
    while Path(lp(p)).exists():
        p = folder / f"{title} {_dt.date.today().isoformat()} ({n}).md"
        n += 1
    Path(lp(p)).write_text(body, encoding="utf-8")
    return p


def load_plan(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
