"""Load and merge multiple BibTeX sources into one stream.

Today there are two sources:

  - `data/paperpile_export.bib` (the Paperpile export polled by CI)
  - `data/slack_inbox.bib` (papers suggested via the `#zettelkasten` Slack
    hashtag — see `src/slack_ingest.py`)

The two are merged here so the rest of the pipeline doesn't need to care.
Each loaded entry is tagged with its source so the feed generator can emit
extension fields (e.g. `_slack_suggestion`) where appropriate.

De-duplication: Paperpile wins. If a paper exists in both files, the
Slack-origin entry is dropped. Match rules:

  1. Same DOI (case-insensitive). Cheapest and most reliable.
  2. Failing DOI, fallback on `(normalized title, first-author surname,
     year)` exact match. Catches the Paperpile-imports-the-same-suggestion
     case.

The function is deliberately tiny — single I/O surface, no enrichment, no
network. Tests in `tests/test_bib_loader.py`.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

from .bibtex_parser import BibEntry, BibTeXParser


# (path, source_tag) pairs. Order matters: the first occurrence wins on
# dedup, so put the canonical source (Paperpile) first.
BibSource = Tuple[str, str]


def load_sources(sources: Sequence[BibSource]) -> List[BibEntry]:
    """Parse each file in `sources` and return one deduped list of entries.

    `sources` is a list of `(path, source_tag)`. Missing files are skipped
    with a warning — letting the pipeline tolerate "no Slack entries yet"
    on a fresh clone.
    """
    logger = logging.getLogger(__name__)
    all_entries: List[BibEntry] = []

    for path, tag in sources:
        p = Path(path)
        if not p.exists():
            logger.info("Bib source %s missing — skipping", path)
            continue
        # Each file gets its own parser so per-file state (e.g. `.entries`)
        # doesn't leak.
        parser = BibTeXParser()
        entries = parser.parse_file(str(p))
        for e in entries:
            e.source = tag
        logger.info("Loaded %d entries from %s (source=%s)",
                    len(entries), path, tag)
        all_entries.extend(entries)

    return _dedup(all_entries)


def _dedup(entries: Iterable[BibEntry]) -> List[BibEntry]:
    """Return entries with cross-source duplicates removed.

    Within a single source we **never** drop anything — the source is the
    ground truth for itself. We only drop a duplicate when the matching
    entry already in the kept list comes from a *different* source (e.g.
    Slack-origin entry duplicating a Paperpile one). This matches the
    "Paperpile wins on collision" rule from the plan without losing
    legitimate Paperpile entries that happen to share a fingerprint.
    """
    logger = logging.getLogger(__name__)
    by_doi: dict[str, BibEntry] = {}
    by_fingerprint: dict[Tuple[str, str, str], BibEntry] = {}
    kept: List[BibEntry] = []

    dropped = 0
    for entry in entries:
        doi = (entry.doi or "").strip().lower() or None
        fp = _fingerprint(entry)

        prior = None
        if doi and doi in by_doi:
            prior = by_doi[doi]
        elif not doi and fp and fp in by_fingerprint:
            prior = by_fingerprint[fp]

        if prior is not None and (prior.source or None) != (entry.source or None):
            logger.debug("Dropping %s (source=%s) — duplicate of %s (source=%s)",
                         entry.key, entry.source, prior.key, prior.source)
            dropped += 1
            continue

        kept.append(entry)
        if doi and doi not in by_doi:
            by_doi[doi] = entry
        if fp and fp not in by_fingerprint:
            by_fingerprint[fp] = entry

    if dropped:
        logger.info("De-dup dropped %d cross-source duplicates", dropped)
    return kept


def _fingerprint(entry: BibEntry) -> Tuple[str, str, str]:
    """A stable (title, first-author-surname, year) triple for fuzzy dedup.

    Returns ('', '', '') for entries with no usable signal; the caller will
    treat that as "no fingerprint" (never matches).
    """
    title = _normalize(entry.title or "")
    surname = ""
    if entry.authors:
        first = entry.authors[0]
        parts = first.split()
        if parts:
            surname = _normalize(parts[-1])
    year = (entry.year or "").strip()
    if not (title and surname and year):
        return ("", "", "")
    return (title, surname, year)


def _normalize(text: str) -> str:
    """Lowercase + strip non-word chars + collapse whitespace."""
    text = unicodedata.normalize("NFC", text).lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text
