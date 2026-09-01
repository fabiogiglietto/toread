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
    slack.display_name.return_value = "Test User"
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


# ---- team fork: attribution + cross-archive dedup -----------------------


def test_duplicate_in_archive_replies_and_skips(tmp_path):
    """A submission whose DOI is already in the published feed is not ingested:
    the bot replies in-thread and the message is counted as a duplicate."""
    import json
    resolver = MagicMock(spec=PaperResolver)
    resolver.resolve.return_value = ResolvedPaper(
        doi="10.1/known", title="A Paper Already In The Archive",
        authors=["Jane Smith"], year="2026", source="crossref",
    )
    ingestor, slack, drive, unpaywall = _build_ingestor(tmp_path,
                                                        resolver=resolver)
    feed_path = tmp_path / "feed.json"
    feed_path.write_text(json.dumps({"items": [
        {"title": "A Paper Already In The Archive",
         "_academic": {"doi": "10.1/known"}},
    ]}), encoding="utf-8")
    ingestor.config.feed_file = feed_path
    slack.fetch_history.return_value = [{
        "ts": "100.0", "text": "#zettelkasten 10.1/known", "user": "U1",
        "files": [{"mimetype": "application/pdf",
                   "url_private_download": "https://files.slack.com/x.pdf"}],
    }]
    summary = ingestor.run()
    assert summary.get("duplicate") == 1
    assert summary.get("added", 0) == 0
    drive.upload.assert_not_called()
    # Replied in-thread and did not append to the inbox bib.
    slack.post_thread_reply.assert_called()
    reply = slack.post_thread_reply.call_args.args[2]
    assert "archive" in reply.lower()
    assert not ingestor.config.inbox_bib_file.exists() or \
        "@article{" not in ingestor.config.inbox_bib_file.read_text()


def test_duplicate_matched_by_title_when_no_doi(tmp_path):
    """Title match alone (no DOI) is enough to flag a duplicate."""
    import json
    resolver = MagicMock(spec=PaperResolver)
    resolver.resolve.return_value = ResolvedPaper(
        title="A Paper Already In The Archive", authors=["Jane Smith"],
        source="minimal",
    )
    ingestor, slack, drive, unpaywall = _build_ingestor(tmp_path,
                                                        resolver=resolver)
    feed_path = tmp_path / "feed.json"
    feed_path.write_text(json.dumps({"items": [
        {"title": "A Paper Already in the Archive!", "_academic": {}},
    ]}), encoding="utf-8")
    ingestor.config.feed_file = feed_path
    slack.fetch_history.return_value = [{
        "ts": "100.0", "text": "#zettelkasten a paper already in the archive",
        "user": "U1",
    }]
    summary = ingestor.run()
    assert summary.get("duplicate") == 1


def test_bot_messages_are_skipped(tmp_path):
    """A hashtag message posted by a bot/app (bot_id present) — e.g. our own
    ✅ / ask-for-PDF / duplicate replies — is never treated as a submission.
    The `bot_message` subtype misses chat.postMessage bot posts, which carry
    a `bot_id` instead."""
    ingestor, slack, drive, unpaywall = _build_ingestor(tmp_path)
    slack.fetch_history.return_value = [
        {"ts": "100.0", "text": "#zettelkasten see https://doi.org/10.9/x",
         "bot_id": "B999"},
    ]
    summary = ingestor.run()
    assert summary.get("skipped") == 1
    assert summary.get("added", 0) == 0


# ---- attribution flag (attribute_suggesters) ----------------------------


def test_attribution_off_keeps_identity_out_of_state(tmp_path):
    """Default (upstream) behavior: no submitter identity in processed_meta,
    byte-compatible with the pre-flag state shape."""
    resolver = MagicMock(spec=PaperResolver)
    resolver.resolve.return_value = ResolvedPaper(
        doi="10.1/x", title="A Distinct New Paper", authors=["Jane Smith"],
        year="2026", source="crossref",
    )
    ingestor, slack, drive, unpaywall = _build_ingestor(tmp_path,
                                                        resolver=resolver)
    slack.fetch_history.return_value = [{
        "ts": "100.0",
        "text": "#zettelkasten please add 10.1/x",
        "user": "U1",
        "files": [{"mimetype": "application/pdf",
                   "url_private_download": "https://files.slack.com/x.pdf"}],
    }]
    summary = ingestor.run()
    assert summary.get("added") == 1
    slack.display_name.assert_not_called()
    state = SlackIngestState.load(ingestor.config.state_file)
    meta = next(iter(state.processed_meta.values()))
    assert "submitted_by" not in meta
    assert "submitted_by_id" not in meta


def test_submitter_recorded_when_attribution_on(tmp_path):
    """With attribute_suggesters on (team fork), a successful ingest records
    the resolved submitter display name + opaque user-id in processed_meta,
    so the feed/RSS can publish `submitted_by*`."""
    resolver = MagicMock(spec=PaperResolver)
    resolver.resolve.return_value = ResolvedPaper(
        doi="10.1/x", title="A Distinct New Paper", authors=["Jane Smith"],
        year="2026", source="crossref",
    )
    ingestor, slack, drive, unpaywall = _build_ingestor(tmp_path,
                                                        resolver=resolver)
    ingestor.config.attribute_suggesters = True
    slack.display_name.return_value = "Jane Smith"
    slack.fetch_history.return_value = [{
        "ts": "100.0",
        "text": "#zettelkasten please add 10.1/x",
        "user": "U1",
        "files": [{"mimetype": "application/pdf",
                   "url_private_download": "https://files.slack.com/x.pdf"}],
    }]
    summary = ingestor.run()
    assert summary.get("added") == 1
    slack.display_name.assert_called_with("U1")
    state = SlackIngestState.load(ingestor.config.state_file)
    meta = next(iter(state.processed_meta.values()))
    assert meta["submitted_by"] == "Jane Smith"
    # The opaque user-id is recorded too, so the team kasten can @-mention.
    assert meta["submitted_by_id"] == "U1"


# ---- dedicated-channel mode (require_hashtag = False) -------------------


def test_no_hashtag_mode_ingests_link_message(tmp_path):
    """With require_hashtag off, a message carrying a paper link (no hashtag)
    is ingested."""
    resolver = MagicMock(spec=PaperResolver)
    resolver.resolve.return_value = ResolvedPaper(
        doi="10.5/nh", title="No Hashtag Needed Paper", authors=["Jane Smith"],
        year="2026", source="crossref",
    )
    ingestor, slack, drive, unpaywall = _build_ingestor(tmp_path,
                                                        resolver=resolver)
    ingestor.config.require_hashtag = False
    slack.fetch_history.return_value = [{
        "ts": "100.0",
        "text": "<https://doi.org/10.5/nh|doi.org/…>",   # link, NO hashtag
        "user": "U1",
        "files": [{"mimetype": "application/pdf",
                   "url_private_download": "https://files.slack.com/x.pdf"}],
    }]
    summary = ingestor.run()
    assert summary.get("added") == 1


