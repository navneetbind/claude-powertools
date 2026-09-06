#!/usr/bin/env python3
"""
Claude PowerTools - browse, move, back up and inspect everything Claude keeps on this Mac.

Grew out of copychat.sh. Stdlib only, no npm, no node, no build step.

  powertools serve          browse + move in a local web UI
  powertools list           newest chats in the terminal
  powertools search TEXT    search titles (add --fts to search message bodies)
  powertools accounts       accounts, orgs and instances with chat counts
  powertools label UUID N   give an account a human name
  powertools move           copy/move chats between accounts (see --help)
  powertools undo           reverse the last move
  powertools index [--fts]  rebuild the index

Data it reads:
  ~/Library/Application Support/<instance>/claude-code-sessions/<account>/<org>/*.json
  ~/.claude/projects/<slug>/<cliSessionId>.jsonl

Only `move` ever writes to those. Everything else is read-only.
"""

from __future__ import annotations

import argparse
import glob
import html
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOME = os.path.expanduser("~")
SUPPORT = os.path.join(HOME, "Library", "Application Support")
TRANSCRIPTS = os.path.join(HOME, ".claude", "projects")
STATE = os.path.join(HOME, ".claude-powertools")
DB = os.path.join(STATE, "index.db")
LABELS = os.path.join(STATE, "labels.json")
BACKUPS = os.path.join(STATE, "backups")
UNDO = os.path.join(STATE, "undo.json")
URLFILE = os.path.join(STATE, "url.txt")

# ---------------------------------------------------------------- discovery


def instance_roots() -> list[tuple[str, str]]:
    """(instance name, claude-code-sessions dir) for every Claude app on this Mac."""
    out = []
    for d in sorted(glob.glob(os.path.join(SUPPORT, "*laude*", "claude-code-sessions"))):
        if os.path.isdir(d):
            out.append((os.path.basename(os.path.dirname(d)), d))
    return out


def buckets() -> list[dict]:
    """Every instance/account/org folder that can hold chats."""
    out = []
    for inst, root in instance_roots():
        for acct in sorted(os.listdir(root)):
            ap = os.path.join(root, acct)
            if not os.path.isdir(ap):
                continue
            for org in sorted(os.listdir(ap)):
                op = os.path.join(ap, org)
                if not os.path.isdir(op):
                    continue
                n = len([f for f in os.listdir(op) if f.endswith(".json")])
                out.append(
                    {
                        "key": f"{inst}/{acct}/{org}",
                        "instance": inst,
                        "account": acct,
                        "org": org,
                        "path": op,
                        "count": n,
                    }
                )
    return out


def load_labels() -> dict:
    try:
        with open(LABELS) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_labels(d: dict) -> None:
    os.makedirs(STATE, exist_ok=True)
    with open(LABELS, "w") as fh:
        json.dump(d, fh, indent=2)


def transcript_for(cli_id: str) -> str | None:
    if not cli_id:
        return None
    hits = glob.glob(os.path.join(TRANSCRIPTS, "*", cli_id + ".jsonl"))
    return hits[0] if hits else None


# ---------------------------------------------------------------- transcript


def read_transcript(path: str, limit: int = 4000) -> list[dict]:
    """Parse a .jsonl transcript into messages of typed blocks, so the UI can
    render prose as prose and tool calls as collapsible rows."""
    msgs: list[dict] = []
    img_n = 0
    if not path or not os.path.exists(path):
        return msgs
    with open(path, errors="ignore") as fh:
        for line in fh:
            if len(msgs) >= limit:
                break
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type")
            if t not in ("user", "assistant"):
                continue
            body = (d.get("message") or {}).get("content")
            blocks: list[dict] = []
            if isinstance(body, str):
                if body.strip():
                    blocks.append({"kind": "text", "text": body})
            elif isinstance(body, list):
                for b in body:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "image":
                        src = b.get("source") or {}
                        blocks.append(
                            {
                                "kind": "image",
                                "idx": img_n,
                                "media": src.get("media_type") or "image/png",
                                "bytes": int(len(src.get("data") or "") * 0.75),
                                "text": "",
                            }
                        )
                        img_n += 1
                    elif bt == "text" and (b.get("text") or "").strip():
                        blocks.append({"kind": "text", "text": b["text"]})
                    elif bt == "thinking" and (b.get("thinking") or "").strip():
                        blocks.append({"kind": "thinking", "text": b["thinking"]})
                    elif bt == "tool_use":
                        arg = b.get("input")
                        blocks.append(
                            {
                                "kind": "tool",
                                "name": str(b.get("name") or "tool"),
                                "text": json.dumps(arg, indent=1)[:4000]
                                if arg is not None
                                else "",
                            }
                        )
                    elif bt == "tool_result":
                        c = b.get("content")
                        if isinstance(c, list):
                            c = "\n".join(
                                x.get("text", "") for x in c if isinstance(x, dict)
                            )
                        blocks.append({"kind": "result", "text": str(c or "")[:4000]})
            if not blocks:
                continue
            msgs.append({"role": t, "ts": d.get("timestamp") or "", "blocks": blocks})
    return msgs



def nth_image(path: str, n: int):
    """Raw bytes of the nth image in a transcript. Images are stored base64
    inline, so they are pulled on demand instead of shipped with the messages."""
    import base64

    i = 0
    with open(path, errors="ignore") as fh:
        for line in fh:
            if '"base64"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            body = (d.get("message") or {}).get("content")
            if not isinstance(body, list):
                continue
            for b in body:
                if isinstance(b, dict) and b.get("type") == "image":
                    if i == n:
                        src = b.get("source") or {}
                        try:
                            return base64.b64decode(src.get("data") or ""), (
                                src.get("media_type") or "image/png"
                            )
                        except Exception:
                            return None, None
                    i += 1
    return None, None


def transcript_files(path: str) -> list[dict]:
    """Files the chat pointed at. A PDF is only ever a reference: Claude records
    the path, page count and size, never the bytes. Text files keep content."""
    out, seen = [], set()
    if not path or not os.path.exists(path):
        return out
    with open(path, errors="ignore") as fh:
        for line in fh:
            if '"pdf_reference"' not in line and '"type": "file"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            a = d.get("attachment") or {}
            t = a.get("type")
            if t not in ("pdf_reference", "file"):
                continue
            fn = a.get("filename")
            if not fn or fn in seen:
                continue
            seen.add(fn)
            out.append(
                {
                    "kind": "pdf" if t == "pdf_reference" else "file",
                    "filename": fn,
                    "pages": a.get("pageCount"),
                    "size": a.get("fileSize"),
                    "exists": os.path.exists(fn),
                    "inline": t == "file",
                }
            )
    return out


def transcript_text(path: str) -> str:
    return "\n".join(
        b["text"] for m in read_transcript(path, limit=100000) for b in m["blocks"]
    )


def find_email(path: str) -> str | None:
    """Claude Code injects the account email into recent transcripts. Use it to
    auto-suggest an account label, since the email is nowhere else on disk."""
    if not path or not os.path.exists(path):
        return None
    with open(path, errors="ignore") as fh:
        for line in fh:
            if "email address is" not in line:
                continue
            m = re.search(r"email address is ([\w.%+-]+@[\w.-]+\.\w+)", line)
            if m:
                return m.group(1)
    return None



# ---------------------------------------------------------------- index

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  reg_path TEXT PRIMARY KEY,
  bucket TEXT, instance TEXT, account TEXT, org TEXT,
  session_id TEXT, cli_id TEXT,
  title TEXT, title_source TEXT, model TEXT, effort TEXT,
  cwd TEXT, created REAL, activity REAL, archived INTEGER,
  turns INTEGER, transcript TEXT, tbytes INTEGER, msgs INTEGER,
  email TEXT, reg_mtime REAL
);
CREATE INDEX IF NOT EXISTS s_bucket ON sessions(bucket);
CREATE INDEX IF NOT EXISTS s_act ON sessions(activity DESC);
CREATE TABLE IF NOT EXISTS fts_state (cli_id TEXT PRIMARY KEY, mtime REAL);
CREATE VIRTUAL TABLE IF NOT EXISTS ft USING fts5(cli_id UNINDEXED, body);
CREATE TABLE IF NOT EXISTS usage (
  cli_id TEXT, day TEXT, model TEXT,
  requests INTEGER, input INTEGER, output INTEGER,
  cache_read INTEGER, cache_create INTEGER, thinking INTEGER,
  PRIMARY KEY (cli_id, day, model)
);
CREATE TABLE IF NOT EXISTS usage_state (cli_id TEXT PRIMARY KEY, mtime REAL);
CREATE INDEX IF NOT EXISTS u_day ON usage(day);
"""


def connect() -> sqlite3.Connection:
    os.makedirs(STATE, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def build_index(fts: bool = False, progress=None) -> dict:
    c = connect()
    seen: list[str] = []
    added = 0
    for b in buckets():
        for f in sorted(glob.glob(os.path.join(b["path"], "*.json"))):
            seen.append(f)
            mt = os.path.getmtime(f)
            row = c.execute(
                "SELECT reg_mtime FROM sessions WHERE reg_path=?", (f,)
            ).fetchone()
            if row and abs(row["reg_mtime"] - mt) < 0.001:
                continue
            try:
                with open(f) as fh:
                    d = json.load(fh)
            except Exception:
                continue
            cli = d.get("cliSessionId") or ""
            tp = transcript_for(cli)
            tb = os.path.getsize(tp) if tp else 0
            n = 0
            if tp:
                with open(tp, "rb") as fh:
                    n = sum(1 for _ in fh)
            c.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f, b["key"], b["instance"], b["account"], b["org"],
                    d.get("sessionId"), cli,
                    d.get("title") or "(untitled)", d.get("titleSource"),
                    d.get("model"), d.get("effort"), d.get("cwd"),
                    d.get("createdAt"), d.get("lastActivityAt"),
                    1 if d.get("isArchived") else 0,
                    d.get("completedTurns"), tp, tb, n,
                    find_email(tp) if tp else None, mt,
                ),
            )
            added += 1
            if progress and added % 25 == 0:
                progress(added)
    if seen:
        q = ",".join("?" * len(seen))
        c.execute(f"DELETE FROM sessions WHERE reg_path NOT IN ({q})", seen)
    c.commit()

    indexed = 0
    if fts:
        rows = c.execute(
            "SELECT DISTINCT cli_id, transcript FROM sessions WHERE transcript IS NOT NULL"
        ).fetchall()
        for r in rows:
            mt = os.path.getmtime(r["transcript"])
            old = c.execute(
                "SELECT mtime FROM fts_state WHERE cli_id=?", (r["cli_id"],)
            ).fetchone()
            if old and abs(old["mtime"] - mt) < 0.001:
                continue
            c.execute("DELETE FROM ft WHERE cli_id=?", (r["cli_id"],))
            c.execute(
                "INSERT INTO ft (cli_id, body) VALUES (?,?)",
                (r["cli_id"], transcript_text(r["transcript"])),
            )
            c.execute(
                "INSERT OR REPLACE INTO fts_state VALUES (?,?)", (r["cli_id"], mt)
            )
            indexed += 1
            if progress and indexed % 10 == 0:
                progress(indexed)
            c.commit()
    c.commit()
    total = c.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"]
    c.close()
    return {"total": total, "updated": added, "fts": indexed}


def auto_labels() -> dict:
    """Name each account. The signed-in account is recorded by Claude itself;
    anything left over falls back to an email seen inside a transcript."""
    out = {u: d["email"] for u, d in account_directory().items()}
    c = connect()
    for r in c.execute(
        "SELECT account, email, COUNT(*) n FROM sessions "
        "WHERE email IS NOT NULL GROUP BY account, email ORDER BY n DESC"
    ):
        out.setdefault(r["account"], r["email"])
    c.close()
    return out


def query(scope=None, q=None, fts=False, archived=False, limit=300, offset=0):
    c = connect()
    where, args = [], []
    if scope:
        where.append("bucket=?")
        args.append(scope)
    if not archived:
        where.append("archived=0")
    if q:
        if fts:
            where.append(
                "(title LIKE ? OR cli_id IN (SELECT cli_id FROM ft WHERE ft MATCH ?))"
            )
            args += [f"%{q}%", q]
        else:
            where.append("(title LIKE ? OR cwd LIKE ?)")
            args += [f"%{q}%", f"%{q}%"]
    sql = "SELECT * FROM sessions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY activity DESC LIMIT ? OFFSET ?"
    rows = [dict(r) for r in c.execute(sql, args + [limit, offset])]
    c.close()
    return rows



# ---------------------------------------------------------------- instances

APPS_DIR = "/Applications"
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,48}$")


def app_bundles() -> dict:
    """Every Claude*.app and the data folder it was patched to use."""
    out = {}
    for p in sorted(glob.glob(os.path.join(APPS_DIR, "*laude*.app"))):
        launcher = os.path.join(p, "Contents", "MacOS", "Claude")
        data = os.path.join(SUPPORT, "Claude")  # untouched app uses the default
        try:
            with open(launcher, "rb") as fh:
                head = fh.read(4096)
            if head.startswith(b"#!"):
                m = re.search(rb'user-data-dir="([^"]+)"', head)
                data = m.group(1).decode() if m else None
        except Exception:
            pass
        out[p] = data
    return out


def dir_size(path: str) -> int:
    r = subprocess.run(["du", "-sk", path], capture_output=True, text=True)
    try:
        return int(r.stdout.split()[0]) * 1024
    except Exception:
        return 0


DISPOSABLE = ("vm_bundles", "Cache", "Code Cache", "GPUCache", "DawnGraphiteCache",
              "DawnWebGPUCache", "ShaderCache", "claude-code-vm", "Crashpad",
              "Service Worker", "Session Storage")


def size_breakdown(data_dir: str) -> dict:
    """Most of an instance folder is VM images and browser cache, not your chats.
    Splitting it stops the folder size from looking like backup size."""
    junk = 0
    for name in DISPOSABLE:
        p = os.path.join(data_dir, name)
        if os.path.isdir(p):
            junk += dir_size(p)
    chats = dir_size(os.path.join(data_dir, "claude-code-sessions")) \
        if os.path.isdir(os.path.join(data_dir, "claude-code-sessions")) else 0
    total = dir_size(data_dir)
    return {"total": total, "chats": chats, "disposable": junk,
            "other": max(0, total - junk - chats)}


def instances() -> list[dict]:
    """Pair each Claude data folder with the app that opens it, and flag the
    ones that lost their other half."""
    apps = app_bundles()
    by_data = {}
    for app, data in apps.items():
        if data:
            by_data.setdefault(os.path.normpath(data), []).append(app)
    out = []
    seen = set()
    for d in sorted(glob.glob(os.path.join(SUPPORT, "*laude*"))):
        if not os.path.isdir(d):
            continue
        key = os.path.normpath(d)
        seen.add(key)
        owners = by_data.get(key, [])
        out.append(
            {
                "name": os.path.basename(d),
                "data": d,
                "apps": owners,
                "chats": len(glob.glob(os.path.join(d, "claude-code-sessions", "*", "*", "*.json"))),
                "bytes": dir_size(d),
                "size": size_breakdown(d),
                "problem": None if owners else "no app opens this folder",
            }
        )
    for app, data in apps.items():
        if data and os.path.normpath(data) not in seen:
            out.append(
                {
                    "name": os.path.basename(app),
                    "data": data,
                    "apps": [app],
                    "chats": 0,
                    "bytes": 0,
                    "size": {"total": 0, "chats": 0, "disposable": 0, "other": 0},
                    "problem": "app points at a folder that does not exist",
                }
            )
    return out


def instance_plan(name: str) -> dict:
    """What creating a new Claude instance would do. Nothing is run here."""
    name = (name or "").strip()
    if not SAFE_NAME.match(name):
        raise ValueError("name must be letters, numbers, spaces, - or _ (max 49)")
    app = os.path.join(APPS_DIR, name + ".app")
    data = os.path.join(SUPPORT, name.replace(" ", "-"))
    src = os.path.join(APPS_DIR, "Claude.app")
    script = f"""#!/bin/bash
