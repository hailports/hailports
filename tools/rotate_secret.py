#!/usr/bin/env python3
"""One-command secret rotation: wire a new key everywhere + restart consumers + verify.

The only manual step left for you is generating the key at the provider (Stripe/CF
don't allow programmatic key creation). Drop new keys into ~/.key-drop as KEY=VALUE
lines (chmod 600) — NEVER paste secrets into chat — then run this. It:
  1. backs up .env + data/integrity/env.good
  2. rewrites the value in .env, env.good, and any tracked file holding the old literal
  3. restarts the mapped consumer services
  4. verifies new value is in place + (for known providers) that it's live
  5. shreds the ~/.key-drop line it consumed

Usage:
  python tools/rotate_secret.py --drop            # process every line in ~/.key-drop
  python tools/rotate_secret.py KEY --value VAL    # rotate one key explicitly
  python tools/rotate_secret.py --audit            # liveness-check all sensitive keys
"""
import argparse, os, re, subprocess, sys, urllib.request, base64, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
ENVGOOD = ROOT / "data" / "integrity" / "env.good"
DROP = Path.home() / ".key-drop"

# which launchd services to bounce when a given key changes
CONSUMERS = {
    "STRIPE_SECRET_KEY": ["self-serve", "stripe-fulfillment"],
    "STRIPE_WEBHOOK_SECRET": ["self-serve", "stripe-fulfillment"],
    "CF_API_TOKEN": [],            # used by agents on-demand; no long-running daemon to bounce
    "LLM_API_ADMIN_KEY": ["llm-api"],
    "GUMROAD_ACCESS_TOKEN": [],
}

def _uid(): return str(os.getuid())

def _read(p): return p.read_text() if p.exists() else ""

def _write_env(p, text):
    had = p.exists()
    mode = p.stat().st_mode & 0o777 if had else 0o400
    if had: os.chmod(p, 0o600)
    p.write_text(text)
    os.chmod(p, mode if had else 0o400)

def cur_value(key):
    m = re.search(rf"^{re.escape(key)}=(.*)$", _read(ENV), re.M)
    return m.group(1).strip().strip('"').strip("'") if m else ""

def live_check(key, val):
    try:
        if key == "STRIPE_SECRET_KEY":
            req = urllib.request.Request("https://api.stripe.com/v1/balance",
                headers={"Authorization": "Basic " + base64.b64encode(f"{val}:".encode()).decode()})
            return urllib.request.urlopen(req, timeout=10).status == 200
        if key == "CF_API_TOKEN":
            req = urllib.request.Request("https://api.cloudflare.com/client/v4/user/tokens/verify",
                headers={"Authorization": f"Bearer {val}"})
            return json.loads(urllib.request.urlopen(req, timeout=10).read()).get("success")
    except Exception as e:
        return f"check-failed:{type(e).__name__}"
    return "no-check"

def rotate(key, new):
    old = cur_value(key)
    if not old:
        print(f"  ! {key} not found in .env — adding fresh"); old = None
    # backup
    for p in (ENV, ENVGOOD):
        if p.exists():
            (p.parent / (p.name + ".bak-rotate")).write_text(_read(p))
    changed = []
    # .env + env.good: replace the KEY= line
    for p in (ENV, ENVGOOD):
        t = _read(p)
        if not t: continue
        nt = re.sub(rf"^{re.escape(key)}=.*$", f"{key}={new}", t, flags=re.M)
        if key not in t: nt = t + (f"\n{key}={new}\n" if not t.endswith("\n") else f"{key}={new}\n")
        if nt != t:
            _write_env(p, nt); changed.append(p.name)
    # live launchd plists live outside the repo (not git-tracked) — update them too
    if old:
        la = Path.home()/"Library"/"LaunchAgents"
        for pl in la.glob("com.claude-stack.*.plist"):
            tt = pl.read_text()
            if old in tt:
                pl.write_text(tt.replace(old, new)); changed.append(pl.name)
    # any tracked file holding the OLD literal value
    if old:
        for f in subprocess.run(["git","-C",str(ROOT),"grep","-Il","--",old],
                                capture_output=True,text=True).stdout.split():
            fp = ROOT / f
            if fp.exists() and "env" not in fp.name:
                fp.write_text(fp.read_text().replace(old, new)); changed.append(f)
    print(f"  rewired in: {', '.join(changed) or '(nothing)'}")
    # restart consumers
    for svc in CONSUMERS.get(key, []):
        subprocess.run(["launchctl","kickstart","-k",f"gui/{_uid()}/com.claude-stack.{svc}"],
                       capture_output=True)
        print(f"  restarted com.claude-stack.{svc}")
    # verify
    print(f"  new value live: {live_check(key, new)}")
    if old: print(f"  old value now: {live_check(key, old)} (want False/401 if provider-revoked)")

LEAK_PATHS = ["mini_env_edit.txt", "agents/stack_engineer.env", "frontdoor/.frontdoor.env"]

def _history_blob_values():
    """Values committed in known secret files (for leak cross-check). Targets the
    known leak paths directly — scanning all objects times out on a multi-GB repo."""
    vals = set()
    try:
        for path in LEAK_PATHS:
            shas = subprocess.run(["git","-C",str(ROOT),"log","--all","--format=%H","--",path],
                                  capture_output=True,text=True,timeout=60).stdout.split()
            for sha in set(shas):
                blob = subprocess.run(["git","-C",str(ROOT),"show",f"{sha}:{path}"],
                                      capture_output=True,text=True).stdout
                for mm in re.finditer(r"[:=]\s*['\"]?([^\s'\"]{12,})", blob):
                    vals.add(mm.group(1))
    except Exception:
        pass
    return vals

def audit():
    leaked = _history_blob_values()
    print("  KEY                         live      in-git-history?")
    for m in re.finditer(r"^([A-Z][A-Z0-9_]*)=(.+)$", _read(ENV), re.M):
        k, v = m.group(1), m.group(2).strip().strip('"').strip("'")
        if not re.search(r"KEY|SECRET|TOKEN|PASS|PASSWORD", k): continue
        if len(v) < 12: continue
        in_hist = "LEAKED" if v in leaked else "clean"
        live = live_check(k, v) if re.search(r"STRIPE_SECRET|CF_API_TOKEN", k) else "-"
        flag = "  <-- ROTATE" if in_hist == "LEAKED" else ""
        print(f"  {k:26}  {str(live):8}  {in_hist}{flag}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("key", nargs="?")
    ap.add_argument("--value"); ap.add_argument("--drop", action="store_true"); ap.add_argument("--audit", action="store_true")
    a = ap.parse_args()
    if a.audit: return audit()
    if a.drop:
        if not DROP.exists(): sys.exit("no ~/.key-drop file")
        lines = DROP.read_text().splitlines()
        for ln in lines:
            if "=" in ln and not ln.strip().startswith("#"):
                k, val = ln.split("=", 1); print(f"rotating {k.strip()}:"); rotate(k.strip(), val.strip())
        DROP.write_text(""); print("cleared ~/.key-drop")
        return
    if a.key and a.value: print(f"rotating {a.key}:"); rotate(a.key, a.value)
    else: ap.print_help()

if __name__ == "__main__":
    main()
