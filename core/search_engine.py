"""Unified search engine — multi-engine, parallel, deduplicated, cached.

Queries ALL available free search APIs in parallel, deduplicates by URL,
merges and ranks by relevance. Graceful fallback if any engine fails.

All agents should import search() from here instead of using DDGS directly.

Engines:
  - DuckDuckGo (ddgs library, unlimited, no key)
  - SearXNG (local instance, 70+ engines)
  - Brave Search API (free tier: 2k/month, needs BRAVE_API_KEY)
  - Serper.dev (free tier: 2.5k queries, needs SERPER_API_KEY)
  - Mojeek (free, HTML scraping)
  - Wikipedia OpenSearch (free, no key)
  - Reddit Search (free JSON API)
  - Hacker News / Algolia (free, no key)
"""

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from html import unescape
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SEARXNG_URL = os.environ.get('SEARXNG_URL', 'http://127.0.0.1:8888')
BRAVE_API_KEY = os.environ.get('BRAVE_API_KEY', '')
SERPER_API_KEY = os.environ.get('SERPER_API_KEY', '')
CACHE_TTL = 3600  # 1 hour
_TIMEOUT = 10  # seconds per engine
# Bound every upstream read so a compromised/oversized engine response can't
# exhaust memory. 5MB is far above any legitimate search JSON/HTML payload.
_MAX_FETCH_BYTES = 5_000_000
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=12)

# ---------------------------------------------------------------------------
# Result cache  {cache_key: (timestamp, results)}
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, list[dict]]] = {}


def _cache_key(prefix: str, query: str, max_results: int) -> str:
    h = hashlib.md5(f"{prefix}:{query}:{max_results}".encode()).hexdigest()[:12]
    return f"{prefix}_{h}"


def _cache_get(key: str) -> Optional[list[dict]]:
    if key in _cache:
        ts, results = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return results
        del _cache[key]
    return None


def _cache_set(key: str, results: list[dict]):
    if len(_cache) > 500:
        now = time.time()
        stale = [k for k, (ts, _) in _cache.items() if now - ts > CACHE_TTL]
        for k in stale:
            del _cache[k]
    _cache[key] = (time.time(), results)


def _strip_html(text: str) -> str:
    """Strip HTML tags and decode entities."""
    text = re.sub(r'<[^>]+>', '', text)
    return unescape(text).strip()


# ---------------------------------------------------------------------------
# Normalize result format: always {title, href, body, engine}
# ---------------------------------------------------------------------------
def _norm(title: str, href: str, body: str, engine: str) -> dict:
    return {
        'title': _strip_html(title or '').strip(),
        'href': (href or '').strip(),
        'body': _strip_html(body or '').strip(),
        'engine': engine,
    }


# ---------------------------------------------------------------------------
# Individual engines (all synchronous — run via executor)
# ---------------------------------------------------------------------------

def _ddgs_search(query: str, max_results: int = 10) -> list[dict]:
    """DuckDuckGo via ddgs library."""
    try:
        from ddgs import DDGS
        raw = list(DDGS().text(query, max_results=max_results))
        return [_norm(r.get('title', ''), r.get('href', ''), r.get('body', ''), 'ddgs') for r in raw]
    except Exception:
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                raw = list(ddgs.text(query, max_results=max_results))
            return [_norm(r.get('title', ''), r.get('href', r.get('link', '')), r.get('body', r.get('snippet', '')), 'ddgs') for r in raw]
        except Exception as e:
            log.debug('DDGS failed: %s', e)
            return []


def _searxng_search(query: str, max_results: int = 10, categories: str = 'general') -> list[dict]:
    """Local SearXNG instance."""
    params = urllib.parse.urlencode({'q': query, 'format': 'json', 'categories': categories, 'pageno': 1})
    url = f'{SEARXNG_URL}/search?{params}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'ClaudeStack/1.0'})
        resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
        data = json.loads(resp.read(_MAX_FETCH_BYTES))
        return [_norm(r.get('title', ''), r.get('url', ''), r.get('content', ''), f"searxng:{r.get('engine', '')}") for r in data.get('results', [])[:max_results]]
    except Exception as e:
        log.debug('SearXNG unavailable: %s', e)
        return []


