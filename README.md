# Claude PowerTools

Browse, move, back up and inspect everything Claude keeps on this Mac: chats,
memory, MCP connections and the app instances themselves.

Grew out of `~/copychat.sh`. Python stdlib only — no npm, no node, no build step.
(The npm `claude-transplant` tool broke on this Mac because the default node is v14.)

## Install

Homebrew only. No npm, no node, no pip: one Python file on the `python3` that
ships with macOS.

    brew install --cask navneetbind/tap/claude-powertools
    powertools open

Upgrade with `brew update && brew upgrade claude-powertools`. Remove with
`brew uninstall --cask claude-powertools` (your data in `~/.claude-powertools` —
index, account names, reset times, transfer backups — is kept; delete that folder
to remove it too).

(`brew install navneetbind/tap/claude-powertools` is the formula; it needs current
Xcode / Command Line Tools, the cask does not.)

## Uninstall

    cd ~/claude-powertools && ./uninstall.sh          # app + launcher
    cd ~/claude-powertools && ./uninstall.sh --purge  # also the index, names, reset times, backups

Neither touches Claude's own data. Your chats, memory and settings are exactly
where Claude left them.

## Update

    cd ~/claude-powertools && git pull && ./install.sh

## Developing

`install.sh` copies the built single file from `dist/`. To run from source
instead, so edits take effect immediately:

    python3 ~/claude-powertools/powertools.py

After changing `powertools.py` or `ui.html`, rebuild the single file before
committing:

    python3 powertools.py bundle --out dist/powertools

## Use

    powertools            # web UI at 127.0.0.1:7788, opens the browser
    powertools list
    powertools search "penny drop" --fts
    powertools accounts
    powertools label ef06d738-... "work gmail"
    powertools move --from <bucket> --to <bucket> --match "GST"   # dry run
    powertools move --from <bucket> --to <bucket> --match "GST" --yes
    powertools undo
    powertools index --fts

A "bucket" is `instance/account-uuid/org-uuid`, as printed by `powertools accounts`.

## Web UI

- **Select all** — header checkbox takes every row on screen, or click
  "select all N matching" to take the whole filtered set across pages.
  Shift-click a checkbox for a range. Cmd+A also selects all loaded rows.
- **Transfer dialog** — Copy vs Move as a choice, the full list of what will
  move, an "overwrites" badge per clashing chat, a skip count, and a button
  labelled with the exact number. Nothing is written until you press it.
- **Chat reader** — full transcript, find-in-chat with highlight and a match
  count, a "hide tool noise" toggle, per-message timestamps, Copy to clipboard,
  and chunked rendering so a 16,000-message chat still opens.
- **Rename accounts** — hover an account in the sidebar, click "rename".
- Esc closes any dialog, `/` focuses search.

## Instances

Each Claude app on the Mac has its own data folder, its own login and its own
chats. `powertools instances` lists them with size and chat count, and flags the
broken pairings: a data folder with no app, or an app pointing at a folder that
is gone.

    powertools instances
    powertools new-instance "Claude Work 4"              # dry run, prints the plan
    powertools new-instance "Claude Work 4" --yes        # build it

The **Instances** panel in the web UI does the same, and has an **Update Claude**
section (status, update, install from a `.dmg`, rebuild, and **Fix my instances**
for instances made by older versions).

### How an instance is built (and why)

Two bundles per instance:

- **Real copy** — `~/Applications/Claude Instances/<Name>.app`: a full copy of
  `Claude.app` (~870 MB) with its own bundle id and display name, re-signed ad-hoc.
  Its main executable is the genuine Mach-O. **This is what gets a menu-bar icon
  and notification permission.** An earlier layout renamed the binary to
  `Claude-real` and put a shell script in its place; macOS then saw a process
  signed `Claude-real-<hash>` with "Info.plist not bound" and never listed it under
  System Settings > Menu Bar or gave it notifications. (Verified with a probe app.)
  Only `CFBundleDisplayName` is changed — renaming `CFBundleName` makes Electron
  look for the wrong `… Helper.app` and crash at startup.
- **Launcher** — `/Applications/<Name>.app` (~1 MB, never changes): starts the real
  copy with `--user-data-dir=<profile>`, or brings it to the front if it is already
  running. The profile path cannot be baked into the real copy (Electron's
  asar-integrity fuse is on), and the real copy is hidden from Spotlight/Launchpad
  because opening it directly starts the **default** profile.

Copies cannot update themselves: ad-hoc signing pins the designated requirement to
the copy's own cdhash, so Squirrel can never validate a genuine update. Rebuild
them after `Claude.app` updates — `scripts/update-claude.sh` (or the Update button)
does. Real copies live in your home folder, so rebuilding them needs no password;
only the launchers (created once) touch `/Applications`.

The frameworks cannot be shared to save the 870 MB. By symlink, the hardened
runtime SIGKILLs an ad-hoc binary that loads Anthropic-signed frameworks
(verified: exit 137). By hard link, re-signing would rewrite `Claude.app`'s own
files.

The first launch of a freshly built copy asks for **"Claude Safe Storage"** — click
Always Allow. Until you do, the app sits with no window. Re-grant Full Disk Access
after a rebuild (the code identity changed). Notifications from an instance are
attributed to the main "Claude" row; a click may open the main app.

## Updating Claude and its instances

    ./scripts/update-claude.sh

