"""PDF download + validation.

Used to keep junk out of the pipeline: an Unpaywall `url_for_pdf` is often
*not* a PDF (landing pages, soft-paywalls, HTML "view PDF" wrappers, expired
hosts). Without validation, we'd silently ingest broken files that then fail
downstream in research-radio.

A candidate PDF must:
- be reachable (HTTP 2xx),
- have `Content-Type: application/pdf` (or octet-stream with a `.pdf` URL),
- start with the `%PDF-` magic bytes,
- be at least 10 KB.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import requests


MIN_PDF_BYTES = 10 * 1024
PDF_MAGIC = b"%PDF-"


@dataclass
class PDFCandidate:
    """A validated PDF — bytes are in memory; caller decides what to do."""

    url: str
    content: bytes
    content_type: str

    @property
    def size(self) -> int:
        return len(self.content)


class PDFValidationError(Exception):
    """Raised when a download isn't actually a usable PDF."""


def download_and_validate(
    url: str,
    *,
    timeout: int = 30,
    user_agent: str = "ToRead/1.0 (slack-ingest)",
    auth_header: Optional[str] = None,
    max_bytes: int = 50 * 1024 * 1024,
) -> PDFCandidate:
    """Download `url` and verify it's a real PDF.

    Raises PDFValidationError on any failure. Caller is expected to catch and
    fall back to "no PDF available" behaviour.

    `auth_header`: optional value for an `Authorization` header (used when
    fetching Slack-attached files via `url_private_download`, which requires
    the bot token).
    """
    logger = logging.getLogger(__name__)
    headers = {"User-Agent": user_agent}
    if auth_header:
        headers["Authorization"] = auth_header

    try:
        resp = requests.get(
            url, headers=headers, timeout=timeout, stream=True,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        raise PDFValidationError(f"download failed: {e}") from e

    if resp.status_code != 200:
        raise PDFValidationError(f"HTTP {resp.status_code} for {url}")

    content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    is_url_pdf = urlparse(url).path.lower().endswith(".pdf")
    if content_type != "application/pdf" and not (
        content_type in ("application/octet-stream", "binary/octet-stream") and is_url_pdf
    ):
        # Read a sniff so the close-with-content message is informative.
        sniff = b""
        for chunk in resp.iter_content(chunk_size=512):
            sniff += chunk
            if len(sniff) >= 64:
                break
        resp.close()
        raise PDFValidationError(
            f"non-PDF content-type {content_type!r} for {url}; "
            f"first bytes: {sniff[:32]!r}"
        )

    # Read the body but cap it.
    buf = bytearray()
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        buf.extend(chunk)
        if len(buf) > max_bytes:
            resp.close()
            raise PDFValidationError(
                f"PDF exceeds max_bytes={max_bytes} for {url}"
            )

    body = bytes(buf)
    if len(body) < MIN_PDF_BYTES:
        raise PDFValidationError(
            f"PDF too small ({len(body)} bytes < {MIN_PDF_BYTES}) for {url}"
        )
    if not body.startswith(PDF_MAGIC):
        raise PDFValidationError(
            f"missing %PDF- magic header for {url} (got {body[:8]!r})"
        )

    logger.debug("Validated PDF: %s (%d bytes)", url, len(body))
    return PDFCandidate(url=url, content=body, content_type=content_type)