def _brave_search(query: str, max_results: int = 10) -> list[dict]:
    """Brave Search API (free tier: 2k/month)."""
    if not BRAVE_API_KEY:
        return []
    params = urllib.parse.urlencode({'q': query, 'count': min(max_results, 20)})
    url = f'https://api.search.brave.com/res/v1/web/search?{params}'
    try:
        req = urllib.request.Request(url, headers={
            'Accept': 'application/json',
            'Accept-Encoding': 'gzip',
            'X-Subscription-Token': BRAVE_API_KEY,
        })
        resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
        data = json.loads(resp.read(_MAX_FETCH_BYTES))
        return [_norm(r.get('title', ''), r.get('url', ''), r.get('description', ''), 'brave') for r in data.get('web', {}).get('results', [])[:max_results]]
    except Exception as e:
        log.debug('Brave search failed: %s', e)
        return []


def _serper_search(query: str, max_results: int = 10) -> list[dict]:
    """Serper.dev Google Search API (free tier: 2.5k queries)."""
    if not SERPER_API_KEY:
        return []
    try:
        payload = json.dumps({'q': query, 'num': min(max_results, 10)}).encode()
        req = urllib.request.Request(
            'https://google.serper.dev/search',
            data=payload,
            headers={'X-API-KEY': SERPER_API_KEY, 'Content-Type': 'application/json'},
            method='POST',
        )
        resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
        data = json.loads(resp.read(_MAX_FETCH_BYTES))
        return [_norm(r.get('title', ''), r.get('link', ''), r.get('snippet', ''), 'serper') for r in data.get('organic', [])[:max_results]]
    except Exception as e:
        log.debug('Serper search failed: %s', e)
        return []


def _mojeek_search(query: str, max_results: int = 10) -> list[dict]:
    """Mojeek — independent search engine, HTML scraping."""
    params = urllib.parse.urlencode({'q': query})
    url = f'https://www.mojeek.com/search?{params}'
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        })
        resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
        html = resp.read(_MAX_FETCH_BYTES).decode('utf-8', errors='replace')
        # Mojeek results: <li class="r1">, <li class="r2">, etc.
        # Each has: <a href="URL">title parts</a> and <p class="s">description</p>
        results = []
        # Match result blocks by numbered class r1..r10
        blocks = re.findall(r'<li\s+class="r\d+"[^>]*>(.*?)</li>', html, re.DOTALL)
        for block in blocks[:max_results]:
            # Extract href from first external link
            link_m = re.search(r'<a[^>]+href="(https?://(?!www\.mojeek\.com)[^"]+)"', block)
            if not link_m:
                continue
            href = link_m.group(1)
            # Title is in <h2> tag
            h2_m = re.search(r'<h2[^>]*>(.*?)</h2>', block, re.DOTALL)
            title = _strip_html(h2_m.group(1)) if h2_m else ''
            if not title:
                # Fallback: second <a> tag text
                a_tags = re.findall(r'<a[^>]*>(.*?)</a>', block, re.DOTALL)
                title = _strip_html(a_tags[1]) if len(a_tags) > 1 else ''
            # Description in <p class="s">
            desc_m = re.search(r'<p\s+class="s">(.*?)</p>', block, re.DOTALL)
            body = _strip_html(desc_m.group(1)) if desc_m else ''
            if title and href:
                results.append(_norm(title, href, body, 'mojeek'))
        return results
    except Exception as e:
        log.debug('Mojeek search failed: %s', e)
        return []


def _wikipedia_search(query: str, max_results: int = 5) -> list[dict]:
    """Wikipedia OpenSearch API — free, no key."""
    params = urllib.parse.urlencode({
        'action': 'opensearch', 'search': query, 'limit': min(max_results, 10),
        'namespace': 0, 'format': 'json',
    })
    url = f'https://en.wikipedia.org/w/api.php?{params}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'ClaudeStack/1.0'})
        resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
        data = json.loads(resp.read(_MAX_FETCH_BYTES))
        if len(data) >= 4:
            titles, descs, urls = data[1], data[2], data[3]
            return [_norm(t, u, d, 'wikipedia') for t, d, u in zip(titles, descs, urls)]
        return []
    except Exception as e:
        log.debug('Wikipedia search failed: %s', e)
        return []


def _reddit_search(query: str, max_results: int = 10) -> list[dict]:
    """Reddit JSON search API — free, no key."""
    params = urllib.parse.urlencode({'q': query, 'limit': min(max_results, 25), 'sort': 'relevance', 't': 'all'})
    url = f'https://www.reddit.com/search.json?{params}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'ClaudeStack/1.0 (search aggregator)'})
        resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
        data = json.loads(resp.read(_MAX_FETCH_BYTES))
        results = []
        for child in data.get('data', {}).get('children', [])[:max_results]:
            d = child.get('data', {})
            title = d.get('title', '')
            href = f"https://www.reddit.com{d.get('permalink', '')}" if d.get('permalink') else d.get('url', '')
            body = (d.get('selftext', '') or '')[:300] or title
            results.append(_norm(title, href, body, 'reddit'))
        return results
    except Exception as e:
        log.debug('Reddit search failed: %s', e)
        return []


