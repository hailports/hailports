#!/usr/bin/env bash
# vpn_socks_setup.sh — stand up the hustle VPN lane as a local SOCKS5 proxy,
# WITHOUT touching the Mac's default route (so Salesforce/work stays residential).
#
# It runs a SECOND, userspace tailscaled that egresses through the Mullvad
# exit node and exposes socks5://127.0.0.1:1055. The main tailscaled is left
# alone (no exit node — that's the whole point).
#
# ONE-TIME human gate: joining a new node to the tailnet needs your approval.
# Everything else is automatic. Re-running is safe/idempotent.
set -euo pipefail

TS=/opt/homebrew/bin/tailscale
TSD=/opt/homebrew/sbin/tailscaled
[ -x "$TSD" ] || TSD=/opt/homebrew/bin/tailscaled
STATE_DIR="$HOME/.tailscale-socks"
SOCK="$STATE_DIR/tailscaled.sock"
STATE="$STATE_DIR/tailscaled.state"
EXIT_NODE="${1:-us-dal-wg-001.host.example}"
PORT=1055
PLIST="$HOME/Library/LaunchAgents/com.claude-stack.vpn-socks.plist"

mkdir -p "$STATE_DIR"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.claude-stack.vpn-socks</string>
  <key>ProgramArguments</key>
  <array>
    <string>$TSD</string>
    <string>--tun=userspace-networking</string>
    <string>--socks5-server=localhost:$PORT</string>
    <string>--outbound-http-proxy-listen=localhost:$PORT</string>
    <string>--statedir=$STATE_DIR</string>
    <string>--socket=$SOCK</string>
    <string>--state=$STATE</string>
  </array>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$HOME/.vpn-socks.out</string>
  <key>StandardErrorPath</key><string>$HOME/.vpn-socks.out</string>
</dict>
</plist>
PLIST

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "started userspace tailscaled (SOCKS on :$PORT), waiting for socket..."
for _ in $(seq 1 20); do [ -S "$SOCK" ] && break; sleep 0.5; done

# Non-interactive join: the stack's OAuth-minted authkey is preauthorized + tagged
# (tag:mini), so the socks node self-authorizes — no browser gate, safe to re-run.
AUTHKEY="$(cat "$HOME/claude-stack/data/secrets/tailscale_authkey" 2>/dev/null || true)"
if [ -n "$AUTHKEY" ]; then
  "$TS" --socket="$SOCK" up --authkey="$AUTHKEY" --hostname=mac-mini-socks --accept-routes=false --timeout=60s
else
  echo ">>> no authkey found; falling back to interactive auth (approve 'mac-mini-socks')."
  "$TS" --socket="$SOCK" up --hostname=mac-mini-socks --accept-routes=false
fi

# Mullvad access is granted to this node's IP via the ACL policy file (nodeAttrs).
# The exit node must be set by TAILSCALE IP — the magicDNS name is rejected here —
# so resolve the chosen Mullvad node name to its 100.x IP, waiting for it to enter
# the netmap (appears only once ACL Mullvad access has propagated).
EXIT_IP=""
for _ in $(seq 1 18); do
  EXIT_IP=$("$TS" --socket="$SOCK" exit-node list 2>/dev/null \
    | awk -v n="$EXIT_NODE" '$2==n {print $1; exit}')
  [ -n "$EXIT_IP" ] && break
  sleep 5
done
if [ -z "$EXIT_IP" ]; then
  echo "!! $EXIT_NODE not in netmap — is this node in the ACL mullvad nodeAttrs?"
  echo "   grant: {\"target\": [\"<this-node-100.x-ip>\"], \"attr\": [\"mullvad\"]}"
  exit 4
fi
"$TS" --socket="$SOCK" set --exit-node="$EXIT_IP"
echo "exit node set: $EXIT_NODE ($EXIT_IP)"

echo
echo "verifying SOCKS egress goes through Mullvad (system route unaffected)..."
MV=$(curl -s --max-time 8 --socks5-hostname 127.0.0.1:$PORT https://am.i.mullvad.net/json || echo '{}')
SYS=$(curl -s --max-time 8 https://api.ipify.org || echo '?')
echo "  SOCKS egress : $MV"
echo "  system route : $SYS  (must be your residential IP)"
echo
echo "done. Hustle Chrome profiles now get --proxy-server=socks5://127.0.0.1:$PORT"
echo "via core/vpn_egress.py; work/Salesforce stays on $SYS."
