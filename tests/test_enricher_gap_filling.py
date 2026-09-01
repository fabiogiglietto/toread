"""A DOI record must not stay abstract-less when another source has one.

`_enrich_by_doi` returned the first source that answered. Crossref answers for
most registered works but holds an abstract for only some — book chapters
especially have none — so the record came back abstract-less, OpenAlex was
never asked, and the paper was summarized downstream from a title and a
byline.
"""

from unittest.mock import MagicMock

from src.metadata_enricher import (
    EnrichedMetadata,
    MetadataEnricher,
    fill_missing_fields,
)


def _enricher(*, crossref=None, openalex=None, semantic_scholar=None):
    """A MetadataEnricher with stub clients and no config/network."""
    e = MetadataEnricher.__new__(MetadataEnricher)
    e.crossref_client = crossref
    e.openalex_client = openalex
    e.semantic_scholar_client = semantic_scholar
    e.arxiv_client = None
    e.max_consecutive_failures = 3
    e.api_failure_counts = {
        "crossref": 0, "openalex": 0, "semantic_scholar": 0, "arxiv": 0,
    }
    e.logger = MagicMock()
    return e


def _client(metadata):
    c = MagicMock()
    c.query_by_doi.return_value = metadata
    return c


def test_abstract_is_filled_from_the_next_source():
    """The exact observed failure: Crossref names the work but has no
    abstract, and OpenAlex does."""
    crossref = _client(EnrichedMetadata(
        source="crossref", venue="The Routledge Companion",
        authors=["Raquel Recuero"], citation_count=0))
    openalex = _client(EnrichedMetadata(
        source="openalex", abstract="A real abstract.", is_open_access=True))
    e = _enricher(crossref=crossref, openalex=openalex)

    md = e._enrich_by_doi("10.4324/9781003477570-5")

    assert md.abstract == "A real abstract."
    # The trusted source's own fields survive untouched.
    assert md.venue == "The Routledge Companion"
    assert md.authors == ["Raquel Recuero"]
    # And the merge is visible in the published provenance field.
    assert md.source == "crossref+openalex"


def test_a_complete_first_hit_still_costs_one_request():
    """The cost guard: gap-filling must not turn every paper into three API
    calls. An abstract in hand ends the loop."""
    crossref = _client(EnrichedMetadata(source="crossref", abstract="Have it."))
    openalex = _client(EnrichedMetadata(source="openalex", abstract="Unused."))
    semantic = _client(EnrichedMetadata(source="semantic_scholar"))
    e = _enricher(crossref=crossref, openalex=openalex,
                  semantic_scholar=semantic)

    md = e._enrich_by_doi("10.1/x")

    assert md.abstract == "Have it."
    assert md.source == "crossref"
    openalex.query_by_doi.assert_not_called()
    semantic.query_by_doi.assert_not_called()


def test_it_keeps_walking_until_an_abstract_turns_up():
    crossref = _client(EnrichedMetadata(source="crossref", venue="V"))
    openalex = _client(EnrichedMetadata(source="openalex", citation_count=7))
    semantic = _client(EnrichedMetadata(
        source="semantic_scholar", abstract="Found at the third."))
    e = _enricher(crossref=crossref, openalex=openalex,
                  semantic_scholar=semantic)

    md = e._enrich_by_doi("10.1/x")

    assert md.abstract == "Found at the third."
    assert md.venue == "V"
    assert md.citation_count == 7
    assert md.source == "crossref+openalex+semantic_scholar"


def test_no_abstract_anywhere_still_returns_the_best_record():
    crossref = _client(EnrichedMetadata(source="crossref", venue="V"))
    openalex = _client(EnrichedMetadata(source="openalex", citation_count=3))
    e = _enricher(crossref=crossref, openalex=openalex)

    md = e._enrich_by_doi("10.1/x")

    assert md is not None
    assert md.abstract is None
    assert md.venue == "V" and md.citation_count == 3


