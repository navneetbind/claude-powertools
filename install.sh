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
"$HOME/.local/bin/powertools" make-app --out "$HOME/Applications/Claude PowerTools.app" >/dev/null
echo "installed  ~/.local/bin/powertools"
echo "app        ~/Applications/Claude PowerTools.app   (double-click, or drag to the Dock)"
echo
echo "run it:    powertools"
