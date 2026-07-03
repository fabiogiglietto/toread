"""Ingest paper suggestions from the `#toread` Slack channel.

Polled by `update_feed.yml` on every CI tick. The bot reads messages tagged
with the configured trigger hashtag (default `#zettelkasten`), resolves each
to a paper + PDF, and writes a minimal BibTeX entry into
`data/slack_inbox.bib`. The downstream pipeline picks that up just like a
Paperpile entry — the multi-source loader merges both files.

Decision flow per message (see plan §1):
  1. Already processed → skip.
  2. Contains the trigger hashtag?
  3. PDF source, in order:
        - PDF attached in the Slack message,
        - `arxiv.org/abs/<id>` URL → fetch PDF from arxiv,
        - DOI extractable → Unpaywall lookup,
        - else → reply in-thread asking for a PDF, record as `pending`.
  4. Validate PDF, upload to the dedicated Drive folder, append to
     `slack_inbox.bib`, post a ✅ confirmation in-thread.

`pending` messages are re-checked each tick by walking the thread for newer
file attachments.

This module deliberately keeps no Slack identities (`user`, channel name) in
the public feed; only `channel_id`, `ts`, and `permalink` end up there. The
suggester user-id stays in `data/slack_state.json` (committed to git but the
repo is public — so still avoid leaking real names; user-ids are opaque).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Local helpers (light imports so the module can be inspected without
# google or slack libs installed).
from .pdf_validator import (
    PDFCandidate,
    PDFValidationError,
    download_and_validate,
)
from .unpaywall_client import UnpaywallClient


DEFAULT_HASHTAG = "#zettelkasten"
DEFAULT_STATE_FILE = "data/slack_state.json"
DEFAULT_INBOX_BIB = "data/slack_inbox.bib"
DEFAULT_FEED = "output/feed.json"


# ---- Duplicate detection --------------------------------------------------
# Normalizers are a deliberate copy of
# fg-zettelkasten/mine-zettelkasten `src/state.py::normalize_doi/normalize_title`
# (the two repos can't share code). Keep them identical so the in-Slack
# "already in the archive" reply here and the downstream fg dedup net agree.

_DOI_PREFIX_RE = re.compile(r"^(?:https?://)?(?:dx\.)?doi\.org/", re.IGNORECASE)
_TITLE_STRIP_RE = re.compile(r"[^a-z0-9\s]")
_WS_NORM_RE = re.compile(r"\s+")


def _norm_doi(doi: Optional[str]) -> Optional[str]:
    if not doi:
        return None
    d = _DOI_PREFIX_RE.sub("", doi.strip()).lower().rstrip(".")
    return d or None


def _norm_title(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    folded = unicodedata.normalize("NFKD", title)
    folded = folded.encode("ascii", "ignore").decode("ascii").lower()
    folded = _WS_NORM_RE.sub(" ", _TITLE_STRIP_RE.sub(" ", folded)).strip()
    return folded if len(folded) >= 8 else None


def load_archive_index(feed_path: Path,
                       inbox_path: Path) -> Tuple[set, set]:
    """Normalized DOIs + titles already in the archive: the published
    `output/feed.json` (Paperpile curation + earlier Slack adds) plus the
    current `slack_inbox.bib` (entries from this/earlier ticks not yet folded
    into the feed). Both reads are best-effort — a missing file yields empty
    sets, never an error."""
    dois: set = set()
    titles: set = set()
    try:
        data = json.loads(Path(feed_path).read_text(encoding="utf-8"))
        for it in data.get("items", []):
            d = _norm_doi((it.get("_academic") or {}).get("doi"))
            if d:
                dois.add(d)
            t = _norm_title(it.get("title"))
            if t:
                titles.add(t)
    except Exception:
        pass
    try:
        text = Path(inbox_path).read_text(encoding="utf-8")
        for m in re.finditer(r"(?i)\bdoi\s*=\s*\{([^}]*)\}", text):
            d = _norm_doi(m.group(1))
            if d:
                dois.add(d)
        for m in re.finditer(r"(?i)\btitle\s*=\s*\{([^}]*)\}", text):
            t = _norm_title(m.group(1))
            if t:
                titles.add(t)
    except Exception:
        pass
    return dois, titles


# ---- URL / DOI extraction --------------------------------------------------


# A pragmatic DOI matcher. The full DOI grammar is wider; this catches the
# 99.9% case (10.xxxx/…). Used both on bare text and on the path portion of
# a URL.
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", re.IGNORECASE)

# Slack wraps URLs as <https://x|x> or <https://x>.
_SLACK_LINK_RE = re.compile(r"<(https?://[^|>\s]+)(?:\|[^>]*)?>")

# Naked URLs (best-effort; Slack usually wraps, but just in case).
_NAKED_URL_RE = re.compile(r"https?://[^\s<>]+")

_ARXIV_ABS_RE = re.compile(
    r"https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?",
    re.IGNORECASE,
)


def extract_urls(text: str) -> List[str]:
    """Return all URLs found in `text`, Slack-wrapped or naked, in order."""
    seen: List[str] = []
    for m in _SLACK_LINK_RE.finditer(text or ""):
        url = m.group(1)
        if url not in seen:
            seen.append(url)
    # Strip out Slack-wrapped fragments before scanning for naked URLs.
    stripped = _SLACK_LINK_RE.sub("", text or "")
    for m in _NAKED_URL_RE.finditer(stripped):
        url = m.group(0).rstrip(".,;)")
        if url not in seen:
            seen.append(url)
    return seen


def extract_doi(text: str, urls: Sequence[str] = ()) -> Optional[str]:
    """Best-effort DOI extraction from `text` and the URLs found in it."""
    # Direct DOI in text
    m = _DOI_RE.search(text or "")
    if m:
        return m.group(1).rstrip(".,;)")
    # DOI in a doi.org URL
    for url in urls:
        if "doi.org" in url.lower():
            mm = _DOI_RE.search(url)
            if mm:
                return mm.group(1).rstrip(".,;)")
    return None


# Standard DOI-bearing <meta> names: Highwire (citation_doi), Dublin Core
# (dc.identifier), PRISM (prism.doi) — used by essentially every academic
# publisher. We only trust these tags, not a body-text scan, to avoid picking
# up a cited reference's DOI.
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_META_ATTR_RE = re.compile(
    r"""(name|property|content)\s*=\s*["']([^"']*)["']""", re.IGNORECASE
)
_DOI_META_NAMES = frozenset((
    "citation_doi", "dc.identifier", "dc.identifier.doi", "prism.doi",
    "bepress_citation_doi", "doi",
))


def extract_doi_from_html(html: str) -> Optional[str]:
    """Pull a DOI from a landing page's `<meta>` tags, or None.

    Handles attribute order variants and `doi:`-prefixed Dublin Core values.
    """
    for tag in _META_TAG_RE.findall(html or ""):
        attrs = {k.lower(): v for k, v in _META_ATTR_RE.findall(tag)}
        label = (attrs.get("name") or attrs.get("property") or "").strip().lower()
        if label in _DOI_META_NAMES and attrs.get("content"):
            m = _DOI_RE.search(attrs["content"])
            if m:
                return m.group(1).rstrip(".,;)")
    return None


def _fetch_html(url: str, *, timeout: int = 15,
                max_bytes: int = 2_000_000) -> Optional[str]:
    """Fetch a landing page's HTML, best-effort and size-capped.

    DOIs live in <head> meta tags near the top, so we cap the read rather than
    pull a whole large page. Returns None for non-HTML responses.
    """
    import requests

    resp = requests.get(
        url,
        headers={"User-Agent": "ToRead/1.0 (slack-ingest; DOI discovery)"},
        timeout=timeout, stream=True,
    )
    resp.raise_for_status()
    ctype = (resp.headers.get("content-type") or "").lower()
    if "html" not in ctype and "xml" not in ctype:
        return None
    data = b""
    for chunk in resp.iter_content(chunk_size=65536):
        if chunk:
            data += chunk
            if len(data) >= max_bytes:
                break
    return data.decode(resp.encoding or "utf-8", errors="replace")


def extract_arxiv_id(urls: Sequence[str]) -> Optional[str]:
    """Return the ArXiv id (`2605.07069`) if any URL looks like ArXiv."""
    for url in urls:
        m = _ARXIV_ABS_RE.search(url)
        if m:
            return m.group(1)
    return None


def has_trigger_hashtag(text: str, hashtag: str) -> bool:
    """Case-insensitive, word-boundary check for the trigger hashtag."""
    if not text:
        return False
    needle = hashtag.lstrip("#").lower()
    return re.search(rf"(?i)(?:^|\W)#{re.escape(needle)}\b", text) is not None


# ---- State ----------------------------------------------------------------


@dataclass
class SlackIngestState:
    """Disk-backed cursor + ledger for what we've already done."""

    last_ts: str = "0"  # Slack ts is "seconds.microseconds" as string
    pending: Dict[str, dict] = field(default_factory=dict)
    processed: Dict[str, str] = field(default_factory=dict)  # ts -> bibkey
    # bibkey -> {channel_id, ts, permalink, pdf_source}.
    # The rss_generator reads this to emit the `_slack_suggestion` extension.
    processed_meta: Dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "SlackIngestState":
        if not path.exists():
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            return cls(
                last_ts=str(data.get("last_ts", "0")),
                pending=dict(data.get("pending", {})),
                processed=dict(data.get("processed", {})),
                processed_meta=dict(data.get("processed_meta", {})),
            )
        except Exception as e:
            logging.getLogger(__name__).warning(
                "Failed to load slack state from %s: %s", path, e
            )
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_ts": self.last_ts,
            "pending": self.pending,
            "processed": self.processed,
            "processed_meta": self.processed_meta,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)


