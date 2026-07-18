"""Web search and URL fetch — local DuckDuckGo search (no API key needed)."""

import asyncio
import logging
import re
from html import unescape
from tools.base import BaseTool, make_tool_def
from core.api_client import ensure_external_api_allowed

log = logging.getLogger(__name__)


def _strip_html(html: str) -> str:
    """Remove HTML tags and decode entities to get plain text."""
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def _search_ddg_library(query: str, max_results: int = 5) -> list[dict]:
    """Search using the duckduckgo_search library (preferred)."""
    from duckduckgo_search import DDGS

    loop = asyncio.get_event_loop()

    def _do_search():
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return results

    raw = await loop.run_in_executor(None, _do_search)
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("href", r.get("link", "")),
            "snippet": r.get("body", r.get("snippet", "")),
        }
        for r in raw
    ]


async def _search_ddg_scrape(query: str, max_results: int = 5) -> list[dict]:
    """Fallback: scrape DuckDuckGo HTML results via httpx."""
    import httpx

    url = "https://html.duckduckgo.com/html/"
    params = {"q": query}
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
        resp = await client.post(url, data=params, headers=headers)
        resp.raise_for_status()
        html = resp.text

    results = []
    # Parse result blocks — each is in a <div class="result ...">
    blocks = re.findall(
        r'<a rel="nofollow" class="result__a" href="(.*?)">(.*?)</a>.*?'
        r'<a class="result__snippet"[^>]*>(.*?)</a>',
        html,
        re.DOTALL,
    )
    for href, title, snippet in blocks[:max_results]:
        results.append({
            "title": _strip_html(title),
            "url": unescape(href),
            "snippet": _strip_html(snippet),
        })

    return results


async def _do_search(query: str, max_results: int = 5) -> list[dict]:
    ensure_external_api_allowed("Web search")
    """Try the library first, fall back to scraping."""
    try:
        return await _search_ddg_library(query, max_results)
    except ImportError:
        log.info("duckduckgo_search not installed, falling back to HTML scrape")
    except Exception as e:
        log.warning(f"duckduckgo_search failed ({e}), falling back to HTML scrape")

    try:
        return await _search_ddg_scrape(query, max_results)
    except Exception as e:
        log.error(f"DuckDuckGo scrape also failed: {e}")
        return []


async def _fetch_url(url: str, max_chars: int = 10000) -> str:
    ensure_external_api_allowed("URL fetch")
    """Fetch a URL and return its text content (HTML stripped)."""
    try:
        import httpx
    except ImportError:
        return "Error: httpx is not installed. Run: pip install httpx"

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")

        if "text/html" in content_type or "application/xhtml" in content_type:
            text = _strip_html(resp.text)
        elif "text/" in content_type or "json" in content_type or "xml" in content_type:
            text = resp.text
        else:
            text = resp.text[:max_chars] if resp.text else "(binary content, not displayable)"

    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n... [truncated]"
    return text


class WebSearchTool(BaseTool):
    name = "web_search"
    description = "Search the web and fetch URL contents using DuckDuckGo"

    def get_definitions(self) -> list[dict]:
        return [
            make_tool_def(
                "web_search",
                "Search the web for a query using DuckDuckGo. Returns top results with titles, URLs, and snippets.",
                {
                    "query": {
                        "type": "string",
                        "description": "The search query",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default: 5, max: 10)",
                    },
                },
                ["query"],
            ),
            make_tool_def(
                "web_fetch",
                "Fetch a specific URL and return its text content (HTML tags stripped). Useful for reading articles, docs, or pages found via web_search.",
                {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum characters to return (default: 10000)",
                    },
                },
                ["url"],
            ),
        ]

    async def handle(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "web_search":
            query = tool_input["query"]
            max_results = min(tool_input.get("max_results", 5), 10)

            try:
                results = await _do_search(query, max_results)
            except Exception as e:
                return f"Search error: {e}"

            if not results:
                return f"No results found for: {query}"

            lines = [f"Search results for: {query}\n"]
            for i, r in enumerate(results, 1):
                lines.append(f"{i}. {r['title']}")
                lines.append(f"   URL: {r['url']}")
                if r.get("snippet"):
                    lines.append(f"   {r['snippet']}")
                lines.append("")
            return "\n".join(lines)

        elif tool_name == "web_fetch":
            url = tool_input["url"]
            max_chars = tool_input.get("max_chars", 10000)

            try:
                text = await _fetch_url(url, max_chars)
                return f"Content from {url}:\n\n{text}"
            except Exception as e:
                return f"Error fetching {url}: {e}"

        else:
            return f"Unknown web tool: {tool_name}"
