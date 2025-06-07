# ToRead - Academic Paper Feed Generator

ToRead converts Paperpile BibTeX exports into RSS and JSON Feed formats, enriched with metadata from academic APIs. It automatically syncs with your Paperpile "To Read" folder and generates feeds that work with modern feed readers and academic workflow tools.

## Features

- **Dual Format Output**: Generates both JSON Feed (primary) and RSS (compatibility)
- **Rich Metadata Enrichment**: Integrates with Crossref and Semantic Scholar APIs
- **Automatic Sync**: Monitors Paperpile exports for updates every 15 minutes
- **Academic Focus**: Includes citation counts, DOI links, PDF access, and venue information
- **Robust Parsing**: Handles complex BibTeX files with LaTeX formatting and missing fields
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
```yaml
# Free API key from https://www.semanticscholar.org/product/api
# Update config.yml:
api:
  semantic_scholar:
    api_key: "YOUR_API_KEY_HERE"
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

## Configuration Reference

### Complete config.yml

```yaml
# Paperpile Configuration
paperpile:
  folder_path: "/To Read"
  export_url: "https://paperpile.com/eb/YOUR_EXPORT_ID"
  local_cache: "data/paperpile_export.bib"
  auto_sync: true
  sync_interval: 15  # minutes

# API Configuration
api:
  crossref:
    enabled: true
    rate_limit: 1.0
    user_agent: "ToRead/1.0 (mailto:your@email.com)"
    
  semantic_scholar:
    enabled: true
    api_key: "YOUR_S2_API_KEY"
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

Create `.github/workflows/update_feed.yml`:

```yaml
name: Update Academic Feed

on:
  schedule:
    - cron: '*/15 * * * *'  # Every 15 minutes
  workflow_dispatch:  # Manual trigger

jobs:
  update-feed:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v4
    
    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.11'
        
    - name: Install dependencies
      run: |
        pip install -r requirements.txt
        
    - name: Generate feeds
      env:
        S2_API_KEY: ${{ secrets.SEMANTIC_SCHOLAR_API_KEY }}
      run: |
        python -m src.main data/paperpile_export.bib -o output/
        
    - name: Commit and push
      run: |
        git config --local user.email "action@github.com"
        git config --local user.name "GitHub Action"
        git add output/
        git diff --staged --quiet || git commit -m "🤖 Update academic feeds"
        git push
```

## Development

### Project Structure

```
toread/
├── src/
│   ├── __init__.py
│   ├── bibtex_parser.py      # BibTeX parsing with error handling
│   ├── metadata_enricher.py  # API clients for Crossref/Semantic Scholar
│   ├── rss_generator.py      # JSON Feed + RSS generation
│   └── main.py              # CLI application
├── tests/
│   ├── test_bibtex_parser.py
│   ├── test_metadata_enricher.py
│   └── test_rss_generator.py
├── data/                    # Input BibTeX files
├── output/                  # Generated feeds
├── logs/                    # Application logs
├── config.yml              # Configuration
├── requirements.txt
└── README.md
```

### Running Tests

```bash
# Install test dependencies
pip install pytest pytest-cov

# Run tests
pytest tests/

# Run with coverage
pytest --cov=src tests/
```

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
- [Semantic Scholar API](https://api.semanticscholar.org/) - AI-powered academic search