# ---- BibTeX writer --------------------------------------------------------


def _escape_bib(value: str) -> str:
    """Light BibTeX escaping. Sufficient for values we write ourselves."""
    if value is None:
        return ""
    return (
        value.replace("\\", "\\\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\n", " ")
        .strip()
    )


def render_bib_entry(*, key: str, doi: Optional[str], title: Optional[str],
                     authors: Sequence[str], year: Optional[str],
                     url: Optional[str], abstract: Optional[str],
                     suggested_note: str) -> str:
    """Render a `@article{key, …}` block ready to append to the inbox file."""
    fields: List[Tuple[str, str]] = []
    if title:
        fields.append(("title", _escape_bib(title)))
    if authors:
        fields.append(("author", _escape_bib(" and ".join(authors))))
    if year:
        fields.append(("year", _escape_bib(year)))
    if doi:
        fields.append(("doi", _escape_bib(doi)))
    if url:
        fields.append(("url", _escape_bib(url)))
    if abstract:
        fields.append(("abstract", _escape_bib(abstract)))
    fields.append(("note", _escape_bib(suggested_note)))

    body = ",\n".join(f"  {name} = {{{val}}}" for name, val in fields)
    return f"@article{{{key},\n{body}\n}}\n"


# ---- Key minting ----------------------------------------------------------


