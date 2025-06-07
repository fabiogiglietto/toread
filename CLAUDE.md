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
```

### Testing
```bash
# Install test dependencies first
pip install pytest pytest-cov

# Run tests
pytest tests/

# Run with coverage
pytest --cov=src tests/
```

## Architecture

### Core Components

**BibTeXParser** (`src/bibtex_parser.py`): Robust BibTeX parsing with error handling for malformed entries, LaTeX formatting, and missing fields. Outputs structured `BibEntry` objects.

**MetadataEnricher** (`src/metadata_enricher.py`): Multi-API client system that enriches bibliographic entries with:
- Crossref API: Publication metadata, DOI resolution, venue information
- Semantic Scholar API: Citation counts, AI/ML paper specialization, abstracts
- ArXiv API: Preprint metadata and PDF links

**FeedGenerator** (`src/rss_generator.py`): Dual-format feed generation:
- JSON Feed 1.1 (primary): Rich academic metadata with custom `_academic` extensions
- RSS 2.0 (compatibility): Dublin Core extensions for academic metadata

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
- Core Python libraries for XML/JSON processing and argument parsing

## File Structure Notes

- `data/`: Input BibTeX files and cache
- `output/`: Generated feed files
- `logs/`: Application logging
- `src/`: Main application modules
- `config.yml`: Central configuration file

## Testing Approach

When writing tests, follow the existing pattern of testing each component in isolation with mock API responses. The codebase uses pytest for testing framework.