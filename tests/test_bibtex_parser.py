"""Tests for BibTeX parser module."""

import pytest
from datetime import datetime, timezone
from src.bibtex_parser import BibTeXParser, BibEntry


class TestBibTeXParser:
    """Tests for BibTeXParser class."""

    @pytest.fixture
    def parser(self):
        """Create a parser instance."""
        return BibTeXParser()

    def test_parse_simple_article(self, parser):
        """Should parse a simple article entry."""
        bibtex = """
        @article{smith2023,
            author = {John Smith and Jane Doe},
            title = {A Study on Machine Learning},
            journal = {Journal of AI},
            year = {2023},
            volume = {10},
            pages = {1-20}
        }
        """
        entries = parser.parse_string(bibtex)

        assert len(entries) == 1
        entry = entries[0]
        assert entry.key == "smith2023"
        assert entry.entry_type == "article"
        assert entry.title == "A Study on Machine Learning"
        assert entry.year == "2023"
        assert entry.journal == "Journal of AI"
        assert len(entry.authors) == 2
        assert "John Smith" in entry.authors

    def test_parse_with_doi(self, parser):
        """Should parse and clean DOI field."""
        bibtex = """
        @article{test2023,
            author = {Test Author},
            title = {Test Title},
            year = {2023},
            doi = {https://doi.org/10.1234/test.2023}
        }
        """
        entries = parser.parse_string(bibtex)

        assert len(entries) == 1
        assert entries[0].doi == "10.1234/test.2023"

    def test_parse_with_latex_formatting(self, parser):
        """Should remove LaTeX formatting from title."""
        bibtex = """
        @article{latex2023,
            author = {Author},
            title = {\\textbf{Bold} and \\emph{Italic} Text},
            year = {2023}
        }
        """
        entries = parser.parse_string(bibtex)

        assert len(entries) == 1
        assert "\\textbf" not in entries[0].title
        assert "Bold" in entries[0].title

    def test_parse_multiple_entries(self, parser):
        """Should parse multiple entries."""
        bibtex = """
        @article{first2023,
            author = {First Author},
            title = {First Paper},
            year = {2023}
        }

        @inproceedings{second2023,
            author = {Second Author},
            title = {Second Paper},
            year = {2023},
            booktitle = {Conference Proceedings}
        }
        """
        entries = parser.parse_string(bibtex)

        assert len(entries) == 2
        assert entries[0].entry_type == "article"
        assert entries[1].entry_type == "inproceedings"

    def test_parse_with_abstract(self, parser):
        """Should parse abstract field."""
        bibtex = """
        @article{abstract2023,
            author = {Author},
            title = {Title},
            year = {2023},
            abstract = {This is a detailed abstract about the paper.}
        }
        """
        entries = parser.parse_string(bibtex)

        assert len(entries) == 1
        assert "detailed abstract" in entries[0].abstract

    def test_parse_with_keywords(self, parser):
        """Should parse comma-separated keywords."""
        bibtex = """
        @article{keywords2023,
            author = {Author},
            title = {Title},
            year = {2023},
            keywords = {machine learning, deep learning, neural networks}
        }
        """
        entries = parser.parse_string(bibtex)

        assert len(entries) == 1
        assert len(entries[0].keywords) == 3
        assert "machine learning" in entries[0].keywords

    def test_parse_month_name(self, parser):
        """Should convert month name to number."""
        bibtex = """
        @article{month2023,
            author = {Author},
            title = {Title},
            year = {2023},
            month = {January}
        }
        """
        entries = parser.parse_string(bibtex)

        assert len(entries) == 1
        assert entries[0].month == "01"

    def test_parse_empty_content(self, parser):
        """Should handle empty content gracefully."""
        entries = parser.parse_string("")
        assert len(entries) == 0

    def test_parse_malformed_entry(self, parser):
        """Should skip malformed entries and continue."""
        bibtex = """
        @article{good2023,
            author = {Good Author},
            title = {Good Title},
            year = {2023}
        }

        @article{bad2023
            this is malformed

        @article{alsogood2023,
            author = {Also Good},
            title = {Also Good Title},
            year = {2023}
        }
        """
        entries = parser.parse_string(bibtex)

        # Should parse at least the good entries
        assert len(entries) >= 1

    def test_entry_by_key(self, parser):
        """Should retrieve entry by key."""
        bibtex = """
        @article{findme2023,
            author = {Author},
            title = {Find Me},
            year = {2023}
        }
        """
        parser.parse_string(bibtex)

        entry = parser.get_entry_by_key("findme2023")
        assert entry is not None
        assert entry.title == "Find Me"

        missing = parser.get_entry_by_key("nonexistent")
        assert missing is None

    def test_filter_by_type(self, parser):
        """Should filter entries by type."""
        bibtex = """
        @article{art2023,
            author = {Author},
            title = {Article},
            year = {2023}
        }

        @book{book2023,
            author = {Author},
            title = {Book},
            year = {2023}
        }
        """
        parser.parse_string(bibtex)

        articles = parser.filter_entries_by_type("article")
        assert len(articles) == 1
        assert articles[0].entry_type == "article"


class TestBibEntry:
    """Tests for BibEntry dataclass."""

    def test_default_values(self):
        """Should have sensible default values."""
        entry = BibEntry(entry_type="article", key="test")

        assert entry.title is None
        assert entry.authors == []
        assert entry.keywords == []
        assert entry.raw_fields == {}

    def test_with_values(self):
        """Should accept provided values."""
        entry = BibEntry(
            entry_type="article",
            key="test",
            title="Test Title",
            authors=["Author One", "Author Two"],
            year="2023"
        )

        assert entry.title == "Test Title"
        assert len(entry.authors) == 2
        assert entry.year == "2023"
