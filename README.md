# Claude PowerTools

Browse, move, back up and inspect everything Claude keeps on this Mac: chats,
memory, MCP connections and the app instances themselves.

Grew out of `~/copychat.sh`. Python stdlib only — no npm, no node, no build step.
(The npm `claude-transplant` tool broke on this Mac because the default node is v14.)

## Install

No npm and no node. It runs on the `python3` that ships with macOS, which is
deliberate: the npm tool this replaces died because the default node here is v14.

On this Mac it is already installed at `~/.local/bin/powertools`, running the
source in `~/claude-powertools/`, so edits to `powertools.py` or `ui.html` take effect
immediately.

For any other Mac, build one self-contained file and copy it over:

    powertools bundle --out ~/claude-powertools/dist/powertools   # bakes ui.html into the script
    ./install.sh                                     # or just copy dist/powertools
                                                     # anywhere on your PATH

`dist/powertools` is a single executable with no dependencies and nothing to
install alongside it.

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
    powertools new-instance "Claude Work 4"          # dry run, prints the plan
    powertools new-instance "Claude Work 4" --yes    # creates it

Creating one copies `/Applications/Claude.app`, patches its launcher to use a
separate `--user-data-dir`, and re-signs it. That needs admin rights, so macOS
shows its own password prompt. powertools never sees or stores the password.
The Instances panel in the web UI does the same thing with a full preview first.

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
