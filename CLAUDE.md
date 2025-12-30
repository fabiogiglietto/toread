# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ToRead is an academic paper feed generator that converts Paperpile BibTeX exports into RSS and JSON Feed formats, enriched with metadata from academic APIs (Crossref, Semantic Scholar, ArXiv). The application processes academic bibliographic data and generates feeds for consumption by feed readers and academic workflow tools.

## Development Commands

### Virtual Environment
```bash
# Activate virtual environment (always required)
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements.txt
```

### Running the Application
```bash
# Basic usage - convert BibTeX to both JSON Feed and RSS
python -m src.main input.bib -o output/

# Generate only JSON Feed
python -m src.main input.bib -j feed.json

# Generate only RSS
python -m src.main input.bib -r feed.xml

# Disable metadata enrichment (faster, no API calls)
python -m src.main input.bib --no-enrich -o output/

# Custom rate limiting for API calls
python -m src.main input.bib --rate-limit 2.0 -o output/

# Verbose logging for debugging
python -m src.main input.bib -v -o output/

# Use custom config file
python -m src.main input.bib --config custom-config.yml -o output/
```

### Environment Variables
```bash
# Semantic Scholar API key (recommended over config file)
export SEMANTIC_SCHOLAR_API_KEY="your_api_key_here"
```

### Testing
```bash
# Install test dependencies first
pip install pytest pytest-cov

# Run all 62 tests
pytest tests/

# Run with coverage
pytest --cov=src tests/

# Run specific test module
pytest tests/test_utils.py -v
```

## Architecture

### Core Components

**BibTeXParser** (`src/bibtex_parser.py`): Robust BibTeX parsing with error handling for malformed entries, LaTeX formatting, and missing fields. Outputs structured `BibEntry` objects.

**MetadataEnricher** (`src/metadata_enricher.py`): Multi-API client system that enriches bibliographic entries with:
- Crossref API: Publication metadata, DOI resolution, venue information
- Semantic Scholar API: Citation counts, AI/ML paper specialization, abstracts
- ArXiv API: Preprint metadata and PDF links

**FeedGenerator** (`src/rss_generator.py`): Dual-format feed generation with security features:
- JSON Feed 1.1 (primary): Rich academic metadata with custom `_academic` extensions
- RSS 2.0 (compatibility): Dublin Core extensions for academic metadata
- XSS protection: HTML escaping and URL validation

**Cache** (`src/cache.py`): Persistent caching system:
- `MetadataCache`: Stores enriched metadata with 30-day expiration
- `DiscoveryCache`: Tracks when entries were first discovered

**Utils** (`src/utils.py`): Shared text matching utilities:
- `clean_title_for_search()`: Cleans LaTeX and formatting from titles
- `calculate_text_similarity()`: Jaccard similarity with stop word filtering
- `calculate_author_similarity()`: Author name matching across formats

### Data Flow

1. BibTeX file → BibTeXParser → List[BibEntry]
2. BibEntry objects → MetadataEnricher → Dict[key, EnrichedMetadata]
3. Entries + Metadata → FeedGenerator → JSON Feed + RSS output

### Configuration System

Configuration is centralized in `config.yml` with sections for:
- API credentials and rate limiting
- Feed metadata and output settings
- Processing options and error handling
- Paperpile integration settings
- Logging configuration

The config system supports environment variable substitution using `${VAR_NAME}` syntax. CLI arguments override config file values.

### API Integration

The metadata enricher uses a multi-source approach with fallbacks:
1. Primary DOI lookup via Crossref
2. Semantic Scholar for AI/ML papers and citation metrics
3. ArXiv for preprint identification and PDF access
4. Robust rate limiting and retry logic for all APIs

### Output Formats

**JSON Feed**: Modern format with academic extensions including citation counts, DOI links, venue information, and open access indicators.

**RSS**: Traditional format with Dublin Core metadata extensions for compatibility with older feed readers.

## Key Dependencies

- `requests`: HTTP client for API interactions
- `arxiv`: Official ArXiv API client
- `PyYAML`: Configuration file parsing
- Core Python libraries for XML/JSON processing and argument parsing

## File Structure Notes

- `data/`: Input BibTeX files
- `output/`: Generated feed files (feed.json, feed.xml)
- `cache/`: Metadata and discovery caches (JSON)
- `logs/`: Application logging
- `src/`: Main application modules
- `tests/`: Test suite (62 tests across 4 files)
- `config.yml`: Central configuration file (supports env vars)

## Testing Approach

The test suite covers all core components with 62 tests:
- `test_bibtex_parser.py`: BibTeX parsing, LaTeX handling, field extraction
- `test_cache.py`: Cache persistence, expiration, retry logic
- `test_rss_generator.py`: Feed generation, XSS protection, date handling
- `test_utils.py`: Text similarity, author matching, title cleaning

When writing new tests, follow the existing pattern of testing each component in isolation. Use pytest fixtures for common setup.

## Security Considerations

- API keys should be set via environment variables, not hardcoded
- All HTML output is escaped to prevent XSS
- URLs are validated to reject `javascript:` and `data:` schemes
- Config file supports `${VAR}` syntax for secure credential injection