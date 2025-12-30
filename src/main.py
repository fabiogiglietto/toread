"""Main module for the ToRead application."""

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional, Dict, Any

import yaml

from .bibtex_parser import BibTeXParser
from .metadata_enricher import MetadataEnricher
from .rss_generator import FeedGenerator
from .cache import DiscoveryCache


def setup_logging(config: Dict[str, Any] = None, verbose: bool = False) -> None:
    """Configure logging based on config file and command-line options.

    Args:
        config: Logging configuration from config.yml
        verbose: If True, set level to DEBUG regardless of config
    """
    config = config or {}

    # Determine log level
    if verbose:
        level = logging.DEBUG
    else:
        level_name = config.get('level', 'INFO').upper()
        level = getattr(logging, level_name, logging.INFO)

    # Log format
    log_format = config.get(
        'format',
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Configure root logger
    handlers = []

    # Console handler
    if config.get('console', True):
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter(log_format))
        handlers.append(console_handler)

    # File handler
    log_file = config.get('file')
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setFormatter(logging.Formatter(log_format))
        handlers.append(file_handler)

    # Apply configuration
    logging.basicConfig(
        level=level,
        format=log_format,
        handlers=handlers,
        force=True  # Override any existing configuration
    )

    # Reduce verbosity of third-party libraries
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)


def load_config(config_path: str = "config.yml") -> Dict[str, Any]:
    """Load configuration from YAML file with environment variable substitution.

    Supports ${VAR} syntax for environment variable substitution in config values.

    Args:
        config_path: Path to the config file

    Returns:
        Configuration dictionary, or empty dict if file not found
    """
    config_file = Path(config_path)
    if not config_file.exists():
        return {}

    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}

        # Substitute environment variables
        config = _substitute_env_vars(config)
        return config

    except Exception as e:
        print(f"Warning: Failed to load config file {config_path}: {e}")
        return {}


def _substitute_env_vars(obj: Any) -> Any:
    """Recursively substitute ${VAR} patterns with environment variables."""
    if isinstance(obj, dict):
        return {k: _substitute_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_substitute_env_vars(item) for item in obj]
    elif isinstance(obj, str):
        # Match ${VAR_NAME} pattern
        pattern = r'\$\{([^}]+)\}'
        def replace(match):
            var_name = match.group(1)
            return os.environ.get(var_name, match.group(0))
        return re.sub(pattern, replace, obj)
    return obj


