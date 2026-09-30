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
echo "installed  ~/.local/bin/powertools"
echo
echo "run it:    powertools"
