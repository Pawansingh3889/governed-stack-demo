#!/usr/bin/env bash
# Build wheels for the five MCP servers and their governance libraries (none are
# on PyPI) into docker/wheels, so the Dockerfile can install them offline.
# Run from the repo root. Edit the paths if your checkouts live elsewhere.
set -euo pipefail

PY="${PY:-./.venv/Scripts/python.exe}"
OUT="docker/wheels"
PKGS=(
  C:/Projects/sql-steward
  C:/Users/pawan/work/kql-sop
  C:/Users/pawan/work/doc-steward
  C:/Projects/schema-scout
  C:/Projects/thread-recall
  C:/Projects/pii-veil
  C:/Projects/agent-blackbox
)

rm -rf "$OUT"
mkdir -p "$OUT"
for p in "${PKGS[@]}"; do
  echo "wheel: $p"
  "$PY" -m pip wheel --no-deps "$p" -w "$OUT" >/dev/null
done
echo "built $(ls "$OUT"/*.whl | wc -l) wheels into $OUT"
ls "$OUT"
