#!/usr/bin/env python3
"""yt_reauth.py — one-time YouTube re-consent for the HailPorts channel (build-in-public stream).

Upgrades the OAuth grant to include the live-streaming scope (youtube.force-ssl) so the
stack can open/keep a 24/7 live broadcast. Runs a loopback consent flow: it opens a Google
consent URL, you sign in and SELECT THE HAILPORTS CHANNEL (not AI Built Fast — that would
cross-link brands) + click Allow, and it stores the new refresh token to
data/hustle/yt_live_token.json (so .env never has to be touched).

  RUN IT YOURSELF (interactive):  .venv/bin/python tools/yt_reauth.py
  Open the printed URL in a browser ON this mac (loopback catch runs here).
"""
from __future__ import annotations
import json, os, sys, urllib.parse, urllib.request, urllib.error, webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(os.path.expanduser("~/claude-stack"))
for ln in (ROOT / ".env").read_text().splitlines():
    if "=" in ln and not ln.startswith("#"):
        k, _, v = ln.partition("="); os.environ.setdefault(k.strip(), v.strip().strip('"'))
CID = os.environ["YT_CLIENT_ID"]; CS = os.environ["YT_CLIENT_SECRET"]
SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
PORT = 18899  # must be a redirect_uri REGISTERED on the OAuth client (8765 is not → token exchange 400s)
REDIRECT = f"http://localhost:{PORT}/"
OUT = ROOT / "data" / "hustle" / "yt_live_token.json"

_code = {}
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.urlparse(self.path).query
        _code.update(urllib.parse.parse_qs(q))
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
        ok = "code" in _code
        self.wfile.write(b"<h2>" + (b"authorized -- you can close this tab" if ok
                                    else b"no code received") + b"</h2>")
    def log_message(self, *a): pass


def main():
    auth = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": CID, "redirect_uri": REDIRECT, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "select_account consent"})
    print("\n1) at the link below, pick the HAILPORTS channel (NOT AI Built Fast) + click Allow:\n\n" + auth + "\n")
    # Only auto-open the browser for a human at a real terminal (TTY). Any background /
    # agent / launchd run just prints the URL above and never steals the screen. Force
    # off entirely with YT_REAUTH_NO_BROWSER=1.
    _remote = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY") or os.environ.get("SSH_CLIENT"))
    if sys.stdout.isatty() and not _remote and os.environ.get("YT_REAUTH_NO_BROWSER") != "1":
        try: webbrowser.open(auth)
        except Exception: pass
    srv = HTTPServer(("localhost", PORT), H)
    print(f"2) waiting for the redirect on {REDIRECT} ...")
    while "code" not in _code:
        srv.handle_request()
    code = _code["code"][0]
    d = urllib.parse.urlencode({"client_id": CID, "client_secret": CS, "code": code,
                                "redirect_uri": REDIRECT, "grant_type": "authorization_code"}).encode()
    try:
        tok = json.loads(urllib.request.urlopen(
            urllib.request.Request("https://oauth2.googleapis.com/token", data=d), timeout=30).read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(f"TOKEN EXCHANGE FAILED {e.code}: {body}")
        return 1
    if "refresh_token" not in tok:
        print("ERROR: no refresh_token returned:", tok); return 1
    OUT.write_text(json.dumps({"refresh_token": tok["refresh_token"], "scope": tok.get("scope")}, indent=2))
    print(f"\n✅ stored live-scoped token -> {OUT}\nnow tell Claude: 'token done' and it'll go live.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