def test_no_hashtag_mode_skips_plain_chatter(tmp_path):
    """A message with no link and no PDF is still ignored in dedicated-channel
    mode — only links/PDFs are submissions."""
    ingestor, slack, drive, unpaywall = _build_ingestor(tmp_path)
    ingestor.config.require_hashtag = False
    slack.fetch_history.return_value = [
        {"ts": "100.0", "text": "what did everyone think of the talk?",
         "user": "U1"},
    ]
    summary = ingestor.run()
    assert summary.get("skipped") == 1
    assert summary.get("added", 0) == 0


def test_success_reply_links_the_note(tmp_path):
    """The ✅ confirmation links the note permalink (<note_base_url>/<bibkey>),
    known at ingest time because the note filename is the bibkey."""
    resolver = MagicMock(spec=PaperResolver)
    resolver.resolve.return_value = ResolvedPaper(
        doi="10.1/x", title="A Distinct New Paper", authors=["Jane Smith"],
        year="2026", source="crossref",
    )
    ingestor, slack, drive, unpaywall = _build_ingestor(tmp_path,
                                                        resolver=resolver)
    ingestor.config.note_base_url = "https://example.org/Papers/"
    slack.fetch_history.return_value = [{
        "ts": "100.0",
        "text": "#zettelkasten please add 10.1/x",
        "user": "U1",
        "files": [{"mimetype": "application/pdf",
                   "url_private_download": "https://files.slack.com/x.pdf"}],
    }]
    assert ingestor.run().get("added") == 1
    reply = slack.post_thread_reply.call_args.args[2]
    assert "https://example.org/Papers/Smith2026-sl" in reply


def test_from_arxiv_uses_client_results(monkeypatch):
    """_from_arxiv must use arxiv.Client().results() — Search.results() was
    removed in arxiv>=3, which silently broke arxiv metadata resolution."""
    import sys, types
    from src import slack_ingest

    paper = types.SimpleNamespace(
        title="A Paper", authors=[types.SimpleNamespace(name="Jane Doe")],
        published=types.SimpleNamespace(year=2026),
        entry_id="http://arxiv.org/abs/2606.04431", summary="abstract", doi=None,
    )

    class FakeClient:
        def results(self, search):
            return iter([paper])

    fake_arxiv = types.SimpleNamespace(
        Search=lambda **kw: object(), Client=FakeClient
    )
    monkeypatch.setitem(sys.modules, "arxiv", fake_arxiv)

    resolved = slack_ingest.PaperResolver(
        enable_crossref=False, enable_arxiv=True
    )._from_arxiv("2606.04431")
    assert resolved is not None
    assert resolved.title == "A Paper"
    assert resolved.authors == ["Jane Doe"]
    assert resolved.year == "2026"
    assert resolved.source == "arxiv"


# ---- landing-page DOI discovery -----------------------------------------


def test_extract_doi_from_html_meta_tags():
    from src.slack_ingest import extract_doi_from_html
    # Highwire
    assert extract_doi_from_html(
        '<meta name="citation_doi" content="10.1080/abc.123">'
    ) == "10.1080/abc.123"
    # Dublin Core with doi: prefix, attribute order reversed
    assert extract_doi_from_html(
        '<meta content="doi:10.1177/xyz789" name="DC.Identifier">'
    ) == "10.1177/xyz789"
    # PRISM
    assert extract_doi_from_html(
        '<meta name="prism.doi" content="10.1016/j.foo.2026.01"/>'
    ) == "10.1016/j.foo.2026.01"
    # No DOI meta -> None (don't scrape body / cited refs)
    assert extract_doi_from_html(
        '<meta name="description" content="see 10.9/cited in refs">'
    ) is None


def test_resolve_scrapes_doi_from_landing_page():
    """A link with no DOI in the URL still resolves via the landing page's
    citation_doi meta tag (Crossref disabled here, so we just check the DOI)."""
    from src.slack_ingest import PaperResolver
    html = '<html><head><meta name="citation_doi" content="10.5555/landing.42">' \
           '</head></html>'
    resolver = PaperResolver(
        enable_crossref=False, enable_arxiv=False,
        html_fetcher=lambda url: html,
    )
    resolved = resolver.resolve(
        text="<https://example.com/articles/some-slug|some paper>",
        urls=["https://example.com/articles/some-slug"],
    )
    assert resolved.doi == "10.5555/landing.42"


def test_resolve_landing_fetch_failure_is_safe():
    """A landing-page fetch error must not break resolution."""
    from src.slack_ingest import PaperResolver

    def boom(url):
        raise RuntimeError("network down")

    resolver = PaperResolver(
        enable_crossref=False, enable_arxiv=False, html_fetcher=boom,
    )
    resolved = resolver.resolve(text="no doi here",
                                urls=["https://example.com/x"])
    assert resolved.doi is None
    assert resolved.source == "minimal"

# ---- landing-page citation metadata (title-less-drop root cause) ----------


_OJS_HTML = """<html><head>
<title>The Multiple Nuances of Online Firestorms | Italian Sociological Review</title>
<meta name="citation_title" content="The Multiple Nuances of Online Firestorms"/>
<meta name="citation_author" content="Nicola Righetti"/>
<meta name="citation_author" content="Ada Lovelace"/>
<meta name="citation_publication_date" content="2025/01/21"/>
</head></html>"""


def test_extract_citation_meta_prefers_citation_tags():
    from src.slack_ingest import extract_citation_meta_from_html
    meta = extract_citation_meta_from_html(_OJS_HTML)
    assert meta["title"] == "The Multiple Nuances of Online Firestorms"
    assert meta["authors"] == ["Nicola Righetti", "Ada Lovelace"]
    assert meta["year"] == "2025"


def test_extract_citation_meta_og_title_fallback():
    from src.slack_ingest import extract_citation_meta_from_html
    html = '<meta property="og:title" content="A Report &amp; Its Findings">'
    assert extract_citation_meta_from_html(html)["title"] == "A Report & Its Findings"


def test_extract_citation_meta_title_element_last_resort():
    from src.slack_ingest import extract_citation_meta_from_html
    html = "<html><head><title>Networked Publics Revisited | danah.org</title></head></html>"
    assert extract_citation_meta_from_html(html)["title"] == "Networked Publics Revisited"


