#!/bin/sh
# Install powertools: one self-contained file, no npm, no node, no dependencies.
# Uses the python3 that ships with macOS.
set -e
mkdir -p "$HOME/.local/bin"
cp "$(dirname "$0")/dist/powertools" "$HOME/.local/bin/powertools"
chmod +x "$HOME/.local/bin/powertools"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo 'Add this to ~/.zshrc:  export PATH="$HOME/.local/bin:$PATH"' ;;
esac
echo "installed: $HOME/.local/bin/powertools"
echo "run it with:  powertools"
