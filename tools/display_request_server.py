#!/usr/bin/env python3
"""Tiny HTTP receiver so the MacBook can tell the mini which monitor its Screens window is
currently on. display_autoresizer.py reads the file this writes and matches the mini's
resolution to it (no pillarbox/letterbox, no client zoom).

Endpoints (GET, reachable over the Tailscale tailnet / LAN):
  /resize?w=1920&h=1080[&deg=0][&lodpi=1]   write the per-monitor request
  /resize?w=0&h=0                           clear it (revert to the static profile)
  /install                                  returns the MacBook-side helper installer
  /health                                   liveness

No auth by design: a resize of a headless dummy display is harmless, and the only routes
here are within Operator's tailnet/LAN. Bind 0.0.0.0 so both Tailscale and LAN reach it.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path

RUNTIME = Path(os.path.expanduser("~/claude-stack/data/runtime"))
REQ = RUNTIME / "display_request.json"
PORT = int(os.environ.get("DISPLAY_REQUEST_PORT", "8767"))

# Served at /install — run on the MacBook, writes + starts the helper that polls which
# monitor the Screens window is on and pushes its shape here. Detection uses CGWindowList
# (window bounds + owner name are NOT permission-gated, unlike titles), so there is no
# Accessibility / Screen-Recording prompt. Transport is curl -> this server (no SSH keys).
INSTALLER = r'''#!/bin/bash
MINI="${MINI_HOST:-10.0.0.1}"
PORT="${DISPLAY_REQUEST_PORT:-8767}"
mkdir -p "$HOME/Library/LaunchAgents"

cat > "$HOME/.mbdisplay_helper.sh" <<'HELPER'
#!/bin/bash
# Pushes the shape of whichever monitor the Screens window is on to the mini.
MINI="${MINI_HOST:-10.0.0.1}"
PORT="${DISPLAY_REQUEST_PORT:-8767}"
last=""; last_send=0
detect() {
# Monitor the cursor is currently on -> its point (logical "looks like") size. No
# Screen Recording / Accessibility needed (NSEvent.mouseLocation + NSScreen are ungated).
/usr/bin/osascript -l JavaScript <<'JXA'
ObjC.import("AppKit");
ObjC.import("CoreGraphics");
var ss = $.NSScreen.screens, n = ss.count, out = "";
// primary-screen height flips CGWindow's top-left-origin Y into NSScreen's bottom-left space
var primH = n ? ss.objectAtIndex(0).frame.size.height : 0;
function screenSizeAt(px, py) {              // px,py in NSScreen (bottom-left) points
  for (var i = 0; i < n; i++) {
    var f = ss.objectAtIndex(i).frame;
    if (px >= f.origin.x && px < f.origin.x + f.size.width &&
        py >= f.origin.y && py < f.origin.y + f.size.height)
      return Math.round(f.size.width) + "," + Math.round(f.size.height);
  }
  return "";
}
// PRIMARY signal: the monitor the Screens VIEWER WINDOW is on -- not the cursor. So roaming
// the mouse onto another monitor no longer reshapes the mini; only moving the window does.
// CGWindowList bounds + owner name are ungated (no Screen-Recording / Accessibility prompt).
var info = $.CGWindowListCopyWindowInfo(1, 0);  // 1=OnScreenOnly, 0=kCGNullWindowID
var wins = ObjC.deepUnwrap(info) || [], best = null, bestArea = 0;
for (var i = 0; i < wins.length; i++) {
  var w = wins[i], owner = w.kCGWindowOwnerName || "";
  if (!/Screens|Screen Sharing|VNC|RealVNC|Jump Desktop/i.test(owner)) continue;
  var b = w.kCGWindowBounds; if (!b) continue;
  var area = b.Width * b.Height;
  if (area > bestArea) { bestArea = area; best = b; }
}
if (best) {
  var cx = best.X + best.Width / 2;           // CGWindow: top-left origin, points
  out = screenSizeAt(cx, primH - (best.Y + best.Height / 2));
}
// NO cursor fallback (banned): if the Screens window can't be read, push nothing so the mini
// keeps its current shape. The cursor is NEVER consulted -- only the Screens window's monitor.
out
JXA
}
while true; do
  s=$(detect 2>/dev/null); now=$(date +%s)
  if [ -n "$s" ]; then
    w="${s%,*}"; h="${s#*,}"
    if [ "$s" != "$last" ] || [ $((now - last_send)) -ge 12 ]; then
      /usr/bin/curl -s -m 5 "http://$MINI:$PORT/resize?w=$w&h=$h" >/dev/null 2>&1 && { last="$s"; last_send=$now; }
    fi
  fi
  sleep 4
done
HELPER
chmod +x "$HOME/.mbdisplay_helper.sh"

cat > "$HOME/Library/LaunchAgents/com.Operator.mbdisplay.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.Operator.mbdisplay</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$HOME/.mbdisplay_helper.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/tmp/mbdisplay.err</string>
  <key>StandardOutPath</key><string>/tmp/mbdisplay.out</string>
</dict></plist>
PLIST

launchctl unload "$HOME/Library/LaunchAgents/com.Operator.mbdisplay.plist" 2>/dev/null || true
launchctl load "$HOME/Library/LaunchAgents/com.Operator.mbdisplay.plist" 2>/dev/null || true
echo "----- mbdisplay install report -----"
echo "mini reachable:    $(/usr/bin/curl -s -m4 "http://$MINI:$PORT/health" || echo 'NO - cannot reach mini')"
echo "launchd job loaded: $(launchctl list 2>/dev/null | grep -c mbdisplay) (1 = good)"
echo "all monitors:      $(/usr/bin/osascript -l JavaScript -e 'ObjC.import("AppKit");var s=$.NSScreen.screens,n=s.count,o=[];for(var i=0;i<n;i++){var f=s.objectAtIndex(i).frame;o.push(Math.round(f.size.width)+"x"+Math.round(f.size.height));}o.join(", ")' 2>/dev/null)"
SZ=$(/usr/bin/osascript -l JavaScript <<'JXA'
ObjC.import("AppKit");
var p=$.NSEvent.mouseLocation,ss=$.NSScreen.screens,n=ss.count,out="";
for(var i=0;i<n;i++){var f=ss.objectAtIndex(i).frame;
if(p.x>=f.origin.x&&p.x<f.origin.x+f.size.width&&p.y>=f.origin.y&&p.y<f.origin.y+f.size.height){
out=Math.round(f.size.width)+","+Math.round(f.size.height);break;}}
out
JXA
)
if [ -n "$SZ" ]; then
  echo "monitor at cursor: ${SZ/,/x} pts -> pushing to mini now"
  echo "push result:       $(/usr/bin/curl -s -m4 "http://$MINI:$PORT/resize?w=${SZ%,*}&h=${SZ#*,}" || echo 'push FAILED')"
else
  echo "monitor at cursor: NONE (couldn't read displays)"
fi
echo "uninstall:         launchctl unload ~/Library/LaunchAgents/com.Operator.mbdisplay.plist && rm ~/.mbdisplay_helper.sh ~/Library/LaunchAgents/com.Operator.mbdisplay.plist"
echo "------------------------------------"
'''


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/plain"):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/install":
            return self._send(200, INSTALLER)
        if u.path in ("/", "/health"):
            return self._send(200, "display_request_server ok")
        if u.path == "/resize":
            q = parse_qs(u.query)
            try:
                w = int(q.get("w", ["0"])[0])
                h = int(q.get("h", ["0"])[0])
                deg = int(q.get("deg", ["0"])[0])
            except ValueError:
                return self._send(400, "bad params")
            hidpi = q.get("lodpi", ["0"])[0] != "1"
            RUNTIME.mkdir(parents=True, exist_ok=True)
            if w <= 0 or h <= 0:
                try:
                    REQ.unlink()
                except FileNotFoundError:
                    pass
                return self._send(200, "cleared")
            REQ.write_text(json.dumps({"w": w, "h": h, "hidpi": hidpi, "degree": deg}))
            return self._send(200, f"ok {w}x{h} deg{deg} hidpi={hidpi}")
        return self._send(404, "nope")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