def mint_bibkey(*, authors: Sequence[str], year: Optional[str],
                slack_ts: str) -> str:
    """Mint a Paperpile-shaped key, suffixed `-sl<2hex>` to mark Slack origin.

    The suffix is **deterministic** in `slack_ts` (sha1 → first 2 hex chars)
    so that re-processing the same Slack message — e.g. when a push fails
    after `state.json` was committed but before the cursor was updated —
    yields the same key, not a duplicate. `slack_ts` is globally unique per
    message in a workspace, so collisions across messages are vanishingly
    rare; collisions within the same message are by design.
    """
    suffix = "sl" + hashlib.sha1(slack_ts.encode("utf-8")).hexdigest()[:2]
    if authors:
        first = authors[0].split()[-1] if authors[0].split() else "Unknown"
        first = re.sub(r"[^A-Za-z]", "", first) or "Unknown"
    else:
        first = "Unknown"
    year_part = year if (year and re.match(r"^\d{4}$", str(year))) else "ND"
    if first == "Unknown" and year_part == "ND":
        # No metadata at all — fall back to a ts-derived stub so we still get
        # a stable, unique key.
        ts_short = slack_ts.replace(".", "")[:10]
        return f"Slack{ts_short}-{suffix}"
    return f"{first}{year_part}-{suffix}"


# ---- Paper resolution ----------------------------------------------------


@dataclass
class ResolvedPaper:
    """The minimum metadata we need to write a sensible BibTeX entry + name a
    Drive file."""

    doi: Optional[str] = None
    title: Optional[str] = None
    authors: List[str] = field(default_factory=list)
    year: Optional[str] = None
    url: Optional[str] = None
    abstract: Optional[str] = None
    arxiv_id: Optional[str] = None
    source: str = ""  # "crossref" | "arxiv" | "minimal"


