"""Tests for src.slack_ingest.

These exercise the URL/DOI/hashtag helpers and the decision branches of the
orchestrator with everything network-y stubbed out.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.pdf_validator import PDFCandidate, PDFValidationError
from src.slack_ingest import (
    DEFAULT_HASHTAG,
    IngestConfig,
    PaperResolver,
    ResolvedPaper,
    SlackIngestState,
    SlackIngestor,
    extract_doi,
    extract_urls,
    extract_arxiv_id,
    has_trigger_hashtag,
    mint_bibkey,
    render_bib_entry,
)


# ---- URL / DOI / hashtag helpers ----------------------------------------


def test_extract_urls_handles_slack_wrap():
    text = "Look: <https://example.org/paper|paper> and naked https://arxiv.org/abs/2605.07069"
    urls = extract_urls(text)
    assert "https://example.org/paper" in urls
    assert "https://arxiv.org/abs/2605.07069" in urls
    assert len(urls) == 2


def test_extract_urls_dedups_and_keeps_order():
    text = "<https://a.org> <https://a.org|a> https://a.org"
    assert extract_urls(text) == ["https://a.org"]


def test_extract_doi_from_text():
    assert extract_doi("Read 10.1234/abcd.efgh now", []) == "10.1234/abcd.efgh"


def test_extract_doi_from_doi_org_url():
    urls = ["https://doi.org/10.5204/mcj.3247"]
    assert extract_doi("see this", urls) == "10.5204/mcj.3247"


def test_extract_doi_returns_none_when_absent():
    assert extract_doi("no doi here", ["https://example.org/x"]) is None


def test_extract_arxiv_id():
    urls = ["https://arxiv.org/abs/2605.07069v2"]
    assert extract_arxiv_id(urls) == "2605.07069"


def test_has_trigger_hashtag_case_insensitive():
    assert has_trigger_hashtag("Please add #Zettelkasten", "#zettelkasten")
    assert has_trigger_hashtag("#ZETTELKASTEN at start", "#zettelkasten")
    assert not has_trigger_hashtag("nothing here", "#zettelkasten")


def test_has_trigger_hashtag_word_boundary():
    assert not has_trigger_hashtag("#zettelkastens", "#zettelkasten")
    assert has_trigger_hashtag("foo #zettelkasten bar", "#zettelkasten")


# ---- key minting --------------------------------------------------------


def test_mint_bibkey_uses_author_year():
    key = mint_bibkey(authors=["Jane Smith"], year="2026",
                     slack_ts="1715000000.000123")
    assert key.startswith("Smith2026-sl")
    assert len(key.split("-")[1]) == 4  # "sl" + 2 hex


def test_mint_bibkey_falls_back_when_no_metadata():
    key = mint_bibkey(authors=[], year=None, slack_ts="1715000000.000123")
    assert key.startswith("Slack")
    assert "-sl" in key


def test_mint_bibkey_strips_non_letters_from_surname():
    key = mint_bibkey(authors=["Anne-Marie O'Brien"], year="2026",
                     slack_ts="1.0")
    # Last-token is "O'Brien" → letters only "OBrien"
    assert key.startswith("OBrien2026-sl")


# ---- bibtex rendering ---------------------------------------------------


def test_render_bib_entry_basic_shape():
    out = render_bib_entry(
        key="Smith2026-sla7",
        doi="10.1/x",
        title="Hello world",
        authors=["Jane Smith", "John Doe"],
        year="2026",
        url="https://doi.org/10.1/x",
        abstract="An abstract.",
        suggested_note="Suggested via Slack on 2026-05-20",
    )
    assert out.startswith("@article{Smith2026-sla7,")
    assert "title = {Hello world}" in out
    assert "author = {Jane Smith and John Doe}" in out
    assert "doi = {10.1/x}" in out
    assert "note = {Suggested via Slack on 2026-05-20}" in out
    assert out.rstrip().endswith("}")


def test_render_bib_entry_escapes_braces():
    out = render_bib_entry(
        key="K-sla1", doi=None, title="A {weird} title",
        authors=[], year=None, url=None, abstract=None,
        suggested_note="x",
    )
    assert r"\{" in out and r"\}" in out


# ---- state persistence --------------------------------------------------


def test_state_roundtrip(tmp_path):
    state = SlackIngestState(
        last_ts="1715000000.000001",
        pending={"1.0": {"text": "x"}},
        processed={"2.0": "Smith2026-sla7"},
        processed_meta={"Smith2026-sla7": {"ts": "2.0", "channel_id": "C1"}},
    )
    p = tmp_path / "state.json"
    state.save(p)
    loaded = SlackIngestState.load(p)
    assert loaded.last_ts == "1715000000.000001"
    assert loaded.pending == {"1.0": {"text": "x"}}
    assert loaded.processed == {"2.0": "Smith2026-sla7"}
    assert loaded.processed_meta == {
        "Smith2026-sla7": {"ts": "2.0", "channel_id": "C1"}
    }


def test_state_load_missing_returns_default(tmp_path):
    loaded = SlackIngestState.load(tmp_path / "missing.json")
    assert loaded.last_ts == "0"
    assert loaded.pending == {}


# ---- orchestrator decision branches -------------------------------------


def _real_pdf_bytes():
    return b"%PDF-" + b"x" * 20_000


def _candidate_pdf(url="https://example.org/x.pdf"):
    return PDFCandidate(url=url, content=_real_pdf_bytes(),
                       content_type="application/pdf")


def _build_ingestor(tmp_path, *, downloader=None, unpaywall=None,
                    resolver=None):
    cfg = IngestConfig(
        channel_id="C123",
        hashtag=DEFAULT_HASHTAG,
        state_file=tmp_path / "state.json",
        inbox_bib_file=tmp_path / "inbox.bib",
        dry_run=False,
        confirm_on_success=True,
    )
    slack = MagicMock()
    slack.token = "xoxb-test"
    slack.fetch_history.return_value = []
    slack.fetch_thread.return_value = []
    slack.get_permalink.return_value = "https://slack.example/p"
    drive = MagicMock()
    drive.upload.return_value = {"id": "F", "name": "n.pdf",
                                 "webViewLink": "https://drive/x"}
    unpaywall = unpaywall or MagicMock()
    unpaywall.lookup = unpaywall.lookup if hasattr(unpaywall, "lookup") else MagicMock(return_value=None)
    if not hasattr(unpaywall, "save"):
        unpaywall.save = MagicMock()
    resolver = resolver or PaperResolver()
    ingestor = SlackIngestor(
        config=cfg, slack=slack, unpaywall=unpaywall,
        drive_uploader=drive, resolver=resolver,
        pdf_downloader=downloader or (lambda url, **kw: _candidate_pdf(url)),
    )
    return ingestor, slack, drive, unpaywall


def test_skips_messages_without_hashtag(tmp_path):
    ingestor, slack, drive, unpaywall = _build_ingestor(tmp_path)
    slack.fetch_history.return_value = [
        {"ts": "100.0", "text": "Just regular chatter, no hashtag"},
    ]
    summary = ingestor.run()
    assert summary["skipped"] == 1
    assert summary["added"] == 0
    drive.upload.assert_not_called()


def test_attached_pdf_path_ingests(tmp_path):
    # Resolver returns a paper based on the DOI in text.
    fake_paper = ResolvedPaper(
        doi="10.1/x", title="X", authors=["Jane Smith"], year="2026",
        url="https://doi.org/10.1/x", source="crossref",
    )
    resolver = MagicMock(spec=PaperResolver)
    resolver.resolve.return_value = fake_paper

    ingestor, slack, drive, unpaywall = _build_ingestor(tmp_path,
                                                         resolver=resolver)
    slack.fetch_history.return_value = [
        {
            "ts": "100.0",
            "text": "#zettelkasten please add 10.1/x",
            "user": "U1",
            "files": [{
                "mimetype": "application/pdf",
                "url_private_download": "https://files.slack.com/x.pdf",
            }],
        },
    ]
    summary = ingestor.run()
    assert summary.get("added") == 1
    drive.upload.assert_called_once()
    # State and inbox.bib were written
    assert ingestor.config.state_file.exists()
    bib = ingestor.config.inbox_bib_file.read_text(encoding="utf-8")
    assert "Smith2026-sl" in bib
    assert "10.1/x" in bib
    # Confirmation reply posted
    slack.post_thread_reply.assert_called()
    # Slack token threaded through to download (via auth_header)


def test_no_pdf_no_doi_asks_for_pdf(tmp_path):
    resolver = MagicMock(spec=PaperResolver)
    resolver.resolve.return_value = ResolvedPaper(source="minimal")
    ingestor, slack, drive, unpaywall = _build_ingestor(tmp_path,
                                                         resolver=resolver)
    slack.fetch_history.return_value = [
        {"ts": "100.0", "text": "#zettelkasten interesting paper", "user": "U1"},
    ]
    summary = ingestor.run()
    assert summary.get("asked_for_pdf") == 1
    assert summary.get("added", 0) == 0
    drive.upload.assert_not_called()
    # A reply was posted requesting the PDF
    slack.post_thread_reply.assert_called()
    msg = slack.post_thread_reply.call_args.args[2]
    assert "attach" in msg.lower()


def test_unpaywall_path(tmp_path):
    from src.unpaywall_client import UnpaywallResult
    resolver = MagicMock(spec=PaperResolver)
    resolver.resolve.return_value = ResolvedPaper(
        doi="10.1/x", title="X", authors=["Jane Smith"], year="2026",
        url="https://doi.org/10.1/x", source="crossref",
    )
    unpaywall = MagicMock()
    unpaywall.lookup.return_value = UnpaywallResult(
        doi="10.1/x", is_oa=True,
        best_oa_pdf_url="https://example.org/oa.pdf"
    )
    unpaywall.save = MagicMock()

    ingestor, slack, drive, _ = _build_ingestor(
        tmp_path, resolver=resolver, unpaywall=unpaywall,
    )
    slack.fetch_history.return_value = [
        {"ts": "100.0", "text": "#zettelkasten 10.1/x", "user": "U1"},
    ]
    summary = ingestor.run()
    assert summary.get("added") == 1
    drive.upload.assert_called()
    # The note in the inbox.bib should reference unpaywall as the source
    bib = ingestor.config.inbox_bib_file.read_text(encoding="utf-8")
    assert "pdf_source=unpaywall" in bib


def test_unpaywall_validation_failure_asks(tmp_path):
    from src.unpaywall_client import UnpaywallResult
    resolver = MagicMock(spec=PaperResolver)
    resolver.resolve.return_value = ResolvedPaper(
        doi="10.1/x", title="X", authors=["Jane Smith"], year="2026",
        source="crossref",
    )
    unpaywall = MagicMock()
    unpaywall.lookup.return_value = UnpaywallResult(
        doi="10.1/x", is_oa=True,
        best_oa_pdf_url="https://example.org/landing.html"
    )
    unpaywall.save = MagicMock()

    # Downloader rejects the (HTML masquerading as PDF) URL.
    def fake_download(url, **kw):
        if "landing" in url:
            raise PDFValidationError("not a PDF")
        return _candidate_pdf(url)

    ingestor, slack, drive, _ = _build_ingestor(
        tmp_path, resolver=resolver, unpaywall=unpaywall,
        downloader=fake_download,
    )
    slack.fetch_history.return_value = [
        {"ts": "100.0", "text": "#zettelkasten 10.1/x", "user": "U1"},
    ]
    summary = ingestor.run()
    assert summary.get("asked_for_pdf") == 1
    assert summary.get("added", 0) == 0


def test_arxiv_fast_path(tmp_path):
    resolver = MagicMock(spec=PaperResolver)
    resolver.resolve.return_value = ResolvedPaper(
        title="ArXiv paper", authors=["Lynnette Ng"], year="2026",
        url="https://arxiv.org/abs/2605.07069",
        arxiv_id="2605.07069", source="arxiv",
    )
    downloaded_urls = []

    def fake_download(url, **kw):
        downloaded_urls.append(url)
        return _candidate_pdf(url)

    ingestor, slack, drive, unpaywall = _build_ingestor(
        tmp_path, resolver=resolver, downloader=fake_download,
    )
    slack.fetch_history.return_value = [
        {"ts": "100.0",
         "text": "#zettelkasten https://arxiv.org/abs/2605.07069", "user": "U1"},
    ]
    summary = ingestor.run()
    assert summary.get("added") == 1
    # Should have hit arxiv pdf URL, not Unpaywall.
    assert any("arxiv.org/pdf/2605.07069" in u for u in downloaded_urls)
    unpaywall.lookup.assert_not_called()


def test_pending_message_followup_thread_attachment(tmp_path):
    resolver = MagicMock(spec=PaperResolver)
    resolver.resolve.return_value = ResolvedPaper(
        doi="10.1/x", title="X", authors=["Jane Smith"], year="2026",
        source="crossref",
    )
    ingestor, slack, drive, unpaywall = _build_ingestor(
        tmp_path, resolver=resolver,
    )
    # Seed state: one pending message
    state = SlackIngestState(
        last_ts="100.0",
        pending={"100.0": {
            "text": "#zettelkasten 10.1/x", "user": "U1",
            "channel_id": "C123", "permalink": "https://slack.example/p",
        }},
    )
    state.save(ingestor.config.state_file)
    # Thread now contains a reply with an attached PDF
    slack.fetch_thread.return_value = [
        {"ts": "100.0", "text": "#zettelkasten 10.1/x"},
        {"ts": "101.0", "text": "Here's the PDF",
         "files": [{"mimetype": "application/pdf",
                    "url_private_download": "https://files.slack.com/y.pdf"}]},
    ]
    slack.fetch_history.return_value = []  # nothing new
    summary = ingestor.run()
    assert summary.get("added") == 1
    # Pending should be empty after success
    new_state = SlackIngestState.load(ingestor.config.state_file)
    assert "100.0" not in new_state.pending
    assert any(k.startswith("Smith2026-sl") for k in new_state.processed_meta)


def test_already_processed_is_idempotent(tmp_path):
    resolver = MagicMock(spec=PaperResolver)
    resolver.resolve.return_value = ResolvedPaper(source="minimal")
    ingestor, slack, drive, unpaywall = _build_ingestor(
        tmp_path, resolver=resolver,
    )
    state = SlackIngestState(
        last_ts="100.0",
        processed={"100.0": "Smith2026-sla7"},
    )
    state.save(ingestor.config.state_file)
    slack.fetch_history.return_value = [
        {"ts": "100.0", "text": "#zettelkasten old message"},
    ]
    summary = ingestor.run()
    # No new ingestion, no new ask.
    assert summary.get("added", 0) == 0
    assert summary.get("asked_for_pdf", 0) == 0


def test_mint_bibkey_is_deterministic_in_ts():
    """Re-processing the same Slack message must mint the same key — the
    bibkey suffix is sha1(ts)[:2], not random. Without this, a lost
    state.json on a push-retry edge case would mint duplicates."""
    a = mint_bibkey(authors=["Jane Smith"], year="2026",
                    slack_ts="1715000000.000123")
    b = mint_bibkey(authors=["Jane Smith"], year="2026",
                    slack_ts="1715000000.000123")
    c = mint_bibkey(authors=["Jane Smith"], year="2026",
                    slack_ts="1715000001.000123")
    assert a == b
    assert a != c  # Different `ts` → different suffix.


def test_reprocessing_after_state_loss_does_not_duplicate(tmp_path):
    """Simulate: a previous run wrote slack_inbox.bib but the state.json
    update never made it to git. The next run re-processes the same
    message; we must NOT append a second bib entry."""
    resolver = MagicMock(spec=PaperResolver)
    resolver.resolve.return_value = ResolvedPaper(
        doi="10.1/x", title="X", authors=["Jane Smith"], year="2026",
        source="crossref",
    )
    ingestor, slack, drive, unpaywall = _build_ingestor(
        tmp_path, resolver=resolver,
    )
    # First pass — ingests and writes the file.
    msg = {
        "ts": "100.000",
        "text": "#zettelkasten 10.1/x",
        "user": "U1",
        "files": [{"mimetype": "application/pdf",
                   "url_private_download": "https://files.slack.com/x.pdf"}],
    }
    slack.fetch_history.return_value = [msg]
    summary1 = ingestor.run()
    assert summary1.get("added") == 1
    bib_after_first = ingestor.config.inbox_bib_file.read_text(encoding="utf-8")
    # "Lose" the state.
    ingestor.config.state_file.unlink()
    # Re-process the same message.
    slack.fetch_history.return_value = [msg]
    summary2 = ingestor.run()
    bib_after_second = ingestor.config.inbox_bib_file.read_text(encoding="utf-8")
    # Bib file unchanged on the second pass.
    assert bib_after_first == bib_after_second
    # Only one entry total.
    assert bib_after_second.count("@article{") == 1