set -e
APP_ORIG={shlex.quote(src)}
APP_NEW={shlex.quote(app)}
DATA_DIR={shlex.quote(data)}

rm -rf "$APP_NEW"
cp -R "$APP_ORIG" "$APP_NEW"
mv "$APP_NEW/Contents/MacOS/Claude" "$APP_NEW/Contents/MacOS/Claude-real"

cat > "$APP_NEW/Contents/MacOS/Claude" <<'INNEREOF'
#!/bin/bash
DIR="$( cd "$( dirname "${{BASH_SOURCE[0]}}" )" && pwd )"
exec "$DIR/Claude-real" --user-data-dir={shlex.quote(data)} "$@"
INNEREOF

chmod +x "$APP_NEW/Contents/MacOS/Claude"
codesign --force --deep --sign - "$APP_NEW"
echo DONE
"""
    return {
        "name": name,
        "app": app,
        "data": data,
        "source": src,
        "script": script,
        "app_exists": os.path.exists(app),
        "data_exists": os.path.exists(data),
        "source_ok": os.path.exists(src),
    }


def create_instance(plan: dict) -> dict:
    """Run the plan. macOS shows its own password prompt - Claude PowerTools never sees
    the password, and never stores it."""
    if not plan["source_ok"]:
        return {"ok": False, "msg": "/Applications/Claude.app not found"}
    os.makedirs(STATE, exist_ok=True)
    sh = os.path.join(STATE, "new-instance.sh")
    with open(sh, "w") as fh:
        fh.write(plan["script"])
    os.chmod(sh, 0o755)
    r = subprocess.run(
        ["osascript", "-e",
         f'do shell script {json.dumps(sh)} with administrator privileges'],
        capture_output=True, text=True,
    )
    ok = r.returncode == 0 and "DONE" in (r.stdout or "")
    return {
        "ok": ok,
        "msg": "created " + plan["app"] if ok
        else (r.stderr or r.stdout or "cancelled").strip()[:400],
        "script_path": sh,
    }


# ---------------------------------------------------------------- export


def as_markdown(row: dict, msgs: list[dict], imgdir: str = "", files=None) -> str:
    head = [
        f"# {row.get('title') or '(untitled)'}",
        "",
        f"- Project: `{row.get('cwd') or ''}`",
        f"- Model: {row.get('model') or ''}",
        f"- Messages: {len(msgs)}",
        f"- Session: `{row.get('cli_id') or ''}`",
        "",
    ]
    for f in files or []:
        note = "" if f["exists"] else "  (file no longer on disk)"
        if f["kind"] == "pdf":
            head.append(
                f"- PDF referenced: `{f['filename']}` "
                f"({f.get('pages')} pages){note}  -  only the path was stored, not the file"
            )
        else:
            head.append(f"- File attached: `{f['filename']}`{note}")
    head += ["", "---", ""]
    body = []
    for m in msgs:
        body.append(f"### {m['role']}  ·  {(m.get('ts') or '')[:19].replace('T', ' ')}")
        for b in m["blocks"]:
            if b["kind"] == "text":
                body.append(b["text"])
            elif b["kind"] == "image":
                body.append(f"![screenshot]({imgdir}/image-{b['idx']:03d}.{b.get('ext','png')})"
                            if imgdir else "*(screenshot)*")
            elif b["kind"] == "tool":
                body.append(f"<details><summary>tool: {b.get('name')}</summary>\n\n```json\n{b['text']}\n```\n</details>")
            elif b["kind"] == "thinking":
                body.append(f"<details><summary>thinking</summary>\n\n```\n{b['text']}\n```\n</details>")
            else:
                body.append(f"<details><summary>result</summary>\n\n```\n{b['text']}\n```\n</details>")
        body.append("")
    return "\n".join(head + body)


def safe_filename(s: str, fallback: str = "chat") -> str:
    s = re.sub(r"[^A-Za-z0-9 ._-]", "", (s or "")).strip() or fallback
    return s[:70]


def export_many(rows: list[dict], outdir: str) -> dict:
    os.makedirs(outdir, exist_ok=True)
    n = 0
    for r in rows:
        tp = r.get("transcript") or ""
        msgs = read_transcript(tp)
        if not msgs:
            continue
        stamp = ""
        if r.get("activity"):
            stamp = datetime.fromtimestamp(r["activity"] / 1000).strftime("%Y-%m-%d ")
        base = safe_filename(stamp + (r.get("title") or ""))
        imgs = [b for m in msgs for b in m["blocks"] if b["kind"] == "image"]
        imgdir = ""
        if imgs:
            imgdir = base + "-images"
            os.makedirs(os.path.join(outdir, imgdir), exist_ok=True)
            for b in imgs:
                raw, media = nth_image(tp, b["idx"])
                if raw:
                    ext = {"image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}.get(media, "png")
                    b["ext"] = ext
                    with open(os.path.join(outdir, imgdir, f"image-{b['idx']:03d}.{ext}"), "wb") as ih:
                        ih.write(raw)
        fn = base + ".md"
        with open(os.path.join(outdir, fn), "w") as fh:
            fh.write(as_markdown(r, msgs, imgdir, transcript_files(tp)))
        n += 1
    return {"written": n, "dir": outdir}



# ---------------------------------------------------------------- memory

MEMORY_ROOT = os.path.join(HOME, ".claude", "projects")


def memory_dirs() -> list[str]:
    return sorted(glob.glob(os.path.join(MEMORY_ROOT, "*", "memory")))


def parse_memory(path: str) -> dict:
    """One memory file: frontmatter plus the body Claude wrote."""
    raw = open(path, errors="ignore").read()
    fm, body = "", raw
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", raw, re.S)
    if m:
        fm, body = m.group(1), m.group(2)

    def field(k):
        g = re.search(rf"^\s*{k}:\s*(.+?)\s*$", fm, re.M)
        return g.group(1).strip() if g else None

    project = os.path.basename(os.path.dirname(os.path.dirname(path)))
    return {
        "name": field("name") or os.path.basename(path)[:-3],
        "file": path,
        "project": project.replace("-Users-user-", "").replace("-", "/"),
        "description": field("description") or "",
        "type": field("type") or "note",
        "session": field("originSessionId"),
        "modified": field("modified") or "",
        "links": sorted(set(re.findall(r"\[\[([^\]]+)\]\]", body))),
        "body": body.strip(),
        "bytes": os.path.getsize(path),
    }


def memories() -> dict:
    """Everything Claude has written down, plus the index file it keeps."""
    items, index = [], None
    for d in memory_dirs():
        for f in sorted(glob.glob(os.path.join(d, "*.md"))):
            if os.path.basename(f) == "MEMORY.md":
                index = {"file": f, "body": open(f, errors="ignore").read()}
                continue
            try:
                items.append(parse_memory(f))
            except Exception:
                pass
    names = {i["name"] for i in items}
    for i in items:
        i["dangling"] = [l for l in i["links"] if l not in names]
    items.sort(key=lambda x: x["modified"], reverse=True)
    return {"items": items, "index": index}



# ---------------------------------------------------------------- mcp

SECRETISH = re.compile(r"token|secret|password|api[_-]?key|auth|credential|cookie", re.I)


def _redact(cfg) -> dict:
    """Show which settings exist, never their values. These files hold tokens."""
    out = {}
    for k, v in (cfg or {}).items():
        if SECRETISH.search(k):
            out[k] = "(hidden)"
        elif isinstance(v, str) and len(v) > 90:
            out[k] = v[:60] + "…"
        elif isinstance(v, (dict, list)):
            out[k] = f"({type(v).__name__})"
        else:
            out[k] = v
    return out


def _servers_from(cfg: dict, scope: str, source: str, shared: bool) -> list[dict]:
    out = []
    for name, v in (cfg.get("mcpServers") or {}).items():
        v = v if isinstance(v, dict) else {}
        cmd = v.get("command") or ""
        target = v.get("url") or v.get("serverUrl") or ""
        if cmd and not target:
            args = v.get("args") or []
            remote = next((a for a in args if isinstance(a, str) and a.startswith("http")), "")
            target = remote or (os.path.basename(cmd) + " " + " ".join(map(str, args[:2])))
        out.append(
            {
                "id": f"cfg:{scope}:{name}",
                "name": name,
                "scope": scope,
                "shared": shared,
                "source": source,
                "kind": "remote" if str(target).startswith("http") else "local",
                "target": str(target)[:120],
                "enabled": True,
                "copyable": not shared,
                "settings": _redact(v.get("env") or {}),
                "secrets": [k for k in (v.get("env") or {}) if SECRETISH.search(k)],
            }
        )
    return out


def mcp_servers() -> list[dict]:
    """Every MCP server on this Mac and which Claude it belongs to.

    Four places define them: each app instance's own config, extensions installed
    into an instance, the shared CLI settings, and per-project .mcp.json files.
    """
    out = []
    for inst, _root in instance_roots():
        base = os.path.join(SUPPORT, inst)
        cfg_path = os.path.join(base, "claude_desktop_config.json")
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path) as fh:
                    out += _servers_from(json.load(fh), inst, cfg_path, False)
            except Exception:
                pass
        # extensions bring their own server plus their own settings
        for ext in sorted(glob.glob(os.path.join(base, "Claude Extensions", "*"))):
            name = os.path.basename(ext)
            sett = os.path.join(base, "Claude Extensions Settings", name + ".json")
            enabled, conf = True, {}
            if os.path.exists(sett):
                try:
                    with open(sett) as fh:
                        d = json.load(fh)
                    enabled = d.get("isEnabled", True)
                    conf = _redact(d.get("userConfig") or {})
                except Exception:
                    pass
            raw = {}
            if os.path.exists(sett):
                try:
                    with open(sett) as fh:
                        raw = (json.load(fh).get("userConfig") or {})
                except Exception:
                    raw = {}
            out.append(
                {
                    "id": f"ext:{inst}:{name}",
                    "name": name.replace("ant.dir.gh.", ""),
                    "scope": inst,
                    "shared": False,
                    "source": ext,
                    "kind": "extension",
                    "target": str(conf.get("grafana_url") or conf.get("url") or ""),
                    "enabled": enabled,
                    "copyable": True,
                    "settings": conf,
                    "secrets": [k for k in raw if SECRETISH.search(k)],
                }
            )
    for p in (os.path.join(HOME, ".claude", "settings.json"),
              os.path.join(HOME, ".claude.json")):
        if os.path.exists(p):
            try:
                with open(p) as fh:
                    out += _servers_from(json.load(fh), "Claude Code (all instances)", p, True)
            except Exception:
                pass
    for p in sorted(glob.glob(os.path.join(HOME, "Downloads", "*", "*", ".mcp.json"))):
        try:
            with open(p) as fh:
                out += _servers_from(
                    json.load(fh), "project: " + os.path.basename(os.path.dirname(p)), p, True
                )
        except Exception:
            pass
    return out



def reveal_secret(server_id: str, key: str) -> dict:
    """Show one stored value, on explicit request. Values Claude has encrypted
    come back as the encrypted blob, because that is all that is on disk."""
    srv = next((m for m in mcp_servers() if m["id"] == server_id), None)
    if not srv or key not in (srv.get("secrets") or []):
        return {"ok": False, "msg": "unknown setting"}
    val = None
    if srv["id"].startswith("ext:"):
        name = os.path.basename(srv["source"])
        sett = os.path.join(os.path.dirname(os.path.dirname(srv["source"])),
                            "Claude Extensions Settings", name + ".json")
        try:
            with open(sett) as fh:
                val = (json.load(fh).get("userConfig") or {}).get(key)
        except Exception:
            pass
    else:
        try:
            with open(srv["source"]) as fh:
                val = ((json.load(fh).get("mcpServers") or {})
                       .get(srv["name"], {}).get("env") or {}).get(key)
        except Exception:
            pass
    if val is None:
        return {"ok": False, "msg": "not found"}
    enc = isinstance(val, str) and val.startswith("__encrypted__")
    return {"ok": True, "key": key, "value": val,
            "encrypted": enc,
            "note": "Claude encrypted this; the real value is not stored in the file"
            if enc else "stored in plain text in that file"}


def copy_mcp(server_id: str, dest_instance: str, dry: bool = True) -> dict:
    """Copy one MCP server into another Claude app instance."""
    srv = next((m for m in mcp_servers() if m["id"] == server_id), None)
    if not srv:
        return {"ok": False, "msg": "unknown server"}
    if srv["shared"]:
        return {"ok": False,
                "msg": "this one already applies to every instance, nothing to copy"}
    if dest_instance == srv["scope"]:
        return {"ok": False, "msg": "that is where it already lives"}
    names = [i for i, _ in instance_roots()]
    if dest_instance not in names:
        return {"ok": False, "msg": "unknown instance"}
    base = os.path.join(SUPPORT, dest_instance)
    writes, notes = [], []

    if srv["id"].startswith("ext:"):
        ext_name = os.path.basename(srv["source"])
        dst_ext = os.path.join(base, "Claude Extensions", ext_name)
        dst_set = os.path.join(base, "Claude Extensions Settings", ext_name + ".json")
        src_set = os.path.join(os.path.dirname(os.path.dirname(srv["source"])),
                               "Claude Extensions Settings", ext_name + ".json")
        writes.append(("tree", srv["source"], dst_ext))
        if os.path.exists(src_set):
            writes.append(("file", src_set, dst_set))
            notes.append("its settings come too, including the stored token")
        if os.path.exists(dst_ext):
            notes.append("an extension of the same name is already there and will be replaced")
    else:
        dst_cfg = os.path.join(base, "claude_desktop_config.json")
        writes.append(("merge", srv["source"], dst_cfg))
        try:
            with open(dst_cfg) as fh:
                if srv["name"] in (json.load(fh).get("mcpServers") or {}):
                    notes.append("a server called %s is already there and will be replaced"
                                 % srv["name"])
        except Exception:
            pass
        notes.append("any token in its env is copied as-is")

    plan = {"ok": True, "server": srv["name"], "from": srv["scope"],
            "to": dest_instance, "writes": [(w[0], w[2]) for w in writes], "notes": notes}
    if dry:
        return plan

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(BACKUPS, stamp)
    os.makedirs(bdir, exist_ok=True)
    journal = {"stamp": stamp, "mode": "mcp", "backup": bdir, "actions": [], "restores": []}
    for kind, src, dst in writes:
        if os.path.exists(dst):
            b = os.path.join(bdir, "was__" + os.path.basename(dst))
            if os.path.isdir(dst):
                shutil.copytree(dst, b, dirs_exist_ok=True)
            else:
                shutil.copy2(dst, b)
            journal["restores"].append({"path": dst, "backup": b, "dir": os.path.isdir(dst)})
        else:
            journal["restores"].append({"path": dst, "backup": None, "dir": kind == "tree"})
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if kind == "tree":
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
        elif kind == "file":
            shutil.copy2(src, dst)
        else:
            with open(src) as fh:
                entry = (json.load(fh).get("mcpServers") or {})[srv["name"]]
            cur = {}
            if os.path.exists(dst):
                try:
                    with open(dst) as fh:
                        cur = json.load(fh)
                except Exception:
                    cur = {}
            cur.setdefault("mcpServers", {})[srv["name"]] = entry
            with open(dst, "w") as fh:
                json.dump(cur, fh, indent=2)
    with open(UNDO, "w") as fh:
        json.dump(journal, fh, indent=2)
    plan["msg"] = "copied to " + dest_instance + " - quit that Claude app fully and reopen it"
    plan["backup"] = bdir
    return plan



def mcp_snippet(server_id: str, reveal: bool = False) -> dict:
    """What you would need to set this server up on a different Mac."""
    srv = next((m for m in mcp_servers() if m["id"] == server_id), None)
    if not srv:
        return {"ok": False, "msg": "unknown server"}

    def mask(d):
        return {
            k: ("PUT-YOUR-OWN-VALUE-HERE" if (SECRETISH.search(k) and not reveal) else v)
            for k, v in (d or {}).items()
        }

    if srv["id"].startswith("ext:"):
        ext_name = os.path.basename(srv["source"])
        sett = os.path.join(os.path.dirname(os.path.dirname(srv["source"])),
                            "Claude Extensions Settings", ext_name + ".json")
        conf = {}
        try:
            with open(sett) as fh:
                conf = json.load(fh).get("userConfig") or {}
        except Exception:
            pass
        return {
            "ok": True, "name": srv["name"], "kind": "extension",
            "snippet": json.dumps({"userConfig": mask(conf)}, indent=2),
            "target": "the other Mac installs the extension itself",
            "has_secrets": bool(srv["secrets"]),
            "steps": [
                "On the other Mac, open Claude and install the same extension "
                f"({srv['name']}) from the extension directory.",
                "Open its settings in Claude and fill in the values below.",
                "The token cannot travel: Claude encrypts it per machine, so paste "
                "a fresh one from Grafana or whichever service it belongs to.",
                "Quit Claude fully with Cmd+Q and reopen it.",
            ],
        }

    entry = {}
    try:
        with open(srv["source"]) as fh:
            entry = (json.load(fh).get("mcpServers") or {}).get(srv["name"], {})
    except Exception:
        pass
    entry = dict(entry)
    if entry.get("env"):
        entry["env"] = mask(entry["env"])
    local = bool(entry.get("command"))
    return {
        "ok": True, "name": srv["name"], "kind": srv["kind"],
        "snippet": json.dumps({"mcpServers": {srv["name"]: entry}}, indent=2),
        "target": "~/Library/Application Support/<Claude app name>/claude_desktop_config.json",
        "has_secrets": bool(srv["secrets"]),
        "steps": [
            "On the other Mac, quit Claude fully with Cmd+Q.",
            "Open the file above. If it already has an mcpServers block, add this "
            "server inside it rather than replacing the whole file.",
            ("The command path below is from this Mac. Install the same tool there "
             "and correct the path if it differs.") if local
            else "This one is a remote URL, so nothing needs installing.",
            "Fill in any value marked PUT-YOUR-OWN-VALUE-HERE.",
            "Reopen Claude.",
        ],
    }



# ---------------------------------------------------------------- move house

BACKUP_VERSION = 1


def backup_plan(with_transcripts: bool = True) -> dict:
    regs = sum(len(glob.glob(os.path.join(b["path"], "*.json"))) for b in buckets())
    tr = glob.glob(os.path.join(TRANSCRIPTS, "*", "*.jsonl"))
    mem = sum(len(glob.glob(os.path.join(d, "*.md"))) for d in memory_dirs())
    tbytes = sum(os.path.getsize(f) for f in tr) if with_transcripts else 0
    return {
        "registrations": regs,
        "transcripts": len(tr) if with_transcripts else 0,
        "memories": mem,
        "mcp": len(mcp_servers()),
        "bytes": tbytes,
    }


def make_backup(out: str, with_transcripts: bool = True) -> dict:
    """Everything that makes this Mac's Claude yours, in one zip."""
    import zipfile

    out = os.path.abspath(os.path.expanduser(out))
    if not out.endswith(".zip"):
        out += ".zip"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    man = {
        "version": BACKUP_VERSION,
        "made": datetime.now().isoformat(timespec="seconds"),
        "machine": os.uname().nodename,
        "home": HOME,
        "instances": [],
        "with_transcripts": with_transcripts,
    }
    n = {"regs": 0, "transcripts": 0, "memories": 0, "configs": 0}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for b in buckets():
            man["instances"].append(
                {"instance": b["instance"], "account": b["account"],
                 "org": b["org"], "chats": b["count"]}
            )
            for f in glob.glob(os.path.join(b["path"], "*.json")):
                z.write(f, f"registrations/{b['instance']}/{b['account']}/{b['org']}/"
                           f"{os.path.basename(f)}")
                n["regs"] += 1
        if with_transcripts:
            for f in glob.glob(os.path.join(TRANSCRIPTS, "*", "*.jsonl")):
                slug = os.path.basename(os.path.dirname(f))
                z.write(f, f"transcripts/{slug}/{os.path.basename(f)}")
                n["transcripts"] += 1
        for d in memory_dirs():
            proj = os.path.basename(os.path.dirname(d))
            for f in glob.glob(os.path.join(d, "*.md")):
                z.write(f, f"memory/{proj}/{os.path.basename(f)}")
                n["memories"] += 1
        for inst, _ in instance_roots():
            cfg = os.path.join(SUPPORT, inst, "claude_desktop_config.json")
            if os.path.exists(cfg):
                z.write(cfg, f"mcp/{inst}/claude_desktop_config.json")
                n["configs"] += 1
            for f in glob.glob(os.path.join(SUPPORT, inst,
                                            "Claude Extensions Settings", "*.json")):
                z.write(f, f"mcp/{inst}/extension-settings/{os.path.basename(f)}")
                n["configs"] += 1
        for p in (os.path.join(HOME, ".claude", "settings.json"),
                  os.path.join(HOME, ".claude.json")):
            if os.path.exists(p):
                z.write(p, "claude-code/" + os.path.basename(p))
                n["configs"] += 1
        z.writestr("manifest.json", json.dumps(man, indent=2))
    return {"file": out, "bytes": os.path.getsize(out), **n}


