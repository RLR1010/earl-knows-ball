"""IndexNow submission (Bing, Yandex, Seznam, Naver instant indexing).

IndexNow is a simple ping protocol: POST a JSON body containing the host, a
key, the location of that key file, and the list of URLs to (re)crawl. Bing
uses these pings to crawl new/updated URLs within minutes instead of waiting
for the next organic crawl.

Setup per host:
  1. Put a text file named ``{key}.txt`` at the site root containing the key
     (frontend ``public/{key}.txt``).
  2. Set ``INDEXNOW_KEY`` (and ``SITE_URL``) in the backend ``.env``.
Google does not use IndexNow; for Google, submit the RSS feed as a sitemap /
use Search Console. Bing Webmaster Tools also accepts an RSS/Atom feed URL
directly as a sitemap.
"""
from __future__ import annotations

import logging

import httpx

from app.core.config import settings

log = logging.getLogger("earl.indexnow")

INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"

# Only ping search engines from the real production host. Even if the feature is
# enabled by mistake in dev/staging, submissions for any other host are refused.
ALLOWED_HOSTS = {"earlknowsball.com"}


def _host_of(site_url: str) -> str:
    return site_url.replace("https://", "").replace("http://", "").rstrip("/")


async def submit_urls(
    urls: list[str],
    *,
    site_url: str | None = None,
    key: str | None = None,
    enabled: bool | None = None,
) -> dict:
    """Submit a list of URLs to IndexNow.

    ``urls`` may be absolute or site-relative (``/nfl/articles/foo``). Returns a
    result dict; never raises (network errors are reported, not thrown).

    Refuses to submit unless the feature is enabled AND the resolved host is in
    ``ALLOWED_HOSTS`` — so a dev/staging box can never ping search engines.
    """
    if not (settings.indexnow_enabled if enabled is None else enabled):
        return {"ok": False, "skipped": True, "reason": "indexnow disabled"}

    site = (site_url or settings.site_url or "").rstrip("/")
    key = key or settings.indexnow_key
    if not key:
        return {"ok": False, "skipped": True, "reason": "indexnow_key not configured"}
    if not site:
        return {"ok": False, "skipped": True, "reason": "site_url not configured"}

    host = _host_of(site)
    if host not in ALLOWED_HOSTS:
        return {
            "ok": False,
            "skipped": True,
            "reason": f"host {host!r} not in allowlist {sorted(ALLOWED_HOSTS)}",
        }

    full = [u if u.startswith("http") else f"{site}{u if u.startswith('/') else '/' + u}" for u in urls]
    # de-dup, keep order
    seen: set[str] = set()
    full = [u for u in full if not (u in seen or seen.add(u))]
    if not full:
        return {"ok": True, "submitted": 0}

    payload = {
        "host": host,
        "key": key,
        "keyLocation": f"{site}/{key}.txt",
        "urlList": full,
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(INDEXNOW_ENDPOINT, json=payload)
        ok = resp.status_code in (200, 202)
        if not ok:
            log.warning("IndexNow returned %s: %s", resp.status_code, resp.text[:200])
        return {
            "ok": ok,
            "status": resp.status_code,
            "submitted": len(full),
            "host": payload["host"],
        }
    except Exception as exc:  # network/transport
        log.warning("IndexNow submit failed: %s", exc)
        return {"ok": False, "error": str(exc), "submitted": 0}
