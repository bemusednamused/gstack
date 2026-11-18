---
description: Job 1. Consolidate the Obsidian vault onto the main Windows machine safely, with verified backups first.
argument-hint: "<vault name>, e.g. Stu's AI Brain"
---

# Job 1: Obsidian vault clean-up

Vault name: **$ARGUMENTS**

**Target end state:**
- One Obsidian Sync remote vault named after the vault above.
- The main machine (Windows) holds the primary local copy at `%USERPROFILE%\<vault name>`.
- The Mac holds a second local copy, attached to the same remote.
- The old Windows root is parked (renamed), not deleted. The quarantine is untouched.
- Useful old-only content is imported into `_Imported/REX-<date>/` inside the vault, for review.
- Weekly verified backups go to a local drive. Nothing in this setup uses Dropbox.

Follow `CLAUDE.md`. Work through the phases in order. After each phase, give the owner a one-paragraph
plain-English status. Stop on any STOP condition.

Detect the machine first: Windows means the main machine; macOS means the Mac.

---

## Phase M. On the Mac (run this first, in a Claude Code session on the Mac)

1. `python3 -m opskit init-config --vault-name "$ARGUMENTS" --known-launchagent <label of the old Mac Obsidian backup LaunchAgent, if the owner's audit names one>`.
   Open `config.local.json` and confirm `roots.mac_brain` is the vault whose folder name matches. If
   `backup_dests` is empty, ask the owner to plug in an external drive, then rerun with `--force`.
2. `python3 -m opskit preflight`. **STOP** if Obsidian's old backup LaunchAgent is loaded, or if
   `mac_brain` has a recursive chain or sits inside a Dropbox, iCloud or Google Drive folder.
   Report which vaults are registered and whether Sync is enabled in each.
   Note that `core-plugins.json` comes in two formats; opskit reads both.
3. `python3 -m opskit backup mac_brain`. It must report `VERIFIED`.
4. **HUMAN (Mac, Obsidian app):** in the vault, open Settings → Sync and read out the **remote vault
   name** it's connected to, or say "not connected". Also check whether "Sync all other file types"
   is on. Record both answers in the session note.
5. Tell the owner: *"Don't edit notes on the Mac until Job 1 is finished."*

## Phase R1. Main machine: preflight and backup (read-only for the vault)

1. `git pull`, then `python -m unittest discover -s tests`. All tests must pass.
2. `python -m opskit init-config --vault-name "$ARGUMENTS"`. Check:
   - `roots.rex_old` is the old Windows root (normally `%USERPROFILE%\Obsidian`).
   - `roots.brain_target` is `%USERPROFILE%\$ARGUMENTS`.
   - `backup_dests` lists at least one non-system drive. Two is better; ask the owner to plug one in
     if there are none.
3. `python -m opskit preflight`. Expected:
   - the four known tasks show Disabled;
   - `rex_old` is inside no cloud folder;
   - the quarantine is skipped.
   **STOP** if any known writer is Enabled.
4. If Obsidian is running: **HUMAN:** quit Obsidian completely (also from the system tray), then
   rerun preflight.
5. `python -m opskit backup rex_old`. It must be `VERIFIED` on every available destination.

## Phase R2. Park the old root and create the new vault folder

1. Show the owner the commands in steps 2 and 3 and why they are needed. Their approval of the
   permission prompt is the go-ahead.
2. `python -m opskit park-old --confirm`. This renames `rex_old` to `...-OLD-<date>` and refuses to
   run without a fresh verified backup.
3. `python -m opskit prepare-target --confirm`. This creates the empty vault folder.

## Phase R3. Attach the main machine to the remote (HUMAN, about 3 minutes)

Give the owner exactly these steps:

1. Open Obsidian. In the vault switcher, choose **Open folder as vault** and pick
   `%USERPROFILE%\$ARGUMENTS`.
2. Settings → Core plugins → turn on **Sync**.
3. Settings → Sync → **Choose** the remote vault named in Phase M step 4. Don't create a new one.
4. Under Sync settings, turn on the same file-type toggles as the Mac. "Sync all other types" is
   needed for .xlsx, .csv and .html files.
5. Wait for **Fully synced**, then say "done".

If the Mac said "not connected", or there is no matching remote, use **Plan B** below.

## Phase R4. Prove what came down

1. `python -m opskit scan brain_target`. **STOP** if there's any recursive chain or link. That means
   the remote holds the old recursive tree; go to Plan B.
2. Copy the Mac backup's `*.manifest.json` onto this machine. Ask the owner to put the latest one
   from the Mac's backup drive somewhere on this machine. That's the only file they need to move.
3. `python -m opskit compare <mac manifest.json> brain_target`. Explain the differences by extension:
   - Mac-only non-Markdown files usually mean a "sync other types" toggle is off.
   - `.obsidian` differences are expected.
   Pass: zero recursion, and every Mac Markdown note present and identical.

## Phase R5. Reconcile the old Windows content (Claude does the review)

1. `python -m opskit reconcile`, then read `RUN DIR/plan.md`.
2. Open each `conflict` pair yourself (both paths are listed) and classify it:
   - **trivial**: whitespace or metadata only;
   - **substantive**: content or status changed;
   - **schema-sensitive**: frontmatter keys renamed, such as `business_area` → `business`.
   Read `review-hidden` items and say what they are. Flag any note that would switch on an external
   automation; for example, a customer email acknowledgement. Those are imported for reference only.
3. Give the owner one summary: counts, the few items that matter, and your recommendation.
   One approval covers the whole plan.
4. `python -m opskit apply-plan <RUN DIR>/plan.json --confirm`. This copies files only, under
   `_Imported/REX-<date>/`, and never overwrites.
5. `python -m opskit verify --plan <RUN DIR>/plan.json`. It must say PASS. Sync then carries the
   imports to the Mac.

## Phase R6. Lock in

1. `python -m opskit schedule --only WcOps_VaultBackup --confirm` sets up a weekly verified backup
   of the live vault.
2. Write the session note: Phases M–R6, run IDs, counts, decisions, open items. Save it with
   `python -m opskit session-note "Vault Cleanup" <file> --confirm`. It lands in `Cowork Sessions/`.
3. Back on the Mac (Phase M session): `python3 -m opskit compare mac_brain <manifest from a fresh REX scan>`
   should show the `_Imported` files arriving. The owner can resume editing.

**Retention (owner decides later, not in this job):** the parked old root, the quarantine, the legacy
Google Drive vault, and the Dropbox snapshot ZIPs. Leave them all untouched.

---

## Plan B: the remote is missing or contaminated

1. **HUMAN:** in Obsidian on this machine, Settings → Sync → **Disconnect**.
2. Quit Obsidian. Restore the verified Mac backup into a new folder:
   `python -m opskit restore <mac zip> "%USERPROFILE%\$ARGUMENTS (clean)" --confirm`.
   Set `roots.brain_target` in `config.local.json` to that folder.
   **HUMAN:** in Obsidian, choose **Open folder as vault** and pick that folder.
   The synced-but-contaminated folder stays where it is, untouched, for inspection.
3. **HUMAN:** Settings → Sync → **Create new remote vault** named "$ARGUMENTS" and connect.
   If the Obsidian plan allows only one remote, the owner deletes the old remote first.
   This is safe only because the Mac backup is verified.
4. **HUMAN (Mac):** Disconnect Sync. Rename the old Mac vault folder (add `-OLD-<date>`). Create an
   empty folder with the vault name, open it as a vault, and connect it to the new remote.
5. Continue at Phase R4. The Mac's old folder stays as a rollback copy.
