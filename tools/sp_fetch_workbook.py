#!/usr/bin/env python3
"""Fetch Craig's SharePoint master workbook through the authenticated OWA CDP browser (:18811).

Strategy: real tab navigation to the share link with download=1 (Chrome handles the
SSO/share-redemption redirects), with downloads forced into a scratch dir we watch.
"""
import glob
import json
import os
import shutil
import sys
import time
import urllib.request

import websocket

# SCREEN-JACK GUARD: module-level Page.navigate against CDP :18811 collides with the
# visible Etsy case-study browser and hijacks that window onto the SharePoint workbook.
# Disabled unless explicitly enabled. (Was popping the workbook in Operator's face on a loop.)
if os.environ.get("SP_WORKBOOK_LIVE") != "1":
    print("sp_fetch_workbook disabled (screen-jack guard). set SP_WORKBOOK_LIVE=1 to enable.")
    sys.exit(0)

CDP = "http://127.0.0.1:18811"
SHARE_URL = ("https://CompanyA-my.sharepoint.com/:x:/r/personal/cb700244_na_redacted_com/Documents/___Ag/DPP/"
             "Items%20Identifed%20during%20Update%20Meeting%20051926%20-%20status%200605_UPDATED_Rich_Format_MATCHED_CB.xlsx"
             "?d=w7452d50a0fa34d0c981283ddeb805207&e=4%3aec2d4ddc1eeb4de68ce94674db5e8081&sharingv2=true&fromShare=true&at=9"
             "&download=1")
DL_DIR = "/tmp/spdl"
OUT = "/home/user/Downloads.internal/CB_MASTER_0605_UPDATED_Rich_Format_MATCHED_CB.xlsx"

os.makedirs(DL_DIR, exist_ok=True)
for f in glob.glob(DL_DIR + "/*"):
    os.remove(f)

def jget(path):
    with urllib.request.urlopen(CDP + path, timeout=10) as r:
        return json.load(r)

def jput(path):
    req = urllib.request.Request(CDP + path, method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)

class Cdp:
    def __init__(self, url):
        self.ws = websocket.create_connection(url, timeout=60, suppress_origin=True)
        self.n = 0

    def send(self, method, params=None, timeout=45):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                return msg
        raise TimeoutError(method)

ver = jget("/json/version")
browser = Cdp(ver["webSocketDebuggerUrl"])
browser.send("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": DL_DIR, "eventsEnabled": True})

tab = jput("/json/new?about:blank")
page = Cdp(tab["webSocketDebuggerUrl"])
page.send("Page.enable")
page.send("Page.navigate", {"url": SHARE_URL})

deadline = time.time() + 75
found = None
while time.time() < deadline:
    done = [f for f in glob.glob(DL_DIR + "/*") if not f.endswith(".crdownload")]
    if done:
        found = done[0]
        break
    time.sleep(1.5)

title = ""
try:
    r = page.send("Runtime.evaluate", {"expression": "document.title + ' :: ' + location.href.slice(0,140)", "returnByValue": True}, timeout=10)
    title = r.get("result", {}).get("result", {}).get("value", "")
except Exception:
    pass

try:
    jget("/json/close/" + tab["id"])
except Exception:
    pass

if found and open(found, "rb").read(2) == b"PK":
    shutil.copy(found, OUT)
    print("SAVED", OUT, os.path.getsize(OUT), "bytes")
    sys.exit(0)
print("FAILED. page state:", title)
if found:
    print("non-xlsx download:", found, open(found, "rb").read(60))
sys.exit(1)
