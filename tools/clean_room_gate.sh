#!/bin/bash
# Autonomous PII/credential gate. Installed as a git pre-commit hook AND runnable
# in CI. Blocks any commit/build that reintroduces a secret, identity marker, or
# lead-data file. Zero human review — the scanner is the reviewer.
# Self-locating: works from the repo root regardless of where it's invoked.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
SCANNER="$ROOT/tools/clean_room.py"
[ -f "$SCANNER" ] || SCANNER="$(dirname "$0")/clean_room.py"
if ! python3 "$SCANNER" scan "$ROOT" >/tmp/clean_room_gate.out 2>&1; then
  echo "❌ CLEAN-ROOM GATE FAILED — PII/credentials detected. Commit blocked."
  tail -20 /tmp/clean_room_gate.out
  echo "Fix or remove the flagged content, then retry. (override: SKIP_CLEAN_ROOM=1)"
  [ "${SKIP_CLEAN_ROOM:-0}" = "1" ] && { echo "⚠️ overridden"; exit 0; }
  exit 1
fi
echo "✅ clean-room gate passed — no PII/credentials"