class PaperResolver:
    """Translate a Slack message into a ResolvedPaper, using Crossref/ArXiv.

    The full enrichment still runs later in the main pipeline; we just need
    enough to mint a bibkey and a Drive filename.

    `enable_crossref` / `enable_arxiv` exist for test isolation — they let a
    unit test disable network calls without monkeypatching. Production code
    leaves both at the default `True`.
    """

    def __init__(self, enable_crossref: bool = True,
                 enable_arxiv: bool = True,
                 enable_doi_scrape: bool = True,
                 html_fetcher=None):
        self.enable_crossref = enable_crossref
        self.enable_arxiv = enable_arxiv
        # When no DOI is in the message/URL, fetch the landing page and read
        # its DOI <meta> tags. `html_fetcher` is injectable for tests.
        self.enable_doi_scrape = enable_doi_scrape
        self._html_fetcher = html_fetcher or _fetch_html
        self.logger = logging.getLogger(__name__)

    def resolve(self, *, text: str, urls: Sequence[str]) -> ResolvedPaper:
        arxiv_id = extract_arxiv_id(urls)
        if arxiv_id and self.enable_arxiv:
            paper = self._from_arxiv(arxiv_id)
            if paper:
                return paper

        doi = extract_doi(text, urls)
        # No DOI in the text/URL — try the landing page's meta tags. Covers
        # publisher links that don't embed the DOI in their path.
        if not doi and self.enable_doi_scrape:
            doi = self._doi_from_landing(urls, arxiv_id)
        if doi and self.enable_crossref:
            paper = self._from_crossref(doi)
            if paper:
                return paper

        # Last resort: a "minimal" resolved paper with just the URL/DOI we saw.
        return ResolvedPaper(
            doi=doi,
            url=urls[0] if urls else None,
            arxiv_id=arxiv_id,
            source="minimal",
        )

    def _doi_from_landing(self, urls: Sequence[str],
                          arxiv_id: Optional[str]) -> Optional[str]:
        """Fetch each non-arXiv URL and read a DOI from its <meta> tags."""
        for url in urls:
            if arxiv_id and "arxiv.org" in url.lower():
                continue
            try:
                html = self._html_fetcher(url)
            except Exception as e:  # network/parse issues must never break ingest
                self.logger.warning("Landing-page fetch failed for %s: %s", url, e)
                continue
            doi = extract_doi_from_html(html or "")
            if doi:
                self.logger.info("Resolved DOI %s from landing page %s", doi, url)
                return doi
        return None

    def _from_crossref(self, doi: str) -> Optional[ResolvedPaper]:
        # We need `title` for the Drive filename, but the existing
        # `EnrichedMetadata` shape doesn't include it. So make a direct call
        # to Crossref and parse the few fields we want — keeping the existing
        # module untouched.
        try:
            import requests
            from urllib.parse import quote
            resp = requests.get(
                f"https://api.crossref.org/works/{quote(doi, safe='')}",
                headers={"User-Agent": "ToRead/1.0 (slack-ingest)"},
                timeout=15,
            )
        except Exception as e:
            self.logger.warning("Crossref lookup failed for %s: %s", doi, e)
            return None
        if resp.status_code != 200:
            return None
        try:
            msg = resp.json().get("message", {})
        except ValueError:
            return None

        title = msg.get("title") or []
        title = title[0] if isinstance(title, list) and title else None

        authors: List[str] = []
        for a in msg.get("author") or []:
            given = (a.get("given") or "").strip()
            family = (a.get("family") or "").strip()
            name = " ".join(p for p in (given, family) if p)
            if name:
                authors.append(name)

        year = None
        for date_field in ("published-print", "published-online", "issued"):
            parts = (msg.get(date_field) or {}).get("date-parts") or []
            if parts and parts[0]:
                year = str(parts[0][0])
                break

        return ResolvedPaper(
            doi=doi,
            title=title,
            authors=authors,
            year=year,
            url=msg.get("URL") or f"https://doi.org/{doi}",
            abstract=msg.get("abstract"),
            source="crossref",
        )

    def _from_arxiv(self, arxiv_id: str) -> Optional[ResolvedPaper]:
        try:
            # The ArxivClient in metadata_enricher takes a title-based query
            # so we use the `arxiv` package directly for an id lookup.
            # arxiv>=3 removed Search.results(); fetch via Client().results().
            import arxiv
            search = arxiv.Search(id_list=[arxiv_id], max_results=1)
            results = arxiv.Client().results(search)
            paper = next(results, None)
        except Exception as e:
            self.logger.warning("ArXiv lookup failed for %s: %s", arxiv_id, e)
            return None
        if paper is None:
            return None
        return ResolvedPaper(
            doi=getattr(paper, "doi", None) or None,
            title=paper.title,
            authors=[a.name for a in paper.authors],
            year=str(paper.published.year) if paper.published else None,
            url=paper.entry_id,
            abstract=paper.summary,
            arxiv_id=arxiv_id,
            source="arxiv",
        )


# ---- Slack-side adapter (kept thin so it's mockable) ----------------------


