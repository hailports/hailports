#!/usr/bin/env python3
"""READ-ONLY Salesforce prod query helper for sprint-ticket analysis.

Usage:
  python3 /tmp/sprint3/sfq.py soql "SELECT ..."          # REST data query
  python3 /tmp/sprint3/sfq.py tooling "SELECT ..."       # Tooling API query
  python3 /tmp/sprint3/sfq.py describe ObjectName        # object describe (fields)
  python3 /tmp/sprint3/sfq.py get /services/data/v62.0/... # arbitrary GET path
Only GET requests are possible — no writes can be made through this script.
"""
import asyncio, sys, json, urllib.request, urllib.parse, urllib.error

sys.path.insert(0, "/home/user/claude-stack")
from dotenv import load_dotenv
load_dotenv("/home/user/claude-stack/.env")
from auth.oauth_manager import OAuthManager

API = "v62.0"

async def main():
    mode, arg = sys.argv[1], sys.argv[2]
    t = await OAuthManager().get_token("salesforce")
    tok, inst = t["access_token"], t["instance_url"]
    if mode == "soql":
        path = f"/services/data/{API}/query?q={urllib.parse.quote(arg)}"
    elif mode == "tooling":
        path = f"/services/data/{API}/tooling/query?q={urllib.parse.quote(arg)}"
    elif mode == "describe":
        path = f"/services/data/{API}/sobjects/{arg}/describe"
    elif mode == "get":
        path = arg
    else:
        sys.exit(f"unknown mode {mode}")
    url = inst + path
    out = []
    while True:
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
            d = json.load(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as e:
            print(json.dumps({"error": e.code, "body": e.read().decode()[:1000]}))
            return
        if isinstance(d, dict) and "records" in d:
            out.extend(d["records"])
            nxt = d.get("nextRecordsUrl")
            if nxt and len(out) < 2000:
                url = inst + nxt
                continue
            print(json.dumps({"totalSize": d.get("totalSize"), "records": out}, default=str))
        else:
            print(json.dumps(d, default=str))
        return

asyncio.run(main())
