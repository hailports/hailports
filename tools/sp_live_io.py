#!/usr/bin/env python3
"""GET/PUT Craig's live SharePoint workbook via the authenticated OWA CDP browser (:18811).

Usage: sp_live_io.py get <local_path>   |   sp_live_io.py put <local_path>
"""
import base64
import json
import sys
import time
import urllib.parse
import urllib.request

import websocket

CDP = "http://127.0.0.1:18811"
SITE = "https://CompanyA-my.sharepoint.com/personal/cb700244_na_redacted_com"
REL = "/personal/cb700244_na_redacted_com/Documents/___Ag/DPP/Items Identifed during Update Meeting 051926 - status 0605_UPDATED_Rich_Format_MATCHED_CB.xlsx"

def jget(path):
    with urllib.request.urlopen(CDP + path, timeout=10) as r:
        return json.load(r)

def jput(path):
    req = urllib.request.Request(CDP + path, method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)

class Cdp:
    def __init__(self, url):
        self.ws = websocket.create_connection(url, timeout=120, suppress_origin=True)
        self.n = 0

    def send(self, method, params=None, timeout=110):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                return msg
        raise TimeoutError(method)

def evaluate(page, js, timeout=100):
    r = page.send("Runtime.evaluate", {"expression": js, "awaitPromise": True, "returnByValue": True}, timeout=timeout)
    res = r.get("result", {})
    if "exceptionDetails" in res:
        raise RuntimeError(json.dumps(res["exceptionDetails"])[:400])
    return res.get("result", {}).get("value")

def file_api_url():
    return SITE + "/_api/web/GetFileByServerRelativePath(decodedurl='" + urllib.parse.quote(REL) + "')/$value"


def open_site_tab():
    # SCREEN-JACK GUARD: navigates CDP :18811, which collides with the visible Etsy
    # browser and hijacks that window. Disabled unless explicitly enabled.
    import os as _os
    if _os.environ.get("SP_WORKBOOK_LIVE") != "1":
        raise RuntimeError("sp_live_io disabled (screen-jack guard); set SP_WORKBOOK_LIVE=1 to enable")
    tab = jput("/json/new?" + SITE + "/_api/web?$select=Title")
    page = Cdp(tab["webSocketDebuggerUrl"])
    page.send("Page.enable")
    time.sleep(2)
    return tab, page

def close_tab(tab):
    try:
        jget("/json/close/" + tab["id"])
    except Exception:
        pass

def do_get(out_path):
    tab, page = open_site_tab()
    js = """
(async () => {
  const r = await fetch(%s, {credentials: "include", headers: {"Accept": "application/octet-stream"}});
  if (!r.ok) return "ERR:" + r.status + ":" + (await r.text()).slice(0, 200);
  const bytes = new Uint8Array(await r.arrayBuffer());
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return "B64:" + btoa(bin);
})()
""" % (json.dumps(file_api_url()),)
    val = evaluate(page, js)
    close_tab(tab)
    if isinstance(val, str) and val.startswith("B64:"):
        data = base64.b64decode(val[4:])
        if data[:2] == b"PK":
            open(out_path, "wb").write(data)
            print("SAVED", out_path, len(data), "bytes")
            return 0
        print("NOT_XLSX:", data[:120])
        return 2
    print("FAILED:", str(val)[:300])
    return 1

def do_put(local_path):
    data = open(local_path, "rb").read()
    assert data[:2] == b"PK", "not an xlsx"
    b64 = base64.b64encode(data).decode()
    tab, page = open_site_tab()
    js = """
(async () => {
  const dig = await fetch(%s + "/_api/contextinfo", {method: "POST", credentials: "include",
                          headers: {"Accept": "application/json;odata=verbose"}});
  if (!dig.ok) return "ERR_DIGEST:" + dig.status;
  const digest = (await dig.json()).d.GetContextWebInformation.FormDigestValue;
  const b64 = %s;
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const r = await fetch(%s, {method: "POST", credentials: "include", body: bytes,
                         headers: {"X-HTTP-Method": "PUT", "X-RequestDigest": digest,
                                   "Accept": "application/json;odata=verbose"}});
  return "STATUS:" + r.status + ":" + (r.ok ? "OK" : (await r.text()).slice(0, 300));
})()
""" % (json.dumps(SITE), json.dumps(b64), json.dumps(file_api_url()))
    val = evaluate(page, js, timeout=110)
    close_tab(tab)
    print(val)
    return 0 if isinstance(val, str) and (":200:" in val or ":204:" in val) else 1

if __name__ == "__main__":
    mode, path = sys.argv[1], sys.argv[2]
    sys.exit(do_get(path) if mode == "get" else do_put(path))
