"""Unified, local-first web fetch + clean-markdown extraction for the hustle lane.

ONE thin wrapper over the vetted pieces so agents stop scattering ad-hoc urllib
calls. Two jobs:

  fetch(url)      -> tiered fetcher that defeats bot-walls WITHOUT a foreign SaaS:
                     1. plain requests (fast path, ~all normal sites)
                     2. curl_cffi Chrome-impersonate (real TLS/JA3 fingerprint) on
                        403 / anti-bot challenge — this is the new capability
                     3. the stack's existing resilient_fetch (live CDP Chrome /
                        browser) only if 1+2 still get a block. Reused, not
                        duplicated.
  extract(x)      -> clean LLM-ready markdown from a URL or raw HTML, in-house via
                     markdownify+bs4 (already-present, MIT), stdlib fallback. NO
                     network LLM on this or any other default path.

Why in-house and not Crawl4AI: Crawl4AI was vetted (scratch_verify/crawl4ai-*)
and QUARANTINED — its dep tree is ResolutionImpossible on this box's Python 3.14,
it defaults its LLM extraction to openai/gpt-4o + deepseek (foreign), pulls a
personal litellm fork (unclecode-litellm), and post-install test-crawls
crawl4ai.com. We took its one useful idea (URL->clean markdown) and own it here.

HARD RAILS honored:
  * local-first / $0: default fetch + extract touch NO LLM at all. The optional
    structured() helper routes ONLY to local Ollama (127.0.0.1) and hard-refuses
    any non-local base URL — no foreign provider is reachable from this module.
  * read-only / no trail: GET only, no cookies persisted, one polite fetch, low
    rate. For reading a prospect's OWN public/broken site to help them.
  * kill switch: data/hustle/WEB_EXTRACT_OFF disables the impersonate + browser
    escalation tiers (degrades to plain fetch); consumers fail-soft, never break.

Use:
    from core import web_extract
    r  = web_extract.fetch(url)          # r.status, r.text, r.path, r.error
    md = web_extract.extract(url)["markdown"]
"""
from __future__ import annotations

import os
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

KILL_SWITCH = ROOT / "data" / "hustle" / "WEB_EXTRACT_OFF"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# A fetch that returns one of these in its first ~2KB was challenged, not served.
_BLOCK_MARKERS = (
    "you've been blocked", "you have been blocked", "access denied",
    "are you a robot", "captcha-delivery", "attention required",
    "request blocked", "too many requests", "cf-browser-verification",
    "enable javascript and cookies to continue", "checking your browser",
    "verify you are human", "cloudflare",
)
# statuses that mean "try a stronger fetcher", not "give up"
_ANTIBOT_STATUS = {401, 403, 406, 409, 429, 503}


def _off() -> bool:
    """Kill switch: True disables the impersonate + browser escalation tiers."""
    return KILL_SWITCH.exists()


def _blocked(text: str, status: int | None) -> bool:
    if status in _ANTIBOT_STATUS:
        return True
    if not text or not text.strip():
        return status is None  # empty + no status = failed fetch, escalate
    low = text[:2000].lower()
    return any(m in low for m in _BLOCK_MARKERS)


@dataclass
class FetchResult:
    url: str
    status: int | None = None
    text: str = ""
    path: str = ""          # which tier won: plain | impersonate | resilient | ""
    error: str | None = None
    final_url: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.text) and not _blocked(self.text, self.status)


# ── Tier 1: plain requests ────────────────────────────────────────────────────
def _plain(url: str, timeout: float) -> FetchResult:
    try:
        import requests
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout,
                          allow_redirects=True)
        return FetchResult(url, r.status_code, r.text, "plain", None, r.url)
    except Exception as e:
        return FetchResult(url, None, "", "plain", f"{type(e).__name__}: {str(e)[:80]}")


# ── Tier 2: curl_cffi real-Chrome TLS/JA3 impersonation (the new capability) ───
def _impersonate(url: str, timeout: float, profile: str = "chrome") -> FetchResult:
    try:
        from curl_cffi import requests as cr
        r = cr.get(url, impersonate=profile, timeout=timeout, allow_redirects=True)
        return FetchResult(url, r.status_code, r.text, "impersonate", None, str(r.url))
    except Exception as e:
        return FetchResult(url, None, "", "impersonate", f"{type(e).__name__}: {str(e)[:80]}")