def test_extract_citation_meta_empty_html():
    from src.slack_ingest import extract_citation_meta_from_html
    assert extract_citation_meta_from_html("") == {}
    # A too-short <title> is not trusted as a paper title.
    assert extract_citation_meta_from_html("<title>Home</title>") == {}


def test_resolve_uses_landing_citation_meta_when_no_doi():
    """A publisher page with citation meta but no DOI yields a full entry
    (previously: a title-less 'minimal' entry, silently dropped from the feed)."""
    resolver = PaperResolver(
        enable_crossref=False, enable_arxiv=False,
        html_fetcher=lambda url: _OJS_HTML,
    )
    resolved = resolver.resolve(
        text="#zettelkasten <https://journal.example/article/850>",
        urls=["https://journal.example/article/850"],
    )
    assert resolved.source == "landing_page"
    assert resolved.title == "The Multiple Nuances of Online Firestorms"
    assert resolved.authors == ["Nicola Righetti", "Ada Lovelace"]
    assert resolved.year == "2025"
    assert resolved.url == "https://journal.example/article/850"


def test_resolve_pdf_link_stays_minimal():
    """Direct PDF URLs (fetcher returns None: non-HTML) still fall through to
    the minimal path — covered by the in-thread warning instead."""
    resolver = PaperResolver(
        enable_crossref=False, enable_arxiv=False,
        html_fetcher=lambda url: None,
    )
    resolved = resolver.resolve(
        text="no doi", urls=["https://example.org/paper.pdf"])
    assert resolved.source == "minimal"
    assert resolved.title is None


def test_titleless_ingest_warns_instead_of_promising_note(tmp_path):
    """The ack must not promise a note for a title-less entry — it is held
    out of the feed until metadata is backfilled."""
    from unittest.mock import MagicMock
    resolver = MagicMock(spec=PaperResolver)
    resolver.resolve.return_value = ResolvedPaper(
        url="https://example.org/paper.pdf", source="minimal",
    )
    ingestor, slack, drive, unpaywall = _build_ingestor(tmp_path,
                                                        resolver=resolver)
    slack.fetch_history.return_value = [
        {
            "ts": "100.0",
            "text": "#zettelkasten https://example.org/paper.pdf",
            "user": "U1",
            "files": [{
                "mimetype": "application/pdf",
                "url_private_download": "https://files.slack.com/x.pdf",
            }],
        },
    ]
    summary = ingestor.run()
    assert summary.get("added") == 1
    replies = [c.args[2] for c in slack.post_thread_reply.call_args_list]
    assert any("held out of the feed" in r for r in replies)
    assert not any("will be ready in a few minutes" in r for r in replies)


def test_titled_ingest_still_promises_note(tmp_path):
    from unittest.mock import MagicMock
    resolver = MagicMock(spec=PaperResolver)
    resolver.resolve.return_value = ResolvedPaper(
        title="A Fine Paper", authors=["Jane Smith"], year="2026",
        url="https://example.org/x", source="landing_page",
    )
    ingestor, slack, drive, unpaywall = _build_ingestor(tmp_path,
                                                        resolver=resolver)
    slack.fetch_history.return_value = [
        {
            "ts": "100.0",
            "text": "#zettelkasten https://example.org/x",
            "user": "U1",
            "files": [{
                "mimetype": "application/pdf",
                "url_private_download": "https://files.slack.com/x.pdf",
            }],
        },
    ]
    summary = ingestor.run()
    assert summary.get("added") == 1
    replies = [c.args[2] for c in slack.post_thread_reply.call_args_list]
    assert any("will be ready in a few minutes" in r for r in replies)


def test_extract_citation_meta_strips_ojs_view_of_prefix():
    """OJS galley pages title themselves 'View of <paper title>'."""
    from src.slack_ingest import extract_citation_meta_from_html
    html = "<title>View of Online Firestorms and Their Nuances | Some Journal</title>"
    meta = extract_citation_meta_from_html(html)
    assert meta["title"] == "Online Firestorms and Their Nuances"
    # citation_title is authoritative — never rewritten.
    html2 = '<meta name="citation_title" content="View of the Alps from Turin">'
    assert extract_citation_meta_from_html(html2)["title"] == "View of the Alps from Turin"


# ---- DataCite DOI fallback (issue #13, "Untitled" symptom) ----------------

# Trimmed from the real api.datacite.org response for 10.18716/pd.v2i1.11657 —
# the DOI from the third occurrence in issue #13. Crossref 404s on it.
_DATACITE_PAYLOAD = {
    "data": {
        "attributes": {
            "titles": [{"title": "Beyond Artificial Intelligence"}],
            "creators": [{
                "name": "Esposito, Elena", "nameType": "Personal",
                "givenName": "Elena", "familyName": "Esposito",
            }],
            "publicationYear": 2025,
            "url": "https://journals.ub.uni-koeln.de/index.php/phidi/article/view/11657",
            "descriptions": [
                {"description": "Boilerplate.", "descriptionType": "Other"},
                {"description": "The remarkable performance of recent algorithms.",
                 "descriptionType": "Abstract"},
            ],
        }
    }
}


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _datacite_resolver(monkeypatch, *, status=200, payload=None, calls=None):
    """PaperResolver with Crossref disabled and `requests.get` stubbed, so the
    DataCite rung is exercised with no network."""
    import requests
    from src.slack_ingest import PaperResolver

    def fake_get(url, **kwargs):
        if calls is not None:
            calls.append(url)
        return _FakeResponse(status, payload if payload is not None
                             else _DATACITE_PAYLOAD)

    monkeypatch.setattr(requests, "get", fake_get)
    return PaperResolver(enable_crossref=False, enable_arxiv=False,
                         enable_doi_scrape=False)


def test_datacite_resolves_doi_crossref_does_not_hold(monkeypatch):
    """The exact failure from issue #13: a DataCite DOI that Crossref 404s on
    used to fall through to `minimal` — i.e. title-less, then either dropped
    from the feed or published as "Untitled"."""
    calls = []
    resolver = _datacite_resolver(monkeypatch, calls=calls)
    resolved = resolver.resolve(
        text="https://doi.org/10.18716/pd.v2i1.11657",
        urls=["https://doi.org/10.18716/pd.v2i1.11657"],
    )

    assert resolved.source == "datacite"
    assert resolved.title == "Beyond Artificial Intelligence"
    assert resolved.year == "2025"
    assert resolved.abstract == "The remarkable performance of recent algorithms."
    assert "datacite.org" in calls[0]


