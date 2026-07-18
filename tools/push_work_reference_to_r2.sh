#!/bin/bash
# Refresh the work-fallback Worker with the latest Work Reference snapshot: bake the *.md notes
# into src/snapshot.json and redeploy. The edge Worker then serves the last-synced snapshot if
# the mini is down. Read-only on the folder. Needs a CF API token (Workers Scripts edit) in
# ~/.cf_token; without it, exits quietly. (Named *_to_r2 historically; now bake-and-deploy.)
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
DIR="$HOME/Library/CloudStorage/OneDrive-redactedIndustries,Inc/Work Reference"
WORKER="$HOME/claude-stack/worker/work-fallback"
LOG="$HOME/Library/Logs/claude-stack/work-r2-push.log"
mkdir -p "$(dirname "$LOG")"
ts(){ date '+%Y-%m-%d %H:%M:%S'; }

if [ ! -f "$HOME/.cf_token" ]; then
  echo "$(ts) no ~/.cf_token — skipping fallback refresh (see worker/work-fallback/DEPLOY.md)" >> "$LOG"
  exit 0
fi
[ -d "$DIR" ] || { echo "$(ts) work folder not found" >> "$LOG"; exit 0; }
export CLOUDFLARE_API_TOKEN="$(tr -d '[:space:]' < "$HOME/.cf_token")"

# bake the snapshot (raw notes, per-file cap) into the worker bundle.
# This feeds a PUBLIC edge Worker, so every file is run through the surface guard:
# device/stack conflict-copies are skipped and banned tokens scrubbed before baking.
python3 - "$DIR" "$WORKER/src/snapshot.json" "$HOME/claude-stack" <<'PY'
import json, sys, glob, os, datetime
folder, out, root = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, os.path.join(root, "tools"))
from redacted_surface_guard import scrub_text, banned_tokens, _CONFLICT_RE, MANAGED, _name_has_token
toks = banned_tokens()
files = {}
for p in sorted(glob.glob(os.path.join(folder, "*.md"))):
    name = os.path.basename(p)
    if _name_has_token(name, toks):
        continue
    m = _CONFLICT_RE.match(name)
    if m and (m.group("base") + m.group("ext")) in MANAGED:
        continue
    try:
        files[name] = scrub_text(open(p, encoding="utf-8", errors="ignore").read()[:60000], toks)
    except Exception:
        pass
json.dump({"synced_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "files": files},
          open(out, "w"))
print(len(files))
PY
n=$(python3 -c "import json;print(len(json.load(open('$WORKER/src/snapshot.json'))['files']))" 2>/dev/null || echo "?")

# fail-closed gate: never deploy a snapshot that still carries a banned token
if ! python3 "$HOME/claude-stack/tools/redacted_surface_guard.py" check "$WORKER/src/snapshot.json" 2>>"$LOG"; then
  echo "$(ts) ABORT deploy — snapshot failed leak check (see above)" >> "$LOG"
  exit 1
fi

cd "$WORKER" || exit 0
if npx --yes wrangler deploy >>"$LOG" 2>&1; then
  echo "$(ts) baked $n files + deployed work-fallback" >> "$LOG"
else
  echo "$(ts) bake ok ($n files) but wrangler deploy FAILED (see log above)" >> "$LOG"
fi
exit 0