def read_backup(path: str) -> dict:
    import zipfile

    path = os.path.abspath(os.path.expanduser(path))
    with zipfile.ZipFile(path) as z:
        try:
            man = json.loads(z.read("manifest.json"))
        except KeyError:
            return {"ok": False, "msg": "not a powertools backup (no manifest)"}
        names = z.namelist()
    return {
        "ok": True, "file": path, "manifest": man,
        "counts": {
            "registrations": sum(1 for n in names if n.startswith("registrations/")),
            "transcripts": sum(1 for n in names if n.startswith("transcripts/")),
            "memories": sum(1 for n in names if n.startswith("memory/")),
            "configs": sum(1 for n in names
                           if n.startswith("mcp/") or n.startswith("claude-code/")),
        },
    }


def restore_backup(path: str, what: list, dry: bool = True,
                   into: str | None = None) -> dict:
    """Put a backup back. Transcripts and memory are safe to merge. Registrations
    and MCP config are per-machine, so they are opt-in and never silently replace."""
    import zipfile

    info = read_backup(path)
    if not info["ok"]:
        return info
    steps, written, skipped = [], 0, 0
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(BACKUPS, "restore-" + stamp)
    dests = {b["key"]: b for b in buckets()}
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name.endswith("/") or name == "manifest.json":
                continue
            top = name.split("/")[0]
            if top == "transcripts" and "transcripts" in what:
                dst = os.path.join(TRANSCRIPTS, *name.split("/")[1:])
            elif top == "memory" and "memory" in what:
                proj = name.split("/")[1]
                dst = os.path.join(HOME, ".claude", "projects", proj, "memory",
                                   os.path.basename(name))
            elif top == "registrations" and "chats" in what:
                parts = name.split("/")
                if into and into in dests:
                    dst = os.path.join(dests[into]["path"], parts[-1])
                else:
                    dst = os.path.join(SUPPORT, parts[1], "claude-code-sessions",
                                       parts[2], parts[3], parts[4])
            elif top in ("mcp", "claude-code") and "config" in what:
                if top == "claude-code":
                    dst = os.path.join(HOME, os.path.basename(name)) \
                        if name.endswith(".claude.json") \
                        else os.path.join(HOME, ".claude", os.path.basename(name))
                else:
                    parts = name.split("/")
                    dst = (os.path.join(SUPPORT, parts[1], "claude_desktop_config.json")
                           if parts[2] == "claude_desktop_config.json"
                           else os.path.join(SUPPORT, parts[1],
                                             "Claude Extensions Settings", parts[-1]))
            else:
                skipped += 1
                continue
            steps.append((name, dst, os.path.exists(dst)))
            if not dry:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.exists(dst):
                    rel = os.path.relpath(dst, HOME).replace("/", "__")
                    os.makedirs(bdir, exist_ok=True)
                    shutil.copy2(dst, os.path.join(bdir, rel))
                with open(dst, "wb") as fh:
                    fh.write(z.read(name))
                written += 1
    return {
        "ok": True, "dry": dry, "from": path,
        "planned": len(steps), "written": written, "skipped": skipped,
        "overwrites": sum(1 for _, _, e in steps if e),
        "backup": bdir if not dry else None,
        "sample": [d for _, d, _ in steps[:8]],
    }



def find_backups() -> list[dict]:
    """Backup zips sitting in the usual places."""
    out = []
    for d in (os.path.join(HOME, "Desktop"), os.path.join(HOME, "Downloads"), HOME):
        for f in sorted(glob.glob(os.path.join(d, "*.zip"))):
            info = read_backup(f)
            if info.get("ok"):
                out.append(
                    {
                        "file": f,
                        "bytes": os.path.getsize(f),
                        "made": info["manifest"].get("made"),
                        "machine": info["manifest"].get("machine"),
                        "counts": info["counts"],
                    }
                )
    return out