def _hackernews_search(query: str, max_results: int = 10) -> list[dict]:
    """Hacker News via Algolia API — free, no key."""
    params = urllib.parse.urlencode({'query': query, 'hitsPerPage': min(max_results, 20), 'tags': 'story'})
    url = f'https://hn.algolia.com/api/v1/search?{params}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'ClaudeStack/1.0'})
        resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
        data = json.loads(resp.read(_MAX_FETCH_BYTES))
        results = []
        for hit in data.get('hits', [])[:max_results]:
            title = hit.get('title', '')
            href = hit.get('url') or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
            body = (hit.get('story_text', '') or hit.get('comment_text', '') or title or '')[:300]
            results.append(_norm(title, href, body, 'hackernews'))
        return results
    except Exception as e:
        log.debug('HN search failed: %s', e)
        return []


# ---------------------------------------------------------------------------
# Engine registry: (name, function, needs_api_key, is_fast)
# ---------------------------------------------------------------------------
_ENGINES = [
    ('ddgs',        _ddgs_search,       False, True),
    ('searxng',     _searxng_search,    False, False),
    ('brave',       _brave_search,      True,  False),
    ('serper',      _serper_search,     True,  False),
    ('mojeek',      _mojeek_search,     False, False),
    ('wikipedia',   _wikipedia_search,  False, False),
    ('reddit',      _reddit_search,     False, False),
    ('hackernews',  _hackernews_search, False, False),
]


def available_engines() -> list[str]:
    """List engines that are currently usable (keys present if required)."""
    avail = []
    for name, _, needs_key, _ in _ENGINES:
        if needs_key:
            if name == 'brave' and not BRAVE_API_KEY:
                continue
            if name == 'serper' and not SERPER_API_KEY:
                continue
        avail.append(name)
    return avail


# ---------------------------------------------------------------------------
# Deduplication and ranking
# ---------------------------------------------------------------------------
def _normalize_url(url: str) -> str:
    """Normalize URL for dedup."""
    if not url:
        return ''
    parsed = urlparse(url)
    host = parsed.netloc.lower().replace('www.', '')
    path = parsed.path.rstrip('/')
    return f"{host}{path}"


def _deduplicate(all_results: list[dict]) -> list[dict]:
    """Deduplicate by URL. Boost items found by multiple engines."""
    seen: dict[str, dict] = {}
    counts: dict[str, int] = {}
    engines_seen: dict[str, set] = {}

    for r in all_results:
        norm = _normalize_url(r.get('href', ''))
        if not norm:
            continue
        eng = r.get('engine', '')
        if norm not in seen:
            seen[norm] = r
            counts[norm] = 1
            engines_seen[norm] = {eng}
        else:
            if eng not in engines_seen[norm]:
                counts[norm] += 1
                engines_seen[norm].add(eng)
            if len(r.get('body', '')) > len(seen[norm].get('body', '')):
                seen[norm]['body'] = r['body']

    ranked = sorted(seen.values(), key=lambda r: -counts.get(_normalize_url(r.get('href', '')), 0))
    return ranked


# ---------------------------------------------------------------------------
# Parallel execution (threads)
# ---------------------------------------------------------------------------
def _run_engine(name: str, fn, query: str, max_results: int) -> list[dict]:
    ck = _cache_key(name, query, max_results)
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    try:
        results = fn(query, max_results)
        if results:
            _cache_set(ck, results)
        return results or []
    except Exception as e:
        log.debug('Engine %s failed: %s', name, e)
        return []


def _run_engines_parallel(engines_to_run: list, query: str, max_results: int) -> list[dict]:
    """Run engines in parallel via threads, merge results."""
    all_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(engines_to_run), 1)) as pool:
        futures = {
            pool.submit(_run_engine, name, fn, query, max_results): name
            for name, fn, _, _ in engines_to_run
        }
        for future in concurrent.futures.as_completed(futures, timeout=_TIMEOUT + 8):
            try:
                results = future.result()
                if results:
                    all_results.extend(results)
            except Exception as e:
                log.debug('Engine %s error: %s', futures[future], e)
    return all_results


