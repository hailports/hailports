#!/usr/bin/env python3
"""One-time Microsoft Graph device-code sign-in (READ-ONLY mail scope) so the backend
can fetch email attachment contents that New Outlook does not keep on local disk.

Prints the sign-in URL + code, then waits for the user to complete sign-in and stores
the refresh token via token_store (set-and-forget). Read-only: Mail.Read only.
"""
import asyncio
import sys
import time

sys.path.insert(0, "/home/user/claude-stack")
from dotenv import load_dotenv
load_dotenv("/home/user/claude-stack/.env")

import httpx
from auth.token_store import save

CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"  # Microsoft Graph Command Line Tools (Microsoft first-party public client)
SCOPE = "Mail.Read offline_access"
BASE = "https://login.microsoftonline.com/common/oauth2/v2.0"


async def main():
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{BASE}/devicecode", data={"client_id": CLIENT_ID, "scope": SCOPE})
        d = r.json()
        if "device_code" not in d:
            print("DEVICECODE_ERROR:", d.get("error_description") or d, flush=True)
            return
        print("SIGNIN_URL:", d["verification_uri"], flush=True)
        print("CODE:", d["user_code"], flush=True)
        print("(waiting for sign-in — this process will store the token when you finish)", flush=True)
        dc = d["device_code"]
        interval = int(d.get("interval", 5))
        deadline = time.time() + int(d.get("expires_in", 900))
        while time.time() < deadline:
            await asyncio.sleep(interval)
            t = await c.post(f"{BASE}/token", data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": CLIENT_ID, "device_code": dc})
            td = t.json()
            if "access_token" in td:
                save("microsoft", td)
                print("AUTH_SUCCESS: Microsoft Graph token stored (Mail.Read).", flush=True)
                return
            err = td.get("error")
            if err == "slow_down":
                interval += 5
            elif err != "authorization_pending":
                print("AUTH_FAILED:", td.get("error_description") or err, flush=True)
                return
        print("AUTH_TIMEOUT — re-run to retry.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