class ToReadApp:
    """Main application class for converting BibTeX to RSS and JSON Feed formats."""

    def __init__(self, enrich_metadata: bool = True, crossref_config: dict = None,
                 semantic_scholar_config: dict = None, arxiv_config: dict = None,
                 openalex_config: dict = None, cache_config: dict = None,
                 skip_cached_enrichment: bool = False):
        self.bibtex_parser = BibTeXParser()
        self.metadata_enricher = MetadataEnricher(
            crossref_config, semantic_scholar_config, arxiv_config, openalex_config, cache_config
        ) if enrich_metadata else None
        self.feed_generator = FeedGenerator()
        self.skip_cached_enrichment = skip_cached_enrichment
        self.discovery_cache = DiscoveryCache("cache/discovery_cache.json")
    
    def convert_bibtex_to_feeds(self, bibtex_file: str, 
                               json_output_file: Optional[str] = None,
                               rss_output_file: Optional[str] = None) -> tuple[str, str]:
        """Convert a BibTeX file to RSS and JSON Feed formats."""
        print(f"Parsing BibTeX file: {bibtex_file}")
        
        # Parse BibTeX file
        try:
            entries = self.bibtex_parser.parse_file(bibtex_file)
            print(f"Found {len(entries)} entries")
            
            # Set discovery dates for all entries
            entries = self.bibtex_parser.set_discovery_dates(entries, self.discovery_cache)
            self.discovery_cache.save_cache()
            
        except Exception as e:
            print(f"Error parsing BibTeX file: {e}")
            return "", ""
        
        if not entries:
            print("No entries found in BibTeX file")
            return "", ""
        
        # Enrich metadata if enabled
        enriched_metadata = None
        if self.metadata_enricher:
            if self.skip_cached_enrichment:
                print("Running in cache-only mode - using existing cached metadata...")
                try:
                    # Get only cached metadata, don't enrich new entries
                    cached_dicts = self.metadata_enricher.cache.get_all_cached_metadata(entries)
                    enriched_metadata = {}
                    for key, metadata_dict in cached_dicts.items():
                        try:
                            from .metadata_enricher import EnrichedMetadata
                            enriched_metadata[key] = EnrichedMetadata(**metadata_dict)
                        except Exception as e:
                            print(f"Warning: Failed to load cached metadata for {key}: {e}")
                            enriched_metadata[key] = None
                    
                    cached_count = sum(1 for v in enriched_metadata.values() if v is not None)
                    print(f"Using cached metadata for {cached_count}/{len(entries)} entries")
                except Exception as e:
                    print(f"Warning: Error loading cached metadata: {e}")
                    print("Falling back to full enrichment...")
                    enriched_metadata = self.metadata_enricher.enrich_entries(entries)
            else:
                print("Enriching metadata...")
                try:
                    enriched_metadata = self.metadata_enricher.enrich_entries(entries)
                    enriched_count = sum(1 for v in enriched_metadata.values() if v is not None)
                    print(f"Enriched metadata for {enriched_count}/{len(entries)} entries")
                except Exception as e:
                    print(f"Warning: Error enriching metadata: {e}")
                    print("Continuing without metadata enrichment...")
        
        # Generate JSON Feed (primary format)
        json_content = ""
        if json_output_file:
            print("Generating JSON Feed...")
            try:
                json_content = self.feed_generator.generate_json_feed(entries, enriched_metadata)
                with open(json_output_file, 'w', encoding='utf-8') as f:
                    f.write(json_content)
                print(f"JSON Feed saved to: {json_output_file}")
            except Exception as e:
                print(f"Error generating JSON Feed: {e}")
        
        # Generate RSS (compatibility format)
        rss_content = ""
        if rss_output_file:
            print("Generating RSS feed...")
            try:
                rss_content = self.feed_generator.generate_rss(entries, enriched_metadata)
                with open(rss_output_file, 'w', encoding='utf-8') as f:
                    f.write(rss_content)
                print(f"RSS feed saved to: {rss_output_file}")
            except Exception as e:
                print(f"Error generating RSS: {e}")
        
        return json_content, rss_content