def test_datacite_author_is_given_then_family(monkeypatch):
    """`build_filename` takes the last whitespace token as the surname, so the
    creator must come back as "Elena Esposito" — "Esposito, Elena" would file
    the paper under "Elena"."""
    resolver = _datacite_resolver(monkeypatch)
    resolved = resolver.resolve(text="", urls=["https://doi.org/10.18716/pd.v2i1.11657"])

    assert resolved.authors == ["Elena Esposito"]
    assert resolved.authors[0].split()[-1] == "Esposito"


def test_datacite_flips_display_name_without_given_family(monkeypatch):
    """Older DataCite records carry only the "Family, Given" display form."""
    payload = {"data": {"attributes": {
        "titles": [{"title": "A Paper"}],
        "creators": [{"name": "Esposito, Elena"}, {"name": "Cher"}],
        "publicationYear": 2024,
    }}}
    resolver = _datacite_resolver(monkeypatch, payload=payload)
    resolved = resolver.resolve(text="", urls=["https://doi.org/10.5555/x"])

    assert resolved.authors == ["Elena Esposito", "Cher"]


def test_datacite_titleless_record_falls_through(monkeypatch):
    """A DataCite hit with no title is no better than `minimal`, and must not
    claim source="datacite" — that would hide the very failure being fixed."""
    payload = {"data": {"attributes": {"titles": [], "publicationYear": 2024}}}
    calls = []
    resolver = _datacite_resolver(monkeypatch, payload=payload, calls=calls)
    resolved = resolver.resolve(text="", urls=["https://doi.org/10.5555/x"])

    # Assert the rung actually ran: `source == "minimal"` alone would also hold
    # if DataCite were never consulted.
    assert any("datacite.org" in c for c in calls)
    assert resolved.source == "minimal"
    assert resolved.title is None


def test_datacite_non_200_falls_through(monkeypatch):
    """A DOI in neither registry still degrades to `minimal`, not an exception."""
    calls = []
    resolver = _datacite_resolver(monkeypatch, status=404, calls=calls)
    resolved = resolver.resolve(text="", urls=["https://doi.org/10.5555/nope"])

    assert any("datacite.org" in c for c in calls)
    assert resolved.source == "minimal"


def test_datacite_not_called_when_crossref_resolves(monkeypatch):
    """Crossref stays the primary: a Crossref hit must not cost a DataCite
    request."""
    import requests
    from src.slack_ingest import PaperResolver
    calls = []

    crossref_payload = {"message": {
        "title": ["A Crossref Paper"],
        "author": [{"given": "Ada", "family": "Lovelace"}],
        "issued": {"date-parts": [[2021]]},
        "URL": "https://example.org/p",
    }}

    def fake_get(url, **kwargs):
        calls.append(url)
        return _FakeResponse(200, crossref_payload)

    monkeypatch.setattr(requests, "get", fake_get)
    resolver = PaperResolver(enable_arxiv=False, enable_doi_scrape=False)
    resolved = resolver.resolve(text="10.1234/real", urls=[])

    assert resolved.source == "crossref"
    assert len(calls) == 1
    assert "datacite.org" not in calls[0]


