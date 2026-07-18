"""Google Drive via REST API v3.

READ methods need drive.readonly. WRITE/SHARE methods (upload/update/share_link)
require the token to carry the `drive.file` (or `drive`) scope + you re-consenting
via the Google OAuth flow — the code is inert until that grant exists.
"""

import json
import httpx
from tools.base import BaseTool, make_tool_def
from core.api_client import ensure_external_api_allowed


class GoogleDriveTool(BaseTool):
    name = "google_drive"
    description = "Google Drive — search, read, list, upload, update, share"

    def __init__(self, get_token):
        self._get_token = get_token

    async def _headers(self):
        ensure_external_api_allowed("Google Drive API")
        token = await self._get_token("google")
        return {"Authorization": f"Bearer {token}"}

    async def _get(self, path: str, params=None) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"https://www.googleapis.com/drive/v3{path}", headers=await self._headers(), params=params, timeout=30)
            r.raise_for_status()
            return r.json()

    async def _post(self, url: str, params=None, json_body=None) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.post(url, headers=await self._headers(), params=params, json=json_body, timeout=60)
            r.raise_for_status()
            return r.json() if r.content else {}

    async def _multipart(self, metadata: dict, body: bytes, body_mime: str, url: str, method: str = "POST") -> dict:
        """uploadType=multipart create/update — metadata part + media part."""
        boundary = "gdrive_boundary_7f3a"
        parts = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\nContent-Type: {body_mime}\r\n\r\n"
        ).encode() + body + f"\r\n--{boundary}--".encode()
        headers = await self._headers()
        headers["Content-Type"] = f"multipart/related; boundary={boundary}"
        async with httpx.AsyncClient() as c:
            r = await c.request(method, url, headers=headers, params={"uploadType": "multipart", "fields": "id,name,webViewLink"}, content=parts, timeout=120)
            r.raise_for_status()
            return r.json()

    def get_definitions(self) -> list:
        return [
            make_tool_def("gdrive_search", "Search Google Drive files.",
                          {"query": {"type": "string", "description": "Search query (file name or content)"}},
                          ["query"]),
            make_tool_def("gdrive_list_recent", "List recently modified files.",
                          {"count": {"type": "integer"}}, []),
            make_tool_def("gdrive_get_metadata", "Get file metadata.",
                          {"file_id": {"type": "string"}}, ["file_id"]),
            make_tool_def("gdrive_read_file", "Read text content of a Google Doc or text file.",
                          {"file_id": {"type": "string"}}, ["file_id"]),
            make_tool_def("gdrive_upload", "Upload local text/markdown as a new file. Set as_gdoc=true to convert to a native Google Doc.",
                          {"local_path": {"type": "string"}, "name": {"type": "string"},
                           "as_gdoc": {"type": "boolean"}, "folder_id": {"type": "string", "description": "optional parent folder"}},
                          ["local_path", "name"]),
            make_tool_def("gdrive_update", "Overwrite the body of an existing Drive file from a local file.",
                          {"file_id": {"type": "string"}, "local_path": {"type": "string"}}, ["file_id", "local_path"]),
            make_tool_def("gdrive_share_link", "Grant access and return a shareable link. Provide email for a specific person, or set anyone=true for a link-anyone reader. WRITE scope required.",
                          {"file_id": {"type": "string"}, "email": {"type": "string"},
                           "anyone": {"type": "boolean"}, "role": {"type": "string", "description": "reader|commenter|writer (default reader)"}},
                          ["file_id"]),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "gdrive_search":
            q = tool_input["query"].replace("'", "\\'")
            data = await self._get("/files", {"q": f"name contains '{q}' or fullText contains '{q}'", "pageSize": 10,
                                              "fields": "files(id,name,mimeType,modifiedTime,size)"})
            files = data.get("files", [])
            if not files:
                return "No files found."
            return "\n".join(f"{f['name']} ({f['mimeType']}) modified {f['modifiedTime'][:10]}\n  ID: {f['id']}" for f in files)

        elif tool_name == "gdrive_list_recent":
            count = tool_input.get("count", 10)
            data = await self._get("/files", {"pageSize": count, "orderBy": "modifiedTime desc",
                                              "fields": "files(id,name,mimeType,modifiedTime)"})
            files = data.get("files", [])
            return "\n".join(f"{f['name']} ({f['mimeType']}) — {f['modifiedTime'][:10]}" for f in files)

        elif tool_name == "gdrive_get_metadata":
            data = await self._get(f"/files/{tool_input['file_id']}", {"fields": "id,name,mimeType,modifiedTime,size,owners,permissions"})
            return str(data)

        elif tool_name == "gdrive_read_file":
            # Try exporting as text for Google Docs, otherwise download
            meta = await self._get(f"/files/{tool_input['file_id']}", {"fields": "mimeType"})
            mime = meta.get("mimeType", "")
            async with httpx.AsyncClient() as c:
                if "google-apps" in mime:
                    r = await c.get(f"https://www.googleapis.com/drive/v3/files/{tool_input['file_id']}/export",
                                    headers=await self._headers(), params={"mimeType": "text/plain"}, timeout=30)
                else:
                    r = await c.get(f"https://www.googleapis.com/drive/v3/files/{tool_input['file_id']}",
                                    headers=await self._headers(), params={"alt": "media"}, timeout=30)
                r.raise_for_status()
                return r.text[:5000]

        elif tool_name == "gdrive_upload":
            with open(tool_input["local_path"], "rb") as fh:
                body = fh.read()
            meta = {"name": tool_input["name"]}
            if tool_input.get("folder_id"):
                meta["parents"] = [tool_input["folder_id"]]
            if tool_input.get("as_gdoc"):
                meta["mimeType"] = "application/vnd.google-apps.document"
            data = await self._multipart(meta, body, "text/plain",
                                         "https://www.googleapis.com/upload/drive/v3/files")
            return f"Uploaded '{data.get('name')}'\n  ID: {data.get('id')}\n  Link: {data.get('webViewLink')}"

        elif tool_name == "gdrive_update":
            with open(tool_input["local_path"], "rb") as fh:
                body = fh.read()
            data = await self._multipart({}, body, "text/plain",
                                         f"https://www.googleapis.com/upload/drive/v3/files/{tool_input['file_id']}",
                                         method="PATCH")
            return f"Updated '{data.get('name')}' (ID: {data.get('id')})"

        elif tool_name == "gdrive_share_link":
            role = tool_input.get("role", "reader")
            fid = tool_input["file_id"]
            if tool_input.get("email"):
                perm = {"type": "user", "role": role, "emailAddress": tool_input["email"]}
            elif tool_input.get("anyone"):
                perm = {"type": "anyone", "role": role}
            else:
                return "Specify either email=<person> or anyone=true."
            await self._post(f"https://www.googleapis.com/drive/v3/files/{fid}/permissions", json_body=perm)
            meta = await self._get(f"/files/{fid}", {"fields": "name,webViewLink"})
            who = tool_input.get("email") or "anyone with the link"
            return f"Granted {role} to {who}.\n  {meta.get('name')}\n  Link: {meta.get('webViewLink')}"

        else:
            return f"Unknown gdrive tool: {tool_name}"
