"""GitHub reply DRAFTING + posting for the work-GPT gateway.

github_suggest_replies (READ + local-LLM draft; NO write): pull a teammate's recent PR comments (or a
specific PR's), gather ALL available context — the diff hunk the comment sits on, the PR title/body, and
related SF/email/Monday/Zoom context from the work-context index — and draft a suggested reply in Operator's
voice. Nothing is posted; Operator reviews the suggestions.

github_post_reply (GATED outward write on a CompanyA work surface): post a reply to a PR review comment
(in_reply_to) or a PR conversation. Two-call: preview unless explicit_approval=true. Reads as Operator — his
voice, no AI fingerprint (the drafts already are; the body is Operator-reviewed before it posts).
"""
from __future__ import annotations

import re

import httpx

from tools.base import BaseTool, make_tool_def
from core.api_client import ensure_external_api_allowed
from tools.github_read import _read_token, API, DEFAULT_REPO, _REPO_RE

_ALEX_VOICE_SYS = (
    "You are drafting a GitHub PR-comment reply AS Operator (github handle redacted_redacted) to a CompanyA "
    "teammate. VOICE: all-lowercase except proper names and the word 'I'; plain, direct, concise; "
    "shorthand ok (w/, lmk, b/c, +); hyphens not em-dashes; no corporate filler, no AI tells. ALWAYS: "
    "(A) if the teammate is questioning or pushing back on Operator's change/approach, PROTECT his stance — "
    "hold the position and back it with the reasoning from the PR/code/context, don't just cave or defer; "
    "(B) stay friendly, polite + professional; (C) when not fully certain, engage in SOLUTIONING — "
    "propose a concrete next step / option / path forward and invite their input, rather than a bare "
    "'i'll check'. Use the PR, the code hunk, and the work context provided; write AS Operator who knows this "
    "codebase (NEVER reference 'the context' / 'the PR description' / being given info). NEVER invent a "
    "hard fact you don't have (sandbox state, deploy status) — instead solution around it ('if it's in "
    "full + passing we're good to merge; lemme confirm that'). 1-3 sentences. Output ONLY the reply text."
)


def _author_match(author: str, login: str) -> bool:
    a = (author or "").lower().strip()
    lo = (login or "").lower()
    if not a or a in lo:
        return True
    parts = a.split()
    # CompanyA handles are <first-initial><last-initial><digits>_redacted — map a full name
    return len(parts) >= 2 and lo.startswith(parts[0][0] + parts[-1][0])


