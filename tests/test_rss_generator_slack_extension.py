"""Focused test: the `_slack_suggestion` feed extension.

The full RSS generator is covered elsewhere; here we just confirm that:
- A Slack-origin entry produces the extension with the expected keys.
- A Paperpile-origin entry does *not* get the extension.
- Suggester identity never leaks into the extension.
"""

import json

from src.bibtex_parser import BibEntry
from src.rss_generator import FeedGenerator


def _paperpile_entry():
    return BibEntry(
        entry_type="article", key="Smith2026-pp",
        title="Paperpile paper", authors=["Jane Smith"], year="2026",
        doi="10.1/p", source="paperpile",
    )


def _slack_entry():
    return BibEntry(
        entry_type="article", key="Doe2026-sla1",
        title="Slack paper", authors=["John Doe"], year="2026",
        doi="10.1/s", source="slack",
    )


def test_slack_suggestion_emitted_for_slack_source():
    slack_meta = {
        "Doe2026-sla1": {
            "channel_id": "C123",
            "ts": "100.0",
            "permalink": "https://slack.example/p",
            "pdf_source": "slack_attachment",
            # Published in the team fork for attribution.
            "submitted_by": "John Doe",
            # Opaque user-id, published so the kasten can @-mention.
            "submitted_by_id": "U123",
            # Things that must *not* leak through.
            "user": "U999",
            "suggester_email": "secret@example.com",
        }
    }
    gen = FeedGenerator(slack_meta=slack_meta)
    feed = json.loads(gen.generate_json_feed([_paperpile_entry(), _slack_entry()]))
    # The generator keys items by the BibTeX key (the pipeline's canonical id),
    # even when a DOI is present — see _get_entry_guid.
    items_by_id = {item["id"]: item for item in feed["items"]}

    pp_item = items_by_id["bibtex:Smith2026-pp"]
    sl_item = items_by_id["bibtex:Doe2026-sla1"]

    assert "_slack_suggestion" not in pp_item
    assert "_slack_suggestion" in sl_item
    sug = sl_item["_slack_suggestion"]
    assert sug["channel_id"] == "C123"
    assert sug["ts"] == "100.0"
    assert sug["permalink"] == "https://slack.example/p"
    assert sug["pdf_source"] == "slack_attachment"
    assert sug["submitted_by"] == "John Doe"   # published for attribution
    assert sug["submitted_by_id"] == "U123"    # published for @-mentioning
    assert "user" not in sug
    assert "suggester_email" not in sug


def test_slack_suggestion_absent_when_no_meta():
    # source="slack" but slack_meta empty → no extension emitted.
    gen = FeedGenerator(slack_meta={})
    feed = json.loads(gen.generate_json_feed([_slack_entry()]))
    item = feed["items"][0]
    assert "_slack_suggestion" not in item


def test_default_constructor_no_meta():
    # No-arg construction must keep existing behaviour: no slack_meta, no
    # extension on any item.
    gen = FeedGenerator()
    feed = json.loads(gen.generate_json_feed([_paperpile_entry(), _slack_entry()]))
    for item in feed["items"]:
        assert "_slack_suggestion" not in item
