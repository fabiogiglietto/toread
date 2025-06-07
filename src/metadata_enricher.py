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
                 timeout: int = 10):
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
    
    def _clean_title_for_search(self, title: str) -> str:
        """Clean title for better search results."""
        import re
        # Remove LaTeX commands and excessive punctuation
        clean = re.sub(r'\\[a-zA-Z]+\{([^}]*)\}', r'\1', title)
        clean = re.sub(r'[{}]', '', clean)
        clean = re.sub(r'[^\w\s\-:]', ' ', clean)
        clean = re.sub(r'\s+', ' ', clean).strip()
        return clean
    
    def _extract_first_author(self, author_str: str) -> str:
        """Extract first author name for search."""
        if not author_str:
            return ""
        # Split by 'and' and take first
        first = author_str.split(' and ')[0].split(',')[0].strip()
        return first
    
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
        clean_title = self._clean_title_for_search(title)
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
            first_author = self._extract_first_author(author)
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
        
        query_title_clean = self._clean_title_for_search(query_title)
        
        for item in items:
            score = 0.0
            
            # Title similarity (70% weight)
            if 'title' in item and item['title']:
                item_title = item['title'][0] if isinstance(item['title'], list) else str(item['title'])
                title_sim = self._calculate_text_similarity(query_title_clean, self._clean_title_for_search(item_title))
                score += title_sim * 0.7
            
            # Author similarity (30% weight)
            if query_author and 'author' in item and item['author']:
                author_sim = self._calculate_author_similarity(query_author, item['author'])
                score += author_sim * 0.3
            
            # Use Crossref's own score if available (small boost)
            if 'score' in item and item['score']:
                score += min(item['score'] / 100, 0.1)  # Small boost, max 0.1
            
            if score > best_score and score > min_confidence:
                best_score = score
                best_match = {'item': item, 'confidence': score}
        
        return best_match
    
    def _calculate_text_similarity(self, text1: str, text2: str) -> float:
        """Calculate similarity between two texts using word overlap."""
        if not text1 or not text2:
            return 0.0
            
        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())
        
        if not words1 or not words2:
            return 0.0
        
        # Remove very common words that don't help with matching
        stop_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by'}
        words1 = words1 - stop_words
        words2 = words2 - stop_words
        
        if not words1 or not words2:
            return 0.0
        
        intersection = words1.intersection(words2)
        union = words1.union(words2)
        
        # Jaccard similarity
        jaccard = len(intersection) / len(union) if union else 0.0
        
        # Add bonus for exact substring matches
        text1_lower = text1.lower()
        text2_lower = text2.lower()
        if text1_lower in text2_lower or text2_lower in text1_lower:
            jaccard += 0.2
        
        return min(jaccard, 1.0)
    
    def _calculate_author_similarity(self, query_author: str, crossref_authors: List[Dict]) -> float:
        """Calculate similarity between query author and Crossref authors."""
        if not query_author or not crossref_authors:
            return 0.0
        
        # Extract author names from Crossref format
        crossref_names = []
        for author in crossref_authors:
            if 'given' in author and 'family' in author:
                crossref_names.append(f"{author['given']} {author['family']}")
            elif 'family' in author:
                crossref_names.append(author['family'])
        
        if not crossref_names:
            return 0.0
        
        # Parse query authors (handle "and" separated list)
        query_authors = [name.strip() for name in query_author.split(' and ')]
        
        max_similarity = 0.0
        for q_author in query_authors:
            for c_author in crossref_names:
                similarity = self._calculate_text_similarity(q_author, c_author)
                max_similarity = max(max_similarity, similarity)
        
        return max_similarity
    
    def _parse_crossref_response(self, item: Dict) -> EnrichedMetadata:
        """Parse Crossref API response into EnrichedMetadata."""
        metadata = EnrichedMetadata(source="crossref")
        
        # DOI
        if 'DOI' in item:
            metadata.doi = item['DOI']
            metadata.doi_url = f"https://doi.org/{item['DOI']}"
        
        # Abstract
        if 'abstract' in item:
            metadata.abstract = item['abstract']
        
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
                 timeout: int = 10):
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
        clean_title = self._clean_title_for_search(title)
        if len(clean_title) < 10:
            self.logger.debug(f"Title too short for reliable search: {title}")
            return None
        
        # Build search query
        query_parts = [clean_title]
        if author:
            # Use first author name
            first_author = author.split(',')[0].strip() if ',' in author else author.split(' and ')[0].strip()
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
    
    def _clean_title_for_search(self, title: str) -> str:
        """Clean title for better search results."""
        import re
        # Remove LaTeX commands and excessive punctuation
        clean = re.sub(r'\\[a-zA-Z]+\{([^}]*)\}', r'\1', title)
        clean = re.sub(r'[{}]', '', clean)
        clean = re.sub(r'[^\w\s\-:]', ' ', clean)
        clean = re.sub(r'\s+', ' ', clean).strip()
        return clean
    
    def _find_best_semantic_match(self, query_title: str, query_author: str, papers: List[Dict]) -> Optional[Dict]:
        """Find the best matching paper from Semantic Scholar results."""
        best_match = None
        best_score = 0.0
        min_confidence = 0.6  # Lower threshold than Crossref since S2 search is better
        
        query_title_clean = self._clean_title_for_search(query_title)
        
        for paper in papers:
            score = 0.0
            
            # Title similarity (70% weight)
            if paper.get('title'):
                title_sim = self._calculate_text_similarity(query_title_clean, self._clean_title_for_search(paper['title']))
                score += title_sim * 0.7
            
            # Author similarity (30% weight)
            if query_author and paper.get('authors'):
                author_names = [author.get('name', '') for author in paper['authors']]
                author_sim = self._calculate_author_similarity(query_author, author_names)
                score += author_sim * 0.3
            
            if score > best_score and score > min_confidence:
                best_score = score
                best_match = {'paper': paper, 'confidence': score}
        
        return best_match
    
    def _calculate_text_similarity(self, text1: str, text2: str) -> float:
        """Calculate similarity between two texts using word overlap."""
        if not text1 or not text2:
            return 0.0
            
        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())
        
        if not words1 or not words2:
            return 0.0
        
        # Remove very common words
        stop_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by'}
        words1 = words1 - stop_words
        words2 = words2 - stop_words
        
        if not words1 or not words2:
            return 0.0
        
        intersection = words1.intersection(words2)
        union = words1.union(words2)
        
        # Jaccard similarity with substring bonus
        jaccard = len(intersection) / len(union) if union else 0.0
        
        # Bonus for substring matches
        text1_lower = text1.lower()
        text2_lower = text2.lower()
        if text1_lower in text2_lower or text2_lower in text1_lower:
            jaccard += 0.2
        
        return min(jaccard, 1.0)
    
    def _calculate_author_similarity(self, query_author: str, paper_authors: List[str]) -> float:
        """Calculate similarity between query author and paper authors."""
        if not query_author or not paper_authors:
            return 0.0
        
        # Parse query authors
        query_authors = [name.strip() for name in query_author.split(' and ')]
        
        max_similarity = 0.0
        for q_author in query_authors:
            for p_author in paper_authors:
                similarity = self._calculate_text_similarity(q_author, p_author)
                max_similarity = max(max_similarity, similarity)
        
        return max_similarity
    
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
    
    def _clean_title_for_search(self, title: str) -> str:
        """Clean title for ArXiv search."""
        import re
        # Remove LaTeX commands and excessive punctuation
        clean = re.sub(r'\\[a-zA-Z]+\{([^}]*)\}', r'\1', title)
        clean = re.sub(r'[{}]', '', clean)
        clean = re.sub(r'[^\w\s\-:]', ' ', clean)
        clean = re.sub(r'\s+', ' ', clean).strip()
        return clean
    
    def query_by_title(self, title: str, author: str = None) -> Optional[EnrichedMetadata]:
        """Query ArXiv by title with fuzzy matching."""
        if not title or not title.strip():
            self.logger.debug("Empty title provided")
            return None
        
        clean_title = self._clean_title_for_search(title)
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
        
        query_title_clean = self._clean_title_for_search(query_title)
        
        for paper in papers:
            score = 0.0
            
            # Title similarity (70% weight)
            if paper.title:
                title_sim = self._calculate_text_similarity(
                    query_title_clean, 
                    self._clean_title_for_search(paper.title)
                )
                score += title_sim * 0.7
            
            # Author similarity (30% weight)
            if query_author and paper.authors:
                author_names = [author.name for author in paper.authors]
                author_sim = self._calculate_author_similarity(query_author, author_names)
                score += author_sim * 0.3
            
            if score > best_score and score > min_confidence:
                best_score = score
                best_match = {'paper': paper, 'confidence': score}
        
        return best_match
    
    def _calculate_text_similarity(self, text1: str, text2: str) -> float:
        """Calculate similarity between two texts using word overlap."""
        if not text1 or not text2:
            return 0.0
            
        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())
        
        if not words1 or not words2:
            return 0.0
        
        # Remove common words
        stop_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by'}
        words1 = words1 - stop_words
        words2 = words2 - stop_words
        
        if not words1 or not words2:
            return 0.0
        
        intersection = words1.intersection(words2)
        union = words1.union(words2)
        
        # Jaccard similarity with substring bonus
        jaccard = len(intersection) / len(union) if union else 0.0
        
        # Bonus for substring matches
        text1_lower = text1.lower()
        text2_lower = text2.lower()
        if text1_lower in text2_lower or text2_lower in text1_lower:
            jaccard += 0.2
        
        return min(jaccard, 1.0)
    
    def _calculate_author_similarity(self, query_author: str, paper_authors: List[str]) -> float:
        """Calculate similarity between query author and paper authors."""
        if not query_author or not paper_authors:
            return 0.0
        
        # Parse query authors
        query_authors = [name.strip() for name in query_author.split(' and ')]
        
        max_similarity = 0.0
        for q_author in query_authors:
            for p_author in paper_authors:
                similarity = self._calculate_text_similarity(q_author, p_author)
                max_similarity = max(max_similarity, similarity)
        
        return max_similarity
    
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
    
    def _find_best_match(self, query_title: str, query_author: str, papers: List[Dict]) -> Optional[Dict]:
        """Find the best matching paper."""
        best_match = None
        best_score = 0.0
        
        query_title_clean = self._clean_text(query_title)
        
        for paper in papers:
            score = 0.0
            
            # Title similarity (70% weight)
            if paper.get('title'):
                title_sim = self._calculate_text_similarity(query_title_clean, self._clean_text(paper['title']))
                score += title_sim * 0.7
            
            # Author similarity (30% weight)
            if query_author and paper.get('authors'):
                author_names = [author.get('name', '') for author in paper['authors']]
                author_sim = self._calculate_author_similarity(query_author, author_names)
                score += author_sim * 0.3
            
            if score > best_score and score > 0.6:  # Minimum threshold
                best_score = score
                best_match = paper
        
        return best_match
    
    def _calculate_match_confidence(self, query_title: str, query_author: str, paper: Dict) -> float:
        """Calculate confidence score for a match."""
        score = 0.0
        
        if paper.get('title'):
            title_sim = self._calculate_text_similarity(self._clean_text(query_title), self._clean_text(paper['title']))
            score += title_sim * 0.7
        
        if query_author and paper.get('authors'):
            author_names = [author.get('name', '') for author in paper['authors']]
            author_sim = self._calculate_author_similarity(query_author, author_names)
            score += author_sim * 0.3
        
        return min(score, 1.0)
    
    def _calculate_author_similarity(self, query_author: str, paper_authors: List[str]) -> float:
        """Calculate similarity between query author and paper authors."""
        if not query_author or not paper_authors:
            return 0.0
        
        query_authors = [self._clean_text(name) for name in query_author.split(' and ')]
        paper_authors_clean = [self._clean_text(name) for name in paper_authors]
        
        max_sim = 0.0
        for q_author in query_authors:
            for p_author in paper_authors_clean:
                sim = self._calculate_text_similarity(q_author, p_author)
                max_sim = max(max_sim, sim)
        
        return max_sim
    
    def _clean_text(self, text: str) -> str:
        """Clean text for comparison."""
        import re
        text = text.lower().strip()
        text = re.sub(r'[^\w\s]', ' ', text)
        text = re.sub(r'\s+', ' ', text)
        return text
    
    def _calculate_text_similarity(self, text1: str, text2: str) -> float:
        """Calculate similarity between two texts using word overlap."""
        words1 = set(text1.split())
        words2 = set(text2.split())
        
        if not words1 or not words2:
            return 0.0
        
        intersection = words1.intersection(words2)
        union = words1.union(words2)
        
        return len(intersection) / len(union) if union else 0.0
    
class MetadataEnricher:
    """Main enricher that coordinates multiple API clients."""
    
    def __init__(self, crossref_config: Dict = None, semantic_scholar_config: Dict = None, arxiv_config: Dict = None):
        self.logger = logging.getLogger(__name__)
        
        # Initialize clients
        self.crossref_client = None
        self.semantic_scholar_client = None
        self.arxiv_client = None
        self.institutional_enricher = InstitutionalReportEnricher()
        
        if crossref_config and crossref_config.get('enabled', True):
            self.crossref_client = CrossrefClient(
                base_url=crossref_config.get('base_url', 'https://api.crossref.org/works'),
                user_agent=crossref_config.get('user_agent', 'ToRead/1.0'),
                rate_limit=crossref_config.get('rate_limit', 1.0)
            )
        
        if semantic_scholar_config and semantic_scholar_config.get('enabled', True):
            self.semantic_scholar_client = SemanticScholarClient(
                api_key=semantic_scholar_config.get('api_key'),
                base_url=semantic_scholar_config.get('base_url', 'https://api.semanticscholar.org/graph/v1'),
                rate_limit=semantic_scholar_config.get('rate_limit', 1.0)
            )
        
        if arxiv_config and arxiv_config.get('enabled', True):
            self.arxiv_client = ArxivClient(
                rate_limit=arxiv_config.get('rate_limit', 3.0)
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
        if not metadata and entry.title:
            author = ', '.join(entry.authors) if entry.authors else None
            metadata = self._enrich_by_title(entry.title, author, entry.year)
        
        return metadata
    
    def enrich_entries(self, entries: List[BibEntry]) -> Dict[str, Optional[EnrichedMetadata]]:
        """Enrich multiple entries."""
        enriched = {}
        
        for entry in entries:
            try:
                metadata = self.enrich_entry(entry)
                enriched[entry.key] = metadata
                
                if metadata:
                    self.logger.info(f"Successfully enriched entry: {entry.key} (source: {metadata.source})")
                else:
                    self.logger.warning(f"Could not enrich entry: {entry.key}")
                    
            except Exception as e:
                self.logger.error(f"Error enriching entry {entry.key}: {e}")
                enriched[entry.key] = None
        
        return enriched
    
    def _enrich_by_doi(self, doi: str) -> Optional[EnrichedMetadata]:
        """Try to enrich using DOI with multiple APIs."""
        # Try Semantic Scholar first (often has better abstracts)
        if self.semantic_scholar_client:
            metadata = self.semantic_scholar_client.query_by_doi(doi)
            if metadata:
                return metadata
        
        # Fall back to Crossref
        if self.crossref_client:
            metadata = self.crossref_client.query_by_doi(doi)
            if metadata:
                return metadata
        
        return None
    
    def _enrich_by_title(self, title: str, author: str = None, year: str = None) -> Optional[EnrichedMetadata]:
        """Try to enrich using title with multiple APIs."""
        # Try Semantic Scholar first (better search)
        if self.semantic_scholar_client:
            metadata = self.semantic_scholar_client.query_by_title(title, author, year)
            if metadata and metadata.confidence_score and metadata.confidence_score > 0.8:
                return metadata
        
        # Try Crossref
        if self.crossref_client:
            metadata = self.crossref_client.query_by_title(title, author)
            if metadata and metadata.confidence_score and metadata.confidence_score > 0.8:
                return metadata
        
        return None
    
