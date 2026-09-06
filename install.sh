#!/bin/sh
# Claude PowerTools installer. No npm, no node, no pip: one file, uses the
# python3 that ships with macOS. Run from a clone of the repo.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.local/bin"
cp "$HERE/dist/powertools" "$HOME/.local/bin/powertools"
chmod +x "$HOME/.local/bin/powertools"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) printf '\nAdd this line to ~/.zshrc, then open a new terminal:\n  export PATH="$HOME/.local/bin:$PATH"\n\n' ;;
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
