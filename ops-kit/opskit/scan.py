"""Read-only filesystem walk: manifest + hazard detection.

Never follows symlinks/junctions/reparse points. Stops descending into
repeated-name chains (Obsidian/Obsidian/Obsidian/...) and over-deep trees, and
records them as findings instead of traversing them.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Iterable, Optional

from .common import IS_WINDOWS, lp, rel_posix, sha256_file, unlp

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
WIN_MAX_PATH = 260


def repeat_run(parts: list) -> int:
    """Length of the trailing run of identical (case-insensitive) path components."""
    if not parts:
        return 0
    last = parts[-1].lower()
    n = 0
    for p in reversed(parts):
        if p.lower() != last:
            break
        n += 1
    return n


def _is_link(entry: os.DirEntry) -> bool:
    try:
        if entry.is_symlink():
            return True
        if IS_WINDOWS:
            st = entry.stat(follow_symlinks=False)
            return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return True  # unreadable -> treat as hazardous, do not descend
    return False


def walk(root: Path, *, hash_files: bool = True, max_depth: int = 40, repeat_warn: int = 2,
         repeat_hard: int = 4, exclude_prefixes: Iterable[str] = (), max_records: int = 500) -> dict:
    root = Path(unlp(os.path.abspath(str(root))))
    excl = tuple(rel_posix(p).rstrip("/") + "/" for p in exclude_prefixes)
    out = {"root": str(root), "files": [], "links": [], "recursion_warn": [], "recursion_hard": [],
           "depth_cut": [], "long_paths": [], "errors": [], "dirs": 0, "max_depth_seen": 0,
           "long_path_count": 0}
    stack = [(lp(root), [])]
    while stack:
        abs_dir, parts = stack.pop()
        try:
            it = list(os.scandir(abs_dir))
        except OSError as e:
            out["errors"].append({"rel": "/".join(parts), "error": str(e)})
            continue
        out["dirs"] += 1
        for entry in it:
            rel_parts = parts + [entry.name]
            rel = "/".join(rel_parts)
            if excl and (rel + "/").startswith(excl):
                continue
            full_len = len(unlp(entry.path))
            if full_len > WIN_MAX_PATH:
                out["long_path_count"] += 1
                if len(out["long_paths"]) < max_records:
                    out["long_paths"].append({"rel": rel, "len": full_len})
            if _is_link(entry):
                out["links"].append({"rel": rel, "is_dir_hint": _safe_isdir(entry)})
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError as e:
                out["errors"].append({"rel": rel, "error": str(e)})
                continue
            if is_dir:
                run = repeat_run(rel_parts)
                depth = len(rel_parts)
                out["max_depth_seen"] = max(out["max_depth_seen"], depth)
                if run >= repeat_hard:
                    out["recursion_hard"].append({"rel": rel, "repeat": run})
                    continue
                if run >= repeat_warn and len(out["recursion_warn"]) < max_records:
                    out["recursion_warn"].append({"rel": rel, "repeat": run})
                if depth > max_depth:
                    out["depth_cut"].append({"rel": rel, "depth": depth})
                    continue
                stack.append((entry.path, rel_parts))
                continue
            try:
                st = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(st.st_mode):
                    out["links"].append({"rel": rel, "special": True})
                    continue
                rec = {"rel": rel, "size": st.st_size, "mtime": int(st.st_mtime)}
                if hash_files:
                    rec["sha256"] = sha256_file(entry.path)
                out["files"].append(rec)
            except OSError as e:
                out["errors"].append({"rel": rel, "error": str(e)})
    out["files"].sort(key=lambda r: r["rel"])
    out["file_count"] = len(out["files"])
    out["bytes"] = sum(f["size"] for f in out["files"])
    return out


def _safe_isdir(entry: os.DirEntry) -> Optional[bool]:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return None


def hazards(m: dict) -> dict:
    return {"recursion_hard": len(m["recursion_hard"]), "recursion_warn": len(m["recursion_warn"]),
            "links": len(m["links"]), "depth_cut": len(m["depth_cut"]),
            "long_paths": m["long_path_count"], "errors": len(m["errors"])}


def load_manifest_or_scan(src: str, **kw) -> dict:
    """Accept either a manifest JSON produced by this toolkit or a directory to scan."""
    p = Path(src)
    if p.is_file() and p.suffix == ".json":
        m = json.loads(p.read_text(encoding="utf-8"))
        m.setdefault("files", [])
        return m
    return walk(p, **kw)


def index_by_rel(m: dict) -> dict:
    return {f["rel"]: f for f in m["files"]}


def summary_md(title: str, m: dict) -> str:
    h = hazards(m)
    lines = [f"### {title}", "", f"- Root: `{m['root']}`",
             f"- Files: {m['file_count']} ({m['bytes']:,} bytes), dirs: {m['dirs']}, max depth: {m['max_depth_seen']}",
             f"- Hazards: {h}"]
    for k in ("recursion_hard", "links", "depth_cut", "errors"):
        for r in m[k][:15]:
            lines.append(f"  - {k}: `{r.get('rel')}` {({x: y for x, y in r.items() if x != 'rel'})}")
    return "\n".join(lines) + "\n"