def test_datacite_network_error_is_safe(monkeypatch):
    """A DataCite outage must not break ingest."""
    import requests
    from src.slack_ingest import PaperResolver

    def boom(url, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(requests, "get", boom)
    resolver = PaperResolver(enable_crossref=False, enable_arxiv=False,
                             enable_doi_scrape=False)
    resolved = resolver.resolve(text="", urls=["https://doi.org/10.5555/x"])

    assert resolved.source == "minimal"


# ---- Drive filename carries the bibkey (issue #13) -----------------------


def test_uploaded_filename_is_prefixed_with_bibkey(tmp_path):
    """Contract with fg-zettelkasten's `drive_client.find_pdf`, which anchors
    on `{bibkey} - `. Asserted at the call site, not just in `build_filename`,
    because the failure mode is a caller that stops passing the key."""
    fake_paper = ResolvedPaper(
        doi="10.1/x", title="X", authors=["Jane Smith"], year="2026",
        url="https://doi.org/10.1/x", source="crossref",
    )
    resolver = MagicMock(spec=PaperResolver)
    resolver.resolve.return_value = fake_paper

    ingestor, slack, drive, unpaywall = _build_ingestor(tmp_path,
                                                        resolver=resolver)
    slack.fetch_history.return_value = [{
        "ts": "100.0",
        "text": "#zettelkasten please add 10.1/x",
        "user": "U1",
        "files": [{
            "mimetype": "application/pdf",
            "url_private_download": "https://files.slack.com/x.pdf",
        }],
    }]
    ingestor.run()

    # The minted key carries a ts-derived suffix, so derive it rather than
    # hardcoding: what matters is that the *actual* key is the prefix.
    import json
    state = json.loads(ingestor.config.state_file.read_text(encoding="utf-8"))
    bibkey = state["processed"]["100.0"]

    filename = drive.upload.call_args.kwargs["filename"]
    assert filename.startswith(f"{bibkey} - "), filename
    # The Paperpile-shaped remainder is preserved after the prefix.
    assert filename == f"{bibkey} - Smith 2026 - X.pdf"


def test_uploaded_filename_has_bibkey_even_when_untitled(tmp_path):
    """The issue's cascade: with no resolved title the name degrades to
    `Unknown - untitled`, which the downstream title-token matcher can never
    match. The bibkey prefix is what keeps the PDF findable."""
    fake_paper = ResolvedPaper(source="minimal")
    resolver = MagicMock(spec=PaperResolver)
    resolver.resolve.return_value = fake_paper

    ingestor, slack, drive, unpaywall = _build_ingestor(tmp_path,
                                                        resolver=resolver)
    slack.fetch_history.return_value = [{
        "ts": "100.0",
        "text": "#zettelkasten https://example.org/mystery",
        "user": "U1",
        "files": [{
            "mimetype": "application/pdf",
            "url_private_download": "https://files.slack.com/x.pdf",
        }],
    }]
    ingestor.run()

    import json
    state = json.loads(ingestor.config.state_file.read_text(encoding="utf-8"))
    bibkey = state["processed"]["100.0"]

    filename = drive.upload.call_args.kwargs["filename"]
    # Derived, not hardcoded: pinning the ts-derived mint format here would
    # turn a change in `mint_bibkey` into a false failure of the *contract*.
    assert filename == f"{bibkey} - Unknown - untitled.pdf", filename


# ---- pending-queue expiry + channel events (issue #13, minor related) ----


def _iso_days_ago(days):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _seed_pending(tmp_path, entries, pending_max_age_days=None):
    """An ingestor whose state file already holds `pending` entries."""
    import json
    ingestor, slack, drive, unpaywall = _build_ingestor(tmp_path)
    if pending_max_age_days is not None:
        ingestor.config.pending_max_age_days = pending_max_age_days
    slack.fetch_history.return_value = []
    ingestor.config.state_file.write_text(
        json.dumps({"last_ts": "9999999999.0", "processed": {},
                    "processed_meta": {}, "pending": entries}),
        encoding="utf-8",
    )
    return ingestor, slack, drive


def test_pending_entry_expires_after_max_age(tmp_path):
    """Observed in the fork: four entries, 36-45 days old, none of which will
    ever get a PDF, each costing a thread fetch on every tick."""
    ingestor, slack, drive = _seed_pending(tmp_path, {
        "100.0": {"text": "https://example.org/a", "first_seen": _iso_days_ago(45)},
    })
    summary = ingestor.run()

    assert summary["expired"] == 1
    # And it must not cost a thread fetch on the way out.
    slack.fetch_thread.assert_not_called()


def test_recent_pending_entry_survives(tmp_path):
    ingestor, slack, drive = _seed_pending(tmp_path, {
        "100.0": {"text": "https://example.org/a", "first_seen": _iso_days_ago(3)},
    })
    summary = ingestor.run()

    assert summary["expired"] == 0
    slack.fetch_thread.assert_called_once()


def test_expiry_can_be_disabled(tmp_path):
    ingestor, slack, drive = _seed_pending(
        tmp_path,
        {"100.0": {"text": "x", "first_seen": _iso_days_ago(400)}},
        pending_max_age_days=0,
    )
    assert ingestor.run()["expired"] == 0


def test_pending_without_first_seen_is_stamped_not_evicted(tmp_path):
    """An entry of unknown age must start the clock, not be dropped on sight."""
    import json
    ingestor, slack, drive = _seed_pending(tmp_path, {
        "100.0": {"text": "https://example.org/a"},
    })
    summary = ingestor.run()

    assert summary["expired"] == 0
    state = json.loads(ingestor.config.state_file.read_text(encoding="utf-8"))
    assert state["pending"]["100.0"]["first_seen"]


def test_expired_entry_is_not_reingested_in_the_same_run(tmp_path):
    """The loop this could create: an evicted ts is in neither `processed` nor
    `pending`, so only `last_ts` stops step 2 re-ingesting it, re-asking for a
    PDF and putting it straight back. Verified against the real fork state,
    whose `last_ts` leads every pending ts."""
    import json
    ingestor, slack, drive = _seed_pending(tmp_path, {
        "100.0": {"text": "#zettelkasten https://example.org/a",
                  "first_seen": _iso_days_ago(45)},
    })
    summary = ingestor.run()
    state = json.loads(ingestor.config.state_file.read_text(encoding="utf-8"))

    assert summary["expired"] == 1
    assert state["pending"] == {}

    # The guard is the cursor, and it is load-bearing: an evicted ts is in
    # neither `processed` nor `pending`, so the *only* reason step 2 does not
    # see it again is that `fetch_history` is asked for messages newer than
    # `last_ts`. Assert that contract rather than a canned history — feeding
    # the message back in by hand would test a state Slack cannot return.
    oldest = slack.fetch_history.call_args.kwargs["oldest"]
    assert float(oldest) > 100.0, (
        "expired entries are only safe from re-ingest while last_ts leads them"
    )


def test_channel_event_subtypes_are_skipped(tmp_path):
    """Defensive: today the URL/hashtag gate already skips the channel-purpose
    message observed in the fork (it carries no link). This covers a future
    channel event that *does* contain a URL in dedicated-channel mode."""
    ingestor, slack, drive, _unpaywall = _build_ingestor(tmp_path)
    ingestor.config.require_hashtag = False
    slack.fetch_history.return_value = [{
        "ts": "100.0",
        "subtype": "channel_purpose",
        "text": "set the channel description: papers, see https://example.org/x",
        "user": "U1",
    }]
    summary = ingestor.run()

    assert summary["skipped"] == 1
    assert summary["asked_for_pdf"] == 0
    drive.upload.assert_not_called()


def test_file_share_subtype_is_not_skipped(tmp_path):
    """`file_share` is a real user message carrying an attachment — the primary
    way PDFs arrive. It must never be treated as a channel event."""
    from src.slack_ingest import _is_channel_event
    assert not _is_channel_event("file_share")
    assert not _is_channel_event(None)
    assert _is_channel_event("channel_topic")


# ---- DOI candidate ladder (greedy-match root cause) -----------------------


def test_doi_candidates_trims_publisher_slug():
    """The Taylor & Francis shape: the chapter DOI with the URL slug glued on.
    `_DOI_RE` cannot avoid swallowing it, so the ladder trims it back."""
    from src.slack_ingest import doi_candidates
    raw = ("10.4324/9781003477570-5/privatization-public-discourse-"
           "raquel-recuero-camilla-quesada-tavares")
    assert doi_candidates(raw) == [raw, "10.4324/9781003477570-5"]


def test_doi_candidates_never_reaches_the_parent_work():
    """One trim, never two: `10.4324/9781003477570` is the *book*, a valid DOI
    for the wrong work that would resolve to a plausible title with no
    warning."""
    from src.slack_ingest import doi_candidates
    assert doi_candidates("10.4324/9781003477570-5") == ["10.4324/9781003477570-5"]
    assert doi_candidates("10.1177/1461444810365313") == ["10.1177/1461444810365313"]
    assert doi_candidates(None) == []


def test_extract_doi_still_greedy_by_design():
    """The regex is deliberately left greedy — a DOI may legitimately contain
    slashes, so the trimming decision belongs to the ladder, which can *test*
    each candidate, not to the matcher, which cannot."""
    from src.slack_ingest import extract_doi
    url = ("https://www.taylorfrancis.com/chapters/edit/10.4324/9781003477570-5/"
           "privatization-public-discourse-raquel-recuero-camilla-quesada-tavares")
    assert extract_doi(f"<{url}|t&f>", [url]).startswith("10.4324/9781003477570-5/")


def _crossref_payload(title="The Privatization of Public Discourse"):
    return {"message": {
        "title": [title],
        "author": [{"given": "Raquel", "family": "Recuero"},
                   {"given": "Camilla Quesada", "family": "Tavares"}],
        "issued": {"date-parts": [[2025, 12, 22]]},
        "URL": "https://doi.org/10.4324/9781003477570-5",
    }}


def test_trimmed_doi_resolves_and_is_flagged(monkeypatch):
    """End-to-end on the real failure: the slug-laden DOI 404s, the trimmed one
    resolves, and the entry records that the DOI was a guess."""
    import requests
    from src.slack_ingest import PaperResolver
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "9781003477570-5%2Fprivatization" in url or "privatization" in url:
            return _FakeResponse(404, {})
        return _FakeResponse(200, _crossref_payload())

    monkeypatch.setattr(requests, "get", fake_get)
    resolver = PaperResolver(enable_arxiv=False, enable_doi_scrape=False)
    url = ("https://www.taylorfrancis.com/chapters/edit/10.4324/9781003477570-5/"
           "privatization-public-discourse-raquel-recuero-camilla-quesada-tavares")
    resolved = resolver.resolve(text=f"<{url}|t&f>", urls=[url])

    assert resolved.title == "The Privatization of Public Discourse"
    assert resolved.doi == "10.4324/9781003477570-5"
    assert resolved.authors == ["Raquel Recuero", "Camilla Quesada Tavares"]
    assert resolved.year == "2025"
    assert resolved.doi_trimmed is True
    # The untrimmed candidate must be tried first — trimming is a fallback,
    # not the default.
    assert "privatization" in calls[0]


def test_unresolvable_doi_no_longer_suppresses_the_landing_page():
    """The gate was `if not doi`, so a malformed DOI skipped the landing-page
    fallback altogether. Now the fallback runs whenever nothing resolved."""
    from src.slack_ingest import PaperResolver
    resolver = PaperResolver(
        enable_crossref=False, enable_datacite=False, enable_doi_org=False,
        enable_arxiv=False, html_fetcher=lambda url: _OJS_HTML,
    )
    url = "https://publisher.example/chapters/10.4324/9781003477570-5/some-slug"
    resolved = resolver.resolve(text=f"<{url}|chapter>", urls=[url])

    assert resolved.source == "landing_page"
    assert resolved.title == "The Multiple Nuances of Online Firestorms"


# ---- doi.org content negotiation (registration agencies beyond the big two)


# Trimmed from the real doi.org CSL-JSON for 10.3270/101610 (Comunicazione
# politica, Il Mulino — an mEDRA DOI). Crossref and DataCite both 404 on it.
_CSL_PAYLOAD = {
    "type": "article-journal",
    "title": "Il ruolo dei media nella percezione del rischio",
    "author": [{"literal": "Nicola Righetti"}],
    "issued": {"date-parts": [[2021]]},
    "DOI": "10.3270/101610",
    "container-title": "Comunicazione politica",
}


def _doi_org_resolver(monkeypatch, *, status=200, payload=None, calls=None):
    """PaperResolver with only the doi.org rung live and `requests.get` stubbed."""
    import requests
    from src.slack_ingest import PaperResolver

    def fake_get(url, **kwargs):
        if calls is not None:
            calls.append((url, (kwargs.get("headers") or {}).get("Accept")))
        return _FakeResponse(status, payload if payload is not None
                             else _CSL_PAYLOAD)

    monkeypatch.setattr(requests, "get", fake_get)
    return PaperResolver(enable_crossref=False, enable_datacite=False,
                         enable_arxiv=False, enable_doi_scrape=False)


def test_doi_org_resolves_what_crossref_and_datacite_miss(monkeypatch):
    calls = []
    resolver = _doi_org_resolver(monkeypatch, calls=calls)
    resolved = resolver.resolve(text="", urls=["https://doi.org/10.3270/101610"])

    assert resolved.source == "doi.org"
    assert resolved.title == "Il ruolo dei media nella percezione del rischio"
    assert resolved.year == "2021"
    # Content negotiation is the whole mechanism — assert the Accept header.
    assert calls[0][1] == "application/vnd.citationstyles.csl+json"


def test_doi_org_literal_author_is_given_then_family(monkeypatch):
    """`mint_bibkey` takes the last token as the surname, so a `literal` name
    must come back in "Given Family" order."""
    resolver = _doi_org_resolver(monkeypatch)
    resolved = resolver.resolve(text="", urls=["https://doi.org/10.3270/101610"])
    assert resolved.authors == ["Nicola Righetti"]
    assert resolved.authors[0].split()[-1] == "Righetti"


def test_doi_org_flips_comma_separated_literal(monkeypatch):
    payload = dict(_CSL_PAYLOAD, author=[{"literal": "Righetti, Nicola"}])
    resolver = _doi_org_resolver(monkeypatch, payload=payload)
    resolved = resolver.resolve(text="", urls=["https://doi.org/10.3270/101610"])
    assert resolved.authors == ["Nicola Righetti"]


def test_doi_org_title_as_list(monkeypatch):
    payload = dict(_CSL_PAYLOAD, title=["A Paper In A List"])
    resolver = _doi_org_resolver(monkeypatch, payload=payload)
    resolved = resolver.resolve(text="", urls=["https://doi.org/10.3270/101610"])
    assert resolved.title == "A Paper In A List"


def test_doi_org_titleless_record_falls_through(monkeypatch):
    """Same contract as the DataCite rung: no title is no better than
    `minimal`, and must not claim source="doi.org"."""
    calls = []
    resolver = _doi_org_resolver(monkeypatch, payload={"DOI": "10.3270/101610"},
                                 calls=calls)
    resolved = resolver.resolve(text="", urls=["https://doi.org/10.3270/101610"])
    assert calls, "the doi.org rung never ran"
    assert resolved.source == "minimal"
    assert resolved.title is None


def test_titleless_crossref_hit_falls_through_to_doi_org(monkeypatch):
    """A Crossref record with no title used to be returned as-is, shadowing the
    rungs below it — the same silent title-less drop, one layer up."""
    import requests
    from src.slack_ingest import PaperResolver
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "crossref.org" in url:
            return _FakeResponse(200, {"message": {"DOI": "10.3270/101610"}})
        if "datacite.org" in url:
            return _FakeResponse(404, {})
        return _FakeResponse(200, _CSL_PAYLOAD)

    monkeypatch.setattr(requests, "get", fake_get)
    resolver = PaperResolver(enable_arxiv=False, enable_doi_scrape=False)
    resolved = resolver.resolve(text="", urls=["https://doi.org/10.3270/101610"])

    assert resolved.source == "doi.org"
    assert resolved.title == "Il ruolo dei media nella percezione del rischio"


def test_trimmed_doi_provenance_reaches_the_bib(tmp_path):
    """The trim is a bounded guess, so the entry must say so where a librarian
    reads it — the audit trail that justifies allowing the trim at all."""
    resolver = MagicMock(spec=PaperResolver)
    resolver.resolve.return_value = ResolvedPaper(
        doi="10.4324/9781003477570-5",
        title="The Privatization of Public Discourse",
        authors=["Raquel Recuero"], year="2025",
        source="crossref", doi_trimmed=True,
    )
    ingestor, slack, drive, unpaywall = _build_ingestor(tmp_path,
                                                        resolver=resolver)
    slack.fetch_history.return_value = [{
        "ts": "100.0", "text": "#zettelkasten a chapter", "user": "U1",
        "files": [{"mimetype": "application/pdf",
                   "url_private_download": "https://files.slack.com/x.pdf"}],
    }]
    assert ingestor.run().get("added") == 1

    bib = ingestor.config.inbox_bib_file.read_text(encoding="utf-8")
    assert "doi_trimmed=true" in bib
    assert "metadata_source=crossref" in bib


def test_untrimmed_doi_carries_no_trim_marker(tmp_path):
    """`doi_trimmed=true` must mark the exception, not every entry."""
    resolver = MagicMock(spec=PaperResolver)
    resolver.resolve.return_value = ResolvedPaper(
        doi="10.1/x", title="X", authors=["Jane Smith"], year="2026",
        source="crossref",
    )
    ingestor, slack, drive, unpaywall = _build_ingestor(tmp_path,
                                                        resolver=resolver)
    slack.fetch_history.return_value = [{
        "ts": "100.0", "text": "#zettelkasten 10.1/x", "user": "U1",
        "files": [{"mimetype": "application/pdf",
                   "url_private_download": "https://files.slack.com/x.pdf"}],
    }]
    assert ingestor.run().get("added") == 1
    bib = ingestor.config.inbox_bib_file.read_text(encoding="utf-8")
    assert "doi_trimmed" not in bib
    assert "metadata_source=crossref" in bib


def test_scraped_doi_wins_the_minimal_fallback():
    """When neither DOI resolves, the one from a `citation_doi` meta tag is
    recorded — it beats a greedy text match, and it is what the dedup check
    and the downstream enricher will retry on."""
    from src.slack_ingest import PaperResolver
    html = '<meta name="citation_doi" content="10.5555/from.the.page">'
    resolver = PaperResolver(
        enable_crossref=False, enable_datacite=False, enable_doi_org=False,
        enable_arxiv=False, html_fetcher=lambda url: html,
    )
    url = "https://publisher.example/chapters/10.4324/9781003477570-5/some-slug"
    resolved = resolver.resolve(text=f"<{url}|chapter>", urls=[url])

    assert resolved.source == "minimal"
    assert resolved.doi == "10.5555/from.the.page"


# ---- backfill of title-less inbox entries ---------------------------------


_STUCK_BIB = """@article{Slack1787858551-sl4d,
  doi = {10.4324/9781003477570-5/privatization-public-discourse-raquel-recuero},
  url = {https://www.taylorfrancis.com/chapters/edit/10.4324/9781003477570-5/x},
  note = {Suggested via Slack on 2026-08-28; pdf_source=slack_attachment_followup; ts=1787858551.834329}
}

@article{Smith2026-slaa,
  title = {A Paper That Already Resolved},
  author = {Jane Smith},
  year = {2026},
  url = {https://example.org/ok},
  note = {Suggested via Slack on 2026-08-01; pdf_source=arxiv; ts=1.0}
}

@article{Slack1783437956-sld1,
  note = {Suggested via Slack on 2026-07-07; pdf_source=slack_attachment; ts=1783437956.970489}
}
"""


class _StubResolver:
    def __init__(self, paper):
        self._paper = paper
        self.seen = []

    def resolve(self, *, text, urls):
        self.seen.append((text, tuple(urls)))
        return self._paper


def _stuck_inbox(tmp_path):
    path = tmp_path / "slack_inbox.bib"
    path.write_text(_STUCK_BIB, encoding="utf-8")
    return path


def test_backfill_fills_the_titleless_entry(tmp_path):
    from src.slack_ingest import backfill_inbox
    path = _stuck_inbox(tmp_path)
    resolver = _StubResolver(ResolvedPaper(
        doi="10.4324/9781003477570-5",
        title="The Privatization of Public Discourse",
        authors=["Raquel Recuero", "Camilla Quesada Tavares"],
        year="2025", source="crossref", doi_trimmed=True,
    ))
    report = backfill_inbox(path, resolver)
    out = path.read_text(encoding="utf-8")

    assert list(report["fixed"]) == ["Slack1787858551-sl4d"]
    assert "title = {The Privatization of Public Discourse}," in out
    assert "author = {Raquel Recuero and Camilla Quesada Tavares}," in out
    assert "year = {2025}," in out
    # The corrected DOI replaces the malformed one rather than joining it.
    assert "doi = {10.4324/9781003477570-5}," in out
    assert "privatization-public-discourse-raquel-recuero}" not in out
    # Provenance, so a later reader can tell this from a hand-typed fix.
    assert "backfilled=crossref; doi_trimmed=true}" in out


def test_backfill_keeps_the_bibkey(tmp_path):
    """The key is the note filename, the `processed_meta` key and the live site
    URL of any note already built from the broken entry. Re-minting it to
    `Recuero2025-sl4d` would orphan all three."""
    from src.slack_ingest import backfill_inbox
    path = _stuck_inbox(tmp_path)
    backfill_inbox(path, _StubResolver(ResolvedPaper(
        title="T", authors=["Raquel Recuero"], year="2025", source="crossref")))
    out = path.read_text(encoding="utf-8")

    assert "@article{Slack1787858551-sl4d," in out
    assert "Recuero2025" not in out


def test_backfill_leaves_resolved_entries_untouched(tmp_path):
    from src.slack_ingest import backfill_inbox
    path = _stuck_inbox(tmp_path)
    before = path.read_text(encoding="utf-8")
    resolver = _StubResolver(ResolvedPaper(title="X", source="crossref"))
    backfill_inbox(path, resolver)
    after = path.read_text(encoding="utf-8")

    # The already-good entry is byte-identical, and was never re-resolved.
    good = before[before.index("@article{Smith2026-slaa"):
                  before.index("@article{Slack1783437956-sld1")]
    assert good in after
    assert all("example.org/ok" not in t for t, _ in resolver.seen)


def test_backfill_reports_entry_with_nothing_to_resolve_from(tmp_path):
    """The deleted-Slack-message case: no DOI, no URL, nothing to ask about.
    It needs dropping, which is a separate decision — so report, never guess."""
    from src.slack_ingest import backfill_inbox
    path = _stuck_inbox(tmp_path)
    report = backfill_inbox(path, _StubResolver(
        ResolvedPaper(title="Wrong", source="crossref")))

    assert report["no_source"] == ["Slack1783437956-sld1"]
    assert "title = {Wrong}" not in path.read_text(
        encoding="utf-8").split("@article{Slack1783437956-sld1")[1]


def test_backfill_records_entries_the_resolver_still_cannot_name(tmp_path):
    from src.slack_ingest import backfill_inbox
    path = _stuck_inbox(tmp_path)
    before = path.read_text(encoding="utf-8")
    report = backfill_inbox(path, _StubResolver(ResolvedPaper(source="minimal")))

    assert report["unresolved"] == ["Slack1787858551-sl4d"]
    assert report["fixed"] == {}
    assert path.read_text(encoding="utf-8") == before


def test_backfill_dry_run_writes_nothing(tmp_path):
    from src.slack_ingest import backfill_inbox
    path = _stuck_inbox(tmp_path)
    before = path.read_text(encoding="utf-8")
    report = backfill_inbox(path, _StubResolver(ResolvedPaper(
        title="T", authors=["A B"], year="2025", source="crossref")),
        apply=False)

    assert report["fixed"]
    assert path.read_text(encoding="utf-8") == before


def test_backfill_reresolves_from_the_url_when_there_is_no_doi(tmp_path):
    from src.slack_ingest import backfill_inbox
    path = tmp_path / "inbox.bib"
    path.write_text(
        "@article{Slack1783507719-slc1,\n"
        "  url = {https://spir.aoir.org/ojs/index.php/spir/article/view/12170},\n"
        "  note = {Suggested via Slack on 2026-07-08; ts=1783507719.893499}\n"
        "}\n", encoding="utf-8")
    resolver = _StubResolver(ResolvedPaper(
        doi="10.5210/spir.v2021i0.12170", title="COORNET",
        authors=["Fabio Giglietto"], year="2021", source="crossref"))
    backfill_inbox(path, resolver)

    text, urls = resolver.seen[0]
    assert urls == ("https://spir.aoir.org/ojs/index.php/spir/article/view/12170",)
    assert "doi = {10.5210/spir.v2021i0.12170}," in path.read_text(encoding="utf-8")


def test_escape_bib_collapses_wrapped_whitespace():
    """Crossref and `citation_title` wrap long titles across indented source
    lines; the indentation must not survive into the bib entry."""
    from src.slack_ingest import _escape_bib
    assert _escape_bib("COORNET: SURFACE CONTENT,\n        MALICIOUS ACTORS") == \
        "COORNET: SURFACE CONTENT, MALICIOUS ACTORS"


# ---- Drive filename repair ------------------------------------------------


def _entries_from(bib_text):
    import tempfile, pathlib
    from src.slack_ingest import _bib_entries
    d = pathlib.Path(tempfile.mkdtemp())
    p = d / "inbox.bib"
    p.write_text(bib_text, encoding="utf-8")
    return _bib_entries(p)


_BACKFILLED = """@article{Slack1787858551-sl4d,
  title = {The Privatization of Public Discourse},
  author = {Raquel Recuero and Camilla Quesada Tavares},
  year = {2025},
  doi = {10.4324/9781003477570-5},
  url = {https://www.taylorfrancis.com/chapters/edit/10.4324/9781003477570-5/privatization-public-discourse-raquel-recuero-camilla-quesada-tavares},
  note = {ts=1787858551.834329; backfilled=crossref; doi_trimmed=true}
}
"""


def test_greedy_doi_recovers_the_name_a_backfilled_entry_was_uploaded_under():
    """The subtle case: the PDF was uploaded under the *malformed* DOI, which
    `--backfill` has since replaced. `extract_doi` regenerates exactly that
    string from the stored URL, so the old name is recoverable without the
    pre-backfill bib."""
    from src.slack_ingest import plan_drive_renames
    legacy = ("Unknown - 10.4324-9781003477570-5-privatization-public-discourse-"
              "raquel-recuero-camilla-quesada-tavares.pdf")
    plan = plan_drive_renames(_entries_from(_BACKFILLED), [legacy])

    assert plan["unmatched"] == []
    assert len(plan["rename"]) == 1
    item = plan["rename"][0]
    assert item["old"] == legacy
    assert item["new"].startswith("Slack1787858551-sl4d - ")
    assert "Recuero et al. 2025 - The Privatization of Public Discourse" in item["new"]


def test_already_prefixed_files_are_left_alone():
    """Idempotent: a second run must be a no-op, not a double-prefix."""
    from src.slack_ingest import plan_drive_renames
    good = "Slack1787858551-sl4d - Recuero et al. 2025 - The Privatization.pdf"
    plan = plan_drive_renames(_entries_from(_BACKFILLED), [good])

    assert plan["rename"] == []
    assert plan["already_prefixed"] == ["Slack1787858551-sl4d"]


def test_entry_with_no_matching_file_is_reported_not_guessed():
    """A wrong rename silently attaches one paper's PDF to another paper's
    note — worse than leaving it unmatched. Only exact names match."""
    from src.slack_ingest import plan_drive_renames
    plan = plan_drive_renames(
        _entries_from(_BACKFILLED),
        ["Unknown - something entirely unrelated.pdf",
         "Smith 2020 - A Different Paper.pdf"],
    )

    assert plan["rename"] == []
    assert plan["unmatched"] == ["Slack1787858551-sl4d"]


def test_entry_that_resolved_at_ingest_matches_its_paperpile_shaped_name():
    from src.slack_ingest import plan_drive_renames
    bib = """@article{Smith2026-slaa,
  title = {A Paper That Resolved},
  author = {Jane Smith},
  year = {2026},
  url = {https://example.org/ok},
  note = {ts=1.0}
}
"""
    plan = plan_drive_renames(_entries_from(bib),
                              ["Smith 2026 - A Paper That Resolved.pdf"])

    assert len(plan["rename"]) == 1
    assert plan["rename"][0]["new"] == (
        "Smith2026-slaa - Smith 2026 - A Paper That Resolved.pdf")


def test_url_only_entry_matches_the_url_derived_name():
    """slc1's shape: no DOI at ingest, so build_filename fell back to the URL."""
    from src.slack_ingest import plan_drive_renames
    bib = """@article{Slack1783507719-slc1,
  title = {COORNET},
  author = {Fabio Giglietto and Nicola Righetti},
  year = {2021},
  doi = {10.5210/spir.v2021i0.12170},
  url = {https://spir.aoir.org/ojs/index.php/spir/article/view/12170},
  note = {ts=1783507719.893499; backfilled=crossref}
}
"""
    from src.drive_uploader import build_filename
    legacy = build_filename(
        authors=[], year=None,
        title="https://spir.aoir.org/ojs/index.php/spir/article/view/12170")
    plan = plan_drive_renames(_entries_from(bib), [legacy])

    assert len(plan["rename"]) == 1
    assert plan["rename"][0]["new"].startswith("Slack1783507719-slc1 - ")
