# ops-kit

Two operator jobs for Claude Code, plus the small, tested toolkit (`opskit`) they drive.

| Job | Slash command | Runs on | Outcome |
|---|---|---|---|
| 1. Obsidian vault clean-up | `/vault-cleanup "<vault name>"` | Mac (phase M), then main Windows machine | One Sync remote, main machine primary, old root parked, old-only content imported for review, weekly verified backups |
| 2. Hermes operational | `/hermes-operational` | Main Windows machine | Pinned, hardened Hermes; gateway at login; read-only vault snapshot; one weekly read-only Project Monitor; daily health check; kill switch |

## Start (owner)

```bash
git clone <this repo> && cd <repo>/ops-kit
claude              # start Claude Code in THIS folder so its CLAUDE.md and commands load
/vault-cleanup "Your Vault Name"
```

Run Job 1 Phase M on the Mac first, then the rest on the main machine. Job 2's phases H1–H4 can run
in parallel with Job 1. H5 onward needs Job 1 finished.

## What the owner has to do

Everything else is discovered and run by Claude Code.

| When | Action | Why it can't be automated |
|---|---|---|
| Job 1 | Plug in a backup drive if none is found | Physical |
| Job 1 | Read out the Mac's Sync remote name | Obsidian Sync has no API |
| Job 1 | Quit Obsidian when asked | Running app holds the vault |
| Job 1 | Open the new folder as a vault, connect Sync, wait for "Fully synced" | Obsidian GUI only |
| Job 1 | Copy one manifest file from the Mac's backup drive to the main machine | Cross-machine transfer |
| Job 1 | Approve the import plan (one prompt) | Owner decision |
| Job 2 | Click UAC once for `hermes gateway install` | Windows elevation |
| Job 2 | Reboot once at a convenient time | Reboot-persistence test |
| Both | Approve the permission prompts for state-changing commands | Safety gate |

## Safety model

- **Read-only by default.** Commands that change disk state require `--confirm` and are not in the
  permission allowlist (`.claude/settings.json`). Claude Code therefore asks before each one.
  Destructive shell commands are on the deny list.
- **Nothing is deleted.** Old roots are renamed ("parked"). Imports go to `_Imported/<source>/` and
  never overwrite. Every copy is re-hashed. The only deletions happen inside folders opskit owns:
  backup restore-tests and the Hermes snapshot.
- **Recursion guard.** Every copy refuses overlapping source and destination. Scans never follow
  links or junctions, and stop at repeated-name chains (`Obsidian/Obsidian/...`).
- **Backups are proven.** Each ZIP is fully extracted to a temporary folder and every file is
  re-hashed against the source manifest before it counts as verified.
  Parking the old root refuses to run without one.
- **Audit trail.** Every run writes `events.jsonl`, reports and a summary under `<ops_home>/runs/<id>/`,
  plus a global `runs/index.jsonl`. These stay on the machine and are never committed.

## Commands

```
python -m opskit init-config --vault-name "<name>"      # discovers paths; writes config.local.json (git-ignored)
python -m opskit preflight [--hash]                     # writers, registry, Sync flags, hazards
python -m opskit scan <root-key|path>
python -m opskit backup <root-key>...                   # zip + manifest + full restore test
python -m opskit compare <dir|manifest> <dir|manifest>
python -m opskit park-old --confirm                     # rename old root (needs fresh verified backup)
python -m opskit prepare-target --confirm
python -m opskit restore <zip> <empty-dir> --confirm
python -m opskit reconcile                              # writes plan.json + plan.md
python -m opskit apply-plan <plan.json> --confirm
python -m opskit verify [--plan <plan.json>]
python -m opskit session-note "<title>" <file> --confirm
python -m opskit schedule [--only NAME...] --confirm    # Windows Task Scheduler
python -m opskit hermes-health | hermes-pin --confirm | hermes-snapshot | hermes-stop
```

Exit codes: 0 OK · 2 attention (errors recorded) · 3 stopped by a guard.

## Requirements and tests

Python 3.9+ standard library only. Windows long paths are handled with the `\\?\` prefix.

```bash
python -m unittest discover -s tests -v
```

## Repository note

This folder is self-contained. It lives here because this session could only write to this
repository. Move it into its own **private** repository before any run reports or business-specific
configuration are committed. `git subtree split -P ops-kit` keeps the history.