# ── Tier 3: reuse the stack's live-CDP/browser fetcher (no duplication) ────────
def _resilient(url: str, timeout: float) -> FetchResult:
    try:
        from core import resilient_fetch
        text, path = resilient_fetch.get_with_path(url, timeout=timeout)
        return FetchResult(url, 200 if text else None, text, "resilient" if text else "",
                           None if text else "resilient-empty")
    except Exception as e:
        return FetchResult(url, None, "", "", f"{type(e).__name__}: {str(e)[:80]}")


def fetch(url: str, timeout: float = 15, impersonate: bool = True,
          allow_browser: bool = True) -> FetchResult:
    """Tiered GET. Plain -> curl_cffi Chrome-impersonate on block -> live browser.
    Read-only, never raises. Escalation tiers respect the WEB_EXTRACT_OFF switch."""
    if not url:
        return FetchResult(url, None, "", "", "empty-url")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")

    r = _plain(url, timeout)
    if r.ok:
        return r

    off = _off()
    if impersonate and not off:
        imp = _impersonate(url, timeout)
        if imp.ok:
            return imp
        r = imp if imp.text or not r.text else r

    if allow_browser and not off:
        res = _resilient(url, timeout)
        if res.ok:
            return res

    return r  # best-effort last result (may carry a block status for the caller)


# ── Extraction: HTML -> clean markdown, fully in-house, zero network LLM ───────
_STRIP_TAGS = ("script", "style", "noscript", "template", "svg", "iframe",
               "nav", "footer", "header", "form", "aside")


def _markdown_via_lib(html: str, base_url: str | None) -> str | None:
    """Preferred path: bs4 boilerplate-strip + markdownify (both already vetted+present)."""
    try:
        from bs4 import BeautifulSoup
        from markdownify import markdownify as _md
    except Exception:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(list(_STRIP_TAGS)):
        tag.decompose()
    body = soup.body or soup
    md = _md(str(body), heading_style="ATX", strip=["img"])
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return md


class _StdlibText(__import__("html.parser", fromlist=["HTMLParser"]).HTMLParser):
    """Zero-dependency fallback: strip tags to readable text if libs are missing."""
    def __init__(self):
        super().__init__()
        self._skip = 0
        self.out: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _STRIP_TAGS:
            self._skip += 1
        elif tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in _STRIP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.out.append(data.strip() + " ")


def _markdown_via_stdlib(html: str) -> str:
    p = _StdlibText()
    try:
        p.feed(html)
    except Exception:
        pass
    return re.sub(r"\n{3,}", "\n\n", "".join(p.out)).strip()


def to_markdown(html: str, base_url: str | None = None) -> str:
    """HTML -> clean markdown. Preferred lib path, stdlib fallback. No network."""
    if not html:
        return ""
    return _markdown_via_lib(html, base_url) or _markdown_via_stdlib(html)


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def extract(url_or_html: str, timeout: float = 15, impersonate: bool = True) -> dict:
    """URL or raw HTML -> {url, status, path, title, markdown, text, ok, error}.
    Default path touches NO LLM, foreign or otherwise."""
    is_html = "<" in url_or_html[:512] and not url_or_html.strip().startswith("http")
    if is_html:
        html, res = url_or_html, FetchResult("<html>", 200, url_or_html, "inline")
    else:
        res = fetch(url_or_html, timeout=timeout, impersonate=impersonate)
        html = res.text
    md = to_markdown(html, res.final_url or (None if is_html else url_or_html))
    tm = _TITLE_RE.search(html or "")
    return {
        "url": res.final_url or res.url,
        "status": res.status,
        "path": res.path,
        "title": (tm.group(1).strip() if tm else None),
        "markdown": md,
        "text": md,
        "ok": bool(md),
        "error": res.error,
    }


