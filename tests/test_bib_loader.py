"""Tests for src.bib_loader."""

from pathlib import Path

import pytest

from src.bib_loader import load_sources, _fingerprint, _normalize
from src.bibtex_parser import BibEntry


def _write_bib(path: Path, entries: str) -> None:
    path.write_text(entries, encoding="utf-8")


def test_loads_two_sources_and_tags(tmp_path):
    paperpile = tmp_path / "paperpile.bib"
    slack = tmp_path / "slack.bib"
    _write_bib(paperpile, """@article{Smith2026-aa,
  title = {Pap one},
  author = {Smith, Jane},
  year = {2026},
  doi = {10.1/a},
}
""")
    _write_bib(slack, """@article{Doe2026-sla1,
  title = {Sla two},
  author = {Doe, John},
  year = {2026},
  doi = {10.1/b},
}
""")
    out = load_sources([(str(paperpile), "paperpile"), (str(slack), "slack")])
    keys = {e.key: e.source for e in out}
    assert keys == {"Smith2026-aa": "paperpile", "Doe2026-sla1": "slack"}


def test_dedup_paperpile_wins_on_doi(tmp_path):
    paperpile = tmp_path / "p.bib"
    slack = tmp_path / "s.bib"
    _write_bib(paperpile, """@article{Smith2026-aa,
  title = {Original},
  author = {Smith, Jane},
  year = {2026},
  doi = {10.1/SAME},
}
""")
    _write_bib(slack, """@article{Smith2026-sla1,
  title = {Slack copy},
  author = {Smith, Jane},
  year = {2026},
  doi = {10.1/same},
}
""")
    out = load_sources([(str(paperpile), "paperpile"), (str(slack), "slack")])
    keys = [e.key for e in out]
    assert keys == ["Smith2026-aa"]


def test_dedup_falls_back_to_fingerprint(tmp_path):
    paperpile = tmp_path / "p.bib"
    slack = tmp_path / "s.bib"
    # Same paper, no DOI on either — should still dedup via fingerprint.
    _write_bib(paperpile, """@article{Smith2026-aa,
  title = {The Widget Paradox},
  author = {Smith, Jane},
  year = {2026},
}
""")
    _write_bib(slack, """@article{Smith2026-sla1,
  title = {The Widget Paradox!},
  author = {Smith, Jane},
  year = {2026},
}
""")
    out = load_sources([(str(paperpile), "paperpile"), (str(slack), "slack")])
    assert [e.key for e in out] == ["Smith2026-aa"]


def test_missing_file_is_skipped(tmp_path):
    paperpile = tmp_path / "p.bib"
    _write_bib(paperpile, """@article{Smith2026-aa,
  title = {Only one},
  author = {Smith, Jane},
  year = {2026},
}
""")
    out = load_sources([
        (str(paperpile), "paperpile"),
        (str(tmp_path / "nope.bib"), "slack"),
    ])
    assert [e.key for e in out] == ["Smith2026-aa"]


def test_fingerprint_requires_full_signal():
    entry = BibEntry(entry_type="article", key="X", title="Only title")
    # No author, no year → no fingerprint.
    assert _fingerprint(entry) == ("", "", "")


def test_normalize_strips_punctuation():
    assert _normalize("The Widget--Paradox!") == "the widget paradox"


def test_does_not_drop_within_same_source(tmp_path):
    """Regression: a single Paperpile file with two entries sharing the
    fingerprint (or even DOI) must keep both — the source is its own
    ground truth. Dedup is cross-source only."""
    paperpile = tmp_path / "p.bib"
    _write_bib(paperpile, """@article{Smith2026-aa,
  title = {Same title same author same year},
  author = {Smith, Jane},
  year = {2026},
}

@article{Smith2026-bb,
  title = {Same title same author same year},
  author = {Smith, Jane},
  year = {2026},
}
""")
    out = load_sources([(str(paperpile), "paperpile")])
    assert {e.key for e in out} == {"Smith2026-aa", "Smith2026-bb"}
