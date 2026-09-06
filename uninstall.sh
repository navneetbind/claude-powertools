#!/bin/sh
# Removes Claude PowerTools itself. Never touches Claude's own data.
pkill -f "powertools.py serve" 2>/dev/null; pkill -f "powertools serve" 2>/dev/null
rm -f  "$HOME/.local/bin/powertools"
rm -rf "$HOME/Applications/Claude PowerTools.app"
[ -d "/Applications/Claude PowerTools.app" ] && { rm -rf "/Applications/Claude PowerTools.app" 2>/dev/null || echo "run:  sudo rm -rf \"/Applications/Claude PowerTools.app\""; }
if [ "$1" = "--purge" ]; then
  rm -rf "$HOME/.claude-powertools"
  echo "removed the app, the launcher, and all PowerTools state (index, account names, reset times, backups)"
else
  echo "removed the app and the launcher"
  echo "kept ~/.claude-powertools (index, account names, reset times, and the backups from transfers)"
  echo "to remove that too:  ./uninstall.sh --purge"
fi