def main():
    """Main entry point for the command-line interface."""
    # Load configuration from file
    config = load_config()
    api_config = config.get('api', {})
    feeds_config = config.get('feeds', {})
    dirs_config = config.get('directories', {})

    parser = argparse.ArgumentParser(
        description="Convert Paperpile BibTeX exports to RSS feeds",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s input.bib                           # Convert to RSS and print to stdout
  %(prog)s input.bib -o output/                # Convert and save to directory
  %(prog)s input.bib --no-enrich               # Convert without metadata enrichment
  %(prog)s input.bib --rate-limit 2.0          # Use 2 second delay between API calls
  %(prog)s input.bib --skip-cached-enrichment  # Use only cached metadata (fast mode)
  %(prog)s input.bib --timeout 30              # Set 30 second timeout for API requests
        """
    )
    
    parser.add_argument(
        'bibtex_file',
        help='Path to the BibTeX file to convert'
    )
    
    parser.add_argument(
        '-j', '--json-output',
        help='Output JSON Feed file path'
    )
    
    parser.add_argument(
        '-r', '--rss-output',
        help='Output RSS file path'
    )
    
    parser.add_argument(
        '-o', '--output',
        help='Output directory (will create feed.json and feed.xml)'
    )
    
    parser.add_argument(
        '--no-enrich',
        action='store_true',
        help='Disable metadata enrichment from external sources'
    )
    
    parser.add_argument(
        '--rate-limit',
        type=float,
        default=1.0,
        help='Delay in seconds between API calls for metadata enrichment (default: 1.0)'
    )
    
    parser.add_argument(
        '--timeout',
        type=int,
        default=15,
        help='Timeout in seconds for API requests (default: 15)'
    )
    
    parser.add_argument(
        '--skip-cached-enrichment',
        action='store_true',
        help='Skip enrichment for entries that already have cached metadata (faster for scheduled runs)'
    )
    
    parser.add_argument(
        '--feed-title',
        default=feeds_config.get('title', 'ToRead - Academic Papers'),
        help='Title for the RSS feed'
    )

    parser.add_argument(
        '--feed-description',
        default=feeds_config.get('description', 'Academic papers from Paperpile exports'),
        help='Description for the RSS feed'
    )

    parser.add_argument(
        '--feed-link',
        default=feeds_config.get('link', 'https://github.com/user/toread'),
        help='Link for the RSS feed'
    )

    parser.add_argument(
        '--config',
        default='config.yml',
        help='Path to configuration file'
    )

    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose (debug) logging'
    )

    args = parser.parse_args()

    # Setup logging
    logging_config = config.get('logging', {})
    setup_logging(logging_config, verbose=args.verbose)
    
    # Validate input file
    if not Path(args.bibtex_file).exists():
        print(f"Error: BibTeX file '{args.bibtex_file}' does not exist")
        sys.exit(1)
    
    # Determine output files
    json_output = args.json_output
    rss_output = args.rss_output
    
    if args.output:
        # Output directory specified
        output_dir = Path(args.output)
        output_dir.mkdir(parents=True, exist_ok=True)
        if not json_output:
            json_output = str(output_dir / "feed.json")
        if not rss_output:
            rss_output = str(output_dir / "feed.xml")
    
    # Create API configurations, merging config file with command-line args
    crossref_cfg = api_config.get('crossref', {})
    crossref_config = {
        'enabled': crossref_cfg.get('enabled', True),
        'base_url': crossref_cfg.get('base_url', 'https://api.crossref.org/works'),
        'rate_limit': args.rate_limit or crossref_cfg.get('rate_limit', 1.0),
        'timeout': args.timeout or crossref_cfg.get('timeout', 15),
        'user_agent': crossref_cfg.get('user_agent', f'ToRead/1.0 ({args.feed_link})')
    }

    ss_cfg = api_config.get('semantic_scholar', {})
    semantic_scholar_config = {
        'enabled': ss_cfg.get('enabled', True),
        'base_url': ss_cfg.get('base_url', 'https://api.semanticscholar.org/graph/v1'),
        'rate_limit': args.rate_limit or ss_cfg.get('rate_limit', 1.0),
        'timeout': args.timeout or ss_cfg.get('timeout', 15),
        'api_key': os.environ.get('SEMANTIC_SCHOLAR_API_KEY') or ss_cfg.get('api_key')
    }

    arxiv_config = {
        'enabled': True,
        'rate_limit': max(3.0, args.rate_limit),  # ArXiv recommends 3 second delays minimum
        'timeout': args.timeout
    }

    openalex_cfg = api_config.get('openalex', {})
    openalex_config = {
        'enabled': openalex_cfg.get('enabled', True),
        'base_url': openalex_cfg.get('base_url', 'https://api.openalex.org/works'),
        'rate_limit': args.rate_limit or openalex_cfg.get('rate_limit', 0.1),
        'timeout': args.timeout or openalex_cfg.get('timeout', 15),
        'email': os.environ.get('OPENALEX_EMAIL') or openalex_cfg.get('email')
    }

    cache_dir = dirs_config.get('cache', 'cache')
    cache_config = {
        'cache_file': f'{cache_dir}/metadata_cache.json',
        'cache_duration_days': 30
    }
    
    # Create application instance
    app = ToReadApp(
        enrich_metadata=not args.no_enrich,
        crossref_config=crossref_config,
        semantic_scholar_config=semantic_scholar_config,
        arxiv_config=arxiv_config,
        openalex_config=openalex_config,
        cache_config=cache_config,
        skip_cached_enrichment=args.skip_cached_enrichment
    )
    
    # Set feed generator parameters
    app.feed_generator.feed_title = args.feed_title
    app.feed_generator.feed_description = args.feed_description
    app.feed_generator.feed_link = args.feed_link
    
    # Convert BibTeX to feeds
    try:
        json_content, rss_content = app.convert_bibtex_to_feeds(
            args.bibtex_file, 
            json_output, 
            rss_output
        )
        
        # Print to stdout if no output files specified
        if not json_output and not rss_output:
            if json_content:
                print("\n" + "="*50)
                print("JSON FEED OUTPUT:")
                print("="*50)
                print(json_content)
            if rss_content:
                print("\n" + "="*50)
                print("RSS FEED OUTPUT:")
                print("="*50)
                print(rss_content)
    
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()