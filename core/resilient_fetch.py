"""Resilient HTTP GET for the radars — defeats the datacenter-IP / bot blocks.

Reddit public JSON and some SearXNG upstreams 403/block this host's plain
urllib (datacenter fingerprint). This module gives the radars one drop-in
``get(url, timeout)`` that returns text and fails open ("") — swap
``urllib`` for ``resilient_fetch`` with a single import.

It tries, in the order best suited to the URL:
  1. CDP  — fetch through the stack's live CDP Chrome (real residential-grade
            browser fingerprint), reusing the proven scripts/cdp_fetch_thread.js
            pattern. This is what gets past reddit's bot wall.
  2. SearXNG — the local SearXNG JSON proxy (127.0.0.1:8890) for *search-shaped*
            URLs (anything with a ?q= we can translate into a query). Returns
            SearXNG result JSON, not the origin's body, so it's a search
            fallback only.
  3. urllib — plain urllib, fast path for normal (non-blocked) URLs.

Hard hosts (reddit) try CDP first; everything else tries the fast urllib path
first and only escalates on block/failure. Every path is wrapped so a failure
falls through to the next; if all fail we return "".

Use:
    from core import resilient_fetch
    text = resilient_fetch.get(url, timeout=15)        # drop-in for urllib _get
    text, path = resilient_fetch.get_with_path(url)    # also learn which path won
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.parse
from urllib.parse import urlparse, parse_qs, quote
from urllib.request import Request, urlopen

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8890").rstrip("/")

# CDP ports to probe, in preference order. The js default 18801 is often down;
# the stack runs Chrome on several. Env CDP_PORT (if set) is tried first.
_CDP_PORTS = ["18801", "18800", "18802", "18803", "18810"]

_HARD_HOSTS = ("reddit.com",)

# Markers that mean "the fetch was blocked / challenged", not real content.
_BLOCK_MARKERS = (
    "whoa there",
    "you've been blocked",
    "you have been blocked",
    "access denied",
    "are you a robot",
    "captcha-delivery",
    "attention required",
    "request blocked",
    "too many requests",
    "enable javascript and cookies to continue",
    '"error": 403',
    '"error":403',
    '"reason": "banned"',
)

_LAST_PATH = ""
_CDP_PORT_CACHE: str | None = None  # None=unprobed, ""=none live, else port str

# Generic CDP fetch: navigate the live CDP Chrome to the exact URL and return
# document.body.innerText. Same hardened-timeout shape as cdp_fetch_thread.js
# but URL-agnostic (no reddit /.json mangling) — URL + port come in via env.
_CDP_JS = r"""
const url  = process.env.CDP_TARGET_URL;
const PORT = process.env.CDP_PORT || '18801';
const HARD_MS = 25000, STEP_MS = 8000, NAV_MS = 6000;
const hardTimer = setTimeout(() => { console.error('ERR hard-timeout'); process.exit(1); }, HARD_MS);
hardTimer.unref?.();
const withTimeout = (p, ms, l) => Promise.race([
  p, new Promise((_, rej) => { const t = setTimeout(() => rej(new Error('timeout:' + l)), ms); t.unref?.(); }),
]);
(async () => {
  const vr = await withTimeout(
    fetch(`http://127.0.0.1:${PORT}/json/version`, { signal: AbortSignal.timeout(STEP_MS) }), STEP_MS, 'version');
  const v = await vr.json();
  const ws = new WebSocket(v.webSocketDebuggerUrl);
  let id = 0; const pend = {};
  const send = (m, p = {}, s) => withTimeout(new Promise(r => {
    const i = ++id; pend[i] = r;
    ws.send(JSON.stringify(s ? { id: i, sessionId: s, method: m, params: p } : { id: i, method: m, params: p }));
  }), STEP_MS, m);
  ws.addEventListener('message', ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pend[m.id]) { pend[m.id](m.result); delete pend[m.id]; }
  });
  await withTimeout(new Promise((r, rej) => {
    ws.addEventListener('open', r, { once: true });
    ws.addEventListener('error', () => rej(new Error('ws-error')), { once: true });
  }), STEP_MS, 'ws-open');
  const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
  await send('Page.enable', {}, sessionId);
  await send('Page.navigate', { url }, sessionId);
  await new Promise(r => { const t = setTimeout(r, NAV_MS); t.unref?.(); });
  const res = await send('Runtime.evaluate',
    { expression: 'document.body.innerText', returnByValue: true }, sessionId);
  process.stdout.write((res && res.result && res.result.value) || '');
  try { await send('Target.closeTarget', { targetId }); } catch (_) {}
  ws.close();
  clearTimeout(hardTimer);
  process.exit(0);
})().catch(e => { console.error('ERR', e.message); process.exit(1); });
"""


def _looks_ok(text: str, url: str) -> bool:
    """True if `text` looks like real content rather than a block/empty page."""
    if not text or not text.strip():
        return False
    low = text[:2000].lower()
    return not any(m in low for m in _BLOCK_MARKERS)


def _live_cdp_port() -> str | None:
    """Return the first reachable CDP port, or None. Cached per-process."""
    global _CDP_PORT_CACHE
    if _CDP_PORT_CACHE is not None:
        return _CDP_PORT_CACHE or None
    candidates = []
    env_p = os.environ.get("CDP_PORT")
    if env_p:
        candidates.append(env_p)
    candidates += [p for p in _CDP_PORTS if p != env_p]
    for p in candidates:
        try:
            with urlopen(f"http://127.0.0.1:{p}/json/version", timeout=1.5) as r:
                if getattr(r, "status", 200) == 200:
                    _CDP_PORT_CACHE = p
                    return p
        except Exception:
            continue
    _CDP_PORT_CACHE = ""  # probed, none live
    return None


def _cdp(url: str, timeout: float) -> str:
    """Fetch via the live CDP Chrome tab (residential browser fingerprint)."""
    port = _live_cdp_port()
    if not port:
        return ""
    fetch_url = url
    host = urlparse(url).netloc.lower()
    if "www.reddit.com" in host:  # old.reddit draws less bot heat
        fetch_url = url.replace("://www.reddit.com", "://old.reddit.com")
    env = {**os.environ, "CDP_TARGET_URL": fetch_url, "CDP_PORT": str(port)}
    try:
        out = subprocess.run(
            ["node", "-e", _CDP_JS],
            capture_output=True, text=True,
            timeout=max(float(timeout), 20.0) + 10.0, env=env,
        )
        return out.stdout or ""
    except Exception:
        return ""


def _to_query(url: str) -> str | None:
    """Translate a search-shaped URL into a SearXNG query, else None."""
    p = urlparse(url)
    qs = parse_qs(p.query)
    q = qs.get("q", [None])[0]
    if not q:
        return None
    if "reddit.com" in p.netloc and "site:" not in q:
        m = re.search(r"/r/([^/]+)", p.path)
        q = f"{q} site:reddit.com/r/{m.group(1)}" if m else f"{q} site:reddit.com"
    return q


def _searxng(url: str, timeout: float) -> str:
    """Route a search-shaped URL through the local SearXNG JSON proxy."""
    q = _to_query(url)
    if not q:
        return ""
    full = f"{SEARXNG_URL}/search?q={quote(q)}&format=json"
    try:
        with urlopen(Request(full, headers=_UA), timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore")
    except Exception:
        return ""


def _urllib(url: str, timeout: float) -> str:
    """Plain urllib GET (fast path for non-blocked hosts)."""
    try:
        with urlopen(Request(url, headers=_UA), timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore")
    except Exception:
        return ""


_STRATEGIES = {"cdp": _cdp, "searxng": _searxng, "urllib": _urllib}


def _order_for(url: str):
    host = urlparse(url).netloc.lower()
    if any(h in host for h in _HARD_HOSTS):
        return ("cdp", "searxng", "urllib")
    return ("urllib", "cdp", "searxng")


def get_with_path(url: str, timeout: float = 15) -> tuple[str, str]:
    """Like get(), but also returns the name of the path that won (or "")."""
    global _LAST_PATH
    for name in _order_for(url):
        text = _STRATEGIES[name](url, timeout)
        if _looks_ok(text, url):
            _LAST_PATH = name
            return text, name
    _LAST_PATH = ""
    return "", ""


def get(url: str, timeout: float = 15) -> str:
    """Drop-in replacement for a radar's urllib `_get`. Returns text, never raises."""
    return get_with_path(url, timeout)[0]


