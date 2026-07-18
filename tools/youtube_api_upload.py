#!/usr/bin/env python3
"""youtube_api_upload.py — reliable YouTube upload via the Data API v3 (no Studio DOM).

The CDP/Studio uploader is provably broken (shadow-DOM, 0/77). This uploads the rendered
.mp4 straight through the documented resumable Data API using a per-brand refresh token
(YT_REFRESH_TOKEN_<BRAND>, minted once by scripts/youtube_auth.py). No IT, owner's own
account, durable. Returns the live video URL as JSON.

    /opt/homebrew/bin/python3 tools/youtube_api_upload.py --brand redacted \
        --video data/hustle/ima_reels/.../reel.mp4 --title "..." --desc "..." --privacy unlisted

Privacy default = unlisted (safe: nothing goes public-by-surprise; flip to public when sure).
"""
import argparse, json, os, sys
from pathlib import Path
from dotenv import load_dotenv
import google.auth.transport.requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _creds(brand: str) -> Credentials:
    key = f"YT_REFRESH_TOKEN_{brand.upper()}"
    rt = os.environ.get(key)
    if not rt:
        sys.exit(json.dumps({"ok": False, "error": f"{key} missing — run: python3 scripts/youtube_auth.py {brand}"}))
    c = Credentials(
        None, refresh_token=rt,
        client_id=os.environ.get("YT_CLIENT_ID") or os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ.get("YT_CLIENT_SECRET") or os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.upload",
                "https://www.googleapis.com/auth/youtube.readonly"])
    c.refresh(google.auth.transport.requests.Request())
    return c


def upload_video(brand: str, video: str, title: str, desc: str = "",
                 tags="", privacy: str = "unlisted") -> dict:
    """Upload one .mp4 via the Data API v3 with the brand's refresh token. Returns a
    JSON-able dict (never raises). Reusable by the autonomous queue runner."""
    vid = Path(video)
    if not vid.is_file():
        return {"ok": False, "error": f"video not found: {vid}"}
    tag_list = ([t.strip() for t in tags.split(",")] if isinstance(tags, str)
                else [str(t).strip() for t in (tags or [])])
    tag_list = [t for t in tag_list if t][:15]
    try:
        yt = build("youtube", "v3", credentials=_creds(brand), cache_discovery=False)
        # confirm which channel this token controls (channel isolation)
        ch = yt.channels().list(part="snippet", mine=True).execute()
        chan = (ch.get("items") or [{}])[0].get("snippet", {}).get("title", "?")
        body = {
            "snippet": {"title": title[:100], "description": desc[:4900],
                        "tags": tag_list, "categoryId": "22"},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        }
        media = MediaFileUpload(str(vid), chunksize=-1, resumable=True, mimetype="video/*")
        req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
        resp = req.execute()
        vid_id = resp.get("id")
        return {"ok": True, "channel": chan, "video_id": vid_id,
                "url": f"https://youtu.be/{vid_id}", "privacy": privacy}
    except HttpError as e:
        msg = str(e)
        hint = ""
        if "has not been used" in msg or "accessNotConfigured" in msg or "API v3 has not" in msg:
            hint = " → enable 'YouTube Data API v3' on the Cloud project (console.cloud.google.com/apis/library/youtube.googleapis.com), then retry"
        elif "quotaExceeded" in msg:
            hint = " → daily upload quota hit (Data API default ~6 uploads/day); retry tomorrow or request more quota"
        return {"ok": False, "error": msg[:300] + hint}
    except SystemExit as e:  # _creds() calls sys.exit with a JSON error string
        try:
            return json.loads(str(e.code))
        except Exception:
            return {"ok": False, "error": str(e.code)[:300]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", required=True)
    ap.add_argument("--video", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--desc", default="")
    ap.add_argument("--tags", default="")
    ap.add_argument("--privacy", default="unlisted", choices=["private", "unlisted", "public"])
    a = ap.parse_args()

    res = upload_video(a.brand, a.video, a.title, a.desc, a.tags, a.privacy)
    print(json.dumps(res))
    if not res.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
