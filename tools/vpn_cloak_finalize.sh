#!/usr/bin/env bash
# vpn_cloak_finalize.sh — bring the hustle cloak online through a Texas-proximate
# Mullvad exit, AFTER the Tailscale Mullvad add-on is enabled in the admin console.
#
# Split it keeps intact:
#   - MAIN tailscaled: NO exit node (work/CompanyA stays residential — guard-enforced).
#   - SOCKS lane (127.0.0.1:1055): egresses through the chosen Mullvad exit.
#   - vpn_egress.py: only non-work, non-SHIELDED hustle profiles ride the SOCKS lane.
#
# "Maintain my location": picks the nearest US exit, Texas first (Dallas is ~180mi
# from Austin), never Japan/overseas. Live sessions are shielded separately so this
# never yanks a logged-in profile onto the new IP.
set -euo pipefail
TS=/opt/homebrew/bin/tailscale
export PYTHONPATH="$(cd "$(dirname "$0")/.." && pwd):${PYTHONPATH:-}"

echo "== checking for Mullvad exit nodes in the tailnet =="
LIST=$("$TS" exit-node list 2>/dev/null || true)
if ! echo "$LIST" | grep -qiE "mullvad|\.ts\.net"; then
  cat <<'MSG'
BLOCKED: no exit nodes in the tailnet yet.
Enable the Tailscale Mullvad add-on first (one toggle, ~$5/mo):
  login.tailscale.com  ->  admin console  ->  "Exit Nodes" (or Settings)  ->  add Mullvad
Then re-run this script.
MSG
  exit 2
fi

# Nearest-first US preference: Texas cities, then other central/US, never overseas.
pick() {
  for pat in "us-dal" "us-hou" "us-aus" "us-sat" "us-mci" "us-chi" "us-den" "us-"; do
    line=$(echo "$LIST" | grep -iE "$pat" | grep -iv "japan\|jp-\|tokyo\|osaka" | head -1 || true)
    [ -n "$line" ] && { echo "$line" | awk '{for(i=1;i<=NF;i++) if($i ~ /mullvad\.ts\.net/) print $i}' | head -1; return; }
  done
}
NODE="$(pick || true)"
if [ -z "${NODE:-}" ]; then
  echo "No US exit node found; available were:"; echo "$LIST"; exit 3
fi
echo "== chosen exit node (closest to Austin, never overseas): $NODE =="

echo "== standing up the SOCKS lane through it (main route untouched) =="
bash "$(dirname "$0")/vpn_socks_setup.sh" "$NODE"

echo "== VERIFY: work lane still residential =="
python3 "$(dirname "$0")/vpn_lane_guard.py" status || true

echo "== VERIFY: SOCKS lane egresses Mullvad Texas =="
curl -s --max-time 10 --socks5-hostname 127.0.0.1:1055 https://am.i.mullvad.net/json 2>/dev/null \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('  socks egress:',d.get('ip'),'| mullvad:',d.get('mullvad_exit_ip'),'|',d.get('city'),d.get('country'))" \
  || echo "  (socks egress check failed — lane may still be authorizing)"

echo "== profiles NOW cloaked vs shielded =="
for d in "$HOME"/.chrome-cdp-profile*; do
  python3 -m core.vpn_egress status "$d"
done
echo "done. shielded profiles stay residential until you un-shield them on a fresh login:"
echo "  python3 -m core.vpn_egress shield rm <key>"
