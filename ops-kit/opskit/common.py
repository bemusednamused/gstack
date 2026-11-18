"""Shared helpers: config, run records, hashing, long-path handling, guards."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
PKG_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PKG_ROOT / "config.local.json"


class OpsError(Exception):
    """A guard tripped or an input was invalid. The run stops; nothing is half-done."""


# ---------------------------------------------------------------- paths

def lp(path: Any) -> str:
    """Absolute path, with the Windows long-path prefix so >260-char paths work."""
    p = os.path.abspath(str(path))
    if IS_WINDOWS and not p.startswith("\\\\?\\"):
        if p.startswith("\\\\"):
            return "\\\\?\\UNC\\" + p[2:]
        return "\\\\?\\" + p
    return p


def unlp(p: str) -> str:
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    if p.startswith("\\\\?\\"):
        return p[4:]
    return p


def norm_for_compare(p: Any) -> str:
    s = os.path.normpath(os.path.abspath(unlp(str(p))))
    return s.lower() if (IS_WINDOWS or IS_MAC) else s


def is_within(child: Any, parent: Any) -> bool:
    """True if child == parent or child is inside parent (no filesystem access)."""
    c, p = norm_for_compare(child), norm_for_compare(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def assert_disjoint(a: Any, b: Any, what: str) -> None:
    """The recursion guard: refuse any copy where source and destination overlap."""
    if is_within(a, b) or is_within(b, a):
        raise OpsError(f"{what}: '{a}' and '{b}' overlap (one is inside the other). "
                       "Refusing: this is how recursive Obsidian/Obsidian/... trees are made.")


def dir_nonempty(path: Any) -> bool:
    with os.scandir(lp(path)) as it:
        return any(True for _ in it)


def rel_posix(rel: str) -> str:
    return rel.replace("\\", "/")


# ---------------------------------------------------------------- hashing

def sha256_file(path: Any, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(lp(path), "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------- config

def _expand(v: Any) -> Any:
    if isinstance(v, str):
        return os.path.expanduser(os.path.expandvars(v))
    if isinstance(v, list):
        return [_expand(x) for x in v]
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    return v


def load_config(path: Optional[str] = None) -> dict:
    p = Path(path) if path else DEFAULT_CONFIG
    if not p.exists():
        raise OpsError(f"No config at {p}. Run: python -m opskit init-config --vault-name \"<name>\"")
    cfg = _expand(json.loads(p.read_text(encoding="utf-8")))
    for req in ("machine", "ops_home", "roots"):
        if req not in cfg:
            raise OpsError(f"config missing '{req}'")
    return cfg


def root(cfg: dict, key: str, must_exist: bool = True) -> Path:
    """Resolve a configured root, following any rename this toolkit recorded (park-old)."""
    roots = cfg.get("roots", {})
    if key not in roots or not roots[key]:
        raise OpsError(f"root '{key}' is not set in config")
    p = Path(roots[key])
    renames = read_state(cfg, "renames.json", {})
    if not p.exists() and str(p) in renames:
        p = Path(renames[str(p)])
    if must_exist and not Path(lp(p)).exists():
        raise OpsError(f"root '{key}' does not exist: {p}")
    return p


# ---------------------------------------------------------------- state + runs

def now_stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def today() -> str:
    return _dt.date.today().strftime("%Y%m%d")


def state_dir(cfg: dict) -> Path:
    d = Path(cfg["ops_home"]) / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_state(cfg: dict, name: str, default: Any) -> Any:
    p = Path(cfg["ops_home"]) / "state" / name
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def write_state(cfg: dict, name: str, obj: Any) -> None:
    p = state_dir(cfg) / name
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


class Run:
    """One command invocation. Everything it does lands in <ops_home>/runs/<id>/."""

    def __init__(self, cfg: dict, command: str):
        self.cfg = cfg
        self.command = command
        self.id = f"{now_stamp()}-{command}-{uuid.uuid4().hex[:6]}"
        self.dir = Path(cfg["ops_home"]) / "runs" / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.status = "running"
        self.findings: list = []
        self.log("info", f"run {self.id} on {cfg.get('machine')}")

    def log(self, level: str, msg: str, **data: Any) -> None:
        rec = {"t": now_stamp(), "level": level, "msg": msg}
        if data:
            rec["data"] = data
        with open(self.dir / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        if level in ("warn", "error", "stop"):
            self.findings.append(rec)
        print(f"[{level}] {msg}", flush=True)

    def write_json(self, name: str, obj: Any) -> Path:
        p = self.dir / name
        p.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return p

    def write_text(self, name: str, text: str) -> Path:
        p = self.dir / name
        p.write_text(text, encoding="utf-8")
        return p

    def finish(self, status: str, summary: Optional[dict] = None) -> None:
        self.status = status
        rec = {"id": self.id, "command": self.command, "status": status,
               "finished": now_stamp(), "dir": str(self.dir), "summary": summary or {}}
        self.write_json("summary.json", rec)
        with open(Path(self.cfg["ops_home"]) / "runs" / "index.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        print(f"\nRESULT: {status}\nRUN DIR: {self.dir}", flush=True)


# ---------------------------------------------------------------- processes

def run_cmd(args: list, timeout: int = 120, cwd: Optional[str] = None, env: Optional[dict] = None) -> dict:
    """Run a command, never raise; return a record suitable for reports."""
    try:
        cp = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                            env=env, encoding="utf-8", errors="replace")
        return {"args": args, "rc": cp.returncode, "stdout": cp.stdout, "stderr": cp.stderr}
    except FileNotFoundError:
        return {"args": args, "rc": None, "stdout": "", "stderr": "command not found"}
    except subprocess.TimeoutExpired as e:
        return {"args": args, "rc": None, "stdout": e.stdout or "", "stderr": f"timeout after {timeout}s"}


def process_names() -> list:
    if IS_WINDOWS:
        r = run_cmd(["tasklist", "/fo", "csv", "/nh"])
        names = []
        for line in r["stdout"].splitlines():
            m = re.match(r'"([^"]+)"', line)
            if m:
                names.append(m.group(1))
        return names
    r = run_cmd(["ps", "-axo", "comm="])
    return [os.path.basename(x.strip()) for x in r["stdout"].splitlines() if x.strip()]


def obsidian_running() -> bool:
    return any(n.lower().startswith("obsidian") for n in process_names())


# ---------------------------------------------------------------- redaction

_SECRET_KEY = re.compile(r"(key|token|secret|password|passwd|credential|auth)", re.I)


def redact_mapping(obj: Any) -> Any:
    """Replace values of secret-looking keys. Used before any config hits a report."""
    if isinstance(obj, dict):
        return {k: ("<redacted>" if _SECRET_KEY.search(str(k)) and not isinstance(v, (dict, list))
                    else redact_mapping(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_mapping(x) for x in obj]
    return obj


def require_confirm(args: Any, action: str) -> None:
    if not getattr(args, "confirm", False):
        raise OpsError(f"{action} changes files on disk. Re-run with --confirm after the owner approves.")


def iter_chunks(seq: Iterable, n: int):
    buf = []
    for x in seq:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf
