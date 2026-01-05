"""Metadata enricher module for enhancing bibliographic entries with additional data."""

import requests
import time
import logging
import json
import random
import re
import arxiv
from typing import Dict, Optional, List, Union, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from .bibtex_parser import BibEntry
from .cache import MetadataCache
from .utils import (
    clean_title_for_search,
    calculate_text_similarity,
    calculate_author_similarity,
    calculate_crossref_author_similarity,
    extract_first_author,
    strip_jats_xml_tags,
    clean_url,
    extract_title_from_url,
    is_valid_title,
)


@dataclass
class EnrichedMetadata:
    """Enhanced metadata for a bibliographic entry."""
    abstract: Optional[str] = None
    keywords: List[str] = field(default_factory=list)
    doi: Optional[str] = None
    doi_url: Optional[str] = None
    url: Optional[str] = None
    arxiv_url: Optional[str] = None
    pdf_url: Optional[str] = None
    publication_date: Optional[str] = None
    citation_count: Optional[int] = None
    reference_count: Optional[int] = None
    venue: Optional[str] = None
    authors: List[str] = field(default_factory=list)
    subjects: List[str] = field(default_factory=list)
    is_open_access: Optional[bool] = None
    source: Optional[str] = None  # Which API provided the data
    confidence_score: Optional[float] = None  # Match confidence