The one to use. Asks Anthropic's release feed — the same one the app queries,
`api.anthropic.com/api/desktop/darwin/<arch>/squirrel/update` — for the current
build, downloads it from `downloads.claude.ai`, and checks it twice before
anything is installed: the feed's sha256, then strict `codesign`, Team ID
`Q6L2SF6YDW` and Gatekeeper. It refuses a download from any other host. Then it
quits every Claude, swaps the new `Claude.app` in (the old one is kept as
`Claude.old-<timestamp>.app`), and rebuilds every instance that is now behind.
Run it with nothing to update and it still brings stale instances up to date.
One password prompt to install `Claude.app`; rebuilding the instances needs none once their launchers exist.

    ./scripts/reclone-instances.sh [--dry-run] [--force] [--add "Name"]

Rebuilds instances on their own, and converts any older layout (launcher that
execs `Claude.app`, or copy with a `Claude-real` shim) to the current one. `--add`
creates a new instance. Keeps each instance's existing bundle
id so its Notifications row survives, keeps its data folder, and moves the old
bundle to your Trash. A copy that is running is quit first — Electron reads
`app.asar` lazily, so replacing a bundle under a live window breaks it.

It copies with `ditto --noqtn`. A `Claude.app` installed from a browser-downloaded
DMG carries the quarantine flag; that is harmless on the notarized original, but a
quarantined ad-hoc copy is refused by Gatekeeper ("Apple could not verify ...
is free of malware"), and every process started from a window of it is killed.

### When the built-in updater will not install

Squirrel refuses to install while any instance of the target app is running,
gives it about four seconds to exit — a window with Claude Code sessions takes
longer — and throws the download away on failure. It also writes an
`updaterFailedInstall` counter into the profile and then stops trying that
version. `update-claude.sh` avoids all of it. Two older routes remain:

    ./scripts/install-from-dmg.sh ~/Downloads/Claude.dmg

Same checks and swap as `update-claude.sh`, from a DMG you downloaded yourself.

    ./scripts/finish-update.sh

Drives Squirrel itself: clears the counter, quits everything, stages the update
from a throwaway profile (a fresh profile checks within ~30s, an existing one
can take five minutes), then quits so ShipIt can swap the bundle. Least
reliable of the three.

## Export and resume

    powertools export --match "GST"                  # -> ~/Downloads/powertools-export-<ts>/

In the UI, the reader has **Export** (one chat to Markdown) and **Resume**
(copies `cd <project> && claude --resume <id>` to the clipboard). Selecting rows
and pressing **Export** writes the whole selection to a folder in Downloads.

## Usage

    powertools usage                          # this Mac, last 30 days, by model / app / chat
    powertools usage --windows                # inside the app's 5-hour and weekly limit windows
    powertools usage --export ~/Desktop/me.json
    powertools usage --combine a.json b.json  # rank several Macs per week, with share %

The Claude app's limits panel shows the whole account's percentage and Anthropic
does not publish the token size of 100%, so "my share of the meter" cannot be
computed. What can: each person exports from their own Mac at the end of the
week, one person combines the files, and the share column is each person's
slice of the total. The `?t=` in the URL is a random per-launch token that
guards the local API. It is not an account id.

## What Claude remembers

Claude writes itself notes while working with you. They live as Markdown files
with frontmatter under `~/.claude/projects/<project>/memory/`, one fact per file,
plus a `MEMORY.md` index. When Claude says "Saved to memory as some-name", that
is the filename.

    powertools memory                 # all of them, grouped by type
    powertools memory budget          # search name, description and body
    powertools memory --show is-rq-basic-tier-rate-null

The **What Claude remembers** panel in the web UI does the same with search,
renders each note, follows the `[[wiki-links]]` between notes, and can open the
chat a note was learned in.

## What it reads

    ~/Library/Application Support/<instance>/claude-code-sessions/<account>/<org>/*.json
    ~/.claude/projects/<slug>/<cliSessionId>.jsonl

The registration JSON holds the title, model, cwd and timestamps, and its folder
path *is* the account association. The `.jsonl` holds the actual conversation.

Only `move` writes anything. Everything else is read-only.

## Moves are reversible

Every move backs up both sides to `~/.claude-powertools/backups/<timestamp>/` and writes a
journal to `~/.claude-powertools/undo.json`. `powertools undo` reverses the last one, including
restoring anything that was overwritten.

Quit Claude fully (Cmd+Q) and reopen before moved chats appear.

## Account names

Claude stores no email on disk — only account UUIDs. powertools recovers the email
from any transcript where Claude Code injected it, and otherwise you name accounts
yourself with `powertools label`. Names live in `~/.claude-powertools/labels.json`.

## Notes

- Full-text search needs `powertools index --fts` once (~8s, ~80MB db). Incremental after.
- Transcripts with no desktop registration (terminal-only sessions) are not listed.
- Server binds 127.0.0.1 and requires the token in the URL it prints.

## Move to another Mac

    powertools backup                         # -> ~/Desktop/claude-powertools-backup.zip
    powertools backup --no-transcripts        # settings and chat list only, ~5 MB

Copy the zip across, then on the new Mac:

    powertools restore <file>                             # dry run, shows what it would write
    powertools restore <file> --what transcripts,memory,chats,config --yes

In the app, **Backup & move Mac…** does the same with a real macOS file chooser
rather than a typed path, and shows the backup's layout before anything happens:
which Claude app each chat lived in, which account, and whether that place exists
on this Mac yet.

Two ways the chats can land:

- **Recreate the same layout** — each Claude app gets back the chats it had. The
  apps have to exist on the new Mac first; make them under **Instances…** with
  the same names. Accounts line up on their own as long as you sign into the same
  Claude logins, because the account id comes from the login, not the machine.
- **Put every chat into one Claude** — everything merges into a single account you
  pick. Use this when you do not want several Claude apps on the new machine.

`--what` picks what comes back. Transcripts and memory merge safely. Chat
registrations and MCP config are machine-specific, so they are opt-in. Anything
replaced is backed up first. Use `--into <bucket>` to drop every chat into one
account when the account ids differ on the new machine.