class SlackAdapter:
    """Wraps slack_sdk WebClient. The methods we use are deliberately few."""

    def __init__(self, token: str):
        from slack_sdk import WebClient  # local import
        self.client = WebClient(token=token)
        self.token = token
        self.logger = logging.getLogger(__name__)

    def fetch_history(self, channel: str, *, oldest: str = "0",
                      limit: int = 100) -> List[dict]:
        from slack_sdk.errors import SlackApiError
        messages: List[dict] = []
        cursor: Optional[str] = None
        while True:
            try:
                resp = self.client.conversations_history(
                    channel=channel, oldest=oldest, limit=limit,
                    cursor=cursor,
                )
            except SlackApiError as e:
                self.logger.error("Slack history fetch failed: %s", e)
                break
            messages.extend(resp.get("messages", []))
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
            time.sleep(1.0)  # gentle paging
        # Slack returns newest-first; we want chronological.
        messages.sort(key=lambda m: float(m.get("ts", "0")))
        return messages

    def fetch_thread(self, channel: str, parent_ts: str) -> List[dict]:
        from slack_sdk.errors import SlackApiError
        try:
            resp = self.client.conversations_replies(
                channel=channel, ts=parent_ts, limit=200,
            )
        except SlackApiError as e:
            self.logger.error("Slack thread fetch failed: %s", e)
            return []
        return resp.get("messages", [])

    def post_thread_reply(self, channel: str, parent_ts: str, text: str) -> None:
        from slack_sdk.errors import SlackApiError
        try:
            self.client.chat_postMessage(
                channel=channel, thread_ts=parent_ts, text=text,
            )
        except SlackApiError as e:
            self.logger.error("Slack reply failed: %s", e)

    def get_permalink(self, channel: str, message_ts: str) -> Optional[str]:
        from slack_sdk.errors import SlackApiError
        try:
            resp = self.client.chat_getPermalink(
                channel=channel, message_ts=message_ts,
            )
        except SlackApiError as e:
            self.logger.warning("Slack permalink failed: %s", e)
            return None
        return resp.get("permalink")

    def display_name(self, user_id: Optional[str]) -> Optional[str]:
        """Resolve a Slack user-id to a human display name, or None.

        Needs the `users:read` scope. Used to publish submitter attribution on
        the team site — unlike upstream toread, which keeps identity private.
        """
        from slack_sdk.errors import SlackApiError
        if not user_id:
            return None
        try:
            resp = self.client.users_info(user=user_id)
        except SlackApiError as e:
            self.logger.warning("Slack users_info failed: %s", e)
            return None
        u = resp.get("user", {}) or {}
        prof = u.get("profile", {}) or {}
        return (prof.get("real_name") or u.get("real_name")
                or prof.get("display_name") or u.get("name"))


# ---- The orchestrator -----------------------------------------------------


@dataclass
class IngestConfig:
    channel_id: str
    hashtag: str = DEFAULT_HASHTAG
    state_file: Path = Path(DEFAULT_STATE_FILE)
    inbox_bib_file: Path = Path(DEFAULT_INBOX_BIB)
    feed_file: Path = Path(DEFAULT_FEED)
    dry_run: bool = False
    confirm_on_success: bool = True
    # When False, the trigger hashtag is not required: in a dedicated
    # submissions channel any message carrying a paper link or a PDF is treated
    # as a suggestion. (The MINE team channel is itself named #zettelkasten, so
    # the hashtag would render as a channel link, never the literal text.)
    require_hashtag: bool = True
    # When True, resolve and publish the suggester's Slack display name and
    # opaque user-id in the feed (`_slack_suggestion.submitted_by*`). Off by
    # default: upstream toread keeps suggester identity private; the MINE team
    # fork turns this on for site attribution and @-mentions.
    attribute_suggesters: bool = False