class GithubReplyTool(BaseTool):
    name = "github_reply"
    description = "Draft (and, gated, post) replies to GitHub PR comments in Operator's voice."

    def __init__(self):
        self._token = _read_token()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "CompanyA-work-gpt",
        }

    async def _get(self, path: str, params: dict | None = None):
        if not self._token:
            raise RuntimeError("no GitHub token configured.")
        ensure_external_api_allowed("GitHub API")
        async with httpx.AsyncClient() as c:
            r = await c.get(API + path, headers=self._headers(), params=params or {}, timeout=30)
        if r.status_code == 404:
            raise RuntimeError(f"GitHub 404 for {path} (repo/number wrong, or the token can't see it).")
        r.raise_for_status()
        return r.json()

    async def _post_api(self, path: str, payload: dict):
        ensure_external_api_allowed("GitHub API")
        async with httpx.AsyncClient() as c:
            r = await c.post(API + path, headers=self._headers(), json=payload, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"GitHub {r.status_code} for POST {path}: {r.text[:200]}")
        return r.json()

    def get_definitions(self) -> list:
        return [
            make_tool_def(
                "github_suggest_replies",
                "AUTO-SUGGEST replies to GitHub PR comments in Operator's voice, using all available context "
                "(the PR, the code the comment is on, related SF/email/Monday context). READ + draft only "
                "— nothing is posted. Filter by author (a teammate's name/handle) or a specific PR number. "
                f"repo defaults to {DEFAULT_REPO}.",
                {
                    "repo": {"type": "string", "description": f"owner/repo (default {DEFAULT_REPO})"},
                    "author": {"type": "string", "description": "teammate name ('Cinthya Flores') or GitHub handle — suggests replies to THEIR recent comments"},
                    "number": {"type": "integer", "description": "a specific PR number (suggest replies to all its review comments)"},
                    "limit": {"type": "integer", "description": "max comments to draft for (default 6, max 15)"},
                },
                [],
            ),
            make_tool_def(
                "github_post_reply",
                "POST a reply to a GitHub PR comment (GATED — two-call). Preview unless explicit_approval=true. "
                "Reply to a review comment via comment_id, or a PR conversation via number.",
                {
                    "repo": {"type": "string", "description": f"owner/repo (default {DEFAULT_REPO})"},
                    "comment_id": {"type": "integer", "description": "the review comment id to reply under (from github_suggest_replies)"},
                    "number": {"type": "integer", "description": "PR number (posts a conversation comment, if no comment_id)"},
                    "body": {"type": "string", "description": "the reply text (Operator's voice)"},
                    "explicit_approval": {"type": "boolean", "description": "true = actually post; absent/false = preview only"},
                },
                ["body"],
            ),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        ti = tool_input if isinstance(tool_input, dict) else {}
        repo = str(ti.get("repo") or DEFAULT_REPO).strip().rstrip("/")
        if repo.startswith(("http://", "https://")):
            repo = repo.split("github.com/", 1)[-1]
        if not _REPO_RE.match(repo):
            return f"ERROR: repo must be 'owner/name' (got {repo!r})."
        number = ti.get("number")
        try:
            number = int(number) if number not in (None, "") else None
        except (TypeError, ValueError):
            number = None
        if tool_name == "github_suggest_replies":
            try:
                limit = max(1, min(int(ti.get("limit") or 6), 15))
            except (TypeError, ValueError):
                limit = 6
            return await self._suggest(repo, str(ti.get("author") or "").strip(), number, limit)
        if tool_name == "github_post_reply":
            cid = ti.get("comment_id")
            try:
                cid = int(cid) if cid not in (None, "") else None
            except (TypeError, ValueError):
                cid = None
            return await self._post(repo, cid, number, str(ti.get("body") or ""),
                                    bool(ti.get("explicit_approval")))
        return f"Unknown github reply tool: {tool_name}"

    async def _draft(self, repo, pr_num, pr_title, pr_body, path, hunk, comment_body, wc_txt) -> str:
        from core import local_client
        prompt = (f"PR #{pr_num} — {pr_title}\n"
                  + (f"PR description:\n{pr_body}\n" if pr_body else "")
                  + (f"\nCode the comment is on ({path}):\n{hunk}\n" if hunk else "")
                  + (f"\nRelated work context (SF tickets / email / Monday):\n{wc_txt}\n" if wc_txt else "")
                  + f"\nTeammate's comment to reply to:\n{comment_body}\n\nAlex's reply:")
        try:
            txt = await local_client.generate(prompt=prompt, system=_ALEX_VOICE_SYS,
                                              max_tokens=280, temperature=0.3)
        except Exception as e:
            return f"(couldn't draft — {str(e)[:120]})"
        return (txt or "").strip().strip('"').strip()

    async def _suggest(self, repo, author, number, limit) -> str:
        if number:
            data = await self._get(f"/repos/{repo}/pulls/{number}/comments", {"per_page": 100})
            comments = data if isinstance(data, list) else []
        else:
            raw = await self._get(f"/repos/{repo}/pulls/comments",
                                  {"sort": "created", "direction": "desc", "per_page": 100})
            raw = raw if isinstance(raw, list) else []
            comments = [c for c in raw if _author_match(author, (c.get("user") or {}).get("login") or "")]
        if not comments:
            who = f" from '{author}'" if author else ""
            return f"No PR comments{who} to suggest replies for on {repo}."
        comments = comments[:limit]
        try:
            from tools import work_context_index as _wci
        except Exception:
            _wci = None
        out = []
        for c in comments:
            pr_url = c.get("pull_request_url") or ""
            m = re.search(r"/pulls/(\d+)", pr_url)
            pr_num = m.group(1) if m else (str(number) if number else "?")
            pr_title, pr_body = "", ""
            try:
                pr = await self._get(f"/repos/{repo}/pulls/{pr_num}")
                if isinstance(pr, dict):
                    pr_title = pr.get("title") or ""
                    pr_body = (pr.get("body") or "")[:700]
            except Exception:
                pass
            body = (c.get("body") or "").strip()
            hunk = (c.get("diff_hunk") or "")[-1000:]
            wc_txt = ""
            if _wci:
                try:
                    hits = _wci.search(f"{pr_title} {body}", limit=4) or []
                    wc_txt = "\n".join(
                        f"- [{h.get('source')}] {str(h.get('title') or h.get('snippet') or h.get('body') or '')[:150]}"
                        for h in hits[:4])
                except Exception:
                    pass
            draft = await self._draft(repo, pr_num, pr_title, pr_body, c.get("path"), hunk, body, wc_txt)
            out.append((pr_num, c.get("path"), c.get("id"), body, draft))
        lines = [f"Suggested replies on {repo}" + (f" for @{author}" if author else "") + f" ({len(out)}):\n"]
        for pr_num, path, cid, body, draft in out:
            lines.append(f"── PR #{pr_num}  ({path})")
            lines.append(f"   THEY SAID: {body[:220]}")
            lines.append(f"   SUGGESTED REPLY: {draft}")
            lines.append(f"   → to post: github_post_reply {{repo:'{repo}', comment_id:{cid}, body:'<your final text>'}}\n")
        lines.append("Nothing posted — these are drafts. Edit any, then post the ones you want (each gated).")
        return "\n".join(lines)

    async def _post(self, repo, comment_id, number, body, approved) -> str:
        body = (body or "").strip()
        if not body:
            return "ERROR: empty reply body."
        if not (comment_id or number):
            return "ERROR: need comment_id (reply under a review comment) or number (PR conversation comment)."
        target = f"review comment #{comment_id}" if comment_id else f"PR #{number} conversation"
        if not approved:
            return (f"PREVIEW — nothing posted. Reply to {target} on {repo}:\n\n{body}\n\n"
                    "Approve to post: re-send github_post_reply with explicit_approval=true (same params).")
        try:
            if comment_id:
                cinfo = await self._get(f"/repos/{repo}/pulls/comments/{comment_id}")
                pr_url = (cinfo or {}).get("pull_request_url") or ""
                m = re.search(r"/pulls/(\d+)", pr_url)
                if not m:
                    return f"ERROR: couldn't resolve the PR for comment {comment_id}."
                res = await self._post_api(f"/repos/{repo}/pulls/{m.group(1)}/comments/{comment_id}/replies",
                                           {"body": body})
            else:
                res = await self._post_api(f"/repos/{repo}/issues/{number}/comments", {"body": body})
        except Exception as e:
            return f"post failed — {str(e)[:200]}"
        return f"✅ Posted reply to {target}: {(res or {}).get('html_url', '(posted)')}"
