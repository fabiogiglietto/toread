# ToRead - Academic Paper Feed Generator

ToRead converts Paperpile BibTeX exports into RSS and JSON Feed formats, enriched with metadata from academic APIs. It automatically syncs with your Paperpile "To Read" folder and generates feeds that work with modern feed readers and academic workflow tools.

## Features

- **Dual Format Output**: Generates both JSON Feed (primary) and RSS (compatibility)
- **Rich Metadata Enrichment**: Integrates with Crossref, OpenAlex, Semantic Scholar, and ArXiv APIs
- **Smart Automatic Sync**: Monitors Paperpile exports every 30 minutes, only regenerates when content changes
- **Performance Optimized**: Skips unnecessary processing when no new papers detected, saving execution time and API quota
- **Academic Focus**: Includes citation counts, DOI links, PDF access, open access status, and venue information
- **Robust Parsing**: Handles complex BibTeX files with LaTeX formatting and missing fields
- **Fallback Links**: Papers without DOIs get Google Scholar search links as fallback
- **Race Condition Prevention**: Concurrency control ensures safe automated updates
- **Persistent Cache**: Metadata cache tracked in git for reliable cross-run persistence
- **Extensible Architecture**: Easy to add new metadata sources and output formats

## Installation

### Prerequisites

- Python 3.8 or higher
- Git

### Quick Setup

```bash
# Clone the repository
git clone https://github.com/user/toread.git
cd toread

# Create and activate virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Create necessary directories
mkdir -p data output logs cache
```

### Configuration

1. **Set up your Paperpile export URL:**
   - In Paperpile, go to your "To Read" folder
   - Export as BibTeX and copy the permanent URL
   - Update `paperpile.export_url` in config.yml

2. **Configure API access (optional but recommended):**

#### Semantic Scholar API

Set your API key via environment variable (recommended for security):
```bash
export SEMANTIC_SCHOLAR_API_KEY="your_api_key_here"
```

Or in config.yml (supports environment variable substitution):
```yaml
api:
  semantic_scholar:
    api_key: "${SEMANTIC_SCHOLAR_API_KEY}"
```

#### OpenAlex API
No API key required. Configure your email for "polite pool" access (faster responses):
```bash
export OPENALEX_EMAIL="your@email.com"
```

Or directly in config.yml:
```yaml
api:
  openalex:
    email: "your@email.com"
```

#### Crossref API
No API key required, but configure your email for polite usage:
```yaml
api:
  crossref:
    user_agent: "ToRead/1.0 (https://github.com/youruser/toread; mailto:your@email.com)"
```

## Usage

### Command Line Interface

#### Basic Usage
```bash
# Generate both JSON Feed and RSS from a BibTeX file
python -m src.main papers.bib -o output/

# Generate only JSON Feed
python -m src.main papers.bib -j feed.json

# Generate only RSS
python -m src.main papers.bib -r feed.xml

# Disable metadata enrichment (faster)
python -m src.main papers.bib --no-enrich -o output/
```

#### Advanced Options
```bash
# Custom feed metadata
python -m src.main papers.bib \
  --feed-title "My Research Papers" \
  --feed-description "Papers I'm currently reading" \
  --feed-link "https://mysite.com/research" \
  -o output/

# Rate limiting for API calls
python -m src.main papers.bib --rate-limit 2.0 -o output/

# Enable verbose logging for debugging
python -m src.main papers.bib -v -o output/

# Use custom config file
python -m src.main papers.bib --config my-config.yml -o output/

# Skip enrichment for cached entries (faster for scheduled runs)
python -m src.main papers.bib --skip-cached-enrichment -o output/
```

### Programmatic Usage

```python
from src.bibtex_parser import BibTeXParser
from src.metadata_enricher import MetadataEnricher
from src.rss_generator import FeedGenerator

# Parse BibTeX
parser = BibTeXParser()
entries = parser.parse_file('papers.bib')

# Enrich with metadata
enricher = MetadataEnricher(
    crossref_config={'enabled': True},
    semantic_scholar_config={'enabled': True, 'api_key': 'your_key'}
)
metadata = enricher.enrich_entries(entries)

# Generate feeds
generator = FeedGenerator(
    feed_title="My Papers",
    feed_description="Academic papers feed"
)

json_feed = generator.generate_json_feed(entries, metadata)
rss_feed = generator.generate_rss(entries, metadata)
```

## Output Formats

### JSON Feed (Primary Format)

JSON Feed 1.1 compliant with academic extensions:

```json
{
  "version": "https://jsonfeed.org/version/1.1",
  "title": "To Read - Research Papers Feed",
  "items": [
    {
      "id": "doi:10.1000/182",
      "title": "Deep Learning for Academic Paper Analysis",
      "content_text": "Abstract text...",
      "content_html": "<h3>Abstract</h3><p>...</p>",
      "url": "https://doi.org/10.1000/182",
      "date_published": "2024-01-15T00:00:00Z",
      "authors": [{"name": "Jane Smith"}, {"name": "John Doe"}],
      "tags": ["Machine Learning", "AI", "Computer Science"],
      "_academic": {
        "doi": "10.1000/182",
        "citation_count": 42,
        "open_access": true,
        "type": "article",
        "venue": "Journal of AI Research"
      }
    }
  ]
}
```