class SlackIngestor:
    def __init__(
        self,
        config: IngestConfig,
        slack: SlackAdapter,
        unpaywall: UnpaywallClient,
        drive_uploader,  # DriveUploader; typed loosely for testability
        resolver: PaperResolver,
        pdf_downloader=None,
    ):
        self.config = config
        self.slack = slack
        self.unpaywall = unpaywall
        self.drive = drive_uploader
        self.resolver = resolver
        # Allow tests to inject a fake downloader.
        self._download = pdf_downloader or download_and_validate
        self.logger = logging.getLogger(__name__)

    # ---- public entry point ----

    def run(self) -> dict:
        """Process new + pending messages. Returns a small summary dict."""
        state = SlackIngestState.load(self.config.state_file)
        # Papers already in the archive (published feed + inbox) — used to tell
        # a submitter their paper is a duplicate. Built once per run.
        self._archive_dois, self._archive_titles = load_archive_index(
            self.config.feed_file, self.config.inbox_bib_file
        )
        summary = {"added": 0, "asked_for_pdf": 0, "skipped": 0,
                   "duplicate": 0, "errors": 0}

        # 1. Re-poll threads we're waiting on.
        for parent_ts in list(state.pending.keys()):
            try:
                if self._retry_pending(state, parent_ts):
                    summary["added"] += 1
            except Exception as e:  # be defensive
                self.logger.exception("Pending retry failed for %s: %s",
                                      parent_ts, e)
                summary["errors"] += 1

        # 2. Process new messages since `last_ts`.
        messages = self.slack.fetch_history(
            self.config.channel_id, oldest=state.last_ts
        )
        for msg in messages:
            ts = msg.get("ts")
            if not ts:
                continue
            # Skip the cursor message itself, and anything already processed.
            if ts in state.processed or ts in state.pending:
                continue
            if ts == state.last_ts:
                continue
            try:
                outcome = self._process_message(state, msg)
                summary[outcome] = summary.get(outcome, 0) + 1
            except Exception as e:
                self.logger.exception("Failed to process message %s: %s", ts, e)
                summary["errors"] += 1
            # Always advance the cursor — even on skip — to avoid replaying.
            if float(ts) > float(state.last_ts or "0"):
                state.last_ts = ts

        if not self.config.dry_run:
            state.save(self.config.state_file)
            self.unpaywall.save()

        return summary

    # ---- per-message work ----

    def _process_message(self, state: SlackIngestState, msg: dict) -> str:
        """Returns one of: 'added' | 'asked_for_pdf' | 'skipped'."""
        text = msg.get("text") or ""
        ts = msg["ts"]

        if msg.get("subtype") and msg["subtype"] in {
            "channel_join", "channel_leave", "bot_message"
        }:
            return "skipped"

        # Never react to messages posted by a bot/app (including our own ✅ /
        # ask-for-PDF / duplicate replies). The `bot_message` subtype misses
        # chat.postMessage bot posts, which carry a `bot_id` instead.
        if msg.get("bot_id"):
            return "skipped"

        if self.config.require_hashtag:
            if not has_trigger_hashtag(text, self.config.hashtag):
                return "skipped"
        else:
            # Dedicated-channel mode: a message is a submission when it carries
            # a paper link or an attached PDF; plain chatter is ignored.
            if not extract_urls(text) and _first_pdf_file(msg) is None:
                return "skipped"

        return self._ingest(state, msg, text)

    def _ingest(self, state: SlackIngestState, msg: dict, text: str) -> str:
        ts = msg["ts"]
        channel = self.config.channel_id

        urls = extract_urls(text)
        attached_pdf_file = _first_pdf_file(msg)

        # Resolve paper-level metadata first (best-effort) so we can name files
        # and mint a key.
        paper = self.resolver.resolve(text=text, urls=urls)

        # Already in the archive? Tell the submitter and stop — no PDF fetch,
        # no inbox append. Matches on normalized DOI or title.
        nd, nt = _norm_doi(paper.doi), _norm_title(paper.title)
        if (nd and nd in self._archive_dois) or (nt and nt in self._archive_titles):
            self.logger.info(
                "Duplicate suggestion %s (%s)", ts, paper.doi or paper.title
            )
            if not self.config.dry_run:
                self.slack.post_thread_reply(
                    channel, ts,
                    "📚 This paper already looks like it's in the archive — "
                    "skipping it. Thanks for the suggestion!",
                )
            state.processed[ts] = "(duplicate)"
            return "duplicate"

        # Pick a PDF source.
        pdf_candidate: Optional[PDFCandidate] = None
        pdf_source = None

        if attached_pdf_file is not None:
            try:
                pdf_candidate = self._download(
                    attached_pdf_file["url_private_download"],
                    auth_header=f"Bearer {self.slack.token}",
                )
                pdf_source = "slack_attachment"
            except PDFValidationError as e:
                self.logger.warning("Slack-attached PDF rejected: %s", e)
                pdf_candidate = None

        if pdf_candidate is None and paper.arxiv_id:
            arxiv_pdf_url = f"https://arxiv.org/pdf/{paper.arxiv_id}.pdf"
            try:
                pdf_candidate = self._download(arxiv_pdf_url)
                pdf_source = "arxiv"
            except PDFValidationError as e:
                self.logger.warning("ArXiv PDF rejected: %s", e)
                pdf_candidate = None

        unpaywall_pdf_url: Optional[str] = None
        if pdf_candidate is None and paper.doi:
            up = self.unpaywall.lookup(paper.doi)
            if up and up.best_oa_pdf_url:
                unpaywall_pdf_url = up.best_oa_pdf_url
                try:
                    pdf_candidate = self._download(up.best_oa_pdf_url)
                    pdf_source = "unpaywall"
                except PDFValidationError as e:
                    self.logger.warning("Unpaywall PDF rejected: %s", e)
                    pdf_candidate = None

        if pdf_candidate is None:
            self._ask_for_pdf(state, msg, paper)
            return "asked_for_pdf"

        return self._finalise(state, msg, paper, pdf_candidate, pdf_source,
                              unpaywall_pdf_url)

    def _retry_pending(self, state: SlackIngestState, parent_ts: str) -> bool:
        """Re-check a thread for a new PDF attachment. Returns True if ingested."""
        ctx = state.pending[parent_ts]
        channel = self.config.channel_id
        thread = self.slack.fetch_thread(channel, parent_ts)
        # Sort chronological just in case.
        thread.sort(key=lambda m: float(m.get("ts", "0")))
        # The parent is also in the thread; look for an attachment anywhere.
        attached_msg = next(
            (m for m in thread if _first_pdf_file(m) is not None),
            None,
        )
        if attached_msg is None:
            return False

        # Reconstruct the URL/DOI context from the parent message.
        parent_text = ctx.get("text", "")
        urls = extract_urls(parent_text)
        paper = self.resolver.resolve(text=parent_text, urls=urls)

        attached_file = _first_pdf_file(attached_msg)
        try:
            pdf = self._download(
                attached_file["url_private_download"],
                auth_header=f"Bearer {self.slack.token}",
            )
        except PDFValidationError as e:
            self.logger.warning(
                "Pending PDF still invalid for %s: %s", parent_ts, e
            )
            return False

        self._finalise(state, {"ts": parent_ts, "user": ctx.get("user")},
                       paper, pdf, "slack_attachment_followup", None,
                       remove_from_pending=True)
        return True

    def _ask_for_pdf(self, state: SlackIngestState, msg: dict,
                     paper: ResolvedPaper) -> None:
        ts = msg["ts"]
        channel = self.config.channel_id
        permalink = self.slack.get_permalink(channel, ts) if not self.config.dry_run else None
        state.pending[ts] = {
            "channel_id": channel,
            "user": msg.get("user"),
            "text": msg.get("text", ""),
            "permalink": permalink,
            "first_seen": _now_iso(),
            "doi": paper.doi,
            "arxiv_id": paper.arxiv_id,
        }
        if not self.config.dry_run:
            self.slack.post_thread_reply(
                channel, ts,
                "Couldn't find an open-access copy of this paper. Please "
                "attach the PDF in this thread so I can add it.",
            )

    def _finalise(
        self,
        state: SlackIngestState,
        msg: dict,
        paper: ResolvedPaper,
        pdf: PDFCandidate,
        pdf_source: Optional[str],
        unpaywall_pdf_url: Optional[str],
        *,
        remove_from_pending: bool = False,
    ) -> str:
        ts = msg["ts"]
        channel = self.config.channel_id
        bibkey = mint_bibkey(authors=paper.authors, year=paper.year,
                             slack_ts=ts)

        # Drive filename mirrors Paperpile's `Author Year - Title.pdf` shape.
        from .drive_uploader import build_filename  # local for testability
        filename = build_filename(
            authors=paper.authors,
            year=paper.year,
            title=paper.title or (paper.doi or paper.url or "untitled"),
        )

        if not self.config.dry_run:
            self.drive.upload(filename=filename, content=pdf.content)

        permalink = state.pending.get(ts, {}).get("permalink")
        if permalink is None and not self.config.dry_run:
            permalink = self.slack.get_permalink(channel, ts)

        bib_entry = render_bib_entry(
            key=bibkey,
            doi=paper.doi,
            title=paper.title,
            authors=paper.authors,
            year=paper.year,
            url=paper.url,
            abstract=paper.abstract,
            suggested_note=(
                f"Suggested via Slack on {datetime.now(timezone.utc).date()}; "
                f"pdf_source={pdf_source}; ts={ts}"
                + (f"; unpaywall_pdf_url={unpaywall_pdf_url}"
                   if unpaywall_pdf_url else "")
            ),
        )
        # Belt-and-braces idempotency: if a previous run committed
        # slack_inbox.bib but lost state.json (push-retry edge case), we'd
        # otherwise append a duplicate here. The deterministic bibkey suffix
        # means re-processing yields the same key, so this check catches it.
        already_present = (
            not self.config.dry_run
            and _inbox_contains(self.config.inbox_bib_file,
                                bibkey=bibkey, doi=paper.doi)
        )
        if already_present:
            self.logger.info(
                "slack_inbox.bib already has %s — skipping append", bibkey
            )
        elif not self.config.dry_run:
            _append_bib(self.config.inbox_bib_file, bib_entry)

        # Track for the feed-side _slack_suggestion extension; written
        # alongside the state file so the rss_generator can pick it up.
        state.processed[ts] = bibkey
        if remove_from_pending:
            state.pending.pop(ts, None)
        meta = {
            "channel_id": channel,
            "ts": ts,
            "permalink": permalink,
            "pdf_source": pdf_source,
        }
        if self.config.attribute_suggesters:
            # Resolve the submitter's display name for site attribution. The
            # original author is on the message; for the follow-up path it was
            # captured in `pending[ts]["user"]` when we asked for the PDF.
            user_id = msg.get("user") or state.pending.get(ts, {}).get("user")
            submitted_by = (self.slack.display_name(user_id)
                            if user_id and not self.config.dry_run else None)
            # Only a real string is published; anything else (no name
            # resolved) stays out of the JSON-serialized state.
            meta["submitted_by"] = (submitted_by
                                    if isinstance(submitted_by, str) else None)
            # Opaque Slack user-id, published downstream so the team kasten
            # can @-mention the submitter in its #toread digest. Strictly less
            # sensitive than the display name already published above.
            meta["submitted_by_id"] = (user_id
                                       if isinstance(user_id, str) else None)
        state.processed_meta[bibkey] = meta

        if not self.config.dry_run and self.config.confirm_on_success:
            self.slack.post_thread_reply(
                channel, ts,
                f"✅ Added as `{bibkey}`. It'll appear after the next pipeline tick.",
            )

        return "added"


