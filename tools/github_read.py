"""GitHub READ-ONLY bridge (api.github.com) for the work-GPT gateway.

In-process httpx + Bearer token (no `gh` CLI — CLIs have no keychain under launchd).
Every call is a GET; there is deliberately no code path that POST/PATCH/PUT/DELETEs,
merges, or otherwise mutates. Operator's CompanyA GitHub (login redacted, org redactedind).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx

from tools.base import BaseTool, make_tool_def
from core.api_client import ensure_external_api_allowed

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"
DEFAULT_REPO = "redactedind/AG-CDT-SALESFORCE"
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_ALLOWED_KINDS = {"prs", "pr", "comments", "reviews", "commits", "checks", "issues", "issue", "repo"}

# Token env names, in precedence order. gho_ (gh oauth) and github_pat_ (fine-grained PAT) both work.
_TOKEN_ENV_KEYS = ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN")


def _read_file_token(rels) -> str:
    for rel in rels:
        try:
            for line in (ROOT / rel).read_text().splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                name, _, val = line.partition("=")
                if name.strip() in _TOKEN_ENV_KEYS:
                    val = val.strip().strip("'\"")
                    if val:
                        return val
        except Exception:
            continue
    return ""


def _read_token() -> str:
    # A DEDICATED github token file wins over everything else — a known-good token (e.g. one that can see
    # the redactedind org repos) must beat a stale .env PAT that only 404s. Order: dedicated file -> env
    # -> the general .env files.
    t = _read_file_token(("data/secrets/github.env", "data/secrets/github_token.env"))
    if t:
        return t
    for k in _TOKEN_ENV_KEYS:
        v = (os.environ.get(k) or "").strip()
        if v:
            return v
    return _read_file_token((".env", ".env.work"))


class GithubReadTool(BaseTool):
    name = "github_read"
    description = "GitHub READ-ONLY (repos, PRs, reviews/comments, commits, checks, issues)."

    def __init__(self):
        self._token = _read_token()

    async def _get(self, path: str, params: dict | None = None) -> object:
        """Single READ-ONLY GET against api.github.com. No other verb is ever issued."""
        if not self._token:
            raise RuntimeError(
                "no GitHub token configured — set GITHUB_TOKEN (or GH_TOKEN) in the env/.env "
                "the gateway loads, then this tool is live."
            )
        ensure_external_api_allowed("GitHub API")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "CompanyA-work-gpt",
        }
        async with httpx.AsyncClient() as c:
            r = await c.get(API + path, headers=headers, params=params or {}, timeout=30)
        if r.status_code == 404:
            raise RuntimeError(f"GitHub 404 for {path} (repo/number wrong, or the token can't see it).")
        if r.status_code in (401, 403):
            raise RuntimeError(f"GitHub {r.status_code} for {path}: {r.text[:200]}")
        r.raise_for_status()
        return r.json()

    def get_definitions(self) -> list:
        return [
            make_tool_def(
                "github_read",
                "Read Operator's CompanyA GitHub (READ-ONLY — never writes/merges). "
                "kind='prs' lists pull requests (state open|closed|all); 'pr' one PR (needs number); "
                "'comments' a PR's review comments + reviews + conversation (needs number); "
                "'commits' a PR's commits (with number) or a branch/repo's recent commits (ref optional); "
                "'checks' CI check-runs + combined status for a PR (number) or a ref/sha; "
                "'issues' lists issues or one issue (with number); 'repo' repo metadata. "
                f"repo defaults to {DEFAULT_REPO} (the rebate PR repo).",
                {
                    "repo": {"type": "string", "description": f"owner/repo, e.g. {DEFAULT_REPO} (default)"},
                    "kind": {"type": "string", "description": "prs|pr|comments|commits|checks|issues|repo"},
                    "number": {"type": "integer", "description": "PR or issue number (for pr/comments/checks/commits-of-a-PR/issue)"},
                    "author": {"type": "string", "description": "kind='comments' WITHOUT a number = recent PR review comments across the repo; set author to a GitHub username to filter to just theirs"},
                    "state": {"type": "string", "description": "open|closed|all for prs/issues (default open)"},
                    "ref": {"type": "string", "description": "branch/tag/sha for commits or checks (optional)"},
                    "limit": {"type": "integer", "description": "max rows for list kinds (default 20, max 100)"},
                },
                ["kind"],
            ),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        if tool_name != "github_read":
            return f"Unknown github tool: {tool_name}"
        ti = tool_input if isinstance(tool_input, dict) else {}
        repo = str(ti.get("repo") or DEFAULT_REPO).strip().rstrip("/")
        if repo.startswith(("http://", "https://")):
            repo = repo.split("github.com/", 1)[-1]
        if not _REPO_RE.match(repo):
            return f"ERROR: repo must be 'owner/name' (got {repo!r})."
        kind = str(ti.get("kind") or "").strip().lower()
        if kind not in _ALLOWED_KINDS:
            return f"ERROR: kind must be one of {sorted(_ALLOWED_KINDS)} (got {kind!r})."
        number = ti.get("number")
        try:
            number = int(number) if number not in (None, "") else None
        except (TypeError, ValueError):
            return f"ERROR: number must be an integer (got {ti.get('number')!r})."
        state = str(ti.get("state") or "open").strip().lower()
        if state not in {"open", "closed", "all"}:
            state = "open"
        ref = str(ti.get("ref") or "").strip()
        try:
            limit = max(1, min(int(ti.get("limit") or 20), 100))
        except (TypeError, ValueError):
            limit = 20
        try:
            if kind == "repo":
                return self._fmt_repo(await self._get(f"/repos/{repo}"))
            if kind == "prs":
                data = await self._get(f"/repos/{repo}/pulls", {"state": state, "per_page": limit})
                return self._fmt_prs(repo, data, state)
            if kind == "pr":
                if number is None:
                    return "ERROR: kind='pr' needs a PR number."
                return self._fmt_pr(await self._get(f"/repos/{repo}/pulls/{number}"))
            if kind == "issues":
                if number is not None:
                    return self._fmt_issue(await self._get(f"/repos/{repo}/issues/{number}"))
                data = await self._get(f"/repos/{repo}/issues", {"state": state, "per_page": limit})
                return self._fmt_issues(repo, data, state)
            if kind == "issue":
                if number is None:
                    return "ERROR: kind='issue' needs an issue number."
                return self._fmt_issue(await self._get(f"/repos/{repo}/issues/{number}"))
            if kind in ("comments", "reviews"):
                if number is None:
                    # repo-wide RECENT comments (PR inline review + issue/conversation), optionally
                    # filtered by author login — "pull the recent comments from <person>" across the repo.
                    author = str(ti.get("author") or "").strip()
                    per = 100 if author else min(max(limit * 4, 40), 100)
                    review_c = await self._get(f"/repos/{repo}/pulls/comments",
                                               {"sort": "created", "direction": "desc", "per_page": per})
                    issue_c = await self._get(f"/repos/{repo}/issues/comments",
                                              {"sort": "created", "direction": "desc", "per_page": per})
                    return self._fmt_repo_comments(repo, review_c, issue_c, author, limit)
                return await self._fmt_comments(repo, number)
            if kind == "commits":
                if number is not None:
                    data = await self._get(f"/repos/{repo}/pulls/{number}/commits", {"per_page": limit})
                    return self._fmt_commits(f"PR #{number}", data)
                params = {"per_page": limit}
                if ref:
                    params["sha"] = ref
                data = await self._get(f"/repos/{repo}/commits", params)
                return self._fmt_commits(ref or "default branch", data)
            if kind == "checks":
                sha = ref
                if not sha and number is not None:
                    pr = await self._get(f"/repos/{repo}/pulls/{number}")
                    sha = ((pr.get("head") or {}).get("sha") or "") if isinstance(pr, dict) else ""
                if not sha:
                    return "ERROR: kind='checks' needs a PR number or a ref/sha."
                runs = await self._get(f"/repos/{repo}/commits/{sha}/check-runs")
                status = await self._get(f"/repos/{repo}/commits/{sha}/status")
                return self._fmt_checks(sha, runs, status)
        except RuntimeError as e:
            return f"ERROR: {e}"
        except httpx.HTTPError as e:
            return f"ERROR: GitHub request failed: {e}"
        return f"Unknown github tool kind: {kind}"

    @staticmethod
    def _cap(s: str, n: int = 8000) -> str:
        return s if len(s) <= n else s[:n] + f"\n… [truncated {len(s) - n} chars]"

    def _fmt_repo(self, d: dict) -> str:
        if not isinstance(d, dict):
            return "No repo data."
        return self._cap(
            f"{d.get('full_name')} ({'private' if d.get('private') else 'public'})\n"
            f"{d.get('description') or ''}\n"
            f"default branch: {d.get('default_branch')} | open issues+PRs: {d.get('open_issues_count')} | "
            f"stars: {d.get('stargazers_count')} | pushed: {d.get('pushed_at')}\n{d.get('html_url')}"
        )

    def _fmt_prs(self, repo: str, data, state: str) -> str:
        if not isinstance(data, list):
            return f"No PR data: {json.dumps(data, default=str)[:300]}"
        if not data:
            return f"No {state} pull requests on {repo}."
        lines = [f"{state.title()} pull requests on {repo} ({len(data)}):"]
        for p in data:
            lines.append(
                f"  #{p.get('number')} {p.get('title')} — {(p.get('user') or {}).get('login')} "
                f"[{p.get('state')}{', draft' if p.get('draft') else ''}] "
                f"{p.get('head', {}).get('ref')}→{p.get('base', {}).get('ref')} "
                f"(updated {(p.get('updated_at') or '')[:10]})"
            )
        return self._cap("\n".join(lines))

    def _fmt_pr(self, p: dict) -> str:
        if not isinstance(p, dict):
            return "No PR data."
        body = (p.get("body") or "").strip()
        return self._cap(
            f"PR #{p.get('number')}: {p.get('title')}\n"
            f"author: {(p.get('user') or {}).get('login')} | state: {p.get('state')}"
            f"{' (merged)' if p.get('merged') else ''}{' [draft]' if p.get('draft') else ''}\n"
            f"{p.get('head', {}).get('ref')} → {p.get('base', {}).get('ref')} | "
            f"commits {p.get('commits')} | +{p.get('additions')}/-{p.get('deletions')} in {p.get('changed_files')} files | "
            f"comments {p.get('comments')}+{p.get('review_comments')} review\n"
            f"created {(p.get('created_at') or '')[:10]} | updated {(p.get('updated_at') or '')[:10]}\n"
            f"{p.get('html_url')}\n\n{body}"
        )

    def _fmt_repo_comments(self, repo: str, review_c, issue_c, author: str, limit: int) -> str:
        items = [("review", c) for c in (review_c if isinstance(review_c, list) else [])]
        items += [("issue", c) for c in (issue_c if isinstance(issue_c, list) else [])]
        if not items:
            return f"Couldn't read recent comments on {repo}."
        items.sort(key=lambda x: (x[1].get("created_at") or ""), reverse=True)
        a = (author or "").lower().strip()
        parts = a.split()
        initials = (parts[0][0] + parts[-1][0]) if len(parts) >= 2 else ""

        def _match(login):
            lo = login.lower()
            if not a or a in lo:
                return True
            # CompanyA GitHub handles are <first-initial><last-initial><digits>_redacted — map a full name
            # ("Cinthya Flores" -> cf...) to the handle.
            return bool(initials) and lo.startswith(initials)

        rows = []
        for kind_, c in items:
            login = (c.get("user") or {}).get("login") or ""
            if not _match(login):
                continue
            url = c.get("html_url") or ""
            m = re.search(r"/(?:pull|issues)/(\d+)", url)
            num = m.group(1) if m else "?"
            loc = (f" ({c.get('path')}:{c.get('line') or c.get('original_line') or '?'})"
                   if kind_ == "review" else "")
            rows.append(f"  [{(c.get('created_at') or '')[:10]}] {login} on #{num}{loc}: "
                        f"{(c.get('body') or '').strip()[:280]}\n    {url}")
            if len(rows) >= limit:
                break
        who = f" from @{author}" if author else ""
        if not rows:
            hint = (f" — 0 matched (their GitHub login may differ from '{author}'; pass their exact username)"
                    if author else " — none found")
            return f"Recent comments on {repo}{who}{hint}."
        return f"Recent comments on {repo}{who} ({len(rows)}):\n" + "\n".join(rows)

    async def _fmt_comments(self, repo: str, number: int) -> str:
        reviews = await self._get(f"/repos/{repo}/pulls/{number}/reviews", {"per_page": 100})
        review_comments = await self._get(f"/repos/{repo}/pulls/{number}/comments", {"per_page": 100})
        issue_comments = await self._get(f"/repos/{repo}/issues/{number}/comments", {"per_page": 100})
        out = [f"PR #{number} on {repo} — reviews & comments:"]
        if isinstance(reviews, list) and reviews:
            out.append("\nReviews:")
            for rv in reviews:
                if rv.get("state") == "COMMENTED" and not (rv.get("body") or "").strip():
                    continue
                out.append(
                    f"  [{rv.get('state')}] {(rv.get('user') or {}).get('login')} "
                    f"({(rv.get('submitted_at') or '')[:10]}): {(rv.get('body') or '').strip()[:500]}"
                )
        if isinstance(review_comments, list) and review_comments:
            out.append("\nInline (diff) comments:")
            for c in review_comments:
                out.append(
                    f"  {(c.get('user') or {}).get('login')} on {c.get('path')}:{c.get('line') or c.get('original_line')}: "
                    f"{(c.get('body') or '').strip()[:400]}"
                )
        if isinstance(issue_comments, list) and issue_comments:
            out.append("\nConversation comments:")
            for c in issue_comments:
                out.append(
                    f"  {(c.get('user') or {}).get('login')} ({(c.get('created_at') or '')[:10]}): "
                    f"{(c.get('body') or '').strip()[:400]}"
                )
        if len(out) == 1:
            return f"No reviews or comments on PR #{number} ({repo})."
        return self._cap("\n".join(out))

    def _fmt_commits(self, scope: str, data) -> str:
        if not isinstance(data, list):
            return f"No commit data: {json.dumps(data, default=str)[:300]}"
        if not data:
            return f"No commits for {scope}."
        lines = [f"Commits ({scope}) — {len(data)}:"]
        for c in data:
            commit = c.get("commit") or {}
            author = (commit.get("author") or {})
            msg = (commit.get("message") or "").splitlines()[0] if commit.get("message") else ""
            lines.append(f"  {str(c.get('sha') or '')[:8]} {msg[:80]} — {author.get('name')} ({(author.get('date') or '')[:10]})")
        return self._cap("\n".join(lines))

    def _fmt_checks(self, sha: str, runs, status) -> str:
        lines = [f"Checks for {sha[:12]}:"]
        cr = (runs.get("check_runs") if isinstance(runs, dict) else None) or []
        if cr:
            lines.append("Check runs:")
            for r in cr:
                lines.append(f"  {r.get('name')}: {r.get('status')}/{r.get('conclusion') or '—'}")
        else:
            lines.append("  (no check runs)")
        if isinstance(status, dict):
            lines.append(f"Combined status: {status.get('state')} ({status.get('total_count')} contexts)")
            for s in (status.get("statuses") or []):
                lines.append(f"  {s.get('context')}: {s.get('state')} — {(s.get('description') or '')[:80]}")
        return self._cap("\n".join(lines))

    def _fmt_issues(self, repo: str, data, state: str) -> str:
        if not isinstance(data, list):
            return f"No issue data: {json.dumps(data, default=str)[:300]}"
        rows = [i for i in data if not i.get("pull_request")]  # /issues also returns PRs; drop them
        if not rows:
            return f"No {state} issues on {repo}."
        lines = [f"{state.title()} issues on {repo} ({len(rows)}):"]
        for i in rows:
            lines.append(
                f"  #{i.get('number')} {i.get('title')} — {(i.get('user') or {}).get('login')} "
                f"[{i.get('state')}] (updated {(i.get('updated_at') or '')[:10]})"
            )
        return self._cap("\n".join(lines))

    def _fmt_issue(self, i: dict) -> str:
        if not isinstance(i, dict):
            return "No issue data."
        labels = ", ".join(l.get("name") for l in (i.get("labels") or []) if isinstance(l, dict))
        return self._cap(
            f"Issue #{i.get('number')}: {i.get('title')}\n"
            f"author: {(i.get('user') or {}).get('login')} | state: {i.get('state')} | "
            f"comments: {i.get('comments')}{(' | labels: ' + labels) if labels else ''}\n"
            f"created {(i.get('created_at') or '')[:10]} | updated {(i.get('updated_at') or '')[:10]}\n"
            f"{i.get('html_url')}\n\n{(i.get('body') or '').strip()}"
        )
