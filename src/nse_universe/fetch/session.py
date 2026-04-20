"""Anti-bot session factory for NSE.

Layered defenses, in order of impact:
  1. curl_cffi's Chrome TLS fingerprint impersonation. This alone bypasses
     NSE's JA3-based filtering that would silently 403 plain `requests`.
  2. Cookie bootstrap: NSE sets `nsit`/`nseappid`/etc. on the homepage, and
     archive URLs rely on these. Skip the bootstrap and you get 403s even
     with correct TLS.
  3. Rotating realistic User-Agent strings (recent Chrome/Firefox/Edge).
  4. Realistic header pack (Accept, Accept-Language, Sec-Fetch-*, Referer).
  5. Jittered delays between requests — see fetch.bhav.
"""
from __future__ import annotations

import random
import time

from curl_cffi import requests as cffi_requests


USER_AGENTS = (
    # Desktop Chrome (Windows / macOS / Linux)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Desktop Firefox
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:124.0) Gecko/20100101 Firefox/124.0",
    # Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
)

IMPERSONATE_POOL = ("chrome124", "chrome120", "chrome116", "chrome110")

HOMEPAGE = "https://www.nseindia.com/"
WARMUP_ENDPOINTS = (
    "https://www.nseindia.com/all-reports",
    "https://www.nseindia.com/market-data/live-equity-market",
)


def _base_headers(ua: str) -> dict[str, str]:
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }


class NSESession:
    """Thin wrapper around curl_cffi Session with NSE-specific warmup."""

    def __init__(self, *, verbose: bool = False) -> None:
        self.verbose = verbose
        self._sess: cffi_requests.Session | None = None
        self._warmed = False
        self.ua = random.choice(USER_AGENTS)
        self.impersonate = random.choice(IMPERSONATE_POOL)

    @property
    def sess(self) -> cffi_requests.Session:
        if self._sess is None:
            self._sess = cffi_requests.Session(impersonate=self.impersonate)
            self._sess.headers.update(_base_headers(self.ua))
        return self._sess

    def warmup(self) -> None:
        """Visit NSE homepage + a couple of warm pages to harvest cookies."""
        if self._warmed:
            return
        s = self.sess
        s.get(HOMEPAGE, timeout=30)
        time.sleep(random.uniform(0.4, 1.1))
        for url in WARMUP_ENDPOINTS:
            try:
                s.get(url, timeout=30, headers={"Referer": HOMEPAGE})
            except Exception:
                pass  # non-critical
            time.sleep(random.uniform(0.2, 0.6))
        self._warmed = True

    def rotate(self) -> None:
        """Start a fresh session with new UA + impersonation profile."""
        if self._sess is not None:
            try:
                self._sess.close()
            except Exception:
                pass
        self._sess = None
        self._warmed = False
        self.ua = random.choice(USER_AGENTS)
        self.impersonate = random.choice(IMPERSONATE_POOL)

    def get(self, url: str, *, referer: str = HOMEPAGE, timeout: float = 60.0):
        self.warmup()
        headers = {
            "Referer": referer,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Dest": "empty",
        }
        return self.sess.get(url, headers=headers, timeout=timeout)

    def close(self) -> None:
        if self._sess is not None:
            try:
                self._sess.close()
            except Exception:
                pass
            self._sess = None
            self._warmed = False