class CrossrefClient:
    """Client for querying Crossref API with robust error handling and rate limiting."""
    
    def __init__(self, base_url: str = "https://api.crossref.org/works",
                 user_agent: str = "ToRead/1.0", rate_limit: float = 1.0,
                 max_retries: int = 3, backoff_factor: float = 0.5,
                 timeout: int = 15):
        self.base_url = base_url
        self.rate_limit = rate_limit
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.timeout = timeout
        self.logger = logging.getLogger(__name__)
        
        # Setup session with retry strategy
        self.session = requests.Session()
        retry_strategy = Retry(
            total=max_retries,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"],
            backoff_factor=backoff_factor
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        
        self.session.headers.update({
            'User-Agent': user_agent,
            'Accept': 'application/json'
        })
        self.last_request_time = 0
        self.request_count = 0
    
    def _rate_limit(self):
        """Apply rate limiting between requests with jitter to avoid thundering herd."""
        elapsed = time.time() - self.last_request_time
        if elapsed < self.rate_limit:
            # Add small random jitter to prevent synchronized requests
            jitter = random.uniform(0, 0.1)
            time.sleep(self.rate_limit - elapsed + jitter)
        self.last_request_time = time.time()
        self.request_count += 1
        
        # Log request frequency for monitoring
        if self.request_count % 50 == 0:
            self.logger.info(f"Crossref: Made {self.request_count} requests")
    
    def _clean_doi(self, doi: str) -> str:
        """Clean and normalize DOI."""
        if not doi:
            return ""
        # Remove common prefixes and normalize
        clean = doi.replace('https://doi.org/', '').replace('http://dx.doi.org/', '')
        clean = clean.replace('doi:', '').strip()
        return clean
    
    def _is_valid_doi(self, doi: str) -> bool:
        """Basic DOI format validation."""
        if not doi or len(doi) < 7:
            return False
        # Basic pattern: 10.xxxx/yyyy
        return doi.startswith('10.') and '/' in doi[3:]
    
    def query_by_doi(self, doi: str) -> Optional[EnrichedMetadata]:
        """Query Crossref by DOI with retry logic and comprehensive error handling."""
        if not doi or not doi.strip():
            self.logger.debug("Empty DOI provided")
            return None
        
        # Clean and validate DOI
        clean_doi = self._clean_doi(doi)
        if not self._is_valid_doi(clean_doi):
            self.logger.warning(f"Invalid DOI format: {doi}")
            return None
        
        url = f"{self.base_url}/{quote(clean_doi, safe='')}"
        
        for attempt in range(self.max_retries + 1):
            try:
                self._rate_limit()
                
                response = self.session.get(url, timeout=self.timeout)
                
                if response.status_code == 200:
                    try:
                        data = response.json()
                        metadata = self._parse_crossref_response(data.get('message', {}))
                        if metadata:
                            self.logger.debug(f"Successfully enriched DOI: {doi}")
                            return metadata
                    except json.JSONDecodeError:
                        self.logger.error(f"Invalid JSON response from Crossref for DOI: {doi}")
                        return None
                        
                elif response.status_code == 404:
                    self.logger.info(f"DOI not found in Crossref: {doi}")
                    return None
                    
                elif response.status_code == 429:
                    # Rate limited - exponential backoff
                    wait_time = (2 ** attempt) * self.backoff_factor
                    self.logger.warning(f"Rate limited by Crossref, waiting {wait_time}s (attempt {attempt + 1})")
                    time.sleep(wait_time)
                    continue
                    
                elif response.status_code >= 500:
                    # Server error - retry
                    if attempt < self.max_retries:
                        wait_time = (2 ** attempt) * self.backoff_factor
                        self.logger.warning(f"Crossref server error {response.status_code}, retrying in {wait_time}s")
                        time.sleep(wait_time)
                        continue
                    else:
                        self.logger.error(f"Crossref server error {response.status_code} for DOI: {doi} (max retries exceeded)")
                        return None
                        
                else:
                    self.logger.warning(f"Crossref API error {response.status_code} for DOI: {doi}")
                    return None
                    
            except requests.exceptions.Timeout:
                if attempt < self.max_retries:
                    wait_time = (2 ** attempt) * self.backoff_factor
                    self.logger.warning(f"Timeout querying Crossref for DOI: {doi}, retrying in {wait_time}s")
                    time.sleep(wait_time)
                    continue
                else:
                    self.logger.error(f"Timeout querying Crossref for DOI: {doi} (max retries exceeded)")
                    return None
                    
            except requests.exceptions.ConnectionError:
                if attempt < self.max_retries:
                    wait_time = (2 ** attempt) * self.backoff_factor
                    self.logger.warning(f"Connection error querying Crossref for DOI: {doi}, retrying in {wait_time}s")
                    time.sleep(wait_time)
                    continue
                else:
                    self.logger.error(f"Connection error querying Crossref for DOI: {doi} (max retries exceeded)")
                    return None
                    
            except Exception as e:
                self.logger.error(f"Unexpected error querying Crossref for DOI {doi}: {e}")
                return None
        
        return None
    
    def query_by_title(self, title: str, author: str = None) -> Optional[EnrichedMetadata]:
        """Query Crossref by title with retry logic and fuzzy matching."""
        if not title or not title.strip():
            self.logger.debug("Empty title provided")
            return None

        # Clean title for search
        clean_title = clean_title_for_search(title)
        if len(clean_title) < 10:
            self.logger.debug(f"Title too short for reliable search: {title}")
            return None

        params = {
            'query.title': clean_title,
            'rows': 10,  # Get more matches for better selection
            'select': 'DOI,title,author,published-print,published-online,abstract,subject,container-title,references-count,is-referenced-by-count,score'
        }

        if author:
            # Use first author for search
            first_author = extract_first_author(author)
            if first_author:
                params['query.author'] = first_author
        
        for attempt in range(self.max_retries + 1):
            try:
                self._rate_limit()
                
                response = self.session.get(self.base_url, params=params, timeout=self.timeout)
                
                if response.status_code == 200:
                    try:
                        data = response.json()
                        items = data.get('message', {}).get('items', [])
                        
                        if not items:
                            self.logger.info(f"No results found for title: {title[:50]}...")
                            return None
                        
                        # Find best match
                        best_match = self._find_best_title_match(title, author, items)
                        if best_match:
                            metadata = self._parse_crossref_response(best_match['item'])
                            if metadata:
                                metadata.confidence_score = best_match['confidence']
                                self.logger.debug(f"Found match for title with confidence {best_match['confidence']:.2f}")
                                return metadata
                        else:
                            self.logger.info(f"No suitable match found for title: {title[:50]}...")
                            return None
                            
                    except json.JSONDecodeError:
                        self.logger.error(f"Invalid JSON response from Crossref for title: {title[:50]}...")
                        return None
                        
                elif response.status_code == 429:
                    # Rate limited
                    wait_time = (2 ** attempt) * self.backoff_factor
                    self.logger.warning(f"Rate limited by Crossref, waiting {wait_time}s")
                    time.sleep(wait_time)
                    continue
                    
                elif response.status_code >= 500:
                    # Server error
                    if attempt < self.max_retries:
                        wait_time = (2 ** attempt) * self.backoff_factor
                        self.logger.warning(f"Crossref server error {response.status_code}, retrying in {wait_time}s")
                        time.sleep(wait_time)
                        continue
                    else:
                        self.logger.error(f"Crossref server error {response.status_code} for title search (max retries exceeded)")
                        return None
                        
                else:
                    self.logger.warning(f"Crossref API error {response.status_code} for title: {title[:50]}...")
                    return None
                    
            except requests.exceptions.Timeout:
                if attempt < self.max_retries:
                    wait_time = (2 ** attempt) * self.backoff_factor
                    self.logger.warning(f"Timeout querying Crossref for title, retrying in {wait_time}s")
                    time.sleep(wait_time)
                    continue
                else:
                    self.logger.error(f"Timeout querying Crossref for title (max retries exceeded)")
                    return None
                    
            except requests.exceptions.ConnectionError:
                if attempt < self.max_retries:
                    wait_time = (2 ** attempt) * self.backoff_factor
                    self.logger.warning(f"Connection error querying Crossref for title, retrying in {wait_time}s")
                    time.sleep(wait_time)
                    continue
                else:
                    self.logger.error(f"Connection error querying Crossref for title (max retries exceeded)")
                    return None
                    
            except Exception as e:
                self.logger.error(f"Unexpected error querying Crossref for title '{title[:50]}...': {e}")
                return None
        
        return None
    
    def _find_best_title_match(self, query_title: str, query_author: str, items: List[Dict]) -> Optional[Dict]:
        """Find the best matching item by title and author similarity."""
        best_match = None
        best_score = 0.0
        min_confidence = 0.7  # Minimum confidence threshold

        query_title_clean = clean_title_for_search(query_title)

        for item in items:
            score = 0.0

            # Title similarity (70% weight)
            if 'title' in item and item['title']:
                item_title = item['title'][0] if isinstance(item['title'], list) else str(item['title'])
                title_sim = calculate_text_similarity(query_title_clean, clean_title_for_search(item_title))
                score += title_sim * 0.7

            # Author similarity (30% weight)
            if query_author and 'author' in item and item['author']:
                author_sim = calculate_crossref_author_similarity(query_author, item['author'])
                score += author_sim * 0.3

            # Use Crossref's own score if available (small boost)
            if 'score' in item and item['score']:
                score += min(item['score'] / 100, 0.1)  # Small boost, max 0.1

            if score > best_score and score > min_confidence:
                best_score = score
                best_match = {'item': item, 'confidence': score}

        return best_match

    def _parse_crossref_response(self, item: Dict) -> EnrichedMetadata:
        """Parse Crossref API response into EnrichedMetadata."""
        metadata = EnrichedMetadata(source="crossref")

        # DOI
        if 'DOI' in item:
            metadata.doi = item['DOI']
            metadata.doi_url = f"https://doi.org/{item['DOI']}"

        # Abstract - strip JATS XML tags
        if 'abstract' in item:
            metadata.abstract = strip_jats_xml_tags(item['abstract'])
        
        # Authors
        if 'author' in item:
            authors = []
            for author in item['author']:
                if 'given' in author and 'family' in author:
                    authors.append(f"{author['given']} {author['family']}")
                elif 'family' in author:
                    authors.append(author['family'])
            metadata.authors = authors
        
        # Publication date
        for date_field in ['published-print', 'published-online']:
            if date_field in item and 'date-parts' in item[date_field]:
                date_parts = item[date_field]['date-parts'][0]
                if len(date_parts) >= 3:
                    metadata.publication_date = f"{date_parts[0]}-{date_parts[1]:02d}-{date_parts[2]:02d}"
                elif len(date_parts) >= 1:
                    metadata.publication_date = str(date_parts[0])
                break
        
        # Venue/Journal
        if 'container-title' in item and item['container-title']:
            metadata.venue = item['container-title'][0]
        
        # Citation count
        if 'is-referenced-by-count' in item:
            metadata.citation_count = item['is-referenced-by-count']
        
        # Reference count
        if 'references-count' in item:
            metadata.reference_count = item['references-count']
        
        # Subjects
        if 'subject' in item:
            metadata.subjects = item['subject']
        
        return metadata


class SemanticScholarClient:
    """Client for querying Semantic Scholar API with robust error handling and rate limiting."""
    
    def __init__(self, api_key: str = None, base_url: str = "https://api.semanticscholar.org/graph/v1",
                 rate_limit: float = 1.0, max_retries: int = 3, backoff_factor: float = 0.5,
                 timeout: int = 15):
        self.api_key = api_key
        self.base_url = base_url
        self.rate_limit = rate_limit
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.timeout = timeout
        self.logger = logging.getLogger(__name__)
        
        # Setup session with retry strategy
        self.session = requests.Session()
        retry_strategy = Retry(
            total=max_retries,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"],
            backoff_factor=backoff_factor
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        
        headers = {
            'User-Agent': 'ToRead/1.0 (https://github.com/user/toread)',
            'Accept': 'application/json'
        }
        if api_key:
            headers['x-api-key'] = api_key
        
        self.session.headers.update(headers)
        self.last_request_time = 0
        self.request_count = 0
    
    def _rate_limit(self):
        """Apply rate limiting between requests with jitter."""
        elapsed = time.time() - self.last_request_time
        if elapsed < self.rate_limit:
            jitter = random.uniform(0, 0.1)
            time.sleep(self.rate_limit - elapsed + jitter)
        self.last_request_time = time.time()
        self.request_count += 1
        
        # Log request frequency
        if self.request_count % 50 == 0:
            self.logger.info(f"Semantic Scholar: Made {self.request_count} requests")
    
    def _clean_doi(self, doi: str) -> str:
        """Clean and normalize DOI for Semantic Scholar."""
        if not doi:
            return ""
        clean = doi.replace('https://doi.org/', '').replace('http://dx.doi.org/', '')
        clean = clean.replace('doi:', '').strip()
        return clean
    
    def query_by_doi(self, doi: str) -> Optional[EnrichedMetadata]:
        """Query Semantic Scholar by DOI with retry logic and comprehensive error handling."""
        if not doi or not doi.strip():
            self.logger.debug("Empty DOI provided")
            return None
        
        # Clean DOI
        clean_doi = self._clean_doi(doi)
        if not clean_doi:
            self.logger.warning(f"Invalid DOI format: {doi}")
            return None
        
        url = f"{self.base_url}/paper/DOI:{clean_doi}"
        params = {
            'fields': 'title,authors,abstract,venue,year,citationCount,referenceCount,externalIds,url,openAccessPdf'
        }
        
        for attempt in range(self.max_retries + 1):
            try:
                self._rate_limit()
                
                response = self.session.get(url, params=params, timeout=self.timeout)
                
                if response.status_code == 200:
                    try:
                        data = response.json()
                        metadata = self._parse_semantic_scholar_response(data)
                        if metadata:
                            self.logger.debug(f"Successfully enriched DOI via Semantic Scholar: {doi}")
                            return metadata
                    except json.JSONDecodeError:
                        self.logger.error(f"Invalid JSON response from Semantic Scholar for DOI: {doi}")
                        return None
                        
                elif response.status_code == 404:
                    self.logger.info(f"DOI not found in Semantic Scholar: {doi}")
                    return None
                    
                elif response.status_code == 429:
                    # Rate limited - exponential backoff
                    wait_time = (2 ** attempt) * self.backoff_factor
                    self.logger.warning(f"Rate limited by Semantic Scholar, waiting {wait_time}s (attempt {attempt + 1})")
                    time.sleep(wait_time)
                    continue
                    
                elif response.status_code == 403:
                    # API key issues
                    self.logger.error(f"Semantic Scholar API access denied (403) - check API key")
                    return None
                    
                elif response.status_code >= 500:
                    # Server error - retry
                    if attempt < self.max_retries:
                        wait_time = (2 ** attempt) * self.backoff_factor
                        self.logger.warning(f"Semantic Scholar server error {response.status_code}, retrying in {wait_time}s")
                        time.sleep(wait_time)
                        continue
                    else:
                        self.logger.error(f"Semantic Scholar server error {response.status_code} for DOI: {doi} (max retries exceeded)")
                        return None
                        
                else:
                    self.logger.warning(f"Semantic Scholar API error {response.status_code} for DOI: {doi}")
                    return None
                    
            except requests.exceptions.Timeout:
                if attempt < self.max_retries:
                    wait_time = (2 ** attempt) * self.backoff_factor
                    self.logger.warning(f"Timeout querying Semantic Scholar for DOI: {doi}, retrying in {wait_time}s")
                    time.sleep(wait_time)
                    continue
                else:
                    self.logger.error(f"Timeout querying Semantic Scholar for DOI: {doi} (max retries exceeded)")
                    return None
                    
            except requests.exceptions.ConnectionError:
                if attempt < self.max_retries:
                    wait_time = (2 ** attempt) * self.backoff_factor
                    self.logger.warning(f"Connection error querying Semantic Scholar for DOI: {doi}, retrying in {wait_time}s")
                    time.sleep(wait_time)
                    continue
                else:
                    self.logger.error(f"Connection error querying Semantic Scholar for DOI: {doi} (max retries exceeded)")
                    return None
                    
            except Exception as e:
                self.logger.error(f"Unexpected error querying Semantic Scholar for DOI {doi}: {e}")
                return None
        
        return None
    
    def query_by_title(self, title: str, author: str = None, year: str = None) -> Optional[EnrichedMetadata]:
        """Query Semantic Scholar by title with retry logic and fuzzy matching."""
        if not title or not title.strip():
            self.logger.debug("Empty title provided")
            return None

        # Clean title for search
        clean_title = clean_title_for_search(title)
        if len(clean_title) < 10:
            self.logger.debug(f"Title too short for reliable search: {title}")
            return None

        # Build search query
        query_parts = [clean_title]
        if author:
            # Use first author name
            first_author = extract_first_author(author)
            if first_author:
                query_parts.append(first_author)

        query = ' '.join(query_parts)
        
        url = f"{self.base_url}/paper/search"
        params = {
            'query': query,
            'limit': 20,  # Get more results for better matching
            'fields': 'title,authors,abstract,venue,year,citationCount,referenceCount,externalIds,url,openAccessPdf'
        }
        
        if year:
            params['year'] = year
        
        for attempt in range(self.max_retries + 1):
            try:
                self._rate_limit()
                
                response = self.session.get(url, params=params, timeout=self.timeout)
                
                if response.status_code == 200:
                    try:
                        data = response.json()
                        papers = data.get('data', [])
                        
                        if not papers:
                            self.logger.info(f"No results found in Semantic Scholar for title: {title[:50]}...")
                            return None
                        
                        # Find best match
                        best_match = self._find_best_semantic_match(title, author, papers)
                        if best_match:
                            metadata = self._parse_semantic_scholar_response(best_match['paper'])
                            if metadata:
                                metadata.confidence_score = best_match['confidence']
                                self.logger.debug(f"Found Semantic Scholar match with confidence {best_match['confidence']:.2f}")
                                return metadata
                        else:
                            self.logger.info(f"No suitable match found in Semantic Scholar for title: {title[:50]}...")
                            return None
                            
                    except json.JSONDecodeError:
                        self.logger.error(f"Invalid JSON response from Semantic Scholar for title: {title[:50]}...")
                        return None
                        
                elif response.status_code == 429:
                    # Rate limited
                    wait_time = (2 ** attempt) * self.backoff_factor
                    self.logger.warning(f"Rate limited by Semantic Scholar, waiting {wait_time}s")
                    time.sleep(wait_time)
                    continue
                    
                elif response.status_code == 403:
                    self.logger.error("Semantic Scholar API access denied (403) - check API key")
                    return None
                    
                elif response.status_code >= 500:
                    # Server error
                    if attempt < self.max_retries:
                        wait_time = (2 ** attempt) * self.backoff_factor
                        self.logger.warning(f"Semantic Scholar server error {response.status_code}, retrying in {wait_time}s")
                        time.sleep(wait_time)
                        continue
                    else:
                        self.logger.error(f"Semantic Scholar server error {response.status_code} for title search (max retries exceeded)")
                        return None
                        
                else:
                    self.logger.warning(f"Semantic Scholar API error {response.status_code} for title: {title[:50]}...")
                    return None
                    
            except requests.exceptions.Timeout:
                if attempt < self.max_retries:
                    wait_time = (2 ** attempt) * self.backoff_factor
                    self.logger.warning(f"Timeout querying Semantic Scholar for title, retrying in {wait_time}s")
                    time.sleep(wait_time)
                    continue
                else:
                    self.logger.error(f"Timeout querying Semantic Scholar for title (max retries exceeded)")
                    return None
                    
            except requests.exceptions.ConnectionError:
                if attempt < self.max_retries:
                    wait_time = (2 ** attempt) * self.backoff_factor
                    self.logger.warning(f"Connection error querying Semantic Scholar for title, retrying in {wait_time}s")
                    time.sleep(wait_time)
                    continue
                else:
                    self.logger.error(f"Connection error querying Semantic Scholar for title (max retries exceeded)")
                    return None
                    
            except Exception as e:
                self.logger.error(f"Unexpected error querying Semantic Scholar for title '{title[:50]}...': {e}")
                return None
        
        return None
    
    def _find_best_semantic_match(self, query_title: str, query_author: str, papers: List[Dict]) -> Optional[Dict]:
        """Find the best matching paper from Semantic Scholar results."""
        best_match = None
        best_score = 0.0
        min_confidence = 0.6  # Lower threshold than Crossref since S2 search is better

        query_title_clean = clean_title_for_search(query_title)

        for paper in papers:
            score = 0.0

            # Title similarity (70% weight)
            if paper.get('title'):
                title_sim = calculate_text_similarity(query_title_clean, clean_title_for_search(paper['title']))
                score += title_sim * 0.7

            # Author similarity (30% weight)
            if query_author and paper.get('authors'):
                author_names = [author.get('name', '') for author in paper['authors']]
                author_sim = calculate_author_similarity(query_author, author_names)
                score += author_sim * 0.3

            if score > best_score and score > min_confidence:
                best_score = score
                best_match = {'paper': paper, 'confidence': score}

        return best_match

    def _parse_semantic_scholar_response(self, paper: Dict) -> EnrichedMetadata:
        """Parse Semantic Scholar API response into EnrichedMetadata."""
        metadata = EnrichedMetadata(source="semantic_scholar")
        
        # Abstract
        if paper.get('abstract'):
            metadata.abstract = paper['abstract']
        
        # Authors
        if paper.get('authors'):
            authors = [author.get('name', '') for author in paper['authors'] if author.get('name')]
            metadata.authors = authors
        
        # Publication date
        if paper.get('year'):
            metadata.publication_date = str(paper['year'])
        
        # Venue
        if paper.get('venue'):
            metadata.venue = paper['venue']
        
        # Citation count
        if paper.get('citationCount') is not None:
            metadata.citation_count = paper['citationCount']
        
        # Reference count
        if paper.get('referenceCount') is not None:
            metadata.reference_count = paper['referenceCount']
        
        # DOI from external IDs
        if paper.get('externalIds'):
            external_ids = paper['externalIds']
            if external_ids.get('DOI'):
                metadata.doi = external_ids['DOI']
                metadata.doi_url = f"https://doi.org/{external_ids['DOI']}"
            
            # ArXiv URL
            if external_ids.get('ArXiv'):
                arxiv_id = external_ids['ArXiv']
                metadata.arxiv_url = f"https://arxiv.org/abs/{arxiv_id}"
        
        # PDF URL
        if paper.get('openAccessPdf') and paper['openAccessPdf'].get('url'):
            metadata.pdf_url = paper['openAccessPdf']['url']
            metadata.is_open_access = True
        
        return metadata


class InstitutionalReportEnricher:
    """Simple enricher for institutional reports and white papers."""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        
        # Known institutional report sources
        self.known_sources = {
            'data & society': {
                'base_url': 'https://datasociety.net',
                'search_patterns': [
                    'https://datasociety.net/library/{slug}/',
                    'https://datasociety.net/wp-content/uploads/{year}/{month}/{filename}.pdf'
                ]
            }
        }
    
    def enrich_report(self, entry: BibEntry) -> Optional[EnrichedMetadata]:
        """Try to enrich institutional reports with basic web search."""
        if entry.entry_type.lower() != 'techreport':
            return None
        
        institution = entry.raw_fields.get('institution', '').lower()
        
        # Handle Data & Society reports
        if 'data' in institution and 'society' in institution:
            return self._enrich_data_society_report(entry)
        
        return None
    
    def _enrich_data_society_report(self, entry: BibEntry) -> Optional[EnrichedMetadata]:
        """Enrich Data & Society reports with known metadata."""
        if not entry.title:
            return None
        
        # Known report: Red-Teaming in the Public Interest
        if 'red-teaming' in entry.title.lower() and 'public interest' in entry.title.lower():
            metadata = EnrichedMetadata(source="datasociety")
            metadata.abstract = entry.raw_fields.get('abstract')
            metadata.publication_date = entry.raw_fields.get('year', '2025')
            metadata.pdf_url = "https://datasociety.net/wp-content/uploads/2025/02/Red-Teaming-in_the_Public_Interest_FINAL1.pdf"
            metadata.url = "https://datasociety.net/library/red-teaming-in-the-public-interest/"
            metadata.is_open_access = True
            metadata.authors = [
                "Ranjit Singh", "Borhane Blili-Hamelin", "Carol Anderson", 
                "Emnet Tafesse", "Briana Vecchione", "Beth Duckles", "Jacob Metcalf"
            ]
            metadata.venue = "Data & Society"
            return metadata
        
        return None


class ArxivClient:
    """Client for querying ArXiv API with robust error handling and rate limiting."""
    
    def __init__(self, rate_limit: float = 3.0, max_retries: int = 3, timeout: int = 10):
        self.rate_limit = rate_limit  # ArXiv recommends 3 second delays
        self.max_retries = max_retries
        self.timeout = timeout
        self.logger = logging.getLogger(__name__)
        self.client = arxiv.Client(
            page_size=100,
            delay_seconds=rate_limit,
            num_retries=max_retries
        )
        self.last_request_time = 0
        self.request_count = 0
    
    def _rate_limit(self):
        """Apply rate limiting between requests."""
        elapsed = time.time() - self.last_request_time
        if elapsed < self.rate_limit:
            time.sleep(self.rate_limit - elapsed)
        self.last_request_time = time.time()
        self.request_count += 1
        
        if self.request_count % 20 == 0:
            self.logger.info(f"ArXiv: Made {self.request_count} requests")
    
    def query_by_title(self, title: str, author: str = None) -> Optional[EnrichedMetadata]:
        """Query ArXiv by title with fuzzy matching."""
        if not title or not title.strip():
            self.logger.debug("Empty title provided")
            return None

        clean_title = clean_title_for_search(title)
        if len(clean_title) < 10:
            self.logger.debug(f"Title too short for reliable search: {title}")
            return None
        
        try:
            self._rate_limit()
            
            # Search by title
            search = arxiv.Search(
                query=f'ti:"{clean_title}"',
                max_results=10,
                sort_by=arxiv.SortCriterion.Relevance
            )
            
            results = list(self.client.results(search))
            
            if not results:
                # Try broader search without quotes
                search = arxiv.Search(
                    query=f'ti:{clean_title}',
                    max_results=10,
                    sort_by=arxiv.SortCriterion.Relevance
                )
                results = list(self.client.results(search))
            
            if not results:
                self.logger.info(f"No ArXiv results found for title: {title[:50]}...")
                return None
            
            # Find best match
            best_match = self._find_best_arxiv_match(title, author, results)
            if best_match:
                metadata = self._parse_arxiv_response(best_match['paper'])
                if metadata:
                    metadata.confidence_score = best_match['confidence']
                    self.logger.debug(f"Found ArXiv match with confidence {best_match['confidence']:.2f}")
                    return metadata
            else:
                self.logger.info(f"No suitable ArXiv match found for title: {title[:50]}...")
                return None
                
        except Exception as e:
            self.logger.error(f"Error querying ArXiv for title '{title[:50]}...': {e}")
            return None
    
    def _find_best_arxiv_match(self, query_title: str, query_author: str, papers: List) -> Optional[Dict]:
        """Find the best matching paper from ArXiv results."""
        best_match = None
        best_score = 0.0
        min_confidence = 0.6

        query_title_clean = clean_title_for_search(query_title)

        for paper in papers:
            score = 0.0

            # Title similarity (70% weight)
            if paper.title:
                title_sim = calculate_text_similarity(
                    query_title_clean,
                    clean_title_for_search(paper.title)
                )
                score += title_sim * 0.7

            # Author similarity (30% weight)
            if query_author and paper.authors:
                author_names = [author.name for author in paper.authors]
                author_sim = calculate_author_similarity(query_author, author_names)
                score += author_sim * 0.3

            if score > best_score and score > min_confidence:
                best_score = score
                best_match = {'paper': paper, 'confidence': score}

        return best_match

    def _parse_arxiv_response(self, paper) -> EnrichedMetadata:
        """Parse ArXiv paper into EnrichedMetadata."""
        metadata = EnrichedMetadata(source="arxiv")
        
        # Abstract
        if paper.summary:
            metadata.abstract = paper.summary.strip()
        
        # Authors
        if paper.authors:
            metadata.authors = [author.name for author in paper.authors]
        
        # Publication date
        if paper.published:
            metadata.publication_date = paper.published.strftime('%Y-%m-%d')
        
        # ArXiv URL
        if paper.entry_id:
            metadata.arxiv_url = paper.entry_id
        
        # PDF URL
        if paper.pdf_url:
            metadata.pdf_url = paper.pdf_url
            metadata.is_open_access = True
        
        # DOI (if available)
        if paper.doi:
            metadata.doi = paper.doi
            metadata.doi_url = f"https://doi.org/{paper.doi}"
        
        # Journal reference (if published)
        if paper.journal_ref:
            metadata.venue = paper.journal_ref
        
        # Categories as subjects
        if paper.categories:
            metadata.subjects = paper.categories

        return metadata


class OpenAlexClient:
    """Client for querying OpenAlex API with robust error handling and rate limiting."""

    def __init__(self, base_url: str = "https://api.openalex.org/works",
                 email: str = None, rate_limit: float = 0.1,
                 max_retries: int = 3, backoff_factor: float = 0.5,
                 timeout: int = 15):
        self.base_url = base_url
        self.email = email
        self.rate_limit = rate_limit  # OpenAlex allows 10 req/sec
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.timeout = timeout
        self.logger = logging.getLogger(__name__)

        # Setup session with retry strategy
        self.session = requests.Session()
        retry_strategy = Retry(
            total=max_retries,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"],
            backoff_factor=backoff_factor
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        # Set up headers with polite pool email if provided
        headers = {
            'User-Agent': f'ToRead/1.0 (https://github.com/user/toread; mailto:{email})' if email else 'ToRead/1.0',
            'Accept': 'application/json'
        }
        self.session.headers.update(headers)
        self.last_request_time = 0
        self.request_count = 0

    def _rate_limit(self):
        """Apply rate limiting between requests with jitter."""
        elapsed = time.time() - self.last_request_time
        if elapsed < self.rate_limit:
            jitter = random.uniform(0, 0.05)
            time.sleep(self.rate_limit - elapsed + jitter)
        self.last_request_time = time.time()
        self.request_count += 1

        if self.request_count % 50 == 0:
            self.logger.info(f"OpenAlex: Made {self.request_count} requests")

    def _clean_doi(self, doi: str) -> str:
        """Clean and normalize DOI."""
        if not doi:
            return ""
        clean = doi.replace('https://doi.org/', '').replace('http://dx.doi.org/', '')
        clean = clean.replace('doi:', '').strip()
        return clean

    def _reconstruct_abstract(self, inverted_index: dict) -> Optional[str]:
        """Reconstruct abstract from OpenAlex inverted index format."""
        if not inverted_index:
            return None

        # Build list of (position, word) tuples
        words = []
        for word, positions in inverted_index.items():
            for pos in positions:
                words.append((pos, word))

        # Sort by position and join
        words.sort(key=lambda x: x[0])
        return ' '.join(word for _, word in words)

    def query_by_doi(self, doi: str) -> Optional[EnrichedMetadata]:
        """Query OpenAlex by DOI."""
        if not doi or not doi.strip():
            self.logger.debug("Empty DOI provided")
            return None

        clean_doi = self._clean_doi(doi)
        if not clean_doi:
            return None

        # OpenAlex uses full DOI URL as identifier
        url = f"{self.base_url}/https://doi.org/{clean_doi}"

        for attempt in range(self.max_retries + 1):
            try:
                self._rate_limit()

                response = self.session.get(url, timeout=self.timeout)

                if response.status_code == 200:
                    try:
                        data = response.json()
                        metadata = self._parse_response(data)
                        if metadata:
                            self.logger.debug(f"Successfully enriched DOI via OpenAlex: {doi}")
                            return metadata
                    except json.JSONDecodeError:
                        self.logger.error(f"Invalid JSON response from OpenAlex for DOI: {doi}")
                        return None

                elif response.status_code == 404:
                    self.logger.info(f"DOI not found in OpenAlex: {doi}")
                    return None

                elif response.status_code == 429:
                    wait_time = (2 ** attempt) * self.backoff_factor
                    self.logger.warning(f"Rate limited by OpenAlex, waiting {wait_time}s (attempt {attempt + 1})")
                    time.sleep(wait_time)
                    continue

                elif response.status_code >= 500:
                    if attempt < self.max_retries:
                        wait_time = (2 ** attempt) * self.backoff_factor
                        self.logger.warning(f"OpenAlex server error {response.status_code}, retrying in {wait_time}s")
                        time.sleep(wait_time)
                        continue
                    else:
                        self.logger.error(f"OpenAlex server error {response.status_code} for DOI: {doi}")
                        return None

                else:
                    self.logger.warning(f"OpenAlex API error {response.status_code} for DOI: {doi}")
                    return None

            except requests.exceptions.Timeout:
                if attempt < self.max_retries:
                    wait_time = (2 ** attempt) * self.backoff_factor
                    self.logger.warning(f"Timeout querying OpenAlex for DOI: {doi}, retrying in {wait_time}s")
                    time.sleep(wait_time)
                    continue
                else:
                    self.logger.error(f"Timeout querying OpenAlex for DOI: {doi}")
                    return None

            except requests.exceptions.ConnectionError:
                if attempt < self.max_retries:
                    wait_time = (2 ** attempt) * self.backoff_factor
                    self.logger.warning(f"Connection error querying OpenAlex for DOI: {doi}, retrying in {wait_time}s")
                    time.sleep(wait_time)
                    continue
                else:
                    self.logger.error(f"Connection error querying OpenAlex for DOI: {doi}")
                    return None

            except Exception as e:
                self.logger.error(f"Unexpected error querying OpenAlex for DOI {doi}: {e}")
                return None

        return None

    def query_by_title(self, title: str, author: str = None) -> Optional[EnrichedMetadata]:
        """Query OpenAlex by title with fuzzy matching."""
        if not title or not title.strip():
            self.logger.debug("Empty title provided")
            return None

        clean_title = clean_title_for_search(title)
        if len(clean_title) < 10:
            self.logger.debug(f"Title too short for reliable search: {title}")
            return None

        # Use title.search filter for better precision
        params = {
            'filter': f'title.search:{clean_title}',
            'per_page': 10
        }

        # Add email for polite pool
        if self.email:
            params['mailto'] = self.email

        for attempt in range(self.max_retries + 1):
            try:
                self._rate_limit()

                response = self.session.get(self.base_url, params=params, timeout=self.timeout)

                if response.status_code == 200:
                    try:
                        data = response.json()
                        results = data.get('results', [])

                        if not results:
                            self.logger.info(f"No OpenAlex results for title: {title[:50]}...")
                            return None

                        best_match = self._find_best_match(title, author, results)
                        if best_match:
                            metadata = self._parse_response(best_match['work'])
                            if metadata:
                                metadata.confidence_score = best_match['confidence']
                                self.logger.debug(f"Found OpenAlex match with confidence {best_match['confidence']:.2f}")
                                return metadata
                        else:
                            self.logger.info(f"No suitable OpenAlex match for title: {title[:50]}...")
                            return None

                    except json.JSONDecodeError:
                        self.logger.error(f"Invalid JSON response from OpenAlex for title: {title[:50]}...")
                        return None

                elif response.status_code == 429:
                    wait_time = (2 ** attempt) * self.backoff_factor
                    self.logger.warning(f"Rate limited by OpenAlex, waiting {wait_time}s")
                    time.sleep(wait_time)
                    continue

                elif response.status_code >= 500:
                    if attempt < self.max_retries:
                        wait_time = (2 ** attempt) * self.backoff_factor
                        self.logger.warning(f"OpenAlex server error {response.status_code}, retrying in {wait_time}s")
                        time.sleep(wait_time)
                        continue
                    else:
                        self.logger.error(f"OpenAlex server error {response.status_code} for title search")
                        return None

                else:
                    self.logger.warning(f"OpenAlex API error {response.status_code} for title: {title[:50]}...")
                    return None

            except requests.exceptions.Timeout:
                if attempt < self.max_retries:
                    wait_time = (2 ** attempt) * self.backoff_factor
                    self.logger.warning(f"Timeout querying OpenAlex for title, retrying in {wait_time}s")
                    time.sleep(wait_time)
                    continue
                else:
                    self.logger.error(f"Timeout querying OpenAlex for title")
                    return None

            except requests.exceptions.ConnectionError:
                if attempt < self.max_retries:
                    wait_time = (2 ** attempt) * self.backoff_factor
                    self.logger.warning(f"Connection error querying OpenAlex for title, retrying in {wait_time}s")
                    time.sleep(wait_time)
                    continue
                else:
                    self.logger.error(f"Connection error querying OpenAlex for title")
                    return None

            except Exception as e:
                self.logger.error(f"Unexpected error querying OpenAlex for title '{title[:50]}...': {e}")
                return None

        return None

    def _find_best_match(self, query_title: str, query_author: str, works: List[Dict]) -> Optional[Dict]:
        """Find the best matching work by title and author similarity."""
        best_match = None
        best_score = 0.0
        min_confidence = 0.7

        query_title_clean = clean_title_for_search(query_title)

        for work in works:
            score = 0.0

            # Title similarity (70% weight)
            work_title = work.get('title') or work.get('display_name', '')
            if work_title:
                title_sim = calculate_text_similarity(query_title_clean, clean_title_for_search(work_title))
                score += title_sim * 0.7

            # Author similarity (30% weight)
            if query_author and work.get('authorships'):
                author_names = [
                    authorship.get('author', {}).get('display_name', '')
                    for authorship in work['authorships']
                    if authorship.get('author')
                ]
                if author_names:
                    author_sim = calculate_author_similarity(query_author, author_names)
                    score += author_sim * 0.3

            if score > best_score and score >= min_confidence:
                best_score = score
                best_match = {'work': work, 'confidence': score}

        return best_match

    def _parse_response(self, work: Dict) -> EnrichedMetadata:
        """Parse OpenAlex work response into EnrichedMetadata."""
        metadata = EnrichedMetadata(source="openalex")

        # DOI
        if work.get('doi'):
            doi = work['doi'].replace('https://doi.org/', '')
            metadata.doi = doi
            metadata.doi_url = work['doi']

        # Abstract from inverted index
        if work.get('abstract_inverted_index'):
            metadata.abstract = self._reconstruct_abstract(work['abstract_inverted_index'])

        # Authors
        if work.get('authorships'):
            authors = []
            for authorship in work['authorships'][:20]:  # Limit to first 20 authors
                author = authorship.get('author', {})
                if author.get('display_name'):
                    authors.append(author['display_name'])
            metadata.authors = authors

        # Publication date
        if work.get('publication_date'):
            metadata.publication_date = work['publication_date']

        # Citation count
        if work.get('cited_by_count') is not None:
            metadata.citation_count = work['cited_by_count']

        # Venue from primary location
        primary_location = work.get('primary_location') or {}
        source = primary_location.get('source') or {}
        if source.get('display_name'):
            metadata.venue = source['display_name']

        # Open access info
        open_access = work.get('open_access') or {}
        if open_access.get('is_oa') is not None:
            metadata.is_open_access = open_access['is_oa']

        # PDF URL - try multiple locations
        if open_access.get('oa_url'):
            metadata.pdf_url = open_access['oa_url']
        elif work.get('best_oa_location', {}).get('pdf_url'):
            metadata.pdf_url = work['best_oa_location']['pdf_url']
        elif primary_location.get('pdf_url'):
            metadata.pdf_url = primary_location['pdf_url']

        # Landing page URL
        if primary_location.get('landing_page_url'):
            metadata.url = primary_location['landing_page_url']

        # Subjects/concepts
        if work.get('concepts'):
            subjects = [
                concept.get('display_name')
                for concept in work['concepts'][:5]  # Top 5 concepts
                if concept.get('display_name') and concept.get('score', 0) > 0.3
            ]
            metadata.subjects = subjects

        return metadata


class MetadataEnricher:
    """Main enricher that coordinates multiple API clients."""

    def __init__(self, crossref_config: Dict = None, semantic_scholar_config: Dict = None,
                 arxiv_config: Dict = None, openalex_config: Dict = None, cache_config: Dict = None):
        self.logger = logging.getLogger(__name__)

        # Initialize cache
        if cache_config:
            self.cache = MetadataCache(
                cache_file=cache_config.get('cache_file', 'cache/metadata_cache.json'),
                cache_duration_days=cache_config.get('cache_duration_days', 30)
            )
        else:
            self.cache = MetadataCache()

        # Initialize clients
        self.crossref_client = None
        self.semantic_scholar_client = None
        self.arxiv_client = None
        self.openalex_client = None
        self.institutional_enricher = InstitutionalReportEnricher()

        # Circuit breaker for failed APIs (avoid repeated failures)
        self.api_failure_counts = {
            'crossref': 0,
            'semantic_scholar': 0,
            'arxiv': 0,
            'openalex': 0
        }
        self.max_consecutive_failures = 5

        if crossref_config and crossref_config.get('enabled', True):
            self.crossref_client = CrossrefClient(
                base_url=crossref_config.get('base_url', 'https://api.crossref.org/works'),
                user_agent=crossref_config.get('user_agent', 'ToRead/1.0'),
                rate_limit=crossref_config.get('rate_limit', 1.0),
                timeout=crossref_config.get('timeout', 15)
            )

        if semantic_scholar_config and semantic_scholar_config.get('enabled', True):
            self.semantic_scholar_client = SemanticScholarClient(
                api_key=semantic_scholar_config.get('api_key'),
                base_url=semantic_scholar_config.get('base_url', 'https://api.semanticscholar.org/graph/v1'),
                rate_limit=semantic_scholar_config.get('rate_limit', 1.0),
                timeout=semantic_scholar_config.get('timeout', 15)
            )

        if arxiv_config and arxiv_config.get('enabled', True):
            self.arxiv_client = ArxivClient(
                rate_limit=arxiv_config.get('rate_limit', 3.0),
                timeout=arxiv_config.get('timeout', 15)
            )

        if openalex_config and openalex_config.get('enabled', True):
            self.openalex_client = OpenAlexClient(
                base_url=openalex_config.get('base_url', 'https://api.openalex.org/works'),
                email=openalex_config.get('email'),
                rate_limit=openalex_config.get('rate_limit', 0.1),
                timeout=openalex_config.get('timeout', 15)
            )

    def _is_arxiv_paper(self, entry: BibEntry) -> bool:
        """Check if this entry is from ArXiv."""
        # Check journal field
        if entry.journal and 'arxiv' in entry.journal.lower():
            return True
        
        # Check if there's an ArXiv ID in standard fields
        for field_value in [entry.doi, entry.url]:
            if field_value and ('arxiv' in field_value.lower() or field_value.startswith('arXiv:')):
                return True
        
        # Check raw_fields for ArXiv-specific fields
        arxiv_fields = ['archiveprefix', 'eprint', 'primaryclass']
        for field_name, field_value in entry.raw_fields.items():
            if field_name.lower() in arxiv_fields:
                if 'arxiv' in str(field_value).lower():
                    return True
            # Check eprint field for ArXiv ID pattern
            if field_name.lower() == 'eprint' and field_value:
                # ArXiv IDs typically look like: 2501.00123 or cs.CV/0501001
                if re.match(r'^\d{4}\.\d{4,5}$', field_value) or re.match(r'^[a-z-]+/\d{7}$', field_value):
                    return True
        
        return False
    
    def enrich_entry(self, entry: BibEntry) -> Optional[EnrichedMetadata]:
        """Enrich a single bibliographic entry."""
        metadata = None

        # Check if this is an institutional report first
        if entry.entry_type.lower() == 'techreport':
            metadata = self.institutional_enricher.enrich_report(entry)
            if metadata:
                return metadata

        # Check if this is an ArXiv paper
        is_arxiv = self._is_arxiv_paper(entry)

        if is_arxiv and self.arxiv_client:
            # For ArXiv papers, try ArXiv API first
            author = ', '.join(entry.authors) if entry.authors else None
            metadata = self.arxiv_client.query_by_title(entry.title, author)
            if metadata:
                return metadata

        # Try DOI-based enrichment (most reliable)
        if entry.doi:
            metadata = self._enrich_by_doi(entry.doi)

        # If no DOI or DOI lookup failed, try title-based enrichment
        if not metadata and entry.title and is_valid_title(entry.title):
            author = ', '.join(entry.authors) if entry.authors else None
            metadata = self._enrich_by_title(entry.title, author, entry.year)

        # Last resort: try to extract title from URL and search
        if not metadata and entry.url:
            metadata = self._enrich_by_url(entry)

        return metadata

    def _enrich_by_url(self, entry: BibEntry) -> Optional[EnrichedMetadata]:
        """Try to enrich using URL-extracted information."""
        if not entry.url:
            return None

        url = clean_url(entry.url)

        # Try to extract title from URL
        url_title = extract_title_from_url(url)
        if url_title and len(url_title) >= 10:
            self.logger.debug(f"Trying URL-extracted title: {url_title}")
            author = ', '.join(entry.authors) if entry.authors else None
            metadata = self._enrich_by_title(url_title, author, entry.year)
            if metadata:
                return metadata

        # Create minimal metadata from URL if nothing else works
        # This provides at least a valid URL link
        metadata = EnrichedMetadata(source="url")
        metadata.url = url

        # Try to determine if it's open access based on URL domain
        open_access_domains = ['arxiv.org', 'biorxiv.org', 'medrxiv.org', 'osf.io',
                              'zenodo.org', 'ssrn.com', 'researchgate.net']
        for domain in open_access_domains:
            if domain in url.lower():
                metadata.is_open_access = True
                break

        return metadata
    
    def enrich_entries(self, entries: List[BibEntry]) -> Dict[str, Optional[EnrichedMetadata]]:
        """Enrich multiple entries using cache to avoid redundant API calls."""
        # Clean up expired cache entries
        self.cache.cleanup_expired()
        
        # Get cache statistics
        cache_stats = self.cache.get_cache_stats()
        self.logger.info(f"Cache stats: {cache_stats['valid_entries']} valid, {cache_stats['expired_entries']} expired entries")
        
        # Start with all cached metadata and convert back to EnrichedMetadata objects
        cached_dicts = self.cache.get_all_cached_metadata(entries)
        enriched = {}
        for key, metadata_dict in cached_dicts.items():
            try:
                enriched[key] = EnrichedMetadata(**metadata_dict)
            except Exception as e:
                self.logger.warning(f"Failed to convert cached metadata for {key}: {e}")
        
        cached_count = len(enriched)
        
        # Get entries that need enrichment (includes weekly retry logic for failures)
        retriable_entries = self.cache.get_retriable_entries(entries)
        
        self.logger.info(f"Processing {len(entries)} entries: {cached_count} cached, {len(retriable_entries)} need enrichment")
        
        # Enrich only retriable entries
        for entry in retriable_entries:
            try:
                metadata = self.enrich_entry(entry)
                enriched[entry.key] = metadata
                
                if metadata:
                    # Store in cache for future use
                    self.cache.store_metadata(entry, metadata)
                    self.logger.info(f"Successfully enriched and cached entry: {entry.key} (source: {metadata.source})")
                else:
                    # Store failure in cache to avoid retrying for a week
                    error_details = []
                    
                    # Check what fields are available for enrichment
                    if entry.doi:
                        error_details.append(f"DOI: {entry.doi}")
                    else:
                        error_details.append("No DOI available")
                    
                    if entry.title:
                        error_details.append(f"Title length: {len(entry.title)} chars")
                    else:
                        error_details.append("No title available")
                    
                    if entry.authors:
                        error_details.append(f"Authors: {', '.join(entry.authors[:2])}{'...' if len(entry.authors) > 2 else ''}")
                    else:
                        error_details.append("No authors available")
                    
                    # Check which APIs are enabled
                    api_status = []
                    if self.crossref_client:
                        api_status.append("Crossref enabled")
                    if self.semantic_scholar_client:
                        api_status.append("Semantic Scholar enabled")
                    if self.arxiv_client:
                        api_status.append("ArXiv enabled")
                    
                    error_msg = f"Could not enrich entry: {entry.key} | {' | '.join(error_details)} | APIs: {', '.join(api_status) if api_status else 'None enabled'}"
                    self.logger.warning(error_msg)
                    
                    # Store failure in cache to avoid retrying for a week
                    self.cache.store_failure(entry, error_msg)
                    
            except Exception as e:
                error_msg = f"Error enriching entry {entry.key}: {e}"
                self.logger.error(error_msg)
                enriched[entry.key] = None
                
                # Store failure in cache to avoid retrying for a week
                self.cache.store_failure(entry, error_msg)
        
        # Save cache to disk
        self.cache.save_cache()
        
        return enriched
    
    def _enrich_by_doi(self, doi: str) -> Optional[EnrichedMetadata]:
        """Try to enrich using DOI with multiple APIs."""
        # Try Crossref first (more reliable and comprehensive)
        if self.crossref_client and self.api_failure_counts['crossref'] < self.max_consecutive_failures:
            try:
                metadata = self.crossref_client.query_by_doi(doi)
                if metadata:
                    self.api_failure_counts['crossref'] = 0  # Reset on success
                    return metadata
                else:
                    self.api_failure_counts['crossref'] += 1
            except Exception as e:
                self.api_failure_counts['crossref'] += 1
                self.logger.debug(f"Crossref DOI query failed: {e}")

        # Try OpenAlex (good coverage, open access info)
        if self.openalex_client and self.api_failure_counts['openalex'] < self.max_consecutive_failures:
            try:
                metadata = self.openalex_client.query_by_doi(doi)
                if metadata:
                    self.api_failure_counts['openalex'] = 0  # Reset on success
                    return metadata
                else:
                    self.api_failure_counts['openalex'] += 1
            except Exception as e:
                self.api_failure_counts['openalex'] += 1
                self.logger.debug(f"OpenAlex DOI query failed: {e}")

        # Fall back to Semantic Scholar
        if self.semantic_scholar_client and self.api_failure_counts['semantic_scholar'] < self.max_consecutive_failures:
            try:
                metadata = self.semantic_scholar_client.query_by_doi(doi)
                if metadata:
                    self.api_failure_counts['semantic_scholar'] = 0  # Reset on success
                    return metadata
                else:
                    self.api_failure_counts['semantic_scholar'] += 1
            except Exception as e:
                self.api_failure_counts['semantic_scholar'] += 1
                self.logger.debug(f"Semantic Scholar DOI query failed: {e}")

        return None
    
    def _enrich_by_title(self, title: str, author: str = None, year: str = None) -> Optional[EnrichedMetadata]:
        """Try to enrich using title with multiple APIs."""
        # Try Crossref first (more reliable for title matching)
        if self.crossref_client and self.api_failure_counts['crossref'] < self.max_consecutive_failures:
            try:
                metadata = self.crossref_client.query_by_title(title, author)
                if metadata:
                    if metadata.confidence_score and metadata.confidence_score > 0.75:  # Lowered threshold slightly
                        self.logger.debug(f"Crossref title match found with confidence {metadata.confidence_score:.3f}")
                        self.api_failure_counts['crossref'] = 0  # Reset on success
                        return metadata
                    elif metadata.confidence_score:
                        self.logger.debug(f"Crossref title match rejected - low confidence {metadata.confidence_score:.3f} (threshold: 0.75)")
                    else:
                        self.logger.debug("Crossref title match rejected - no confidence score")
                else:
                    self.logger.debug("No Crossref title match found")
            except Exception as e:
                self.api_failure_counts['crossref'] += 1
                self.logger.debug(f"Crossref title query failed: {e}")

        # Try OpenAlex (good coverage, open access info)
        if self.openalex_client and self.api_failure_counts['openalex'] < self.max_consecutive_failures:
            try:
                metadata = self.openalex_client.query_by_title(title, author)
                if metadata:
                    if metadata.confidence_score and metadata.confidence_score > 0.7:
                        self.logger.debug(f"OpenAlex title match found with confidence {metadata.confidence_score:.3f}")
                        self.api_failure_counts['openalex'] = 0  # Reset on success
                        return metadata
                    elif metadata.confidence_score:
                        self.logger.debug(f"OpenAlex title match rejected - low confidence {metadata.confidence_score:.3f} (threshold: 0.7)")
                    else:
                        self.logger.debug("OpenAlex title match rejected - no confidence score")
                else:
                    self.logger.debug("No OpenAlex title match found")
            except Exception as e:
                self.api_failure_counts['openalex'] += 1
                self.logger.debug(f"OpenAlex title query failed: {e}")

        # Try Semantic Scholar (better search for newer papers)
        if self.semantic_scholar_client and self.api_failure_counts['semantic_scholar'] < self.max_consecutive_failures:
            try:
                metadata = self.semantic_scholar_client.query_by_title(title, author, year)
                if metadata:
                    if metadata.confidence_score and metadata.confidence_score > 0.7:  # Lower threshold for S2
                        self.logger.debug(f"Semantic Scholar title match found with confidence {metadata.confidence_score:.3f}")
                        self.api_failure_counts['semantic_scholar'] = 0  # Reset on success
                        return metadata
                    elif metadata.confidence_score:
                        self.logger.debug(f"Semantic Scholar title match rejected - low confidence {metadata.confidence_score:.3f} (threshold: 0.7)")
                    else:
                        self.logger.debug("Semantic Scholar title match rejected - no confidence score")
                else:
                    self.logger.debug("No Semantic Scholar title match found")
            except Exception as e:
                self.api_failure_counts['semantic_scholar'] += 1
                self.logger.debug(f"Semantic Scholar title query failed: {e}")

        return None
    
