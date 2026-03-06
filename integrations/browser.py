"""Browser automation via Playwright — headless Chromium with anti-detection."""

import logging
from pathlib import Path
from typing import Optional

from config import DATA_DIR, FAMILY_TIMEZONE

log = logging.getLogger(__name__)

# Persistent browser profile for session/cookie persistence
PROFILE_DIR = DATA_DIR / "browser-profile"

# Singleton browser context
_browser = None
_page = None

_REALISTIC_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


async def _ensure_browser():
    """Lazy-init a persistent browser context. Reuses across calls."""
    global _browser, _page
    if _page and not _page.is_closed():
        return _page

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("Playwright is not installed. Browser tools are unavailable.")

    try:
        pw = await async_playwright().start()
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)

        _browser = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=True,
            user_agent=_REALISTIC_UA,
            viewport={"width": 1280, "height": 900},
            timezone_id=FAMILY_TIMEZONE,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        _page = _browser.pages[0] if _browser.pages else await _browser.new_page()
        log.info("Browser started (headless, persistent profile)")
        return _page
    except Exception as e:
        log.error(f"Failed to start browser: {e}")
        raise RuntimeError(f"Browser failed to start: {e}")


async def navigate(url: str) -> dict:
    """Navigate to a URL and return page info."""
    page = await _ensure_browser()
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return {
            "url": page.url,
            "title": await page.title(),
            "status": resp.status if resp else None,
        }
    except Exception as e:
        return {"error": str(e), "url": url}


async def screenshot(path: Optional[str] = None) -> str:
    """Take a screenshot of the current page. Returns file path."""
    page = await _ensure_browser()
    from config import TMP_DIR
    save_path = path or str(TMP_DIR / "screenshot.png")
    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=save_path, full_page=False)
    return save_path


async def snapshot() -> str:
    """Get page content as structured text (accessibility tree). Cheaper than screenshot."""
    page = await _ensure_browser()
    # Get readable text content
    content = await page.evaluate("""() => {
        // Remove scripts, styles, and hidden elements
        const clone = document.cloneNode(true);
        for (const el of clone.querySelectorAll('script, style, noscript, [hidden], [aria-hidden="true"]')) {
            el.remove();
        }
        return clone.body ? clone.body.innerText : document.title;
    }""")
    # Truncate to reasonable size
    return content[:8000] if content else "(empty page)"


async def click(selector: str) -> dict:
    """Click an element by CSS selector or text content."""
    page = await _ensure_browser()
    try:
        # Try CSS selector first
        try:
            await page.click(selector, timeout=5000)
            return {"success": True, "selector": selector}
        except Exception:
            pass
        # Fall back to text-based click
        await page.get_by_text(selector, exact=False).first.click(timeout=5000)
        return {"success": True, "text": selector}
    except Exception as e:
        return {"error": str(e), "selector": selector}


async def type_text(selector: str, text: str) -> dict:
    """Type text into an input field."""
    page = await _ensure_browser()
    try:
        await page.fill(selector, text, timeout=5000)
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}


async def select_option(selector: str, value: str) -> dict:
    """Select a dropdown option."""
    page = await _ensure_browser()
    try:
        await page.select_option(selector, value, timeout=5000)
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}


async def press_key(key: str) -> dict:
    """Press a keyboard key (Enter, Tab, Escape, etc.)."""
    page = await _ensure_browser()
    try:
        await page.keyboard.press(key)
        return {"success": True, "key": key}
    except Exception as e:
        return {"error": str(e)}


async def get_current_url() -> str:
    """Get the current page URL."""
    page = await _ensure_browser()
    return page.url


async def close_browser():
    """Close the browser (cleanup)."""
    global _browser, _page
    if _browser:
        try:
            await _browser.close()
        except Exception:
            pass
    _browser = _page = None
