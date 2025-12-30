"""Tests for shared utility functions."""

import pytest
from src.utils import (
    clean_title_for_search,
    calculate_text_similarity,
    calculate_author_similarity,
    calculate_crossref_author_similarity,
    extract_first_author,
)


class TestCleanTitleForSearch:
    """Tests for clean_title_for_search function."""

    def test_removes_latex_commands(self):
        """LaTeX commands like \\textbf{} should be removed."""
        title = "A \\textbf{Bold} Approach to \\emph{Machine} Learning"
        result = clean_title_for_search(title)
        assert "\\textbf" not in result
        assert "\\emph" not in result
        assert "Bold" in result
        assert "Machine" in result

    def test_removes_braces(self):
        """Curly braces should be removed."""
        title = "{Deep} Learning for {NLP}"
        result = clean_title_for_search(title)
        assert "{" not in result
        assert "}" not in result
        assert "Deep" in result

    def test_normalizes_whitespace(self):
        """Multiple spaces should be normalized to single spaces."""
        title = "Machine    Learning   with   Python"
        result = clean_title_for_search(title)
        assert "    " not in result
        assert result == "Machine Learning with Python"

    def test_handles_empty_string(self):
        """Empty strings should return empty."""
        assert clean_title_for_search("") == ""
        assert clean_title_for_search(None) == ""

    def test_preserves_hyphens_and_colons(self):
        """Hyphens and colons should be preserved."""
        title = "Pre-trained Models: A Survey"
        result = clean_title_for_search(title)
        assert "-" in result
        assert ":" in result


class TestCalculateTextSimilarity:
    """Tests for calculate_text_similarity function."""

    def test_identical_texts(self):
        """Identical texts should have similarity close to 1.0."""
        text = "machine learning neural networks"
        result = calculate_text_similarity(text, text)
        assert result >= 0.9

    def test_completely_different_texts(self):
        """Completely different texts should have low similarity."""
        result = calculate_text_similarity(
            "machine learning neural networks",
            "banana apple orange fruit"
        )
        assert result < 0.3

    def test_partial_overlap(self):
        """Partial overlap should give moderate similarity."""
        result = calculate_text_similarity(
            "machine learning models",
            "deep learning models"
        )
        assert 0.3 < result < 0.8

    def test_stop_words_ignored(self):
        """Stop words should not affect similarity."""
        result1 = calculate_text_similarity("the machine", "a machine")
        result2 = calculate_text_similarity("machine", "machine")
        # Both should be high since stop words are filtered
        assert result1 >= 0.9
        assert result2 >= 0.9

    def test_empty_strings(self):
        """Empty strings should return 0."""
        assert calculate_text_similarity("", "hello") == 0.0
        assert calculate_text_similarity("hello", "") == 0.0
        assert calculate_text_similarity("", "") == 0.0

    def test_substring_bonus(self):
        """Substring matches should get a bonus."""
        result = calculate_text_similarity(
            "neural networks",
            "deep neural networks"
        )
        # Should be higher due to substring bonus
        assert result > 0.5


class TestCalculateAuthorSimilarity:
    """Tests for calculate_author_similarity function."""

    def test_exact_match(self):
        """Exact author match should give high similarity."""
        result = calculate_author_similarity(
            "John Smith",
            ["John Smith", "Jane Doe"]
        )
        assert result >= 0.9

    def test_and_separated_authors(self):
        """Should handle 'and'-separated author strings."""
        result = calculate_author_similarity(
            "John Smith and Jane Doe",
            ["John Smith"]
        )
        assert result >= 0.9

    def test_no_match(self):
        """No matching authors should give low similarity."""
        result = calculate_author_similarity(
            "John Smith",
            ["Alice Brown", "Bob Wilson"]
        )
        assert result < 0.3

    def test_empty_inputs(self):
        """Empty inputs should return 0."""
        assert calculate_author_similarity("", ["John"]) == 0.0
        assert calculate_author_similarity("John", []) == 0.0


class TestCalculateCrossrefAuthorSimilarity:
    """Tests for calculate_crossref_author_similarity function."""

    def test_crossref_format(self):
        """Should handle Crossref author format."""
        crossref_authors = [
            {"given": "John", "family": "Smith"},
            {"given": "Jane", "family": "Doe"}
        ]
        result = calculate_crossref_author_similarity("John Smith", crossref_authors)
        assert result >= 0.9

    def test_family_only(self):
        """Should handle authors with only family name."""
        crossref_authors = [{"family": "Smith"}]
        result = calculate_crossref_author_similarity("Smith", crossref_authors)
        assert result >= 0.9

    def test_empty_authors(self):
        """Empty author list should return 0."""
        assert calculate_crossref_author_similarity("John", []) == 0.0


class TestExtractFirstAuthor:
    """Tests for extract_first_author function."""

    def test_single_author(self):
        """Single author should be returned as-is."""
        assert extract_first_author("John Smith") == "John Smith"

    def test_and_separated(self):
        """Should extract first author from 'and'-separated list."""
        result = extract_first_author("John Smith and Jane Doe and Bob Wilson")
        assert result == "John Smith"

    def test_comma_separated_name(self):
        """Should handle 'Last, First' format."""
        result = extract_first_author("Smith, John and Doe, Jane")
        assert result == "Smith"

    def test_empty_string(self):
        """Empty string should return empty."""
        assert extract_first_author("") == ""