def search_snippets(q: str, ids: list, per: int = 2) -> dict:
    """A line of context around each hit, so a search says why a chat matched."""
    if not q or not ids:
        return {}
    c = connect()
    out = {}
    try:
        rows = c.execute(
            "SELECT cli_id, snippet(ft, 1, '<<', '>>', ' … ', 14) AS s "
            "FROM ft WHERE ft MATCH ? AND cli_id IN (%s) LIMIT 400"
            % ",".join("?" * len(ids)),
            [q] + list(ids),
        ).fetchall()
        for r in rows:
            out.setdefault(r["cli_id"], [])
            if len(out[r["cli_id"]]) < per:
                out[r["cli_id"]].append(r["s"])
    except Exception:
        pass
    c.close()
    return out


def fts_ready() -> int:
    c = connect()
    try:
        n = c.execute("SELECT COUNT(*) n FROM fts_state").fetchone()["n"]
    except Exception:
        n = 0
    c.close()
    return n



def pick_path(kind: str = "file") -> dict:
    """Open the real macOS file chooser. This is a local app, so the picker
    belongs on the desktop rather than being a path the user has to type."""
    if kind == "folder":
        script = ('POSIX path of (choose folder with prompt '
                  '"Where should the backup go?")')
    else:
        script = ('POSIX path of (choose file with prompt '
                  '"Pick a Claude PowerTools backup" of type {"public.zip-archive"})')
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        return {"ok": False, "msg": "cancelled"}
    return {"ok": True, "path": r.stdout.strip()}


def backup_layout(path: str) -> dict:
    """How the backup was arranged, and whether this Mac has the same places."""
    info = read_backup(path)
    if not info.get("ok"):
        return info
    import zipfile

    here = {b["key"]: b for b in buckets()}
    have_inst = {i for i, _ in instance_roots()}
    counts = {}
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if not n.startswith("registrations/") or n.endswith("/"):
                continue
            p = n.split("/")
            if len(p) < 5:
                continue
            counts[(p[1], p[2], p[3])] = counts.get((p[1], p[2], p[3]), 0) + 1
    rows = []
    for (inst, acct, org), n in sorted(counts.items(), key=lambda x: -x[1]):
        key = f"{inst}/{acct}/{org}"
        rows.append(
            {
                "key": key, "instance": inst, "account": acct, "org": org, "chats": n,
                "instance_here": inst in have_inst,
                "exact_here": key in here,
            }
        )
    return {"ok": True, "rows": rows, "targets": sorted(here),
            "manifest": info["manifest"], "counts": info["counts"]}




# ---------------------------------------------------------------- leveldb

def _varint(b, i):
    r = sh = 0
    while True:
        c = b[i]; i += 1; r |= (c & 0x7F) << sh; sh += 7
        if c < 0x80:
            return r, i


def _snappy(b: bytes) -> bytes:
    """Minimal snappy decompressor. Chrome's LevelDB blocks use it and no
    library ships with macOS, so 25 lines beats a dependency."""
    _, i = _varint(b, 0); out = bytearray()
    while i < len(b):
        t = b[i]; i += 1; k = t & 3
        if k == 0:
            n = t >> 2
            if n >= 60:
                nb = n - 59; n = int.from_bytes(b[i:i + nb], "little"); i += nb
            n += 1; out += b[i:i + n]; i += n
        else:
            if k == 1:
                n = ((t >> 2) & 7) + 4; off = ((t >> 5) << 8) | b[i]; i += 1
            elif k == 2:
                n = (t >> 2) + 1; off = int.from_bytes(b[i:i + 2], "little"); i += 2
            else:
                n = (t >> 2) + 1; off = int.from_bytes(b[i:i + 4], "little"); i += 4
            if off == 0 or off > len(out):
                raise ValueError("bad snappy offset")
            for _ in range(n):
                out.append(out[-off])
    return bytes(out)


def leveldb_text(path: str) -> list:
    """Plaintext of every block in a LevelDB .ldb (or the raw .log). Strings
    Chrome stored as UTF-16 collapse to ASCII by dropping NULs."""
    d = open(path, "rb").read()
    if path.endswith(".log"):
        return [d.decode("utf-8", "ignore").replace("\x00", "")]
    if len(d) < 48 or d[-8:] != bytes.fromhex("57fb808b247547db"):
        return []
    foot = d[-48:-8]
    _, i = _varint(foot, 0); _, i = _varint(foot, i)
    ioff, i = _varint(foot, i); isz, i = _varint(foot, i)

    def block(off, size):
        raw = d[off:off + size]
        return _snappy(raw) if d[off + size] == 1 else raw

    try:
        idx = block(ioff, isz)
    except Exception:
        return []
    out = []
    try:
        nrest = int.from_bytes(idx[-4:], "little"); end = len(idx) - 4 - 4 * nrest; p = 0
        while p < end:
            _, p = _varint(idx, p); ns, p = _varint(idx, p); vl, p = _varint(idx, p); p += ns
            h = idx[p:p + vl]; p += vl
            bo, q = _varint(h, 0); bs, q = _varint(h, q)
            try:
                out.append(block(bo, bs).decode("utf-8", "ignore").replace("\x00", ""))
            except Exception:
                pass
    except Exception:
        pass
    return out


ACCOUNT_RE = re.compile(
    r'"account":\{"tagged_id":"[^"]*","uuid":"([0-9a-f-]{36})","email_address":"([^"]+)"'
    r'(?:,"full_name":"([^"]*)")?(?:,"display_name":"([^"]*)")?'
    r'.{0,400}?"organization":\{"id":\d+,"uuid":"([0-9a-f-]{36})","name":"([^"]*)"', re.S)


def desktop_accounts() -> dict:
    """The account each Claude app is signed into, read from the app's own
    browser cache. Works even for apps that never touched Claude Code."""
    out = {}
    for inst, _ in instance_roots():
        base = os.path.join(SUPPORT, inst, "Local Storage", "leveldb")
        for f in sorted(glob.glob(os.path.join(base, "*.ldb")) + glob.glob(os.path.join(base, "*.log"))):
            try:
                texts = leveldb_text(f)
            except Exception:
                continue
            for t in texts:
                for m in ACCOUNT_RE.finditer(t):
                    uid, email, full, disp, org_uuid, org_name = m.groups()
                    out[uid] = {"email": email, "name": full or disp, "org": org_name,
                                "org_uuid": org_uuid, "plan": None, "role": None,
                                "source": f"{inst} app cache", "instance": inst}
    return out


# ---------------------------------------------------------------- who

def account_directory() -> dict:
    """Map account ids to the person behind them. Claude records the signed-in
    account in ~/.claude.json, and agent sessions keep their own copy, which is
    how accounts other than the current one get named."""
    out = {}
    paths = [os.path.join(HOME, ".claude.json")]
    paths += glob.glob(os.path.join(SUPPORT, "*laude*", "local-agent-mode-sessions",
                                    "*", "*", "agent", "*", ".claude", ".claude.json"))
    paths += glob.glob(os.path.join(SUPPORT, "*laude*", "local-agent-mode-sessions",
                                    "*", "*.json"))
    for p in paths:
        try:
            with open(p) as fh:
                o = (json.load(fh) or {}).get("oauthAccount") or {}
        except Exception:
            continue
        uid = o.get("accountUuid")
        if not uid or not o.get("emailAddress"):
            continue
        out.setdefault(uid, {
            "email": o.get("emailAddress"),
            "name": o.get("fullName") or o.get("displayName"),
            "org": o.get("organizationName"),
            "org_uuid": o.get("organizationUuid"),
            "plan": o.get("organizationType"),
            "role": o.get("organizationRole"),
            "source": p,
        })
    for uid, d in desktop_accounts().items():
        out.setdefault(uid, d)
    return out


# ---------------------------------------------------------------- settings

SETTINGS_FILE = os.path.join(HOME, ".claude", "settings.json")

KNOWN_SETTINGS = [
    {"key": "cleanupPeriodDays", "type": "number", "default": 30,
     "title": "Keep chat history for",
     "why": "How many days Claude Code keeps a conversation before deleting it. "
            "The default is 30. Raise it and old chats stop disappearing.",
     "unit": "days"},
    {"key": "remoteControlAtStartup", "type": "bool", "default": True,
     "title": "Let sessions be driven remotely at startup",
     "why": "Off means a session does not connect for remote control when it starts."},
    {"key": "autoUploadSessions", "type": "bool", "default": True,
     "title": "Upload sessions automatically",
     "why": "Off keeps your conversations on this Mac unless you share one yourself."},
    {"key": "agentPushNotifEnabled", "type": "bool", "default": False,
     "title": "Notify when an agent finishes",
     "why": "A push notification when background work completes."},
    {"key": "inputNeededNotifEnabled", "type": "bool", "default": False,
     "title": "Notify when Claude needs you",
     "why": "A push notification when a session is waiting on your answer."},
    {"key": "theme", "type": "text", "default": "dark", "title": "Theme",
     "why": "The colour theme Claude Code uses in the terminal."},
    {"key": "effortLevel", "type": "text", "default": "medium", "title": "Effort level",
     "why": "How hard Claude works by default before answering."},
    {"key": "skipDangerousModePermissionPrompt", "type": "bool", "default": False,
     "title": "Skip the dangerous-mode warning",
     "why": "On means Claude Code stops asking before entering the mode that "
            "bypasses permission prompts. Leaving this off is the safer choice.",
     "risky": True},
]


def read_settings() -> dict:
    try:
        with open(SETTINGS_FILE) as fh:
            cur = json.load(fh)
    except Exception:
        cur = {}
    rows = []
    for spec in KNOWN_SETTINGS:
        rows.append({**spec, "value": cur.get(spec["key"]),
                     "set": spec["key"] in cur})
    return {"file": SETTINGS_FILE, "settings": rows,
            "other_keys": [k for k in cur if k not in {s["key"] for s in KNOWN_SETTINGS}]}


def write_setting(key: str, value) -> dict:
    """Change one setting, keeping a copy of the file first."""
    spec = next((s for s in KNOWN_SETTINGS if s["key"] == key), None)
    if not spec:
        return {"ok": False, "msg": "not a setting this tool manages"}
    if spec["type"] == "number":
        try:
            value = int(value)
        except Exception:
            return {"ok": False, "msg": "that needs to be a whole number"}
        if not 1 <= value <= 3650:
            return {"ok": False, "msg": "pick something between 1 and 3650 days"}
    if spec["type"] == "bool":
        value = bool(value)
    try:
        with open(SETTINGS_FILE) as fh:
            cur = json.load(fh)
    except Exception:
        cur = {}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(BACKUPS, "settings-" + stamp)
    os.makedirs(bdir, exist_ok=True)
    if os.path.exists(SETTINGS_FILE):
        shutil.copy2(SETTINGS_FILE, os.path.join(bdir, "settings.json"))
    was = cur.get(key)
    cur[key] = value
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE, "w") as fh:
        json.dump(cur, fh, indent=2)
    return {"ok": True, "key": key, "was": was, "now": value, "backup": bdir,
            "msg": "changed. New Claude Code sessions pick this up; "
                   "one already running keeps the old value."}



# ---------------------------------------------------------------- usage

def scan_usage(path: str) -> dict:
    """Token counts per model per day, straight from the assistant records."""
    agg = {}
    with open(path, errors="ignore") as fh:
        for line in fh:
            if '"usage"' not in line or '"assistant"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") != "assistant":
                continue
            m = d.get("message") or {}
            u = m.get("usage") or {}
            if not u:
                continue
            day = (d.get("timestamp") or "")[:10]
            model = m.get("model") or "unknown"
            k = (day, model)
            a = agg.setdefault(k, [0, 0, 0, 0, 0, 0])
            a[0] += 1
            a[1] += u.get("input_tokens") or 0
            a[2] += u.get("output_tokens") or 0
            a[3] += u.get("cache_read_input_tokens") or 0
            a[4] += u.get("cache_creation_input_tokens") or 0
            a[5] += ((u.get("output_tokens_details") or {}).get("thinking_tokens") or 0)
    return agg


def build_usage(progress=None) -> int:
    """Incremental: only transcripts that changed since last time."""
    c = connect()
    done = 0
    for f in glob.glob(os.path.join(TRANSCRIPTS, "*", "*.jsonl")):
        cli = os.path.basename(f)[:-6]
        mt = os.path.getmtime(f)
        old = c.execute("SELECT mtime FROM usage_state WHERE cli_id=?", (cli,)).fetchone()
        if old and abs(old["mtime"] - mt) < 0.001:
            continue
        c.execute("DELETE FROM usage WHERE cli_id=?", (cli,))
        for (day, model), a in scan_usage(f).items():
            c.execute("INSERT OR REPLACE INTO usage VALUES (?,?,?,?,?,?,?,?,?)",
                      (cli, day, model, *a))
        c.execute("INSERT OR REPLACE INTO usage_state VALUES (?,?)", (cli, mt))
        done += 1
        if done % 20 == 0:
            c.commit()
            if progress:
                progress(done)
    c.commit()
    c.close()
    return done


