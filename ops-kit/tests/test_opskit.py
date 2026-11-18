"""Offline tests for opskit. Run: python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opskit import backup, hermes_ops, preflight, scan, vault  # noqa: E402
from opskit.common import OpsError, Run, assert_disjoint, is_within, redact_mapping, require_confirm  # noqa: E402


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="opskit-test-"))
        self.cfg = {"machine": "test", "ops_home": str(self.tmp / "ops"), "roots": {}}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_(self, name="t"):
        return Run(self.cfg, name)


class TestScan(Base):
    def test_repeat_run(self):
        self.assertEqual(scan.repeat_run(["a", "Obsidian", "obsidian", "Obsidian"]), 3)
        self.assertEqual(scan.repeat_run(["a", "b"]), 1)
        self.assertEqual(scan.repeat_run([]), 0)

    def test_stops_at_recursive_chain_and_records_it(self):
        deep = self.tmp / "v" / "Obsidian" / "Obsidian" / "Obsidian" / "Obsidian" / "Obsidian"
        write(deep / "x.md", "deep")
        write(self.tmp / "v" / "note.md", "hi")
        m = scan.walk(self.tmp / "v")
        self.assertEqual(len(m["recursion_hard"]), 1)
        self.assertEqual(m["recursion_hard"][0]["rel"], "Obsidian/Obsidian/Obsidian/Obsidian")
        self.assertEqual([f["rel"] for f in m["files"]], ["note.md"])

    def test_symlink_not_followed(self):
        write(self.tmp / "v" / "a.md", "a")
        os.symlink(self.tmp / "v", self.tmp / "v" / "loop")
        m = scan.walk(self.tmp / "v")
        self.assertEqual([f["rel"] for f in m["files"]], ["a.md"])
        self.assertEqual(m["links"][0]["rel"], "loop")

    def test_exclude_prefix(self):
        write(self.tmp / "v" / "Obsidian" / "inner.md", "i")
        write(self.tmp / "v" / "outer.md", "o")
        m = scan.walk(self.tmp / "v", exclude_prefixes=["Obsidian"])
        self.assertEqual([f["rel"] for f in m["files"]], ["outer.md"])


class TestGuards(Base):
    def test_disjoint(self):
        assert_disjoint(self.tmp / "a", self.tmp / "b", "x")
        with self.assertRaises(OpsError):
            assert_disjoint(self.tmp / "a", self.tmp / "a" / "b", "x")
        with self.assertRaises(OpsError):
            assert_disjoint(self.tmp / "a" / "b", self.tmp / "a", "x")
        self.assertFalse(is_within(self.tmp / "ab", self.tmp / "a"))

    def test_confirm_required(self):
        with self.assertRaises(OpsError):
            require_confirm(SimpleNamespace(confirm=False), "x")
        require_confirm(SimpleNamespace(confirm=True), "x")

    def test_redaction(self):
        r = redact_mapping({"api_key": "s3cret", "mode": "manual", "nested": {"token": "t", "x": 1}})
        self.assertEqual(r, {"api_key": "<redacted>", "mode": "manual", "nested": {"token": "<redacted>", "x": 1}})


class TestBackup(Base):
    def test_backup_verify_and_restore(self):
        src = self.tmp / "vault"
        write(src / "a.md", "alpha")
        write(src / ".git" / "HEAD", "ref: x")
        write(src / ".obsidian" / "app.json", "{}")
        write(src / "deep" / "b — em dash.md", "beta ✓")
        dest = self.tmp / "drive"
        dest.mkdir()
        run = self.run_()
        res = backup.backup_root(run, self.cfg, "k", src, [str(dest)])
        self.assertTrue(res[0]["verified"])
        self.assertEqual(res[0]["file_count"], 4)
        self.assertEqual(len(backup.latest_verified(self.cfg, "k")), 1)
        out = self.tmp / "restored"
        r = backup.restore_zip(run, Path(res[0]["zip"]), out)
        self.assertEqual(r["mismatched"], [])
        self.assertEqual((out / "deep" / "b — em dash.md").read_text(encoding="utf-8"), "beta ✓")
        # verify leaves no restore-test folder behind
        self.assertEqual([p.name for p in (dest / "k").iterdir() if p.is_dir()], [])

    def test_backup_refuses_destination_inside_source(self):
        src = self.tmp / "vault"
        write(src / "a.md", "a")
        (src / "bk").mkdir()
        with self.assertRaises(OpsError):
            backup.backup_root(self.run_(), self.cfg, "k", src, [str(src / "bk")])

    def test_backup_refuses_recursive_tree(self):
        src = self.tmp / "vault"
        write(src / "Obsidian" / "Obsidian" / "Obsidian" / "Obsidian" / "x.md", "x")
        (self.tmp / "d").mkdir()
        with self.assertRaises(OpsError):
            backup.backup_root(self.run_(), self.cfg, "k", src, [str(self.tmp / "d")])

    def test_restore_refuses_non_empty(self):
        (self.tmp / "full").mkdir()
        write(self.tmp / "full" / "x", "x")
        with self.assertRaises(OpsError):
            backup.restore_zip(self.run_(), self.tmp / "nope.zip", self.tmp / "full")


class TestReconcile(Base):
    def build(self):
        base = self.tmp / "brain"
        write(base / "same.md", "same")
        write(base / "Notes" / "conflict.md", "---\nid: 1\n---\nbody A\n")
        write(base / "fm.md", "---\nbusiness_area: x\n---\nsame body\n")
        write(base / "moved" / "renamed.md", "moved content")
        outer = self.tmp / "old"
        write(outer / "same.md", "same")
        write(outer / "Notes" / "conflict.md", "---\nid: 1\n---\nbody B\n")
        write(outer / "fm.md", "same body\n")
        write(outer / "renamed-old-place.md", "moved content")
        write(outer / "rates.xlsx", "binary-ish")
        write(outer / ".git" / "HEAD", "x")
        write(outer / ".obsidian" / "workspace.json", "{}")
        write(outer / ".trash" / "gone.md", "g")
        write(outer / ".claude-vault" / "meta.json", "{}")
        inner = outer / "Obsidian"
        write(inner / "rates.xlsx", "binary-ish")      # same as outer -> dedupe
        write(inner / "Sessions" / "s1.md", "session")
        return base, outer, inner

    def test_plan_classes(self):
        self.make_plan()

    def make_plan(self):
        base, outer, inner = self.build()
        baseline = scan.walk(base)
        sources = [{"label": "rex-outer", "root": outer, "manifest": scan.walk(outer, exclude_prefixes=["Obsidian"])},
                   {"label": "rex-inner", "root": inner, "manifest": scan.walk(inner)}]
        plan = vault.build_plan(baseline, base, sources, "_Imported/REX-T")
        by = {(i["source"], i["rel"]): i for i in plan["items"]}
        self.assertEqual(by[("rex-outer", "same.md")]["class"], "identical")
        self.assertEqual(by[("rex-outer", "Notes/conflict.md")]["class"], "conflict")
        self.assertEqual(by[("rex-outer", "Notes/conflict.md")]["dest_rel"], "_Imported/REX-T/rex-outer/_conflicts/Notes/conflict.md")
        self.assertEqual(by[("rex-outer", "fm.md")]["class"], "conflict-frontmatter-only")
        self.assertEqual(by[("rex-outer", "renamed-old-place.md")]["class"], "duplicate-elsewhere")
        self.assertEqual(by[("rex-outer", "rates.xlsx")]["class"], "new")
        self.assertEqual(by[("rex-inner", "rates.xlsx")]["class"], "duplicate-in-sources")
        self.assertEqual(by[("rex-inner", "Sessions/s1.md")]["dest_rel"], "_Imported/REX-T/rex-inner/Sessions/s1.md")
        self.assertEqual(by[("rex-outer", ".git/HEAD")]["class"], "skip-git")
        self.assertEqual(by[("rex-outer", ".obsidian/workspace.json")]["class"], "skip-config")
        self.assertEqual(by[("rex-outer", ".trash/gone.md")]["class"], "skip-trash")
        self.assertEqual(by[("rex-outer", ".claude-vault/meta.json")]["class"], "review-hidden")
        self.assertEqual(plan["copy_count"], 3)
        self.assertIn("Import plan", vault.plan_md(plan))
        return base, plan

    def test_apply_never_overwrites_and_verify(self):
        base, plan = self.make_plan()
        run = self.run_()
        before = {p: p.read_bytes() for p in base.rglob("*") if p.is_file()}
        res = vault.apply_plan(run, plan, base)
        self.assertEqual(res["copied"], 3)
        self.assertEqual(res["failed"], [])
        for p, b in before.items():
            self.assertEqual(p.read_bytes(), b, f"baseline file changed: {p}")
        again = vault.apply_plan(run, plan, base)  # idempotent
        self.assertEqual((again["copied"], again["already_present"]), (0, 3))
        v = vault.verify_vault(run, self.cfg, base, plan)
        self.assertTrue(v["passed"])

    def test_apply_refuses_to_overwrite_different_content(self):
        base, plan = self.make_plan()
        clash = base / "_Imported/REX-T/rex-outer/rates.xlsx"
        write(clash, "someone else's file")
        res = vault.apply_plan(self.run_(), plan, base)
        self.assertEqual(len(res["failed"]), 1)
        self.assertEqual(clash.read_text(encoding="utf-8"), "someone else's file")

    def test_compare_ignores_device_state(self):
        a, b = self.tmp / "a", self.tmp / "b"
        write(a / "n.md", "1"); write(b / "n.md", "1")
        write(a / ".obsidian" / "workspace.json", "x"); write(b / ".obsidian" / "workspace.json", "y")
        write(a / "only.csv", "c")
        c = vault.compare(scan.walk(a), scan.walk(b))
        self.assertEqual((c["identical"], len(c["different"]), c["left_only"]), (1, 0, ["only.csv"]))
        self.assertEqual(c["left_only_by_ext"], {".csv": 1})


class TestPark(Base):
    def test_park_requires_verified_backup(self):
        old = self.tmp / "Obsidian"
        write(old / "a.md", "a")
        self.cfg["roots"] = {"rex_old": str(old)}
        with self.assertRaises(OpsError):
            vault.park_old(self.run_(), self.cfg)
        d = self.tmp / "drive"; d.mkdir()
        backup.backup_root(self.run_(), self.cfg, "rex_old", old, [str(d)])
        parked = vault.park_old(self.run_(), self.cfg)
        self.assertFalse(old.exists())
        self.assertTrue((parked / "a.md").exists())
        from opskit.common import root
        self.assertEqual(root(self.cfg, "rex_old"), parked)  # later steps follow the rename


class TestParsers(Base):
    def test_core_plugins_both_formats(self):
        self.assertEqual(preflight.parse_core_plugins('["sync","graph"]'), {"format": "list", "sync": True})
        self.assertEqual(preflight.parse_core_plugins('{"sync": true, "graph": false}'), {"format": "object", "sync": True})
        self.assertEqual(preflight.parse_core_plugins('{"sync": false}')["sync"], False)

    def test_schtasks_csv(self):
        text = ('"HostName","TaskName","Task To Run","Scheduled Task State","Status"\r\n'
                '"REX","\\ObsidianWeeklyBackup","powershell -File backup.ps1","Disabled","Disabled"\r\n'
                '"HostName","TaskName","Task To Run","Scheduled Task State","Status"\r\n'
                '"REX","\\ClaudeVaultDailyCommit","git commit","Enabled","Ready"\r\n'
                '"REX","\\GoogleUpdate","updater.exe","Enabled","Ready"\r\n')
        rows = preflight.parse_schtasks_csv(text)
        self.assertEqual(len(rows), 3)
        t = preflight.interesting_tasks(rows, ["ClaudeVaultDailyCommit", "ObsidianWeeklyBackup"])
        self.assertEqual([x["name"] for x in t], ["\\ObsidianWeeklyBackup", "\\ClaudeVaultDailyCommit"])
        self.assertTrue(t[1]["enabled"] and t[1]["known_writer"])
        self.assertFalse(t[0]["enabled"])

    def test_cloud_folder(self):
        self.assertTrue(preflight.under_cloud_folder("/Users/x/Library/CloudStorage/Dropbox/v"))
        self.assertFalse(preflight.under_cloud_folder("C:\\Users\\x\\Vault"))

    def test_registry(self):
        reg = self.tmp / "obsidian.json"
        reg.write_text(json.dumps({"vaults": {"abc": {"path": str(self.tmp), "open": True}}}))
        v = preflight.read_registry(reg)
        self.assertEqual(v[0]["id"], "abc")
        self.assertTrue(v[0]["exists"])


class TestHermes(Base):
    def test_approvals_parse_and_audit(self):
        y = ("model:\n  default: x\napprovals:\n  mode: smart   # comment\n  timeout: 300\n"
             "  cron_mode: approve\n  unattended_mode: deny\nterminal:\n  backend: local\n")
        a = hermes_ops.parse_approvals_block(y)
        self.assertEqual(a, {"mode": "smart", "timeout": "300", "cron_mode": "approve", "unattended_mode": "deny"})
        issues = hermes_ops.audit_approvals(a)
        self.assertEqual(issues, ["approvals.cron_mode is 'approve', expected 'deny'"])
        self.assertIn("YOLO", hermes_ops.audit_approvals({"mode": "off"})[0])

    def test_snapshot_one_way_and_guarded(self):
        v = self.tmp / "brain"
        write(v / "a.md", "a"); write(v / ".git" / "HEAD", "x"); write(v / "d" / "b.md", "b")
        snap = self.tmp / "ws" / "snap"
        self.cfg["hermes"] = {"snapshot_dir": str(snap)}
        meta = hermes_ops.refresh_snapshot(self.run_(), self.cfg, v)
        self.assertEqual(meta["file_count"], 2)
        self.assertFalse((snap / ".git").exists())
        self.assertFalse(os.access(snap / "a.md", os.W_OK) and os.geteuid() != 0)
        (v / "a.md").unlink()
        meta = hermes_ops.refresh_snapshot(self.run_(), self.cfg, v)
        self.assertEqual((meta["removed"], (v / "d" / "b.md").exists()), (1, True))
        self.assertTrue((v / "d" / "b.md").read_text() == "b")  # vault untouched
        self.cfg["hermes"] = {"snapshot_dir": str(v / "snap-inside-vault")}
        with self.assertRaises(OpsError):
            hermes_ops.refresh_snapshot(self.run_(), self.cfg, v)

    def test_snapshot_refuses_foreign_folder(self):
        v = self.tmp / "brain"; write(v / "a.md", "a")
        foreign = self.tmp / "important"; write(foreign / "keep.txt", "k")
        self.cfg["hermes"] = {"snapshot_dir": str(foreign)}
        with self.assertRaises(OpsError):
            hermes_ops.refresh_snapshot(self.run_(), self.cfg, v)
        self.assertTrue((foreign / "keep.txt").exists())


class TestCli(Base):
    def test_cli_end_to_end_read_only_and_stop_codes(self):
        from opskit.__main__ import main
        v = self.tmp / "brain"; write(v / "a.md", "a")
        cfgp = self.tmp / "cfg.json"
        self.cfg["roots"] = {"brain_target": str(v)}
        cfgp.write_text(json.dumps(self.cfg))
        self.assertEqual(main(["--config", str(cfgp), "scan", "brain_target"]), 0)
        self.assertEqual(main(["--config", str(cfgp), "park-old", "--key", "brain_target"]), 3)  # no --confirm
        self.assertTrue(v.exists())


if __name__ == "__main__":
    unittest.main()
