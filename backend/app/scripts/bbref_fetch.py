"""Robust bball-ref fetcher: 429/5xx-aware retry with exponential backoff + disk cache.

bball-ref rate-limits aggressively (HTTP 429). This module:
- retries on 429/5xx with exponential backoff (respects Retry-After if present)
- caches successful HTML to disk (keyed by URL) so re-runs never refetch
- paces to a max requests/sec to avoid tripping the limiter

Used by the br_id backfill and boxscore cross-check. Accuracy + politeness first.
"""
import logging
import os
import re
import time
import urllib.request
import urllib.error

logger = logging.getLogger("earl.bbref_fetch")

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

DEFAULT_CACHE_DIR = os.environ.get("BBREF_CACHE_DIR", "app/scripts/bbref_cache")
DEFAULT_MIN_INTERVAL = float(os.environ.get("BBREF_MIN_INTERVAL", "2.0"))  # seconds between requests


class BBRateLimited(Exception):
    pass


class Fetch:
    def __init__(self, cache_dir=DEFAULT_CACHE_DIR, min_interval=DEFAULT_MIN_INTERVAL):
        self.cache_dir = cache_dir
        self.min_interval = min_interval
        self._last_req = 0.0
        self._hits = 0
        self._misses = 0
        os.makedirs(cache_dir, exist_ok=True)

    def _cache_path(self, url):
        safe = re.sub(r"[^A-Za-z0-9]+", "_", url)
        return os.path.join(self.cache_dir, safe[:180] + ".html")

    def fetch(self, url, allow_cache=True, max_tries=5):
        """Fetch a URL with 429/5xx backoff and disk cache. Raises BBRateLimited
        if still 429 after max_tries; raises on other HTTP errors."""
        cp = self._cache_path(url)
        if allow_cache and os.path.exists(cp):
            self._hits += 1
            with open(cp, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()

        delay = self.min_interval - (time.time() - self._last_req)
        if delay > 0:
            time.sleep(delay)

        wait = 10.0
        for attempt in range(max_tries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                           "Accept": "text/html,application/xhtml+xml"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    html = r.read().decode("utf-8", "ignore")
                self._last_req = time.time()
                self._misses += 1
                if allow_cache:
                    try:
                        with open(cp, "w", encoding="utf-8") as f:
                            f.write(html)
                    except OSError:
                        pass
                return html
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    ra = e.headers.get("Retry-After")
                    backoff = wait if not ra else max(wait, float(ra))
                    logger.warning(f"429 on {url}; backing off {backoff:0.0f}s (attempt {attempt+1}/{max_tries})")
                    time.sleep(backoff)
                    wait *= 2.5
                    self._last_req = time.time()
                elif e.code >= 500:
                    logger.warning(f"HTTP {e.code} on {url}; retry {attempt+1}")
                    time.sleep(wait)
                    wait *= 2.0
                else:
                    logger.warning(f"HTTP {e.code} on {url} (not retrying): {e}")
                    raise
            except urllib.error.URLError as e:
                logger.warning(f"URLError on {url}: {e}; retry {attempt+1}")
                time.sleep(wait)
                wait *= 2.0
            except Exception as e:
                logger.warning(f"fetch error {url}: {e!r}; retry {attempt+1}")
                time.sleep(wait)
                wait *= 2.0
        raise BBRateLimited(f"still 429/error after {max_tries} tries: {url}")

    @property
    def cache_hits(self):
        return self._hits

    @property
    def cache_misses(self):
        return self._misses


fetch_client = Fetch()