def usage_report(days: int = 30) -> dict:
    """What this Mac has spent, by model, day, project and account."""
    build_usage()
    c = connect()
    since = (datetime.now() - __import__("datetime").timedelta(days=days)).strftime("%Y-%m-%d")
    q = lambda sql, *a: [dict(r) for r in c.execute(sql, a)]
    by_model = q("""SELECT model, SUM(requests) requests, SUM(input) input, SUM(output) output,
                    SUM(cache_read) cache_read, SUM(cache_create) cache_create, SUM(thinking) thinking
                    FROM usage WHERE day>=? GROUP BY model ORDER BY output DESC""", since)
    by_day = q("""SELECT day, model, SUM(output) output, SUM(input+cache_create) input, SUM(requests) requests
                  FROM usage WHERE day>=? GROUP BY day, model ORDER BY day""", since)
    by_project = q("""SELECT COALESCE(s.cwd, '(terminal only)') project, u.model,
                      SUM(u.output) output, SUM(u.requests) requests
                      FROM usage u LEFT JOIN sessions s ON s.cli_id=u.cli_id
                      WHERE u.day>=? GROUP BY project, u.model ORDER BY output DESC LIMIT 40""", since)
    by_instance = q("""SELECT COALESCE(s.instance,'Terminal only') app, u.model,
                       SUM(u.output) output, SUM(u.requests) requests
                       FROM usage u LEFT JOIN sessions s ON s.cli_id=u.cli_id
                       WHERE u.day>=? GROUP BY app, u.model ORDER BY app, output DESC""", since)
    by_account = q("""SELECT COALESCE(s.account,'Terminal only') account,
                      COALESCE(s.instance,'') instance, u.model,
                      SUM(u.output) output, SUM(u.input+u.cache_create) input, SUM(u.requests) requests
                      FROM usage u LEFT JOIN sessions s ON s.cli_id=u.cli_id
                      WHERE u.day>=? GROUP BY account, instance, u.model ORDER BY output DESC""", since)
    top_chats = q("""SELECT u.cli_id, COALESCE(s.title, '(terminal session)') title, s.reg_path,
                     COALESCE(s.instance,'Terminal only') app,
                     u.model, SUM(u.output) output, SUM(u.requests) requests, MAX(u.day) last_day
                     FROM usage u LEFT JOIN sessions s ON s.cli_id=u.cli_id
                     WHERE u.day>=? GROUP BY u.cli_id, u.model ORDER BY output DESC LIMIT 15""", since)
    recent_chats = q("""SELECT u.cli_id, COALESCE(s.title, '(terminal session)') title, s.reg_path,
                     COALESCE(s.instance,'Terminal only') app,
                     u.model, SUM(u.output) output, SUM(u.requests) requests, MAX(u.day) last_day
                     FROM usage u LEFT JOIN sessions s ON s.cli_id=u.cli_id
                     WHERE u.day>=? GROUP BY u.cli_id, u.model ORDER BY last_day DESC, output DESC LIMIT 15""", since)
    first = c.execute("SELECT MIN(day) d FROM usage").fetchone()["d"]
    c.close()
    labels, auto = load_labels(), auto_labels()
    for r in by_account:
        r["label"] = (labels.get(r["account"]) or auto.get(r["account"])
                      or ("Terminal only" if r["account"] == "Terminal only" else r["account"][:8]))
    return {"days": days, "since": since, "first_day": first, "by_model": by_model,
            "by_day": by_day, "by_project": by_project, "by_account": by_account,
            "by_instance": by_instance, "top_chats": top_chats, "recent_chats": recent_chats,
            "machine": os.uname().nodename}


def usage_csv(days: int = 30) -> str:
    r = usage_report(days)
    out = ["machine,day,model,requests,output_tokens,input_tokens"]
    for x in r["by_day"]:
        out.append(f"{r['machine']},{x['day']},{x['model']},{x['requests']},{x['output']},{x['input']}")
    return "\n".join(out)



def models_used(cli_ids: list) -> dict:
    """What each chat actually ran on, with tokens, not what the app was set to."""
    if not cli_ids:
        return {}
    build_usage()
    c = connect()
    out = {}
    for i in range(0, len(cli_ids), 500):
        chunk = cli_ids[i:i + 500]
        for r in c.execute(
            "SELECT cli_id, model, SUM(output) output, SUM(input+cache_create) input, "
            "SUM(requests) requests, MIN(day) first, MAX(day) last FROM usage "
            "WHERE cli_id IN (%s) AND model NOT LIKE '<%%' GROUP BY cli_id, model "
            "HAVING SUM(output) > 0 ORDER BY output DESC"
            % ",".join("?" * len(chunk)), chunk):
            out.setdefault(r["cli_id"], []).append(
                {"model": r["model"], "output": r["output"], "input": r["input"],
                 "requests": r["requests"], "first": r["first"], "last": r["last"]})
    c.close()
    return out




# ---------------------------------------------------------------- resets per account

RESETS = os.path.join(STATE, "resets.json")


def load_resets() -> dict:
    try:
        with open(RESETS) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_reset(account: str, weekday: int, hhmm: str) -> None:
    d = load_resets()
    d[account] = {"weekday": int(weekday), "hhmm": hhmm}
    os.makedirs(STATE, exist_ok=True)
    with open(RESETS, "w") as fh:
        json.dump(d, fh, indent=2)


def reset_for(account: str) -> tuple:
    """Each Claude subscription has its own weekly reset moment. Default Thu 22:30."""
    r = load_resets().get(account or "", {})
    return int(r.get("weekday", 3)), r.get("hhmm", "22:30")


def account_scope(account: str) -> tuple:
    """(uuids for that email, whether terminal sessions belong to it here)."""
    if not account:
        return None, True
    uids = {u for u, d in account_directory().items() if d["email"] == account}
    uids |= {u for u, l in load_labels().items() if l == account}
    return uids, signed_in_email() == account


def known_accounts() -> list:
    who = account_directory()
    emails = {d["email"] for d in who.values()} | {l for l in load_labels().values() if "@" in l}
    return sorted(emails)



def scope_options() -> list:
    """Things you can export for: each Claude app (with the account it is signed
    into), plus bare accounts. An app with no known email still gets a stable key."""
    who = account_directory()
    labels = load_labels()
    out, seen = [], set()
    for b in buckets():
        uid = b["account"]
        email = (who.get(uid) or {}).get("email") or (labels.get(uid) if "@" in labels.get(uid, "") else None)
        key = email or uid
        if (b["instance"], key) in seen:
            continue
        seen.add((b["instance"], key))
        out.append({"value": f"inst:{b['instance']}|{uid}", "kind": "instance",
                    "instance": b["instance"], "uid": uid, "account": key,
                    "label": f"{b['instance']}  —  {email or 'account ' + uid[:8]}",
                    "chats": b["count"]})
    for email in known_accounts():
        out.append({"value": f"acct:{email}", "kind": "account", "account": email,
                    "label": f"{email}  (every app on this Mac)"})
    return out


def resolve_scope(sel: str) -> dict:
    """sel = '' | 'acct:<email>' | 'inst:<app>|<uid>'  ->  what to count and how to label it."""
    if not sel:
        return {"uids": None, "terminal_ok": True, "account": "", "instance": "", "label": "all accounts"}
    if sel.startswith("acct:"):
        email = sel[5:]
        uids, term = account_scope(email)
        return {"uids": uids, "terminal_ok": term, "account": email, "instance": "", "label": email}
    if sel.startswith("inst:"):
        inst, uid = sel[5:].split("|", 1)
        who = account_directory()
        email = (who.get(uid) or {}).get("email")
        key = email or uid
        return {"uids": {uid}, "terminal_ok": bool(email) and signed_in_email() == email,
                "account": key, "instance": inst,
                "label": f"{inst} ({email or 'account ' + uid[:8]})"}
    return resolve_scope("")


def scope_public(sc: dict) -> dict:
    return {k: (sorted(v) if isinstance(v, set) else v) for k, v in sc.items()}


# ---------------------------------------------------------------- limit windows

def _last_reset(weekday: int, hhmm: str):
    """Most recent occurrence of <weekday> at <hhmm>, local time. Mon=0."""
    import datetime as dt
    now = dt.datetime.now()
    h, m = (int(x) for x in hhmm.split(":"))
    cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
    back = (now.weekday() - weekday) % 7
    cand -= dt.timedelta(days=back)
    if cand > now:
        cand -= dt.timedelta(days=7)
    return cand


def window_report(reset_weekday: int = 3, reset_hhmm: str = "22:30", account: str = "") -> dict:
    """Usage on this Mac inside the same windows the Claude app meters:
    the rolling 5-hour window and the weekly window since the last reset.
    Scans transcripts directly because the daily table is too coarse for 5h."""
    import datetime as dt
    now = dt.datetime.now().astimezone()
    starts = {
        "5h": now - dt.timedelta(hours=5),
        "week": _last_reset(reset_weekday, reset_hhmm).astimezone(),
    }
    acc = {k: {"models": {}, "apps": {}, "chats": {}} for k in starts}
    uids, term_ok = account_scope(account)
    for when, model, app, cli, title, out, inp in _events_since(starts["week"], uids, term_ok):
        for k, start in starts.items():
            if when < start:
                continue
            a = acc[k]
            mrow = a["models"].setdefault(model, {"replies": 0, "output": 0, "input": 0})
            mrow["replies"] += 1; mrow["output"] += out; mrow["input"] += inp
            arow = a["apps"].setdefault(app, {}).setdefault(model, {"replies": 0, "output": 0})
            arow["replies"] += 1; arow["output"] += out
            crow = a["chats"].setdefault(cli, {"title": title, "app": app, "models": {}})
            cm = crow["models"].setdefault(model, {"replies": 0, "output": 0})
            cm["replies"] += 1; cm["output"] += out
    result = {"now": now.isoformat(timespec="minutes"),
              "reset": {"weekday": reset_weekday, "hhmm": reset_hhmm},
              "windows": {}}
    for k in starts:
        a = acc[k]
        chats = sorted(a["chats"].values(),
                       key=lambda x: -sum(m["output"] for m in x["models"].values()))[:12]
        result["windows"][k] = {
            "since": starts[k].isoformat(timespec="minutes"),
            "models": a["models"], "apps": a["apps"], "chats": chats,
        }
    return result



def _events_since(start, account_filter=None, terminal_ok=True) -> list:
    """(when, model, app, cli_id, title, output, input) for every reply after start."""
    import datetime as dt
    c = connect()
    reg = {r["cli_id"]: (r["instance"], r["title"], r["account"]) for r in
           c.execute("SELECT cli_id, instance, title, account FROM sessions WHERE cli_id IS NOT NULL")}
    c.close()
    ev = []
    for f in glob.glob(os.path.join(TRANSCRIPTS, "*", "*.jsonl")):
        if dt.datetime.fromtimestamp(os.path.getmtime(f)).astimezone() < start:
            continue
        cli = os.path.basename(f)[:-6]
        app, title, acct = reg.get(cli, ("Terminal only", "(terminal session)", None))
        if account_filter is not None:
            # keep chats registered to that account; terminal sessions count only
            # when this Mac's Claude Code login is that same account
            if acct is None:
                if not terminal_ok:
                    continue
            elif acct not in account_filter:
                continue
        with open(f, errors="ignore") as fh:
            for line in fh:
                if '"usage"' not in line or '"assistant"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "assistant" or not d.get("timestamp"):
                    continue
                try:
                    when = dt.datetime.fromisoformat(
                        d["timestamp"].replace("Z", "+00:00")).astimezone()
                except Exception:
                    continue
                if when < start:
                    continue
                mm = d.get("message") or {}
                u = mm.get("usage") or {}
                model = mm.get("model") or "unknown"
                if model.startswith("<"):
                    continue
                ev.append((when, model, app, cli, title, u.get("output_tokens") or 0,
                           (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)))
    return ev


def weekly_history(weeks: int = 4, reset_weekday: int = 3, reset_hhmm: str = "22:30",
                   account_filter=None, terminal_ok=True) -> dict:
    """Usage bucketed into the app's weekly windows, with a per-day split inside each."""
    import datetime as dt
    last = _last_reset(reset_weekday, reset_hhmm).astimezone()
    starts = [last - dt.timedelta(days=7 * i) for i in range(weeks)][::-1]
    ev = _events_since(starts[0], account_filter, terminal_ok)
    out = []
    for i, st in enumerate(starts):
        en = starts[i + 1] if i + 1 < len(starts) else dt.datetime.now().astimezone() + dt.timedelta(days=7)
        models, apps, days = {}, {}, {}
        for when, model, app, cli, title, o, inp in ev:
            if not (st <= when < en):
                continue
            m = models.setdefault(model, {"replies": 0, "output": 0, "input": 0})
            m["replies"] += 1; m["output"] += o; m["input"] += inp
            a = apps.setdefault(app, {}).setdefault(model, {"replies": 0, "output": 0})
            a["replies"] += 1; a["output"] += o
            dd = days.setdefault(when.strftime("%Y-%m-%d"), {}).setdefault(model, 0)
            days[when.strftime("%Y-%m-%d")][model] = dd + o
        out.append({"start": st.isoformat(timespec="minutes"),
                    "end": min(en, dt.datetime.now().astimezone()).isoformat(timespec="minutes"),
                    "current": i == len(starts) - 1,
                    "models": models, "apps": apps,
                    "days": [{"day": k, "models": v} for k, v in sorted(days.items())]})
    return {"reset": {"weekday": reset_weekday, "hhmm": reset_hhmm}, "weeks": out}


def local_ips() -> list:
    r = subprocess.run(["ifconfig"], capture_output=True, text=True)
    return sorted({m for m in re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", r.stdout)
                   if not m.startswith("127.")})


def signed_in_email() -> str | None:
    try:
        with open(os.path.join(HOME, ".claude.json")) as fh:
            return ((json.load(fh).get("oauthAccount") or {}).get("emailAddress"))
    except Exception:
        return None


