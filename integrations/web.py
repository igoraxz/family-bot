"""Web search and fetch integration."""

import logging
import re

import httpx

log = logging.getLogger(__name__)

_http: httpx.AsyncClient | None = None


async def get_client() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    return _http


async def fetch_url(url: str) -> dict:
    """Fetch a URL and return {success, content, status_code, content_type}.

    Content is returned as text, truncated to 50k chars.
    """
    client = await get_client()
    try:
        resp = await client.get(url)
        content_type = resp.headers.get("content-type", "")
        text = resp.text[:50000]

        # Basic HTML → text stripping for readability
        if "html" in content_type:
            text = _strip_html(text)

        return {
            "success": True,
            "content": text,
            "status_code": resp.status_code,
            "content_type": content_type,
        }
    except Exception as e:
        log.error(f"fetch_url error: {e}")
        return {"success": False, "content": str(e), "status_code": 0, "content_type": ""}


async def web_search(query: str, num_results: int = 5) -> dict:
    """Search the web using DuckDuckGo via ddgs library."""
    import asyncio
    try:
        from ddgs import DDGS

        def _search():
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=num_results))

        raw = await asyncio.to_thread(_search)

        results = []
        for r in raw:
            results.append({
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": r.get("body", ""),
            })

        return {
            "success": True,
            "query": query,
            "results": results,
            "count": len(results),
        }
    except Exception as e:
        log.error(f"web_search error: {e}")
        return {"success": False, "query": query, "results": [], "error": str(e)}


def _strip_html(html: str) -> str:
    """Basic HTML to text conversion."""
    # Remove script and style blocks
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Remove tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:50000]