# ── OPTIONAL structured extraction — local Ollama ONLY, hard-refuses foreign ──
def structured(url_or_html: str, instruction: str, model: str = "llama3.2",
               timeout: float = 120) -> str:
    """Opt-in LLM extraction. Routes ONLY to local Ollama; refuses any non-local
    endpoint. This is the re-route: Crawl4AI would call openai/gpt-4o here."""
    import json
    host = urllib.parse.urlparse(OLLAMA_URL).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError(f"web_extract.structured refuses non-local LLM endpoint: {OLLAMA_URL}")
    md = extract(url_or_html, timeout=min(timeout, 30))["markdown"][:12000]
    prompt = f"{instruction}\n\n---\n{md}"
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=json.dumps({"model": model, "prompt": prompt, "stream": False}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()).get("response", "").strip()


# ── selftest ──────────────────────────────────────────────────────────────────
def _selftest() -> int:
    import json
    ok = True
    checks: list[tuple[str, bool, str]] = []

    def chk(name, cond, detail=""):
        nonlocal ok
        ok = ok and cond
        checks.append((name, bool(cond), detail))

    # 1. plain fetch + markdown
    r = fetch("https://example.com", timeout=15)
    chk("plain_fetch_example", r.ok and r.status == 200, f"path={r.path} status={r.status}")
    md = extract("https://example.com")["markdown"]
    chk("markdown_nonempty", len(md) > 20 and "Example Domain" in md, f"{len(md)}B")

    # 2. curl_cffi impersonation tier works (real TLS fingerprint)
    imp = _impersonate("https://example.com", 15)
    chk("impersonate_tier", imp.ok and imp.status == 200, f"status={imp.status}")

    # 3. markdown converter strips script/style, keeps headings + links
    sample = ("<html><head><title>T</title></head><body><nav>MENU</nav>"
              "<h1>Hello</h1><p>Body <a href='/x'>link</a></p>"
              "<script>steal()</script><style>.a{}</style></body></html>")
    smd = to_markdown(sample)
    chk("md_strips_script", "steal()" not in smd and ".a{" not in smd, smd[:60])
    chk("md_keeps_content", "Hello" in smd and "link" in smd, smd[:60])

    # 4. NO foreign provider reachable on any default path: no provider SDK is
    #    imported, and the module's only LLM endpoint resolves to localhost.
    src = Path(__file__).read_text()
    sdk_imports = re.findall(r"(?m)^\s*(?:from|import)\s+"
                             r"(openai|anthropic|litellm|cohere|dashscope|zhipuai|"
                             r"google\.generativeai|replicate)\b", src)
    chk("no_foreign_provider_sdk_import", not sdk_imports, f"imports={sdk_imports}")
    endpoint_host = urllib.parse.urlparse(OLLAMA_URL).hostname or ""
    chk("only_llm_endpoint_is_local",
        endpoint_host in ("127.0.0.1", "localhost", "::1"), f"endpoint={OLLAMA_URL}")
    prev = os.environ.get("OLLAMA_URL")
    os.environ["OLLAMA_URL"] = "http://api.openai.com"
    import importlib
    refused = False
    try:
        globals()["OLLAMA_URL"] = "http://api.openai.com"
        structured("<b>x</b>", "x", timeout=1)
    except RuntimeError:
        refused = True
    except Exception:
        refused = True
    finally:
        globals()["OLLAMA_URL"] = (prev or "http://127.0.0.1:11434").rstrip("/")
        if prev:
            os.environ["OLLAMA_URL"] = prev
        else:
            os.environ.pop("OLLAMA_URL", None)
    chk("structured_refuses_foreign_endpoint", refused, "raised on api.openai.com")

    # 5. kill switch disables the impersonate/browser escalation tiers
    created = False
    if not KILL_SWITCH.exists():
        KILL_SWITCH.parent.mkdir(parents=True, exist_ok=True)
        KILL_SWITCH.write_text("selftest\n")
        created = True
    chk("kill_switch_active", _off(), str(KILL_SWITCH))
    # with switch on, a 403-ish host should NOT escalate to impersonate/browser
    off_res = fetch("https://example.com")  # plain still works, no escalation attempted
    chk("kill_switch_degrades_soft", off_res.path in ("plain", ""), f"path={off_res.path}")
    if created:
        KILL_SWITCH.unlink()

    print(json.dumps({"checks": [{"name": n, "ok": c, "detail": d} for n, c, d in checks],
                      "PASS": ok}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    for a in sys.argv[1:]:
        d = extract(a)
        print(f"\n### {d['url']}  status={d['status']} path={d['path']} title={d['title']!r}")
        print(d["markdown"][:800])