def usage_export(reset_weekday: int = 3, reset_hhmm: str = "22:30",
                 name: str = "", account: str = "") -> dict:
    """One file per Mac, scoped to ONE Claude account so everyone's files line up.
    Only chats that belong to that account are counted. Terminal sessions have no
    account on them, so they count only when this Mac is signed into that account."""
    who = account_directory()
    by_email = {}
    for uid, d in who.items():
        by_email.setdefault(d["email"], set()).add(uid)
    for uid, lbl in load_labels().items():
        if "@" in lbl:
            by_email.setdefault(lbl, set()).add(uid)
    me = signed_in_email()
    account = account or me or ""
    uids = by_email.get(account, set())
    terminal_ok = bool(me) and me == account
    hist = weekly_history(8, reset_weekday, reset_hhmm,
                          account_filter=uids if account else None, terminal_ok=terminal_ok)
    return {
        "format": "claude-powertools-usage/2",
        "name": name.strip() or os.environ.get("USER") or "",
        "machine": os.uname().nodename,
        "ips": local_ips(),
        "account": account,
        "signed_in_here": me,
        "terminal_counted": terminal_ok,
        "exported": datetime.now().astimezone().isoformat(timespec="minutes"),
        "counts_only": "Claude Code sessions (terminal and the Code tab of the Claude app). "
                       "Claude chat on claude.ai or in the app is not stored locally and is not counted.",
        "history": hist,
    }


def usage_combine(files: list, account: str = "", expected: int = 0) -> dict:
    """Merge exports from several Macs. Files for a different Claude account are
    set aside and reported, so one person's own subscription never pollutes the
    shared-account picture."""
    people, rejected = [], []
    for f in files:
        try:
            with open(f) as fh:
                d = json.load(fh)
        except Exception:
            rejected.append({"file": f, "why": "not readable as JSON"})
            continue
        if not str(d.get("format", "")).startswith("claude-powertools-usage"):
            rejected.append({"file": f, "why": "not a PowerTools usage export"})
            continue
        d["_file"] = f
        people.append(d)
    if not people:
        return {"ok": False, "msg": "no usage exports found in those files"}
    if not account:
        counts = {}
        for p in people:
            a = p.get("account") or ""
            counts[a] = counts.get(a, 0) + 1
        account = max(counts, key=counts.get)
    same = [p for p in people if (p.get("account") or "") == account]
    other = [p for p in people if (p.get("account") or "") != account]
    for p in other:
        rejected.append({"file": p["_file"], "who": p.get("name") or p.get("machine"),
                         "why": f"exported for {p.get('account') or 'no account'}, not {account}"})
    weeks = {}
    resets = {}
    for p in same:
        r = p.get("reset") or {}
        resets.setdefault(f"{r.get('weekday','?')} {r.get('hhmm','?')}", []).append(p.get("name") or p.get("machine"))
    for p in same:
        label = p.get("name") or p.get("machine") or "?"
        for w in p["history"]["weeks"]:
            key = w["start"][:10]
            b = weeks.setdefault(key, {"start": w["start"], "current": w["current"], "people": {}})
            b["people"][label] = {"models": w["models"], "machine": p.get("machine"),
                                  "instance": p.get("instance") or "",
                                  "ips": p.get("ips") or [], "terminal": p.get("terminal_counted")}
    result = []
    for key in sorted(weeks):
        b = weeks[key]
        rows = []
        for who, info in b["people"].items():
            ms = info["models"]
            rows.append({"who": who, "machine": info["machine"], "ips": info["ips"],
                         "instance": info.get("instance", ""),
                         "terminal": info["terminal"], "per_model": ms,
                         "fable": sum(v["output"] for m, v in ms.items() if "fable" in m),
                         "total": sum(v["output"] for v in ms.values())})
        tf = sum(r["fable"] for r in rows) or 1
        tt = sum(r["total"] for r in rows) or 1
        for r in rows:
            r["fable_share"] = round(r["fable"] / tf * 100)
            r["total_share"] = round(r["total"] / tt * 100)
        rows.sort(key=lambda r: -r["fable"])
        result.append({"start": b["start"], "current": b["current"], "rows": rows})
    missing = max(0, expected - len(same)) if expected else 0
    return {"ok": True, "account": account, "people": len(same), "rejected": rejected,
            "expected": expected, "missing": missing,
            "reset_mismatch": resets if len(resets) > 1 else None, "weeks": result}



def window_report_scoped(reset_weekday, reset_hhmm, sc: dict) -> dict:
    import datetime as dt
    now = dt.datetime.now().astimezone()
    starts = {"5h": now - dt.timedelta(hours=5),
              "week": _last_reset(reset_weekday, reset_hhmm).astimezone()}
    acc = {k: {"models": {}, "apps": {}, "chats": {}} for k in starts}
    for when, model, app, cli, title, out, inp in _events_since(starts["week"], sc["uids"], sc["terminal_ok"]):
        for k, start in starts.items():
            if when < start:
                continue
            a = acc[k]
            m = a["models"].setdefault(model, {"replies": 0, "output": 0, "input": 0})
            m["replies"] += 1; m["output"] += out; m["input"] += inp
            ar = a["apps"].setdefault(app, {}).setdefault(model, {"replies": 0, "output": 0})
            ar["replies"] += 1; ar["output"] += out
            cr = a["chats"].setdefault(cli, {"title": title, "app": app, "models": {}})
            cm = cr["models"].setdefault(model, {"replies": 0, "output": 0})
            cm["replies"] += 1; cm["output"] += out
    res = {"now": now.isoformat(timespec="minutes"),
           "reset": {"weekday": reset_weekday, "hhmm": reset_hhmm}, "windows": {}}
    for k in starts:
        a = acc[k]
        chats = sorted(a["chats"].values(), key=lambda x: -sum(v["output"] for v in x["models"].values()))[:12]
        res["windows"][k] = {"since": starts[k].isoformat(timespec="minutes"),
                             "models": a["models"], "apps": a["apps"], "chats": chats}
    return res


def usage_export_scoped(reset_weekday, reset_hhmm, name: str, sc: dict) -> dict:
    hist = weekly_history(8, reset_weekday, reset_hhmm, sc["uids"], sc["terminal_ok"])
    return {
        "format": "claude-powertools-usage/2",
        "name": name.strip() or os.environ.get("USER") or "",
        "machine": os.uname().nodename,
        "ips": local_ips(),
        "account": sc["account"],
        "instance": sc["instance"],
        "signed_in_here": signed_in_email(),
        "terminal_counted": sc["terminal_ok"],
        "reset": {"weekday": reset_weekday, "hhmm": reset_hhmm},
        "exported": datetime.now().astimezone().isoformat(timespec="minutes"),
        "counts_only": "Claude Code sessions (terminal and the Code tab of the Claude app). "
                       "Claude chat on claude.ai or in the app is not stored locally and is not counted.",
        "history": hist,
    }



def usage_between(scope_sel: str, start_iso: str, end_iso: str) -> dict:
    """Tokens this Mac spent between two moments, split Fable vs everything.
    Used to calibrate the account meter: read the % twice while only this Mac
    is using the account, and tokens-per-percent falls out."""
    import datetime as dt
    sc = resolve_scope(scope_sel)
    a = dt.datetime.fromisoformat(start_iso).astimezone()
    b = dt.datetime.fromisoformat(end_iso).astimezone()
    if b < a:
        a, b = b, a
    tot = {"fable": {"output": 0, "input": 0, "replies": 0}, "all": {"output": 0, "input": 0, "replies": 0}}
    for when, model, app, cli, title, out, inp in _events_since(a, sc["uids"], sc["terminal_ok"]):
        if when > b:
            continue
        for k in (("fable",) if "fable" in model else ()) + ("all",):
            tot[k]["output"] += out; tot[k]["input"] += inp; tot[k]["replies"] += 1
    return {"from": a.isoformat(timespec="minutes"), "to": b.isoformat(timespec="minutes"), **tot}


# ---------------------------------------------------------------- move


def plan_move(reg_paths: list[str], dest_key: str, mode: str) -> dict:
    dests = {b["key"]: b for b in buckets()}
    if dest_key not in dests:
        raise ValueError(f"unknown destination {dest_key}")
    dest = dests[dest_key]
    steps, skipped = [], []
    for p in reg_paths:
        if not os.path.exists(p):
            skipped.append({"path": p, "why": "source is gone"})
            continue
        if os.path.dirname(p) == dest["path"]:
            skipped.append({"path": p, "why": "already in destination"})
            continue
        target = os.path.join(dest["path"], os.path.basename(p))
        title = "(untitled)"
        try:
            with open(p) as fh:
                title = json.load(fh).get("title") or title
        except Exception:
            pass
        steps.append(
            {
                "src": p,
                "dst": target,
                "title": title,
                "overwrite": os.path.exists(target),
            }
        )
    return {"dest": dest, "mode": mode, "steps": steps, "skipped": skipped}


def apply_move(plan: dict) -> dict:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(BACKUPS, stamp)
    os.makedirs(bdir, exist_ok=True)
    journal = {"stamp": stamp, "mode": plan["mode"], "backup": bdir, "actions": []}
    done = 0
    for s in plan["steps"]:
        # back up both sides before touching anything
        shutil.copy2(s["src"], os.path.join(bdir, "src__" + os.path.basename(s["src"])))
        if s["overwrite"]:
            shutil.copy2(
                s["dst"], os.path.join(bdir, "dst__" + os.path.basename(s["dst"]))
            )
        shutil.copy2(s["src"], s["dst"])
        if plan["mode"] == "move":
            os.remove(s["src"])
        journal["actions"].append(
            {
                "src": s["src"],
                "dst": s["dst"],
                "overwrote": s["overwrite"],
                "removed_src": plan["mode"] == "move",
            }
        )
        done += 1
    os.makedirs(STATE, exist_ok=True)
    with open(UNDO, "w") as fh:
        json.dump(journal, fh, indent=2)
    return {"moved": done, "backup": bdir, "stamp": stamp}


def undo_last() -> dict:
    if not os.path.exists(UNDO):
        return {"ok": False, "msg": "nothing to undo"}
    with open(UNDO) as fh:
        j = json.load(fh)
    restored = 0
    for r in reversed(j.get("restores") or []):
        if r["backup"]:
            if r["dir"]:
                shutil.rmtree(r["path"], ignore_errors=True)
                shutil.copytree(r["backup"], r["path"])
            else:
                shutil.copy2(r["backup"], r["path"])
        elif os.path.exists(r["path"]):
            shutil.rmtree(r["path"], ignore_errors=True) if r["dir"] else os.remove(r["path"])
        restored += 1
    for a in reversed(j["actions"]):
        if a["overwrote"]:
            b = os.path.join(j["backup"], "dst__" + os.path.basename(a["dst"]))
            if os.path.exists(b):
                shutil.copy2(b, a["dst"])
        elif os.path.exists(a["dst"]):
            os.remove(a["dst"])
        if a["removed_src"]:
            b = os.path.join(j["backup"], "src__" + os.path.basename(a["src"]))
            if os.path.exists(b):
                shutil.copy2(b, a["src"])
        restored += 1
    os.rename(UNDO, UNDO + "." + j["stamp"] + ".done")
    return {"ok": True, "restored": restored, "msg": f"reversed {restored} change(s)"}


# ---------------------------------------------------------------- web

UI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")
EMBEDDED_UI = ""  # filled in by `powertools bundle` to make a single-file build


def page() -> str:
    """Prefer ui.html next to the script (dev); fall back to the embedded copy
    so a bundled single file works on its own."""
    if os.path.exists(UI):
        with open(UI, encoding="utf-8") as fh:
            return fh.read()
    if EMBEDDED_UI:
        return EMBEDDED_UI
    raise FileNotFoundError("ui.html not found and no embedded UI in this build")


def bundle(out: str) -> str:
    """Write a single self-contained executable: this script with ui.html baked in."""
    src = os.path.abspath(__file__)
    with open(src, encoding="utf-8") as fh:
        code = fh.read()
    with open(UI, encoding="utf-8") as fh:
        html_src = fh.read()
    marker = 'EMBEDDED_UI = ""  # filled in by `powertools bundle`'
    line = [l for l in code.splitlines() if l.startswith('EMBEDDED_UI = ""')][0]
    code = code.replace(line, "EMBEDDED_UI = " + repr(html_src), 1)
    out = os.path.abspath(os.path.expanduser(out))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(code)
    os.chmod(out, 0o755)
    return out