# ---------------------------------------------------------------------------
# Async parallel execution (for agents in an event loop)
# ---------------------------------------------------------------------------
async def _run_engines_async(engines_to_run: list, query: str, max_results: int) -> list[dict]:
    loop = asyncio.get_event_loop()
    all_results = []

    async def _run_one(name, fn):
        try:
            results = await asyncio.wait_for(
                loop.run_in_executor(_executor, _run_engine, name, fn, query, max_results),
                timeout=_TIMEOUT + 5
            )
            return results or []
        except Exception as e:
            log.debug('Async engine %s failed: %s', name, e)
            return []

    tasks = [_run_one(name, fn) for name, fn, _, _ in engines_to_run]
    results_lists = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results_lists:
        if isinstance(r, list):
            all_results.extend(r)
    return all_results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def search(query: str, max_results: int = 10, categories: str = 'general') -> list[dict]:
    """Search using all available engines in parallel, deduplicate, rank.

    Returns list of {title, href, body, engine}.
    Drop-in replacement for the old single-engine search().
    """
    ck = _cache_key('unified', query, max_results)
    cached = _cache_get(ck)
    if cached is not None:
        return cached[:max_results]

    engines_to_run = [(n, fn, k, f) for n, fn, k, f in _ENGINES if n in available_engines()]
    all_results = _run_engines_parallel(engines_to_run, query, max_results)
    deduped = _deduplicate(all_results)
    _cache_set(ck, deduped)
    return deduped[:max_results]


def search_fast(query: str, max_results: int = 10) -> list[dict]:
    """Fast search — DDGS only, no network overhead from slow engines."""
    ck = _cache_key('fast', query, max_results)
    cached = _cache_get(ck)
    if cached is not None:
        return cached[:max_results]
    results = _ddgs_search(query, max_results)
    _cache_set(ck, results)
    return results[:max_results]


def search_deep(query: str, max_results: int = 30) -> list[dict]:
    """Deep search — ALL engines for maximum coverage, higher limit."""
    ck = _cache_key('deep', query, max_results)
    cached = _cache_get(ck)
    if cached is not None:
        return cached[:max_results]

    engines_to_run = [(n, fn, k, f) for n, fn, k, f in _ENGINES if n in available_engines()]
    all_results = _run_engines_parallel(engines_to_run, query, max_results)
    deduped = _deduplicate(all_results)
    _cache_set(ck, deduped)
    return deduped[:max_results]


async def async_search(query: str, max_results: int = 10) -> list[dict]:
    """Async version of search() for agents already in an event loop."""
    ck = _cache_key('async', query, max_results)
    cached = _cache_get(ck)
    if cached is not None:
        return cached[:max_results]

    engines_to_run = [(n, fn, k, f) for n, fn, k, f in _ENGINES if n in available_engines()]
    all_results = await _run_engines_async(engines_to_run, query, max_results)
    deduped = _deduplicate(all_results)
    _cache_set(ck, deduped)
    return deduped[:max_results]


async def async_search_deep(query: str, max_results: int = 30) -> list[dict]:
    """Async deep search — ALL engines, maximum coverage."""
    ck = _cache_key('adeep', query, max_results)
    cached = _cache_get(ck)
    if cached is not None:
        return cached[:max_results]

    engines_to_run = [(n, fn, k, f) for n, fn, k, f in _ENGINES if n in available_engines()]
    all_results = await _run_engines_async(engines_to_run, query, max_results)
    deduped = _deduplicate(all_results)
    _cache_set(ck, deduped)
    return deduped[:max_results]


def search_news(query: str, max_results: int = 10) -> list[dict]:
    """Search news (SearXNG news category + DDGS fallback)."""
    results = _searxng_search(query, max_results, categories='news')
    if not results:
        results = _ddgs_search(query, max_results)
    return results


def search_images(query: str, max_results: int = 5) -> list[dict]:
    """Search images (SearXNG images category)."""
    return _searxng_search(query, max_results, categories='images')


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import sys
    q = ' '.join(sys.argv[1:]) or 'salesforce admin'
    print(f"\n=== Available engines: {available_engines()} ===\n")
    print(f"--- search('{q}') ---")
    results = search(q, max_results=20)
    engines_hit = set()
    for i, r in enumerate(results, 1):
        engines_hit.add(r['engine'])
        print(f"  {i}. [{r['engine']}] {r['title']}")
        print(f"     {r['href']}")
        if r['body']:
            print(f"     {r['body'][:120]}...")
        print()
    print(f"--- {len(results)} results from {len(engines_hit)} engines: {engines_hit} ---")
