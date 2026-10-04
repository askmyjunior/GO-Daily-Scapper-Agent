#!/bin/sh
# Re-copy the vendored modules from go-ingestion and refresh their checksums.
# Run on the machine that holds go-ingestion; commit the result.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
src="$here/../AP GOs Download Code/go-ingestion"
for f in "$here"/vendor/*.py; do
  cp "$src/$(basename "$f")" "$f"
done
cd "$here/vendor"
tmp=$(mktemp)
sed '/^```$/,$d' README.md > "$tmp"
{ cat "$tmp"; echo '```'; shasum -a 256 *.py; echo '```'; } > README.md
rm -f "$tmp"
echo "re-copied $(ls *.py | wc -l | tr -d ' ') modules"
