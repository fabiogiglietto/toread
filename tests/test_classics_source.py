"""The Paperpile "Classics" source: loader flag, dedup inheritance, feed field.

A classic is an ordinary paper filed in a top-level Paperpile folder whose
export is a second source tagged `classic`; the feed marks it `_classic: true`
so downstream can skip announcing works added in bulk.
"""

import json
from pathlib import Path

from src.bib_loader import CLASSIC_TAG, load_sources
from src.bibtex_parser import BibEntry
from src.rss_generator import FeedGenerator

VOSOUGHI = """@article{Vosoughi2018-ab,
  title = {The spread of true and false news online},
  author = {Vosoughi, Soroush and Roy, Deb and Aral, Sinan},
  year = {2018},
  doi = {10.1126/science.aap9559},
}
"""
CURRENT = """@article{Smith2026-aa,
  title = {A current paper},
  author = {Smith, Jane},
  year = {2026},
  doi = {10.1/current},
}
"""


def _sources(tmp_path: Path, to_read: str, classics: str):
    pp = tmp_path / "paperpile_export.bib"
    cl = tmp_path / "paperpile_classics.bib"
    pp.write_text(to_read, encoding="utf-8")
    cl.write_text(classics, encoding="utf-8")
    return [(str(pp), "paperpile"), (str(cl), CLASSIC_TAG)]


def test_classics_entries_are_flagged_and_others_are_not(tmp_path):
    out = load_sources(_sources(tmp_path, CURRENT, VOSOUGHI))
    flags = {e.key: e.is_classic for e in out}
    assert flags == {"Smith2026-aa": False, "Vosoughi2018-ab": True}


def test_a_work_in_both_folders_appears_once_and_stays_classic(tmp_path):
    out = load_sources(_sources(tmp_path, CURRENT + VOSOUGHI, VOSOUGHI))
    vosoughi = [e for e in out if e.doi and e.doi.lower() == "10.1126/science.aap9559"]
    assert len(vosoughi) == 1
    assert vosoughi[0].source == "paperpile"  # Paperpile (To Read) still wins
    assert vosoughi[0].is_classic


def test_missing_classics_file_is_tolerated(tmp_path):
    pp = tmp_path / "paperpile_export.bib"
    pp.write_text(CURRENT, encoding="utf-8")
    out = load_sources([(str(pp), "paperpile"),
                        (str(tmp_path / "absent.bib"), CLASSIC_TAG)])
    assert [e.key for e in out] == ["Smith2026-aa"]


def test_feed_marks_only_classics():
    classic = BibEntry(entry_type="article", key="Vosoughi2018-ab",
                       title="The spread of true and false news online",
                       authors=["Soroush Vosoughi"], year="2018",
                       doi="10.1126/science.aap9559", source=CLASSIC_TAG,
                       is_classic=True)
    current = BibEntry(entry_type="article", key="Smith2026-aa",
                       title="A current paper", authors=["Jane Smith"],
                       year="2026", doi="10.1/current", source="paperpile")
    feed = json.loads(FeedGenerator().generate_json_feed([classic, current]))
    items = {i["id"]: i for i in feed["items"]}
    assert items["bibtex:Vosoughi2018-ab"]["_classic"] is True
    assert "_classic" not in items["bibtex:Smith2026-aa"]


def test_classic_item_validates_against_the_feed_schema():
    import pytest
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(Path("schema/feed.schema.json").read_text())
    classic = BibEntry(entry_type="article", key="Vosoughi2018-ab",
                       title="The spread of true and false news online",
                       authors=["Soroush Vosoughi"], year="2018",
                       doi="10.1126/science.aap9559", source=CLASSIC_TAG,
                       is_classic=True)
    from datetime import datetime, timezone
    classic.discovery_date = datetime(2026, 9, 25, tzinfo=timezone.utc)
    feed = json.loads(FeedGenerator().generate_json_feed([classic]))
    jsonschema.validate(feed, schema)