class Handler(BaseHTTPRequestHandler):
    token = ""

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        raw = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _ok(self, obj):
        self._send(200, json.dumps(obj))

    def _auth(self, qs) -> bool:
        return secrets.compare_digest(qs.get("t", [""])[0], self.token)

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/":
            return self._send(200, page(), "text/html; charset=utf-8")
        if not self._auth(qs):
            return self._send(403, json.dumps({"error": "bad token"}))
        if u.path == "/api/tree":
            labels = load_labels()
            auto = auto_labels()
            who = account_directory()
            bs = buckets()
            for b in bs:
                b["label"] = labels.get(b["account"]) or auto.get(b["account"])
                b["who"] = who.get(b["account"])
            return self._ok({"buckets": bs})
        if u.path == "/api/usage":
            return self._ok(usage_report(int(qs.get("days", ["30"])[0])))
        if u.path == "/api/usage_between":
            return self._ok(usage_between(qs.get("scope", [""])[0], qs.get("from", [""])[0], qs.get("to", [""])[0]))
        if u.path == "/api/usage_windows":
            sc = resolve_scope(qs.get("scope", [""])[0])
            wd, at = reset_for(sc["account"])
            r = window_report_scoped(wd, at, sc)
            r["scope"] = scope_public(sc)
            return self._ok(r)
        if u.path == "/api/usage_history":
            sc = resolve_scope(qs.get("scope", [""])[0])
            wd, at = reset_for(sc["account"])
            h = weekly_history(int(qs.get("weeks", ["8"])[0]), wd, at, sc["uids"], sc["terminal_ok"])
            h["scope"] = scope_public(sc)
            return self._ok(h)
        if u.path == "/api/resets":
            return self._ok({"options": scope_options(), "signed_in": signed_in_email(),
                             "resets": load_resets()})
        if u.path == "/api/usage_export":
            sc = resolve_scope(qs.get("scope", [""])[0])
            wd, at = reset_for(sc["account"])
            raw = json.dumps(usage_export_scoped(wd, at, qs.get("name", [""])[0], sc),
                             indent=1).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Disposition",
                             f'attachment; filename="usage-{os.uname().nodename}.json"')
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            return self.wfile.write(raw)
        if u.path == "/api/pick_many":
            r = subprocess.run(["osascript", "-e",
                'set fs to choose file with prompt "Pick everyone\'s usage exports" '
                'of type {"public.json", "public.plain-text"} with multiple selections allowed',
                "-e", "set out to \"\"",
                "-e", "repeat with f in fs",
                "-e", "set out to out & POSIX path of f & linefeed",
                "-e", "end repeat", "-e", "return out"],
                capture_output=True, text=True)
            paths = [p for p in (r.stdout or "").split("\n") if p.strip()]
            return self._ok({"ok": r.returncode == 0, "paths": paths})
        if u.path == "/api/usage.csv":
            raw = usage_csv(int(qs.get("days", ["30"])[0])).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition",
                             f'attachment; filename="claude-usage-{os.uname().nodename}.csv"')
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            return self.wfile.write(raw)
        if u.path == "/api/settings":
            return self._ok(read_settings())
        if u.path == "/api/sessions":
            labels = load_labels()
            auto = auto_labels()
            hits = query(
                scope=qs.get("scope", [""])[0] or None,
                q=qs.get("q", [""])[0] or None,
                fts=qs.get("fts", ["0"])[0] == "1",
                archived=qs.get("archived", ["0"])[0] == "1",
                limit=100000,
            )
            if qs.get("idsonly", ["0"])[0] == "1":
                return self._ok({"ids": [r["reg_path"] for r in hits]})
            rows = hits[: int(qs.get("limit", ["400"])[0])]
            snips = {}
            if qs.get("fts", ["0"])[0] == "1" and qs.get("q", [""])[0]:
                snips = search_snippets(qs["q"][0], [r["cli_id"] for r in rows])
            used = models_used([r["cli_id"] for r in rows if r["cli_id"]])
            for r in rows:
                r["label"] = labels.get(r["account"]) or auto.get(r["account"])
                r["snippets"] = snips.get(r["cli_id"], [])
                r["used"] = used.get(r["cli_id"], [])
            c = connect()
            total = c.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"]
            c.close()
            return self._ok({"rows": rows, "matched": len(hits), "total": total,
                             "fts_indexed": fts_ready()})
        if u.path == "/api/memory":
            return self._ok(memories())
        if u.path == "/api/mcp":
            return self._ok({
                "servers": mcp_servers(),
                "instances": [i for i, _ in instance_roots()],
            })
        if u.path == "/api/pick":
            return self._ok(pick_path(qs.get("kind", ["file"])[0]))
        if u.path == "/api/backup_layout":
            return self._ok(backup_layout(qs.get("file", [""])[0]))
        if u.path == "/api/backup_info":
            return self._ok({
                "here": backup_plan(True),
                "light": backup_plan(False),
                "found": find_backups(),
                "buckets": [b["key"] for b in buckets()],
                "default_out": os.path.join(HOME, "Desktop", "claude-powertools-backup.zip"),
            })
        if u.path == "/api/mcp_snippet":
            return self._ok(mcp_snippet(qs.get("id", [""])[0],
                                        qs.get("reveal", ["0"])[0] == "1"))
        if u.path == "/api/mcp_reveal":
            return self._ok(reveal_secret(qs.get("id", [""])[0], qs.get("key", [""])[0]))
        if u.path == "/api/instances":
            tr = glob.glob(os.path.join(TRANSCRIPTS, "*", "*.jsonl"))
            return self._ok({
                "instances": instances(),
                "transcripts": {
                    "path": TRANSCRIPTS,
                    "files": len(tr),
                    "bytes": sum(os.path.getsize(f) for f in tr),
                },
            })
        if u.path == "/api/export":
            rows = query(limit=100000)
            row = next((r for r in rows if r["cli_id"] == qs.get("id", [""])[0]), None)
            if not row:
                return self._send(404, json.dumps({"error": "unknown chat"}))
            tp = row.get("transcript") or ""
            msgs = read_transcript(tp)
            text = as_markdown(row, msgs, "", transcript_files(tp))
            fn = safe_filename(row.get("title")) + ".md"
            raw = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{fn}"')
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            return self.wfile.write(raw)
        if u.path == "/api/image":
            p = transcript_for(qs.get("id", [""])[0])
            raw, media = nth_image(p, int(qs.get("n", ["0"])[0])) if p else (None, None)
            if not raw:
                return self._send(404, json.dumps({"error": "no image"}))
            self.send_response(200)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            return self.wfile.write(raw)
        if u.path == "/api/session":
            cli = qs.get("id", [""])[0]
            p = transcript_for(cli)
            return self._ok({
                "messages": read_transcript(p) if p else [],
                "files": transcript_files(p) if p else [],
                "used": models_used([cli]).get(cli, []),
            })
        return self._send(404, json.dumps({"error": "no"}))

    def do_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not self._auth(qs):
            return self._send(403, json.dumps({"error": "bad token"}))
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if u.path == "/api/reindex":
            return self._ok(build_index(fts=qs.get("fts", ["0"])[0] == "1"))
        if u.path == "/api/undo":
            return self._ok(undo_last())
        if u.path == "/api/label":
            labels = load_labels()
            labels[payload["uuid"]] = payload["label"]
            save_labels(labels)
            return self._ok({"ok": True})
        if u.path == "/api/reset":
            save_reset(payload.get("account", ""), payload.get("weekday", 3), payload.get("hhmm", "22:30"))
            return self._ok({"ok": True})
        if u.path == "/api/usage_combine":
            return self._ok(usage_combine(payload.get("files") or [], payload.get("account") or "",
                                          int(payload.get("expected") or 0)))
        if u.path == "/api/usage_accounts":
            who = account_directory()
            emails = sorted({d["email"] for d in who.values()} |
                            {l for l in load_labels().values() if "@" in l})
            return self._ok({"emails": emails, "signed_in": signed_in_email()})
        if u.path == "/api/setting":
            return self._ok(write_setting(payload.get("key", ""), payload.get("value")))
        if u.path == "/api/backup":
            r = make_backup(payload.get("out") or
                            os.path.join(HOME, "Desktop", "claude-powertools-backup.zip"),
                            bool(payload.get("transcripts")))
            return self._ok(r)
        if u.path == "/api/restore":
            return self._ok(restore_backup(payload.get("file", ""),
                                           payload.get("what") or [],
                                           dry=payload.get("dry", True),
                                           into=payload.get("into") or None))
        if u.path == "/api/mcp_copy":
            return self._ok(copy_mcp(payload.get("id", ""), payload.get("to", ""),
                                     payload.get("dry", True)))
        if u.path == "/api/instance":
            try:
                plan = instance_plan(payload.get("name", ""))
            except ValueError as e:
                return self._ok({"error": str(e)})
            if payload.get("dry", True):
                return self._ok(plan)
            return self._ok(create_instance(plan))
        if u.path == "/api/export_many":
            rows = query(limit=100000)
            want = set(payload.get("ids") or [])
            picked = [r for r in rows if r["reg_path"] in want]
            out = os.path.join(
                HOME, "Downloads",
                "powertools-export-" + datetime.now().strftime("%Y%m%d-%H%M%S"),
            )
            return self._ok(export_many(picked, out))
        if u.path == "/api/move":
            plan = plan_move(payload["ids"], payload["dest"], payload["mode"])
            if payload.get("dry"):
                return self._ok(
                    {
                        "steps": [
                            {"title": s["title"], "overwrite": s["overwrite"]}
                            for s in plan["steps"]
                        ],
                        "skipped": plan["skipped"],
                    }
                )
            res = apply_move(plan)
            build_index()
            return self._ok(res)
        return self._send(404, json.dumps({"error": "no"}))



def port_busy(port: int) -> bool:
    import socket

    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def open_ui(port: int) -> str:
    """What the double-clickable app runs: reuse the server if it is already up,
    otherwise start one in the background, then open the browser."""
    if port_busy(port):
        try:
            with open(URLFILE) as fh:
                url = fh.read().strip()
            if url:
                webbrowser.open(url)
                return "opened the Claude PowerTools already running on port %d" % port
        except Exception:
            pass
        return "something else is using port %d" % port
    log = os.path.join(STATE, "server.log")
    os.makedirs(STATE, exist_ok=True)
    with open(log, "ab") as fh:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "serve",
             "--port", str(port), "--no-open"],
            stdout=fh, stderr=fh, start_new_session=True,
        )
    for _ in range(60):
        if port_busy(port) and os.path.exists(URLFILE):
            time.sleep(0.3)
            with open(URLFILE) as fh:
                url = fh.read().strip()
            webbrowser.open(url)
            return "Claude PowerTools is running at " + url
        time.sleep(0.25)
    return "server did not come up; see " + log


def make_app(dest: str) -> str:
    """Build a double-clickable .app that launches Claude PowerTools. No admin needed."""
    dest = os.path.abspath(os.path.expanduser(dest))
    macos = os.path.join(dest, "Contents", "MacOS")
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(macos)
    exe = os.path.join(macos, "powertools")
    with open(exe, "w") as fh:
        # env, not sys.executable: the app keeps working even if the python
        # that ran the installer is later removed or upgraded
        fh.write(
            "#!/bin/sh\nexec /usr/bin/env python3 %s open\n"
            % shlex.quote(os.path.abspath(__file__))
        )
    os.chmod(exe, 0o755)
    with open(os.path.join(dest, "Contents", "Info.plist"), "w") as fh:
        fh.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>'
            "<key>CFBundleName</key><string>Claude PowerTools</string>"
            "<key>CFBundleDisplayName</key><string>Claude PowerTools</string>"
            "<key>CFBundleIdentifier</key><string>local.claude-powertools</string>"
            "<key>CFBundleExecutable</key><string>Claude PowerTools</string>"
            "<key>CFBundlePackageType</key><string>APPL</string>"
            "<key>CFBundleShortVersionString</key><string>1.0</string>"
            "<key>LSUIElement</key><true/>"
            "<key>NSHighResolutionCapable</key><true/>"
            "</dict></plist>\n"
        )
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", dest],
                   capture_output=True)
    return dest


def serve(port: int, open_browser: bool = True):
    print("indexing…", flush=True)
    st = build_index()
    print(f"  {st['total']} chats ({st['updated']} updated)")
    os.makedirs(STATE, exist_ok=True)
    tokfile = os.path.join(STATE, "token")
    try:
        with open(tokfile) as fh:
            Handler.token = fh.read().strip()
        assert len(Handler.token) >= 16
    except Exception:
        Handler.token = secrets.token_urlsafe(16)
        with open(tokfile, "w") as fh:
            fh.write(Handler.token)
        os.chmod(tokfile, 0o600)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/?t={Handler.token}"
    os.makedirs(STATE, exist_ok=True)
    with open(URLFILE, "w") as fh:
        fh.write(url)
    print(f"\nClaude PowerTools → {url}\n(localhost only, token required, Ctrl+C to stop)\n")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


# ---------------------------------------------------------------- cli


def fmt_rows(rows: list[dict]) -> None:
    labels = load_labels()
    auto = auto_labels()
    print(f"{'DATE':<11} {'TITLE':<46} {'ACCOUNT':<24} {'MSGS':>6}  MODEL")
    for r in rows:
        d = (
            datetime.fromtimestamp(r["activity"] / 1000).strftime("%Y-%m-%d")
            if r["activity"]
            else "—"
        )
        acct = labels.get(r["account"]) or auto.get(r["account"]) or r["account"][:8]
        print(
            f"{d:<11} {(r['title'] or '')[:44]:<46} {acct[:22]:<24} "
            f"{r['msgs'] or 0:>6}  {(r['model'] or '').replace('claude-','')}"
        )