### RSS Feed (Compatibility Format)

Standard RSS 2.0 with Dublin Core extensions for academic metadata.

## API Setup Guide

### Metadata Enrichment Priority

ToRead queries APIs in this order (first successful match wins):

1. **Crossref** - Primary source for DOI lookups
2. **OpenAlex** - Good coverage, open access detection
3. **Semantic Scholar** - AI/ML papers, citation metrics
4. **ArXiv** - Preprints and working papers
5. **Google Scholar** - Fallback search link if all else fails

### Crossref API

No signup required, but configure polite usage:

```yaml
api:
  crossref:
    user_agent: "ToRead/1.0 (https://github.com/youruser/toread; mailto:your@email.com)"
    rate_limit: 1.0  # seconds between requests
```

**Benefits:**
- Comprehensive academic database
- Publication dates and venues
- Publisher information
- DOI resolution

### OpenAlex API

No signup required. Free access to 240M+ scholarly works.

```yaml
api:
  openalex:
    email: "your@email.com"  # For faster "polite pool" access
    rate_limit: 0.1  # 10 requests/second allowed
```

**Benefits:**
- Open access detection and PDF URLs
- Good abstract coverage (60%+ of works)
- Citation counts
- No API key required

### Semantic Scholar API

1. **Sign up** at [semanticscholar.org/product/api](https://www.semanticscholar.org/product/api)
2. **Get your API key** from the dashboard
3. **Update config.yml:**
```yaml
api:
  semantic_scholar:
    api_key: "your_api_key_here"
```

**Benefits:**
- Citation counts and metrics
- Better abstract coverage
- AI/ML paper specialization
- Open access PDF links

## Configuration Reference

### Complete config.yml

```yaml
# Paperpile Configuration
paperpile:
  folder_path: "/To Read"
  export_url: "https://paperpile.com/eb/YOUR_EXPORT_ID"
  local_cache: "data/paperpile_export.bib"
  auto_sync: true
  sync_interval: 30  # minutes (optimized for performance)

# API Configuration
api:
  crossref:
    enabled: true
    rate_limit: 1.0
    user_agent: "ToRead/1.0 (mailto:your@email.com)"

  openalex:
    enabled: true
    email: "your@email.com"  # For polite pool access
    rate_limit: 0.1

  semantic_scholar:
    enabled: true
    api_key: "${SEMANTIC_SCHOLAR_API_KEY}"
    rate_limit: 1.0

# Feed Output Settings
feeds:
  title: "To Read - Research Papers Feed"
  description: "Academic papers from Paperpile enriched with metadata"
  link: "https://github.com/user/toread"

  json_feed:
    enabled: true
    output_file: "output/feed.json"
    include_full_metadata: true

  rss:
    enabled: true
    output_file: "output/feed.xml"
    simplified: true

  max_items: 100
  include_abstract: true
  abstract_max_length: 500
```

## Automation

### GitHub Actions (Recommended)

This repository includes a production-ready GitHub Actions workflow with advanced features:

#### Key Features
- **Smart Change Detection**: Only regenerates feeds when Paperpile export actually changes
- **Performance Optimized**: Skips processing when no changes detected (saves 75-85% execution time)
- **Concurrency Control**: Prevents race conditions from simultaneous workflow runs
- **Robust Retry Logic**: Handles concurrent updates gracefully with exponential backoff
- **Metadata Caching**: Reuses cached API responses to minimize API calls

#### Setup

1. **Configure GitHub Secrets**:
   - Go to repository Settings → Secrets and variables → Actions
   - Add `PAPERPILE_EXPORT_URL`: Your Paperpile export URL
   - Add `SEMANTIC_SCHOLAR_API_KEY`: Your S2 API key (optional but recommended)

2. **The workflow runs automatically**:
   - Every 30 minutes via cron schedule
   - On manual trigger via workflow_dispatch
   - On code changes to src/, data/, or config.yml

3. **Monitor workflow runs**:
   - View in the Actions tab of your repository
   - Workflows skip processing when no changes detected
   - Check logs for entry count changes and performance metrics

#### Sample Workflow (Simplified)

The full workflow is in `.github/workflows/update_feed.yml`. Here's a simplified version:

```yaml
name: Update Academic Feed

on:
  schedule:
    - cron: '*/30 * * * *'  # Every 30 minutes
  workflow_dispatch:

jobs:
  update-feed:
    runs-on: ubuntu-latest

    # Prevent concurrent runs
    concurrency:
      group: update-feed
      cancel-in-progress: false

    steps:
    - uses: actions/checkout@v4
      with:
        fetch-depth: 0

    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.11'
        cache: 'pip'

    - name: Install dependencies
      run: pip install -r requirements.txt

    - name: Restore metadata cache
      uses: actions/cache@v3
      with:
        path: cache/
        key: toread-metadata-cache-v2-${{ hashFiles('data/paperpile_export.bib') }}

    - name: Download and check Paperpile export
      run: |
        # Download BibTeX
        curl -L "${{ secrets.PAPERPILE_EXPORT_URL }}" -o /tmp/new.bib

        # Compare with previous version
        if diff -q data/paperpile_export.bib /tmp/new.bib; then
          echo "No changes detected - skipping feed generation"
          echo "changed=false" >> $GITHUB_OUTPUT
        else
          mv /tmp/new.bib data/paperpile_export.bib
          echo "changed=true" >> $GITHUB_OUTPUT
        fi
      id: bib-check

    - name: Generate feeds
      if: steps.bib-check.outputs.changed == 'true'
      run: |
        python -m src.main data/paperpile_export.bib -o output/ \
          --feed-title "To Read - Research Papers" \
          --rate-limit 3.0 --skip-cached-enrichment

    - name: Commit and push with retry
      if: steps.bib-check.outputs.changed == 'true'
      run: |
        git config --local user.email "action@github.com"
        git config --local user.name "GitHub Action"

        # Sync-before-push strategy prevents merge conflicts
        for i in {1..5}; do
          git fetch origin main
          git reset --soft origin/main
          git add data/paperpile_export.bib output/
          git commit -m "🤖 Update academic feeds"
          git push && break || sleep $((5 * i))
        done
```

#### Performance Impact

- **Without changes**: ~30 seconds (download + comparison only)
- **With changes**: ~2-3 minutes (full processing + API enrichment)
- **API calls saved**: 100% when no changes detected
- **Execution time saved**: 75-85% on average

## Development

### Project Structure

```
toread/
├── src/
│   ├── __init__.py
│   ├── bibtex_parser.py      # BibTeX parsing with error handling
│   ├── metadata_enricher.py  # API clients for Crossref/Semantic Scholar/ArXiv
│   ├── rss_generator.py      # JSON Feed + RSS generation with XSS protection
│   ├── cache.py              # Metadata and discovery date caching
│   ├── utils.py              # Shared text matching utilities
│   └── main.py               # CLI application with config loading
├── tests/
│   ├── __init__.py
│   ├── test_bibtex_parser.py # 13 tests for BibTeX parsing
│   ├── test_cache.py         # 12 tests for caching system
│   ├── test_openalex.py      # 9 tests for OpenAlex client
│   ├── test_rss_generator.py # 15 tests for feed generation
│   └── test_utils.py         # 42 tests for text utilities
├── data/                     # Input BibTeX files
├── output/                   # Generated feeds
├── cache/                    # Metadata cache (JSON)
├── logs/                     # Application logs
├── config.yml                # Configuration (supports env vars)
├── requirements.txt
└── README.md
```

### Running Tests

```bash
# Install test dependencies
pip install pytest pytest-cov

# Run all 91 tests
pytest tests/

# Run with coverage report
pytest --cov=src tests/

# Run specific test file
pytest tests/test_utils.py -v

# Run tests matching a pattern
pytest -k "test_xss" -v
```

## Security

ToRead includes several security measures:

- **API Key Protection**: API keys should be set via environment variables, not hardcoded
- **XSS Prevention**: All titles and content are HTML-escaped before output
- **URL Validation**: Only `http://` and `https://` URLs are allowed in feeds; `javascript:` and `data:` schemes are rejected
- **Input Sanitization**: BibTeX input is parsed safely with proper escaping

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `SEMANTIC_SCHOLAR_API_KEY` | Semantic Scholar API authentication |
| `OPENALEX_EMAIL` | OpenAlex "polite pool" access (faster responses) |

## Troubleshooting

### Common Issues

#### "No entries found in BibTeX file"
- Check BibTeX file format and encoding
- Ensure the file is properly exported from Paperpile
- Try with `--no-enrich` flag to isolate parsing issues

#### API Rate Limiting
- Increase `rate_limit` values in config.yml
- Check API key validity for Semantic Scholar
- Ensure proper user agent for Crossref

#### Missing Metadata
- Some papers may not be in academic databases
- Preprint papers often have limited metadata
- Check logs for API response details

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature-name`
3. Make changes and add tests
4. Run tests: `pytest`
5. Submit a pull request

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Related Projects

- [JSON Feed](https://jsonfeed.org/) - Modern feed format specification
- [Paperpile](https://paperpile.com/) - Reference management tool
- [Crossref API](https://api.crossref.org/) - Academic metadata API
- [OpenAlex](https://openalex.org/) - Open catalog of scholarly works
- [Semantic Scholar API](https://api.semanticscholar.org/) - AI-powered academic search
- [ArXiv](https://arxiv.org/) - Open-access preprint repository