# ---- module-level utilities -----------------------------------------------


def _first_pdf_file(msg: dict) -> Optional[dict]:
    """Return the first PDF file attached to a Slack message, if any."""
    for f in msg.get("files") or []:
        if (f.get("mimetype") or "").lower() == "application/pdf":
            return f
        # Some clients attach with filetype rather than mimetype.
        if (f.get("filetype") or "").lower() == "pdf":
            return f
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_bib(path: Path, entry: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if path.exists() else "w"
    with open(path, mode, encoding="utf-8") as f:
        if mode == "a":
            f.write("\n")
        f.write(entry)


def _inbox_contains(path: Path, *, bibkey: str,
                    doi: Optional[str]) -> bool:
    """Return True if `slack_inbox.bib` already has an entry with this key or
    DOI. Used as a belt-and-braces idempotency guard: if a previous run's
    state was lost but its inbox-file commit survived, we won't append a
    duplicate.
    """
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return False
    # @article{<bibkey>, …
    if re.search(rf"@\w+\s*\{{\s*{re.escape(bibkey)}\s*,", text):
        return True
    if doi:
        # doi = {<doi>}  (case-insensitive — DOIs are)
        doi_pattern = re.escape(doi.strip())
        if re.search(rf"(?i)\bdoi\s*=\s*\{{\s*{doi_pattern}\s*\}}", text):
            return True
    return False


# ---- CLI entry point ------------------------------------------------------


def _build_config_from_env(args) -> Optional[IngestConfig]:
    channel = os.environ.get("SLACK_TOREAD_CHANNEL_ID")
    if not channel:
        logging.getLogger(__name__).warning(
            "SLACK_TOREAD_CHANNEL_ID not set — skipping Slack ingest."
        )
        return None
    hashtag = os.environ.get("SLACK_TRIGGER_HASHTAG", DEFAULT_HASHTAG)
    # SLACK_REQUIRE_HASHTAG=false → dedicated-channel mode (any link/PDF is a
    # submission). Defaults to true, preserving upstream toread behavior.
    require_hashtag = (os.environ.get(
        "SLACK_REQUIRE_HASHTAG", "true") or "true").lower() != "false"
    # SLACK_ATTRIBUTE_SUGGESTERS=true → publish submitter identity in the feed
    # (team fork). Defaults to false, preserving upstream privacy behavior.
    attribute_suggesters = (os.environ.get(
        "SLACK_ATTRIBUTE_SUGGESTERS", "false") or "false").lower() == "true"
    return IngestConfig(
        channel_id=channel,
        hashtag=hashtag,
        state_file=Path(args.state_file),
        inbox_bib_file=Path(args.inbox_bib),
        feed_file=Path(args.feed_file),
        dry_run=args.dry_run,
        confirm_on_success=(os.environ.get(
            "SLACK_CONFIRM_ON_SUCCESS", "true") or "true").lower() != "false",
        require_hashtag=require_hashtag,
        attribute_suggesters=attribute_suggesters,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest Slack #zettelkasten suggestions.")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    parser.add_argument("--inbox-bib", default=DEFAULT_INBOX_BIB)
    parser.add_argument("--feed-file", default=DEFAULT_FEED,
                        help="Published feed.json, read for duplicate detection.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Do not write files, post replies, or upload PDFs.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    cfg = _build_config_from_env(args)
    if cfg is None:
        return 0  # not configured; pipeline continues

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        logging.warning("SLACK_BOT_TOKEN not set — skipping Slack ingest.")
        return 0

    unpaywall_email = os.environ.get("UNPAYWALL_EMAIL")
    if not unpaywall_email:
        logging.warning(
            "UNPAYWALL_EMAIL not set — Unpaywall fallback unavailable."
        )

    drive_folder = os.environ.get("SLACK_INBOX_DRIVE_FOLDER_ID")
    if not drive_folder:
        logging.warning(
            "SLACK_INBOX_DRIVE_FOLDER_ID not set — skipping Slack ingest."
        )
        return 0

    # Build dependencies.
    slack = SlackAdapter(bot_token)
    if unpaywall_email:
        unpaywall = UnpaywallClient(email=unpaywall_email)
    else:
        unpaywall = _NullUnpaywall()
    from .drive_uploader import DriveUploader
    drive = DriveUploader(folder_id=drive_folder)

    resolver = PaperResolver()

    ingestor = SlackIngestor(
        config=cfg,
        slack=slack,
        unpaywall=unpaywall,
        drive_uploader=drive,
        resolver=resolver,
    )
    summary = ingestor.run()
    logging.info("Slack ingest summary: %s", summary)
    return 0


class _NullUnpaywall:
    """Stand-in when no UNPAYWALL_EMAIL is configured."""

    def lookup(self, doi):  # noqa: D401
        return None

    def save(self):  # noqa: D401
        return None


if __name__ == "__main__":
    sys.exit(main())
