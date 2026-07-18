"""OneDrive / SharePoint write + share via Microsoft Graph.

Mirrors GoogleDriveTool's DI: constructed with a `get_token` callable that returns a
Microsoft Graph bearer token. Inert until you complete `onedrive_signin.py` (write
scopes) — the existing Mail.Read token cannot upload or share.

LANE NOTE: the locally-mounted OneDrive is CompanyA corporate (work lane). Uploading
money-lane (BrandA/hustle) content here crosses Hard Rail #4. Pass an explicit
`drive` target and keep the air-gap deliberate.
"""

import httpx
from tools.base import BaseTool, make_tool_def
from core.api_client import ensure_external_api_allowed

GRAPH = "https://graph.microsoft.com/v1.0"


class OneDriveTool(BaseTool):
    name = "onedrive"
    description = "OneDrive/SharePoint — upload, overwrite, and create share links via Graph"

    def __init__(self, get_token):
        self._get_token = get_token

    async def _headers(self):
        ensure_external_api_allowed("Microsoft Graph API")
        token = await self._get_token("microsoft")
        return {"Authorization": f"Bearer {token}"}

    def _drive_root(self, drive_id: str | None) -> str:
        # default = the signed-in user's personal OneDrive; else a specific drive (SharePoint lib)
        return f"/drives/{drive_id}" if drive_id else "/me/drive"

    def get_definitions(self) -> list:
        return [
            make_tool_def("onedrive_upload",
                          "Upload a local file to OneDrive/SharePoint at dest_path (e.g. 'BrandA/revops_copy.md'). Overwrites if it exists.",
                          {"local_path": {"type": "string"}, "dest_path": {"type": "string"},
                           "drive_id": {"type": "string", "description": "optional SharePoint/library drive id; omit for personal OneDrive"}},
                          ["local_path", "dest_path"]),
            make_tool_def("onedrive_share_link",
                          "Create a share link for an item. email = invite a specific person (recommended for external); anyone=true = link-anyone. Returns the URL.",
                          {"item_path": {"type": "string"}, "email": {"type": "string"},
                           "anyone": {"type": "boolean"}, "can_edit": {"type": "boolean"},
                           "drive_id": {"type": "string"}},
                          ["item_path"]),
            make_tool_def("onedrive_list_drives",
                          "List the drives you can write to (personal OneDrive + SharePoint libraries) with their ids.",
                          {}, []),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        base = self._drive_root(tool_input.get("drive_id"))

        if tool_name == "onedrive_list_drives":
            async with httpx.AsyncClient() as c:
                me = await c.get(f"{GRAPH}/me/drive", headers=await self._headers(), timeout=30)
                me.raise_for_status()
                d = me.json()
                sites = await c.get(f"{GRAPH}/me/followedSites", headers=await self._headers(), timeout=30)
            lines = [f"personal OneDrive — id: {d.get('id')}"]
            if sites.status_code == 200:
                for s in sites.json().get("value", []):
                    lines.append(f"site: {s.get('displayName')} — {s.get('webUrl')}")
            return "\n".join(lines)

        elif tool_name == "onedrive_upload":
            with open(tool_input["local_path"], "rb") as fh:
                body = fh.read()
            path = tool_input["dest_path"].lstrip("/")
            url = f"{GRAPH}{base}/root:/{path}:/content"
            async with httpx.AsyncClient() as c:
                r = await c.put(url, headers=await self._headers(), content=body, timeout=120)
                r.raise_for_status()
                item = r.json()
            return f"Uploaded to OneDrive: {item.get('name')}\n  item id: {item.get('id')}\n  webUrl: {item.get('webUrl')}"

        elif tool_name == "onedrive_share_link":
            path = tool_input["item_path"].lstrip("/")
            item_url = f"{GRAPH}{base}/root:/{path}:"
            async with httpx.AsyncClient() as c:
                meta = await c.get(item_url, headers=await self._headers(), timeout=30)
                meta.raise_for_status()
                item_id = meta.json()["id"]
                if tool_input.get("email"):
                    r = await c.post(f"{GRAPH}{base}/items/{item_id}/invite",
                                     headers=await self._headers(), timeout=30,
                                     json={"recipients": [{"email": tool_input["email"]}],
                                           "requireSignIn": True, "sendInvitation": True,
                                           "roles": ["write" if tool_input.get("can_edit") else "read"],
                                           "message": "Shared via claude-stack."})
                    r.raise_for_status()
                    val = r.json().get("value", [{}])[0]
                    link = val.get("link", {}).get("webUrl", "(invitation sent; link visible in recipient's mail)")
                    return f"Invited {tool_input['email']} ({'edit' if tool_input.get('can_edit') else 'read'}).\n  Link: {link}"
                else:
                    r = await c.post(f"{GRAPH}{base}/items/{item_id}/createLink",
                                     headers=await self._headers(), timeout=30,
                                     json={"type": "edit" if tool_input.get("can_edit") else "view",
                                           "scope": "anonymous" if tool_input.get("anyone") else "organization"})
                    r.raise_for_status()
                    return f"Share link: {r.json().get('link', {}).get('webUrl')}"

        else:
            return f"Unknown onedrive tool: {tool_name}"