def main():
    ap = argparse.ArgumentParser(prog="powertools", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="browse and move chats in a local web UI")
    s.add_argument("--port", type=int, default=7788)
    s.add_argument("--no-open", action="store_true")

    o = sub.add_parser("open", help="open the UI, starting the server if needed")
    o.add_argument("--port", type=int, default=7788)

    ma = sub.add_parser("make-app", help="build a double-clickable launcher app")
    ma.add_argument("--out", default="~/Applications/Claude PowerTools.app")

    i = sub.add_parser("index", help="rebuild the index")
    i.add_argument("--fts", action="store_true", help="also index message bodies")

    l = sub.add_parser("list", help="newest chats")
    l.add_argument("--limit", type=int, default=25)
    l.add_argument("--scope")

    q = sub.add_parser("search", help="search chats")
    q.add_argument("text")
    q.add_argument("--fts", action="store_true", help="search message bodies too")
    q.add_argument("--limit", type=int, default=40)

    sub.add_parser("accounts", help="accounts, orgs and instances")

    lb = sub.add_parser("label", help="name an account")
    lb.add_argument("uuid")
    lb.add_argument("name")

    m = sub.add_parser("move", help="copy or move chats between accounts")
    m.add_argument("--from", dest="src", required=True, help="source bucket key")
    m.add_argument("--to", dest="dst", required=True, help="destination bucket key")
    m.add_argument("--match", help="only chats whose title contains this")
    m.add_argument("--mode", choices=["copy", "move"], default="copy")
    m.add_argument("--yes", action="store_true", help="skip the dry run")

    sub.add_parser("undo", help="reverse the last move")
    mem = sub.add_parser("memory", help="what Claude has written down about you")
    mem.add_argument("query", nargs="?", help="filter by name, description or body")
    mem.add_argument("--show", help="print one memory in full, by name")

    bk = sub.add_parser("backup", help="zip up chats, memory and config to move Macs")
    bk.add_argument("--out", default="~/Desktop/claude-powertools-backup.zip")
    bk.add_argument("--no-transcripts", action="store_true",
                    help="registrations and settings only, much smaller")

    rs = sub.add_parser("restore", help="put a backup onto this Mac")
    rs.add_argument("file")
    rs.add_argument("--what", default="transcripts,memory",
                    help="any of: transcripts,memory,chats,config")
    rs.add_argument("--into", help="put every chat into this bucket (see: accounts)")
    rs.add_argument("--yes", action="store_true")

    us = sub.add_parser("usage", help="tokens this Mac has used, by model and day")
    us.add_argument("--days", type=int, default=30)
    us.add_argument("--csv", action="store_true", help="print CSV to share or combine")
    us.add_argument("--windows", action="store_true",
                    help="usage inside the app's 5-hour and weekly limit windows")
    us.add_argument("--reset", default="thu 22:30",
                    help="when the weekly limit resets, e.g. 'thu 22:30'")
    us.add_argument("--export", metavar="FILE", help="write this Mac's usage for combining")
    us.add_argument("--name", default="", help="your name, shown in the combined table")
    us.add_argument("--account", default="", help="the shared account email (default: signed-in)")
    us.add_argument("--combine", nargs="+", metavar="FILE", help="merge exports from several Macs")

    st = sub.add_parser("settings", help="Claude Code settings worth knowing about")
    st.add_argument("key", nargs="?")
    st.add_argument("value", nargs="?")

    sub.add_parser("mcp", help="MCP servers and which Claude each belongs to")
    sub.add_parser("instances", help="Claude app instances, their data and size")

    bd = sub.add_parser("bundle", help="write a single self-contained executable")
    bd.add_argument("--out", default="~/.local/bin/powertools")

    ni = sub.add_parser("new-instance", help="create another Claude app instance")
    ni.add_argument("name")
    ni.add_argument("--yes", action="store_true", help="actually create it")

    ex = sub.add_parser("export", help="export chats to markdown")
    ex.add_argument("--scope", help="bucket key, default everything")
    ex.add_argument("--match", help="only chats whose title contains this")
    ex.add_argument("--out", help="output folder")

    a = ap.parse_args()
    cmd = a.cmd or "serve"

    if cmd == "serve":
        return serve(getattr(a, "port", 7788), not getattr(a, "no_open", False))

    if cmd == "open":
        return print(open_ui(a.port))

    if cmd == "make-app":
        p = make_app(a.out)
        return print(f"built {p}\ndouble-click it, or drag it to your Dock")

    if cmd == "index":
        st = build_index(fts=a.fts)
        return print(
            f"{st['total']} chats indexed ({st['updated']} updated, {st['fts']} transcripts)"
        )

    build_index()

    if cmd == "list":
        return fmt_rows(query(scope=a.scope, limit=a.limit))

    if cmd == "search":
        if a.fts:
            build_index(fts=True)
        return fmt_rows(query(q=a.text, fts=a.fts, limit=a.limit))

    if cmd == "accounts":
        labels, auto = load_labels(), auto_labels()
        inst = None
        for b in buckets():
            if b["instance"] != inst:
                inst = b["instance"]
                print(f"\n{inst}")
            name = labels.get(b["account"]) or auto.get(b["account"]) or "(unlabelled)"
            print(f"  {b['count']:>4}  {name:<28} {b['key']}")
        print("\nname one with:  powertools label <account-uuid> \"my work account\"")
        return

    if cmd == "label":
        labels = load_labels()
        labels[a.uuid] = a.name
        save_labels(labels)
        return print(f"{a.uuid[:8]}… = {a.name}")

    if cmd == "move":
        rows = query(scope=a.src, limit=10000)
        if a.match:
            rows = [r for r in rows if a.match.lower() in (r["title"] or "").lower()]
        if not rows:
            return print("no chats matched")
        plan = plan_move([r["reg_path"] for r in rows], a.dst, a.mode)
        print(f"{a.mode} {len(plan['steps'])} chat(s) → {a.dst}")
        for s2 in plan["steps"][:40]:
            print("   " + ("! overwrite  " if s2["overwrite"] else "  ") + s2["title"][:66])
        for s2 in plan["skipped"]:
            print("   skip: " + s2["why"])
        if not a.yes:
            return print("\ndry run. add --yes to apply.")
        res = apply_move(plan)
        build_index()
        print(f"\ndone: {res['moved']} chat(s)\nbackup: {res['backup']}")
        print("quit Claude fully (Cmd+Q) and reopen to see them")
        return

    if cmd == "undo":
        return print(undo_last()["msg"])

    if cmd == "bundle":
        p = bundle(a.out)
        return print(f"wrote {p}\nrun it with:  {os.path.basename(p)}")

    if cmd == "memory":
        data = memories()
        items = data["items"]
        if a.show:
            hit = next((i for i in items if i["name"] == a.show), None)
            if not hit:
                return print("no memory called " + a.show)
            print(f"# {hit['name']}   [{hit['type']}]\n{hit['description']}\n")
            print(hit["body"])
            if hit["session"]:
                print(f"\nlearned in chat {hit['session']}")
            return
        if a.query:
            q = a.query.lower()
            items = [
                i for i in items
                if q in i["name"].lower() or q in i["description"].lower()
                or q in i["body"].lower()
            ]
        by = {}
        for i in items:
            by.setdefault(i["type"], []).append(i)
        for t in sorted(by):
            print(f"\n{t.upper()}  ({len(by[t])})")
            for i in by[t]:
                print(f"  {i['name']:<42} {i['description'][:70]}")
        print(f"\n{len(items)} memories. see one with:  powertools memory --show <name>")
        return

    if cmd == "backup":
        p = backup_plan(not a.no_transcripts)
        print(f"packing {p['registrations']} chats, {p['transcripts']} transcripts, "
              f"{p['memories']} memories, {p['mcp']} MCP servers…")
        r = make_backup(a.out, not a.no_transcripts)
        print(f"\nwrote {r['file']}  ({r['bytes']/1e6:.0f} MB)")
        print("copy it to the other Mac, then:  powertools restore <file> --what transcripts,memory,chats,config")
        return

    if cmd == "restore":
        info = read_backup(a.file)
        if not info["ok"]:
            return print(info["msg"])
        man = info["manifest"]
        print(f"backup from {man.get('machine')} on {man.get('made')}")
        for k, v in info["counts"].items():
            print(f"  {v:>6}  {k}")
        what = [x.strip() for x in a.what.split(",") if x.strip()]
        r = restore_backup(a.file, what, dry=not a.yes, into=a.into)
        print(f"\nwould write {r['planned']} file(s), {r['overwrites']} already exist"
              if r["dry"] else
              f"\nwrote {r['written']} file(s); backup of replaced files: {r['backup']}")
        for x in r["sample"]:
            print("   " + x)
        if r["dry"]:
            print("\ndry run. add --yes to apply.")
        else:
            print("\nquit Claude fully (Cmd+Q) and reopen it")
        return

    if cmd == "usage":
        days_ = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        wd_, at_ = a.reset.lower().split()
        wd_ = days_.index(wd_[:3])
        k = lambda n: f"{n/1e6:.1f}M" if n >= 1e6 else f"{n/1e3:.0f}k" if n >= 1e3 else str(n)
        if a.export:
            with open(os.path.expanduser(a.export), "w") as fh:
                json.dump(usage_export(wd_, at_, a.name, a.account), fh, indent=1)
            return print(f"wrote {a.export}  -  send it to whoever is combining")
        if a.combine:
            r = usage_combine([os.path.expanduser(f) for f in a.combine])
            if not r["ok"]:
                return print(r["msg"])
            print(f"{r['people']} Mac(s) combined for {r['account']}")
            for x in r["rejected"]:
                print(f"   set aside: {os.path.basename(x['file'])} - {x['why']}")
            for w in r["weeks"]:
                print(f"\nweek from {w['start'][:16].replace('T',' ')}{'  (current)' if w['current'] else ''}")
                print(f"   {'WHO':<44}{'FABLE':>9}{'SHARE':>7}{'ALL MODELS':>12}{'SHARE':>7}")
                for row in w["rows"]:
                    print(f"   {row['who'][:43]:<44}{k(row['fable']):>9}{row['fable_share']:>6}%"
                          f"{k(row['total']):>12}{row['total_share']:>6}%")
            return
        if a.windows:
            r = window_report(wd_, at_)
            for name, label in (("5h", "rolling 5-hour window"), ("week", "weekly window")):
                w = r["windows"][name]
                print(f"\n{label}  (since {w['since'][:16].replace('T',' ')})")
                if not w["models"]:
                    print("   nothing on this Mac")
                for mname, m in sorted(w["models"].items(), key=lambda x: -x[1]["output"]):
                    print(f"   {mname:<26}{m['replies']:>6} replies {k(m['output']):>8} out")
                for app, ms in w["apps"].items():
                    print(f"     {app:<22}" + "  ".join(f"{mn.replace('claude-','')}={k(v['output'])}" for mn, v in ms.items()))
            return
        if a.csv:
            return print(usage_csv(a.days))
        r = usage_report(a.days)
        k = lambda n: f"{n/1e6:.1f}M" if n >= 1e6 else f"{n/1e3:.0f}k"
        print(f"this Mac ({r['machine']}), last {a.days} days, since {r['since']}\n")
        print(f"{'MODEL':<26}{'REPLIES':>9}{'OUTPUT':>10}{'THINKING':>10}{'INPUT+CACHE':>13}")
        for m in r["by_model"]:
            print(f"{m['model']:<26}{m['requests']:>9}{k(m['output']):>10}{k(m['thinking']):>10}"
                  f"{k(m['input']+m['cache_create']):>13}")
        print(f"\nby account:")
        for x in r["by_account"][:8]:
            print(f"  {x['label']:<30}{x['model']:<24}{k(x['output']):>8} out")
        print(f"\ntop chats:")
        for t in r["top_chats"][:8]:
            print(f"  {k(t['output']):>7} out  {t['model'][:16]:<17} {t['title'][:48]}")
        return

    if cmd == "settings":
        if a.key and a.value is not None:
            v = a.value
            if v.lower() in ("true", "on", "yes"):
                v = True
            elif v.lower() in ("false", "off", "no"):
                v = False
            r = write_setting(a.key, v)
            return print(r["msg"] if r["ok"] else "error: " + r["msg"])
        d = read_settings()
        print(d["file"])
        for s2 in d["settings"]:
            shown = s2["value"] if s2["set"] else f"{s2['default']} (default, not set)"
            flag = "  [risky]" if s2.get("risky") else ""
            print(f"\n  {s2['title']}{flag}\n    now: {shown}\n    {s2['why']}")
        print("\nchange one with:  powertools settings cleanupPeriodDays 365")
        return

    if cmd == "mcp":
        by = {}
        for m in mcp_servers():
            by.setdefault(m["scope"], []).append(m)
        for scope in by:
            tag = "  (shared by every instance)" if by[scope][0]["shared"] else ""
            print(f"\n{scope}{tag}")
            for m in by[scope]:
                off = "" if m["enabled"] else "  [disabled]"
                print(f"   {m['name']:<26} {m['kind']:<10} {m['target'][:60]}{off}")
        print("\nsecrets in these files are never printed")
        return

    if cmd == "instances":
        tr = glob.glob(os.path.join(TRANSCRIPTS, "*", "*.jsonl"))
        for i in instances():
            apps = ", ".join(os.path.basename(a) for a in i["apps"]) or "-"
            sz = i["size"]
            print(f"  {i['chats']:>4} chats  {i['bytes']/1e9:>6.2f} GB  {i['name']:<18} {apps}")
            if sz["total"]:
                print(f"        of that, {sz['disposable']/1e9:.2f} GB is cache and VM images "
                      f"you never need to back up")
            if i["problem"]:
                print(f"        ! {i['problem']}  ({i['data']})")
        return

    if cmd == "new-instance":
        try:
            plan = instance_plan(a.name)
        except ValueError as e:
            return print("error:", e)
        print(f"app:    {plan['app']}{'   (ALREADY EXISTS, will be replaced)' if plan['app_exists'] else ''}")
        print(f"data:   {plan['data']}{'   (exists, will be reused)' if plan['data_exists'] else ''}")
        print(f"source: {plan['source']}")
        print("\nthis copies the app, points it at its own data folder and re-signs it.")
        print("macOS will ask for your password; Claude PowerTools never sees it.")
        if not a.yes:
            return print("\ndry run. add --yes to create it.")
        r = create_instance(plan)
        return print(("done: " if r["ok"] else "failed: ") + r["msg"])

    if cmd == "export":
        rows = query(scope=a.scope, limit=100000)
        if a.match:
            rows = [r for r in rows if a.match.lower() in (r["title"] or "").lower()]
        if not rows:
            return print("no chats matched")
        out = a.out or os.path.join(
            HOME, "Downloads", "powertools-export-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        )
        r = export_many(rows, out)
        return print(f"wrote {r['written']} chat(s) to {r['dir']}")


if __name__ == "__main__":
    main()
