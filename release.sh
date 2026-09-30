#!/bin/bash
# Cut a Claude PowerTools release and update the Homebrew tap in one go.
#
#   ./release.sh 1.0.1            tag + push + update tap (formula AND cask)
#   ./release.sh 1.0.1 --dry-run  show what would happen, change nothing
#
# Needs: a clean working tree on main, ~/homebrew-tap cloned next to this repo,
# and the personal SSH key (remote github-personal). Not shipped inside the
# single-file build.
set -eu
VER="${1:-}"; DRY=0; [ "${2:-}" = "--dry-run" ] && DRY=1
[ -n "$VER" ] || { echo "usage: ./release.sh <version> [--dry-run]" >&2; exit 2; }
printf '%s' "$VER" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$' || { echo "version must look like 1.2.3" >&2; exit 2; }

HERE="$(cd "$(dirname "$0")" && pwd)"
TAP="${TAP_DIR:-$HOME/homebrew-tap}"
REPO="navneetbind/claude-powertools"
run() { if [ "$DRY" = 1 ]; then echo "  [dry] $*"; else "$@"; fi; }

cd "$HERE"
[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || { echo "not on main" >&2; exit 1; }
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "working tree has uncommitted changes - commit first" >&2; exit 1; }
git rev-parse "v$VER" >/dev/null 2>&1 && { echo "tag v$VER already exists" >&2; exit 1; }
[ -d "$TAP/.git" ] || { echo "tap not found at $TAP" >&2; exit 1; }
[ -z "$(git -C "$TAP" status --porcelain)" ] || { echo "tap has uncommitted changes" >&2; exit 1; }

echo "==> Rebuilding dist/powertools"
run python3 powertools.py bundle --out dist/powertools
if [ "$DRY" = 0 ] && [ -n "$(git status --porcelain dist/powertools)" ]; then
  git add dist/powertools && git commit -q -m "Rebuild single-file build for v$VER"
fi

echo "==> Tagging v$VER and pushing"
run git tag -a "v$VER" -m "Claude PowerTools $VER"
run git push origin main "v$VER"

echo "==> Fetching the tarball for its checksum"
if [ "$DRY" = 1 ]; then SHA="<sha256 of v$VER tarball>"; else
  TB="$(mktemp /tmp/pt-release.XXXXXX)"
  for i in 1 2 3 4 5; do
    curl -fsSL "https://github.com/$REPO/archive/refs/tags/v$VER.tar.gz" -o "$TB" && break
    sleep 3
  done
  SHA="$(shasum -a 256 "$TB" | awk '{print $1}')"; rm -f "$TB"
fi
echo "    sha256 $SHA"

echo "==> Updating the tap"
if [ "$DRY" = 0 ]; then
  VER="$VER" SHA="$SHA" python3 - "$TAP" <<'PY'
import os, re, sys
tap = sys.argv[1]; ver = os.environ["VER"]; sha = os.environ["SHA"]
f = os.path.join(tap, "Formula", "claude-powertools.rb")
s = open(f).read()
s = re.sub(r'(url ".*?/tags/v)[0-9.]+(\.tar\.gz")', r'\g<1>%s\2' % ver, s)
s = re.sub(r'(sha256 ")[0-9a-f]{64}(")', r'\g<1>%s\2' % sha, s)
open(f, "w").write(s)
c = os.path.join(tap, "Casks", "claude-powertools.rb")
s = open(c).read()
s = re.sub(r'(version ")[0-9.]+(")', r'\g<1>%s\2' % ver, s)
s = re.sub(r'(sha256 ")[0-9a-f]{64}(")', r'\g<1>%s\2' % sha, s)
open(c, "w").write(s)
PY
  git -C "$TAP" add -A && git -C "$TAP" commit -q -m "claude-powertools $VER" && git -C "$TAP" push -q
else
  echo "  [dry] edit version/url/sha256 in $TAP/Formula and $TAP/Casks, commit, push"
fi
echo
echo "Done. People update with:  brew update && brew upgrade claude-powertools"