def last_path() -> str:
    return _LAST_PATH


if __name__ == "__main__":
    import time as _t

    reddit_url = ("https://www.reddit.com/r/smallbusiness/search.json"
                  "?q=need+a+website&restrict_sr=1&sort=new&limit=3")
    normal_url = "https://example.com"

    print(f"CDP port live: {_live_cdp_port() or 'none'}")
    print(f"SearXNG:       {SEARXNG_URL}\n")

    report = {}
    for label, u in (("reddit_search", reddit_url), ("normal", normal_url)):
        t0 = _t.time()
        text, path = get_with_path(u, timeout=20)
        dt = round(_t.time() - t0, 1)
        parsed = None
        if text:
            try:
                j = json.loads(text)
                if isinstance(j, dict) and "data" in j:
                    parsed = f"reddit json, {len(j['data'].get('children', []))} children"
                elif isinstance(j, dict) and "results" in j:
                    parsed = f"searxng json, {len(j['results'])} results"
                else:
                    parsed = "json"
            except Exception:
                parsed = "text (non-json)"
        ok = bool(text)
        report[label] = {"path": path or "FAILED", "ok": ok, "bytes": len(text),
                         "secs": dt, "shape": parsed}
        snippet = (text[:90].replace("\n", " ") if text else "")
        print(f"[{label}] path={path or 'FAILED':8} ok={ok} bytes={len(text)} "
              f"{dt}s shape={parsed}\n  -> {snippet!r}\n")

    # Confirm the SearXNG search path independently (it's only auto-selected as a
    # fallback, so exercise it directly to prove the proxy itself works).
    sx = _searxng("https://www.reddit.com/r/test/search.json?q=marketing", timeout=15)
    sx_ok = False
    try:
        sx_ok = len(json.loads(sx).get("results", [])) > 0
    except Exception:
        pass
    print(f"[searxng direct] reachable={sx_ok} bytes={len(sx)}")

    print("\nSELF-TEST RESULT: " + json.dumps(report))
