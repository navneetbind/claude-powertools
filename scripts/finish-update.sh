#!/bin/bash
# Finish the Claude update. RUN FROM TERMINAL, WITH CLAUDE CLOSED.
#
# Why this is fiddly: every launcher execs the one real /Applications/Claude.app,
# so ANY open Claude window is "the target app" and ShipIt refuses to install
# (SQRLInstallerErrorDomain Code=-9 "App Still Running Error"). Each abort also
# deletes the staged download AND increments a per-profile failure counter.
#
# This script: kills the counter, quits everything, stages the update using a
# THROWAWAY profile (fresh profiles check for updates within ~30s; existing ones
# can take 5+ min), then quits cleanly so ShipIt can swap the bundle.

set -u
SHIPIT="$HOME/Library/Caches/com.anthropic.claudefordesktop.ShipIt"
CUR=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" /Applications/Claude.app/Contents/Info.plist 2>/dev/null)
echo "Current version: $CUR"

# Count real app instances only. Every launcher execs this same binary, so one
# pattern catches them all; --type= excludes Electron's helper processes.
count() { ps -Ao args= | grep -c "^/Applications/Claude.app/Contents/MacOS/Claude "; }

echo "==> Quitting every Claude instance..."
osascript -e 'tell application "Claude" to quit' >/dev/null 2>&1 || true
for i in $(seq 1 40); do [ "$(count)" -eq 0 ] && break; sleep 1; done
if [ "$(count)" -ne 0 ]; then
  echo "    STILL RUNNING. Close every Claude window by hand (Cmd-Q), then re-run."
  ps -Ao pid,args= | grep "^ *[0-9]* /Applications/Claude.app/Contents/MacOS/Claude " | cut -c1-110
  exit 1
fi
echo "    All instances down."

echo "==> Resetting the updater failure counter in every profile..."
for cfg in "$HOME/Library/Application Support/"Claude*/config.json; do
  [ -f "$cfg" ] || continue
  python3 - "$cfg" <<'PY' 2>/dev/null
import json,sys
p=sys.argv[1]
try: d=json.load(open(p))
except Exception: sys.exit()
if d.pop("updaterFailedInstall",None) is not None:
    json.dump(d,open(p,"w"),indent=1); print("    cleared:",p.split("/")[-2])
PY
done

echo "==> Clearing stale Squirrel pointer..."
rm -f "$SHIPIT/ShipItState.plist"

TMPPROF=$(mktemp -d /tmp/claude-upd.XXXXXX)
echo "==> Launching one idle instance on a throwaway profile..."
nohup /Applications/Claude.app/Contents/MacOS/Claude --user-data-dir="$TMPPROF" >/dev/null 2>&1 &
sleep 3

echo "    Waiting for the download to stage (up to 8 min). Do NOT open Claude."
STAGED=""
for i in $(seq 1 96); do
  sleep 5
  if [ -f "$SHIPIT/ShipItState.plist" ]; then
    U=$(plutil -p "$SHIPIT/ShipItState.plist" 2>/dev/null | grep updateBundleURL | sed 's|.*file://||; s|/"$||')
    if [ -n "$U" ] && [ -d "$U" ]; then
      STAGED=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$U/Contents/Info.plist" 2>/dev/null)
      echo "    Staged $STAGED"
      break
    fi
  fi
done

if [ -z "$STAGED" ]; then
  echo "    No staged update appeared. Last updater lines:"
  grep -a "\[updater\]" "$HOME/Library/Logs/Claude/main.log" | tail -5
  osascript -e 'tell application "Claude" to quit' >/dev/null 2>&1 || true
  rm -rf "$TMPPROF"
  exit 1
fi

echo "==> Quitting so ShipIt can swap the bundle..."
osascript -e 'tell application "Claude" to quit' >/dev/null 2>&1 || true

echo "==> Installing. ENTER YOUR PASSWORD WHEN PROMPTED."
for i in $(seq 1 60); do
  V=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" /Applications/Claude.app/Contents/Info.plist 2>/dev/null)
  if [ -n "$V" ] && [ "$V" != "$CUR" ]; then
    echo
    echo "SUCCESS: /Applications/Claude.app is now $V"
    rm -rf "$TMPPROF"
    # Instances are full copies that cannot update themselves; bring them up to
    # the Claude.app just installed.
    if [ -x "$(dirname "$0")/reclone-instances.sh" ]; then
      echo; echo "==> Bringing instances up to Claude.app..."
      "$(dirname "$0")/reclone-instances.sh"
    fi
    exit 0
  fi
  sleep 5
done

echo
echo "Still $CUR. ShipIt's last words:"
ls -t "$SHIPIT"/ShipIt_stderr.log* 2>/dev/null | head -1 | xargs tail -6
rm -rf "$TMPPROF"
exit 1
