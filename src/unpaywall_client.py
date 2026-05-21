"""Unpaywall HTTP client — look up open-access PDF URLs by DOI.

Unpaywall (https://unpaywall.org) is a free service backed by the OurResearch
non-profit. The only credential is an email address (the "polite pool"
identifier); rate limits are generous (100k requests/day) so a single
per-request cache and a 1-second sleep between live calls is overkill but
cheap.

This client is used by `slack_ingest` to find a PDF URL for papers suggested
in Slack without an attachment. It is *not* (yet) wired into the main
`metadata_enricher` corpus — that's a follow-up.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests


@dataclass
class UnpaywallResult:
    """The bits of an Unpaywall response we actually use downstream."""

    doi: str
    is_oa: bool
    best_oa_pdf_url: Optional[str] = None
    license: Optional[str] = None
    host_type: Optional[str] = None  # "publisher" | "repository"
    raw: Optional[dict] = None  # full response for debugging; not cached on disk


class UnpaywallClient:
    """Minimal Unpaywall client with on-disk caching keyed by DOI."""

    BASE_URL = "https://api.unpaywall.org/v2"

    def __init__(
        self,
        email: str,
        cache_file: str = "cache/unpaywall_cache.json",
        cache_duration_days: int = 30,
        rate_limit_seconds: float = 1.0,
        timeout: int = 15,
        user_agent: str = "ToRead/1.0 (slack-ingest)",
    ):
        if not email or "@" not in email:
            raise ValueError(
                "Unpaywall requires a valid email address as the polite-pool "
                "identifier. Set UNPAYWALL_EMAIL or pass email=…"
            )
        self.email = email
        self.cache_file = Path(cache_file)
        self.cache_duration = timedelta(days=cache_duration_days)
        self.rate_limit = rate_limit_seconds
        self.timeout = timeout
        self.user_agent = user_agent
        self.logger = logging.getLogger(__name__)

        self._cache: dict = {}
        self._last_request_ts: float = 0.0
        self._load_cache()

    # ---- public API --------------------------------------------------------

    def lookup(self, doi: str) -> Optional[UnpaywallResult]:
        """Return an UnpaywallResult for the DOI, or None on network/404."""
        doi = self._normalize_doi(doi)
        if not doi:
            return None

        cached = self._get_cached(doi)
        if cached is not None:
            self.logger.debug("Unpaywall cache hit for %s", doi)
            return cached

        self._throttle()
        url = f"{self.BASE_URL}/{quote(doi, safe='/')}"
        params = {"email": self.email}
        try:
            resp = requests.get(
                url,
                params=params,
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            self.logger.warning("Unpaywall request failed for %s: %s", doi, e)
            return None

        if resp.status_code == 404:
            # Not found — cache the negative so we don't ask every tick.
            result = UnpaywallResult(doi=doi, is_oa=False)
            self._store(doi, result)
            return result

        if resp.status_code != 200:
            self.logger.warning(
                "Unpaywall returned %s for %s", resp.status_code, doi
            )
            return None

        try:
            payload = resp.json()
        except ValueError:
            self.logger.warning("Unpaywall returned non-JSON for %s", doi)
            return None

        result = self._parse(payload, doi)
        self._store(doi, result)
        return result

    def save(self) -> None:
        """Persist the cache to disk. Safe to call repeatedly."""
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.logger.error("Failed to save Unpaywall cache: %s", e)

    # ---- internals ---------------------------------------------------------

    @staticmethod
    def _normalize_doi(doi: str) -> str:
        """Trim common prefixes and lowercase. DOIs are case-insensitive."""
        if not doi:
            return ""
        doi = doi.strip()
        for prefix in ("doi:", "https://doi.org/", "http://doi.org/",
                       "https://dx.doi.org/", "http://dx.doi.org/"):
            if doi.lower().startswith(prefix):
                doi = doi[len(prefix):]
                break
        return doi.lower()

    def _parse(self, payload: dict, doi: str) -> UnpaywallResult:
        is_oa = bool(payload.get("is_oa"))
        best = payload.get("best_oa_location") or {}
        pdf_url = best.get("url_for_pdf") if isinstance(best, dict) else None
        license_ = best.get("license") if isinstance(best, dict) else None
        host_type = best.get("host_type") if isinstance(best, dict) else None
        return UnpaywallResult(
            doi=doi,
            is_oa=is_oa,
            best_oa_pdf_url=pdf_url,
            license=license_,
            host_type=host_type,
            raw=None,  # don't persist the full body
        )

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_ts
        if elapsed < self.rate_limit:
            time.sleep(self.rate_limit - elapsed)
        self._last_request_ts = time.time()

    def _load_cache(self) -> None:
        if not self.cache_file.exists():
            return
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                self._cache = json.load(f)
            self.logger.info(
                "Loaded Unpaywall cache with %d entries", len(self._cache)
            )
        except Exception as e:
            self.logger.warning("Failed to load Unpaywall cache: %s", e)
            self._cache = {}

    def _get_cached(self, doi: str) -> Optional[UnpaywallResult]:
        item = self._cache.get(doi)
        if not item:
            return None
        try:
            cached_at = datetime.fromisoformat(item["cached_at"])
        except (KeyError, ValueError):
            return None
        if datetime.now() - cached_at > self.cache_duration:
            del self._cache[doi]
            return None
        data = item.get("result", {})
        # Don't restore `raw`; it isn't serialized.
        return UnpaywallResult(
            doi=data.get("doi", doi),
            is_oa=bool(data.get("is_oa")),
            best_oa_pdf_url=data.get("best_oa_pdf_url"),
            license=data.get("license"),
            host_type=data.get("host_type"),
        )

    def _store(self, doi: str, result: UnpaywallResult) -> None:
        serializable = asdict(result)
        serializable.pop("raw", None)
        self._cache[doi] = {
            "cached_at": datetime.now().isoformat(),
            "result": serializable,
        }
