"""BibTeX parser module for converting Paperpile exports to structured data."""

from __future__ import annotations

import re
import logging
from typing import Dict, List, Optional, TYPE_CHECKING
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone

if TYPE_CHECKING:
    from .cache import DiscoveryCache


@dataclass
class BibEntry:
    """Represents a single bibliographic entry with structured fields."""
    entry_type: str
    key: str
    title: Optional[str] = None
    authors: List[str] = field(default_factory=list)
    year: Optional[str] = None
    month: Optional[str] = None
    doi: Optional[str] = None
    url: Optional[str] = None
    abstract: Optional[str] = None
    keywords: List[str] = field(default_factory=list)
    journal: Optional[str] = None
    volume: Optional[str] = None
    pages: Optional[str] = None
    publisher: Optional[str] = None
    discovery_date: Optional[datetime] = None
    raw_fields: Dict[str, str] = field(default_factory=dict)


class BibTeXParser:
    """Enhanced parser for BibTeX files with graceful error handling."""
    
    def __init__(self, encoding: str = 'utf-8'):
        self.encoding = encoding
        self.entries: List[BibEntry] = []
        self.logger = logging.getLogger(__name__)
        
        # Common field mappings for different BibTeX styles
        self.field_mappings = {
            'title': ['title', 'booktitle'],
            'authors': ['author', 'authors'],
            'year': ['year', 'date'],
            'month': ['month'],
            'doi': ['doi'],
            'url': ['url', 'link', 'howpublished'],
            'abstract': ['abstract', 'summary'],
            'keywords': ['keywords', 'keyword'],
            'journal': ['journal', 'journaltitle'],
            'volume': ['volume'],
            'pages': ['pages', 'page'],
            'publisher': ['publisher']
        }
    
    def parse_file(self, filepath: str) -> List[BibEntry]:
        """Parse a BibTeX file and return list of entries with error handling."""
        file_path = Path(filepath)
        
        if not file_path.exists():
            self.logger.error(f"BibTeX file not found: {filepath}")
            return []
        
        try:
            with open(file_path, 'r', encoding=self.encoding) as file:
                content = file.read()
            
            self.logger.info(f"Successfully read BibTeX file: {filepath}")
            return self.parse_string(content)
            
        except UnicodeDecodeError:
            self.logger.warning(f"UTF-8 decode failed, trying latin-1 encoding for: {filepath}")
            try:
                with open(file_path, 'r', encoding='latin-1') as file:
                    content = file.read()
                return self.parse_string(content)
            except Exception as e:
                self.logger.error(f"Failed to read file with latin-1 encoding: {e}")
                return []
        except Exception as e:
            self.logger.error(f"Error reading BibTeX file {filepath}: {e}")
            return []
    
    def parse_string(self, content: str) -> List[BibEntry]:
        """Parse BibTeX content string and return list of structured entries."""
        self.entries = []
        
        try:
            # Clean and normalize content
            content = self._clean_content(content)
            
            # Find all entries using improved regex
            entry_pattern = r'@(\w+)\s*\{\s*([^,\s]+)\s*,\s*(.*?)\n\s*\}'
            matches = re.finditer(entry_pattern, content, re.DOTALL | re.IGNORECASE)
            
            for match in matches:
                try:
                    entry = self._parse_entry(match)
                    if entry:
                        self.entries.append(entry)
                except Exception as e:
                    self.logger.warning(f"Error parsing entry: {e}")
                    continue
            
            self.logger.info(f"Successfully parsed {len(self.entries)} BibTeX entries")
            return self.entries
            
        except Exception as e:
            self.logger.error(f"Error parsing BibTeX content: {e}")
            return []
    
    def _parse_entry(self, match) -> Optional[BibEntry]:
        """Parse a single BibTeX entry match into a structured BibEntry."""
        entry_type = match.group(1).lower()
        key = match.group(2).strip()
        fields_str = match.group(3)
        
        # Parse raw fields
        raw_fields = self._parse_fields(fields_str)
        
        if not raw_fields:
            self.logger.warning(f"No fields found for entry: {key}")
            return None
        
        # Create structured entry
        entry = BibEntry(
            entry_type=entry_type,
            key=key,
            raw_fields=raw_fields
        )
        
        # Extract and structure common fields
        self._extract_structured_fields(entry, raw_fields)
        
        return entry
    
    def _extract_structured_fields(self, entry: BibEntry, raw_fields: Dict[str, str]) -> None:
        """Extract structured fields from raw BibTeX fields."""
        
        # Title
        entry.title = self._get_field_value(raw_fields, self.field_mappings['title'])
        if entry.title:
            entry.title = self._clean_latex_formatting(entry.title)
        
        # Authors
        authors_str = self._get_field_value(raw_fields, self.field_mappings['authors'])
        if authors_str:
            entry.authors = self._parse_authors(authors_str)
        
        # Year
        entry.year = self._get_field_value(raw_fields, self.field_mappings['year'])
        if entry.year:
            entry.year = self._extract_year(entry.year)
        
        # Month
        entry.month = self._get_field_value(raw_fields, self.field_mappings['month'])
        if entry.month:
            entry.month = self._clean_month(entry.month)
        
        # DOI
        entry.doi = self._get_field_value(raw_fields, self.field_mappings['doi'])
        if entry.doi:
            entry.doi = self._clean_doi(entry.doi)
        
        # URL
        entry.url = self._get_field_value(raw_fields, self.field_mappings['url'])
        if entry.url:
            entry.url = self._clean_url(entry.url)
        
        # Abstract
        entry.abstract = self._get_field_value(raw_fields, self.field_mappings['abstract'])
        if entry.abstract:
            entry.abstract = self._clean_latex_formatting(entry.abstract)
        
        # Keywords
        keywords_str = self._get_field_value(raw_fields, self.field_mappings['keywords'])
        if keywords_str:
            entry.keywords = self._parse_keywords(keywords_str)
        
        # Journal
        entry.journal = self._get_field_value(raw_fields, self.field_mappings['journal'])
        if entry.journal:
            entry.journal = self._clean_latex_formatting(entry.journal)
        
        # Volume
        entry.volume = self._get_field_value(raw_fields, self.field_mappings['volume'])
        
        # Pages
        entry.pages = self._get_field_value(raw_fields, self.field_mappings['pages'])
        
        # Publisher
        entry.publisher = self._get_field_value(raw_fields, self.field_mappings['publisher'])
        if entry.publisher:
            entry.publisher = self._clean_latex_formatting(entry.publisher)
    
    def set_discovery_dates(self, entries: List[BibEntry], discovery_cache: Optional[DiscoveryCache] = None) -> List[BibEntry]:
        """Set discovery dates for entries, using cache for existing entries or publication date as fallback."""
        for entry in entries:
            # Check if we already have a cached discovery date for this entry
            if discovery_cache:
                cached_date = discovery_cache.get_discovery_date(entry)
                if cached_date:
                    entry.discovery_date = cached_date
                    continue
            
            # This is a new entry - set discovery date
            if entry.discovery_date is None:
                # For the initial run, set discovery_date to publication_date for existing papers
                # This assumes all current papers are being "discovered" at their publication date
                pub_date = self._get_publication_datetime(entry)
                if pub_date:
                    entry.discovery_date = pub_date
                else:
                    # No publication date available, use current time
                    entry.discovery_date = datetime.now(timezone.utc)
                
                # Store in cache for future runs
                if discovery_cache:
                    discovery_cache.store_discovery_date(entry, entry.discovery_date)
        
        return entries
    
    def _get_publication_datetime(self, entry: BibEntry) -> Optional[datetime]:
        """Convert entry's publication date to datetime object."""
        if not entry.year:
            return None
            
        try:
            year = int(entry.year)
            month = int(entry.month) if entry.month and entry.month.isdigit() else 1
            day = 15 if entry.month else 1  # Mid-month if we have month, otherwise January 1st
            
            return datetime(year, month, day, tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None
    
    def _get_field_value(self, fields: Dict[str, str], field_names: List[str]) -> Optional[str]:
        """Get the first available field value from a list of possible field names."""
        for field_name in field_names:
            if field_name in fields and fields[field_name].strip():
                return fields[field_name].strip()
        return None
    
    def _clean_content(self, content: str) -> str:
        """Remove comments and normalize content."""
        lines = []
        for line in content.split('\n'):
            # Remove comment lines (but not URLs with %)
            if line.strip().startswith('%') and 'http' not in line:
                continue
            lines.append(line)
        
        return '\n'.join(lines)
    
    def _parse_fields(self, fields_str: str) -> Dict[str, str]:
        """Parse the fields section of a BibTeX entry with improved handling."""
        fields = {}
        
        # Handle multiline field values and nested braces
        current_field = None
        current_value = []
        brace_count = 0
        in_quotes = False
        
        # Tokenize the fields string
        tokens = self._tokenize_fields(fields_str)
        
        i = 0
        while i < len(tokens):
            token = tokens[i]
            
            if '=' in token and current_field is None:
                # New field assignment
                field_name, _, value_start = token.partition('=')
                current_field = field_name.strip().lower()
                current_value = [value_start.strip()] if value_start.strip() else []
            elif current_field is not None:
                current_value.append(token)
            
            # Check if field value is complete
            if current_field is not None:
                value_str = ' '.join(current_value)
                if self._is_field_complete(value_str):
                    # Clean and store the field
                    clean_value = self._clean_field_value(value_str.rstrip(','))
                    if clean_value:
                        fields[current_field] = clean_value
                    current_field = None
                    current_value = []
            
            i += 1
        
        # Handle any remaining field
        if current_field is not None and current_value:
            clean_value = self._clean_field_value(' '.join(current_value).rstrip(','))
            if clean_value:
                fields[current_field] = clean_value
        
        return fields
    
    def _tokenize_fields(self, fields_str: str) -> List[str]:
        """Tokenize fields string while respecting braces and quotes."""
        tokens = []
        current_token = []
        brace_count = 0
        in_quotes = False
        
        for char in fields_str:
            if char == '"' and brace_count == 0:
                in_quotes = not in_quotes
                current_token.append(char)
            elif char == '{' and not in_quotes:
                brace_count += 1
                current_token.append(char)
            elif char == '}' and not in_quotes:
                brace_count -= 1
                current_token.append(char)
            elif char == ',' and brace_count == 0 and not in_quotes:
                if current_token:
                    tokens.append(''.join(current_token).strip())
                    current_token = []
            else:
                current_token.append(char)
        
        if current_token:
            tokens.append(''.join(current_token).strip())
        
        return [token for token in tokens if token]
    
    def _is_field_complete(self, value_str: str) -> bool:
        """Check if a field value is complete (balanced braces/quotes)."""
        brace_count = 0
        quote_count = 0
        
        for char in value_str:
            if char == '{':
                brace_count += 1
            elif char == '}':
                brace_count -= 1
            elif char == '"':
                quote_count += 1
        
        return brace_count == 0 and quote_count % 2 == 0
    
    def _clean_field_value(self, value: str) -> str:
        """Clean field value by removing braces, quotes, and extra whitespace."""
        if not value:
            return ""
        
        value = value.strip()
        
        # Remove outer braces or quotes
        while ((value.startswith('{') and value.endswith('}')) or 
               (value.startswith('"') and value.endswith('"'))):
            value = value[1:-1].strip()
        
        # Clean up whitespace
        value = re.sub(r'\s+', ' ', value).strip()
        
        return value
    
    def _clean_latex_formatting(self, text: str) -> str:
        """Remove LaTeX formatting from text."""
        if not text:
            return ""
        
        # Remove common LaTeX commands
        text = re.sub(r'\\[a-zA-Z]+\{([^}]*)\}', r'\1', text)  # \textbf{text} -> text
        text = re.sub(r'\\[a-zA-Z]+', '', text)  # Remove other commands
        text = re.sub(r'\{([^}]*)\}', r'\1', text)  # Remove remaining braces
        text = re.sub(r'\s+', ' ', text).strip()
        
        return text
    
    def _parse_authors(self, authors_str: str) -> List[str]:
        """Parse authors string into list of individual authors."""
        if not authors_str:
            return []
        
        # Split by 'and' keyword
        authors = re.split(r'\s+and\s+', authors_str, flags=re.IGNORECASE)
        
        # Clean each author name
        cleaned_authors = []
        for author in authors:
            author = author.strip()
            if author:
                # Remove LaTeX formatting
                author = self._clean_latex_formatting(author)
                cleaned_authors.append(author)
        
        return cleaned_authors
    
    def _extract_year(self, year_str: str) -> Optional[str]:
        """Extract year from date/year field."""
        if not year_str:
            return None
        
        # Look for 4-digit year
        year_match = re.search(r'\b(19|20)\d{2}\b', year_str)
        if year_match:
            return year_match.group()
        
        return year_str.strip()
    
    def _clean_month(self, month_str: str) -> Optional[str]:
        """Clean and normalize month field to numeric format."""
        if not month_str:
            return None
        
        month_str = month_str.strip().lower()
        
        # Month name to number mapping
        month_map = {
            'january': '01', 'jan': '01',
            'february': '02', 'feb': '02',
            'march': '03', 'mar': '03',
            'april': '04', 'apr': '04',
            'may': '05',
            'june': '06', 'jun': '06',
            'july': '07', 'jul': '07',
            'august': '08', 'aug': '08',
            'september': '09', 'sep': '09', 'sept': '09',
            'october': '10', 'oct': '10',
            'november': '11', 'nov': '11',
            'december': '12', 'dec': '12'
        }
        
        # Try direct mapping first
        if month_str in month_map:
            return month_map[month_str]
        
        # Try to extract month number if already numeric
        month_match = re.search(r'\b(0?[1-9]|1[0-2])\b', month_str)
        if month_match:
            return f"{int(month_match.group()):02d}"
        
        return None
    
    def _clean_doi(self, doi_str: str) -> str:
        """Clean and normalize DOI."""
        if not doi_str:
            return ""
        
        # Remove DOI prefix if present
        doi = re.sub(r'^(doi:)?(https?://)?((dx\.)?doi\.org/)?', '', doi_str, flags=re.IGNORECASE)
        
        return doi.strip()
    
    def _clean_url(self, url_str: str) -> str:
        """Clean and validate URL."""
        if not url_str:
            return ""
        
        url = url_str.strip()
        
        # Remove LaTeX \url{} wrapper
        url = re.sub(r'\\url\{([^}]*)\}', r'\1', url)
        
        return url
    
    def _parse_keywords(self, keywords_str: str) -> List[str]:
        """Parse keywords string into list."""
        if not keywords_str:
            return []
        
        # Split by common separators
        keywords = re.split(r'[,;]', keywords_str)
        
        # Clean each keyword
        cleaned_keywords = []
        for keyword in keywords:
            keyword = keyword.strip()
            if keyword:
                keyword = self._clean_latex_formatting(keyword)
                cleaned_keywords.append(keyword)
        
        return cleaned_keywords
    
    def get_entry_by_key(self, key: str) -> Optional[BibEntry]:
        """Get entry by its BibTeX key."""
        for entry in self.entries:
            if entry.key == key:
                return entry
        return None
    
    def filter_entries_by_type(self, entry_type: str) -> List[BibEntry]:
        """Filter entries by type (article, book, etc.)."""
        return [entry for entry in self.entries if entry.entry_type == entry_type.lower()]
    
    def get_entries_with_field(self, field_name: str) -> List[BibEntry]:
        """Get entries that have a specific field populated."""
        field_attr = getattr(BibEntry, field_name, None)
        if field_attr is None:
            return []
        
        return [entry for entry in self.entries 
                if getattr(entry, field_name) is not None and 
                getattr(entry, field_name) != [] and 
                getattr(entry, field_name) != ""]