def test_a_failing_source_does_not_stop_the_walk():
    crossref = MagicMock()
    crossref.query_by_doi.side_effect = RuntimeError("boom")
    openalex = _client(EnrichedMetadata(source="openalex", abstract="A."))
    e = _enricher(crossref=crossref, openalex=openalex)

    md = e._enrich_by_doi("10.1/x")

    assert md.abstract == "A."
    assert md.source == "openalex"
    assert e.api_failure_counts["crossref"] == 1


def test_all_sources_dry_returns_none():
    crossref = _client(None)
    e = _enricher(crossref=crossref)
    assert e._enrich_by_doi("10.1/x") is None
    assert e.api_failure_counts["crossref"] == 1


# ---- fill_missing_fields ---------------------------------------------------


def test_fill_never_overwrites_a_value_the_base_already_has():
    base = EnrichedMetadata(source="crossref", abstract="Mine", venue="A")
    other = EnrichedMetadata(source="openalex", abstract="Theirs", venue="B")

    assert fill_missing_fields(base, other) == []
    assert base.abstract == "Mine" and base.venue == "A"


def test_zero_and_false_are_answers_not_gaps():
    """`citation_count: 0` and `is_open_access: False` are real values; a
    truthiness check would clobber them with a later source's."""
    base = EnrichedMetadata(source="crossref", citation_count=0,
                            is_open_access=False)
    other = EnrichedMetadata(source="openalex", citation_count=99,
                             is_open_access=True)

    assert fill_missing_fields(base, other) == []
    assert base.citation_count == 0
    assert base.is_open_access is False


def test_empty_lists_are_gaps_and_get_filled():
    base = EnrichedMetadata(source="crossref", authors=[], keywords=[])
    other = EnrichedMetadata(source="openalex", authors=["A B"], keywords=["k"])

    filled = fill_missing_fields(base, other)

    assert set(filled) == {"authors", "keywords"}
    assert base.authors == ["A B"]


def test_record_level_fields_are_never_merged():
    """`source` and `confidence_score` describe the record, not the work."""
    base = EnrichedMetadata(source="crossref")
    other = EnrichedMetadata(source="openalex", confidence_score=0.9,
                             abstract="A")

    fill_missing_fields(base, other)

    assert base.source == "crossref"
    assert base.confidence_score is None


# ---- OpenAlex null-valued keys ---------------------------------------------


def _openalex_work(**overrides):
    """An OpenAlex work in the shape the live API actually returns: the
    deprecated `host_venue` and an absent OA copy come back as explicit
    nulls, not as missing keys."""
    work = {
        "title": "The Privatization of Public Discourse",
        "publication_year": 2025,
        "host_venue": None,
        "best_oa_location": None,
        "primary_location": {"source": {"display_name": "A Companion"}},
        "open_access": {"is_oa": False},
        "authorships": [{"author": {"display_name": "Raquel Recuero"}}],
        "abstract_inverted_index": {"A": [0], "real": [1], "abstract": [2]},
    }
    work.update(overrides)
    return work


def test_null_valued_oa_location_does_not_break_parsing():
    """`.get(k, {})` returns None when the key exists with a null value — the
    default only covers a *missing* key. This raised AttributeError for every
    work with no OA copy, and the exception was swallowed as "Unexpected
    error querying OpenAlex", silently taking the whole rung out of service.
    """
    from src.metadata_enricher import OpenAlexClient
    md = OpenAlexClient()._parse_response(_openalex_work())

    assert md.abstract == "A real abstract"
    assert md.authors == ["Raquel Recuero"]
    assert md.pdf_url is None


def test_null_author_in_an_authorship_is_skipped_not_fatal():
    from src.metadata_enricher import OpenAlexClient
    work = _openalex_work(authorships=[{"author": None},
                                       {"author": {"display_name": "A B"}}])
    md = OpenAlexClient()._parse_response(work)

    assert "A B" in md.authors


def test_an_oa_pdf_is_still_picked_up_when_present():
    """The guard must not cost us the field it guards."""
    from src.metadata_enricher import OpenAlexClient
    work = _openalex_work(
        open_access={"is_oa": True},
        best_oa_location={"pdf_url": "https://example.org/paper.pdf"})
    md = OpenAlexClient()._parse_response(work)

    assert md.pdf_url == "https://example.org/paper.pdf"
