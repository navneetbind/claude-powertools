#!/bin/sh
# Claude PowerTools installer. No npm, no node, no pip: one file, uses the
# python3 that ships with macOS. Run from a clone of the repo.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.local/bin"
cp "$HERE/dist/powertools" "$HOME/.local/bin/powertools"
chmod +x "$HOME/.local/bin/powertools"
# put ~/.local/bin on PATH so `powertools` runs without the full path. Append
# once to whichever shell rc files exist, guarded by a marker so re-running is safe.
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *)
    MARKER="# added by Claude PowerTools installer"
    LINE='export PATH="$HOME/.local/bin:$PATH"'
    added=""
    for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
      [ -e "$rc" ] || [ "$rc" = "$HOME/.zshrc" ] || continue
      if [ ! -e "$rc" ] || ! grep -qF "$MARKER" "$rc" 2>/dev/null; then
        printf '\n%s\n%s\n' "$MARKER" "$LINE" >> "$rc"
        added="$added $rc"
      fi
    done
    [ -n "$added" ] && printf '\nAdded ~/.local/bin to PATH in:%s\nOpen a new terminal (or run: source ~/.zshrc) to use `powertools` directly.\n\n' "$added"
    ;;
esac
# the app goes next to your other apps when the folder is writable; otherwise
# into ~/Applications, with the one sudo command to move it if you want.
APP="$HOME/Applications/Claude PowerTools.app"
"$HOME/.local/bin/powertools" make-app --out "$APP" >/dev/null
if [ -w /Applications ] && mv "$APP" "/Applications/Claude PowerTools.app" 2>/dev/null; then
  APP="/Applications/Claude PowerTools.app"
fi
echo "installed  ~/.local/bin/powertools"
echo "app        $APP   (double-click, or drag to the Dock)"
case "$APP" in "$HOME"/*)
  echo
  echo "to put it in /Applications with the other apps (asks for your password):"
  echo "  sudo mv \"$APP\" \"/Applications/Claude PowerTools.app\"";;
esac
echo
echo "run it:    powertools"
