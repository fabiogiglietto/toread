"""Feed generator module for converting bibliographic entries to RSS and JSON Feed formats."""

import xml.etree.ElementTree as ET
import json
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any
from urllib.parse import quote
from .bibtex_parser import BibEntry
from .metadata_enricher import EnrichedMetadata
from .utils import strip_jats_xml_tags, clean_url, extract_title_from_url, is_valid_title


class FeedGenerator:
    """Generates RSS and JSON Feed formats from bibliographic entries."""
    
    def __init__(self, feed_title: str = "ToRead - Academic Papers",
                 feed_description: str = "Academic papers from Paperpile exports",
                 feed_link: str = "https://github.com/user/toread",
                 feed_language: str = "en-us",
                 author_name: str = "ToRead Bot",
                 author_url: str = None):
        self.feed_title = feed_title
        self.feed_description = feed_description
        self.feed_link = feed_link
        self.feed_language = feed_language
        self.author_name = author_name
        self.author_url = author_url or feed_link
    
    def _sort_entries_by_discovery_date(self, entries: List[BibEntry]) -> List[BibEntry]:
        """Sort entries by discovery date in reverse chronological order (newest discoveries first)."""
        def get_sort_key(entry: BibEntry) -> datetime:
            """Get discovery date for sorting, with fallback to epoch if missing."""
            if entry.discovery_date:
                return entry.discovery_date
            # Fallback for entries without discovery date
            return datetime(1970, 1, 1, tzinfo=timezone.utc)
        
        # Sort in reverse chronological order (newest discoveries first)
        return sorted(entries, key=get_sort_key, reverse=True)
    
    def generate_json_feed(self, entries: List[BibEntry], 
                          enriched_metadata: Optional[Dict[str, EnrichedMetadata]] = None) -> str:
        """Generate JSON Feed from bibliographic entries (primary format with full metadata)."""
        # Sort entries by discovery date (newest discoveries first)
        sorted_entries = self._sort_entries_by_discovery_date(entries)
        
        feed = {
            "version": "https://jsonfeed.org/version/1.1",
            "title": self.feed_title,
            "description": self.feed_description,
            "home_page_url": self.feed_link,
            "feed_url": f"{self.feed_link}/feed.json",
            "language": self.feed_language,
            "authors": [
                {
                    "name": self.author_name,
                    "url": self.author_url
                }
            ],
            "items": []
        }
        
        # Add items for each entry (now sorted)
        for entry in sorted_entries:
            metadata = enriched_metadata.get(entry.key) if enriched_metadata else None
            item = self._create_json_item(entry, metadata)
            feed["items"].append(item)
        
        return json.dumps(feed, indent=2, ensure_ascii=False)
    
    def generate_rss(self, entries: List[BibEntry], 
                    enriched_metadata: Optional[Dict[str, EnrichedMetadata]] = None) -> str:
        """Generate RSS XML from bibliographic entries (simplified format for compatibility)."""
        # Sort entries by discovery date (newest discoveries first)
        sorted_entries = self._sort_entries_by_discovery_date(entries)
        
        # Create root RSS element
        rss = ET.Element("rss", version="2.0")
        rss.set("xmlns:dc", "http://purl.org/dc/elements/1.1/")
        rss.set("xmlns:content", "http://purl.org/rss/1.0/modules/content/")
        
        # Create channel
        channel = ET.SubElement(rss, "channel")
        
        # Add channel metadata
        self._add_channel_metadata(channel)
        
        # Add items for each entry (now sorted)
        for entry in sorted_entries:
            metadata = enriched_metadata.get(entry.key) if enriched_metadata else None
            item = self._create_rss_item(entry, metadata)
            channel.append(item)
        
        # Convert to string
        return self._prettify_xml(rss)
    
    def _create_json_item(self, entry: BibEntry, metadata: Optional[EnrichedMetadata] = None) -> Dict[str, Any]:
        """Create a JSON Feed item from a bibliographic entry."""
        date_published, is_estimated = self._get_entry_date_iso(entry, metadata)
        
        item = {
            "id": self._get_entry_guid(entry),
            "title": self._get_entry_title(entry),
            "content_text": self._get_entry_description(entry, metadata),
            "date_published": date_published
        }
        
        # Add discovery date
        if entry.discovery_date:
            discovery_date_iso = entry.discovery_date.isoformat().replace('+00:00', 'Z')
            item["_discovery_date"] = discovery_date_iso
        
        # Add estimation indicator if date is estimated
        if is_estimated and date_published:
            item["_date_estimated"] = True
        
        # Add URL if available
        url = self._get_entry_link(entry, metadata)
        if url:
            item["url"] = url
            item["external_url"] = url
        
        # Add authors
        authors = self._get_entry_authors_list(entry, metadata)
        if authors:
            item["authors"] = authors
        
        # Add tags/categories
        tags = self._get_entry_tags(entry, metadata)
        if tags:
            item["tags"] = tags
        
        # Add full content with rich metadata
        content_html = self._get_json_content_html(entry, metadata)
        if content_html:
            item["content_html"] = content_html
        
        # Add custom extensions for academic metadata
        extensions = self._get_academic_extensions(entry, metadata)
        if extensions:
            item["_academic"] = extensions
        
        return item
    
    def _add_channel_metadata(self, channel: ET.Element) -> None:
        """Add metadata to the RSS channel."""
        ET.SubElement(channel, "title").text = self.feed_title
        ET.SubElement(channel, "link").text = self.feed_link
        ET.SubElement(channel, "description").text = self.feed_description
        ET.SubElement(channel, "language").text = self.feed_language
        ET.SubElement(channel, "generator").text = "ToRead RSS Generator"
        
        # Add current timestamp
        now = datetime.now(timezone.utc)
        ET.SubElement(channel, "lastBuildDate").text = now.strftime("%a, %d %b %Y %H:%M:%S %z")
        ET.SubElement(channel, "pubDate").text = now.strftime("%a, %d %b %Y %H:%M:%S %z")
    
    def _create_rss_item(self, entry: BibEntry, metadata: Optional[EnrichedMetadata] = None) -> ET.Element:
        """Create an RSS item from a bibliographic entry."""
        item = ET.Element("item")
        
        # Title
        title = self._get_entry_title(entry)
        ET.SubElement(item, "title").text = title
        
        # Description (abstract or summary)
        description = self._get_entry_description(entry, metadata)
        if description:
            ET.SubElement(item, "description").text = description
        
        # Link (DOI, arXiv, or generated)
        link = self._get_entry_link(entry, metadata)
        if link:
            ET.SubElement(item, "link").text = link
        
        # GUID (unique identifier)
        guid = self._get_entry_guid(entry)
        guid_elem = ET.SubElement(item, "guid")
        guid_elem.text = guid
        guid_elem.set("isPermaLink", "false")
        
        # Publication date
        pub_date = self._get_entry_date(entry, metadata)
        if pub_date:
            ET.SubElement(item, "pubDate").text = pub_date
        
        # Authors
        authors = self._get_entry_authors(entry)
        if authors:
            ET.SubElement(item, "dc:creator").text = authors
        
        # Categories/Keywords
        self._add_categories(item, entry, metadata)
        
        # Content (detailed description)
        content = self._get_entry_content(entry, metadata)
        if content:
            content_elem = ET.SubElement(item, "content:encoded")
            content_elem.text = f"<![CDATA[{content}]]>"
        
        # Discovery date
        if entry.discovery_date:
            discovery_date_rss = entry.discovery_date.strftime("%a, %d %b %Y %H:%M:%S %z")
            ET.SubElement(item, "dc:date").text = discovery_date_rss
        
        return item
    
    def _get_entry_title(self, entry: BibEntry, metadata: Optional[EnrichedMetadata] = None) -> str:
        """Extract title from entry with HTML escaping for safety.

        Tries multiple sources in order:
        1. Entry title (if valid)
        2. URL-extracted title (if entry has URL)
        3. Fallback to 'Untitled'
        """
        title = entry.title

        # Check if we have a valid title
        if not is_valid_title(title):
            # Try to extract from URL
            url = entry.url
            if url:
                url_title = extract_title_from_url(clean_url(url))
                if url_title:
                    title = url_title

        # Final fallback
        if not title:
            title = 'Untitled'

        # Clean up LaTeX formatting
        title = title.replace('{', '').replace('}', '')
        # Escape HTML to prevent XSS
        title = self._escape_html(title.strip())
        return title
    
    def _get_entry_description(self, entry: BibEntry, metadata: Optional[EnrichedMetadata]) -> Optional[str]:
        """Get description for RSS item."""
        abstract = None

        # Prefer enriched abstract
        if metadata and metadata.abstract:
            abstract = strip_jats_xml_tags(metadata.abstract)
        # Fall back to entry abstract
        elif entry.abstract:
            abstract = strip_jats_xml_tags(entry.abstract)

        if abstract:
            return abstract[:500] + "..." if len(abstract) > 500 else abstract

        # Create summary from available fields
        summary_parts = []

        if entry.journal:
            summary_parts.append(f"Published in {entry.journal}")

        if entry.year:
            summary_parts.append(f"Year: {entry.year}")

        if entry.authors:
            authors_str = ", ".join(entry.authors)
            authors_str = authors_str[:100] + "..." if len(authors_str) > 100 else authors_str
            summary_parts.append(f"Authors: {authors_str}")

        return " | ".join(summary_parts) if summary_parts else None
    
    def _get_entry_link(self, entry: BibEntry, metadata: Optional[EnrichedMetadata]) -> Optional[str]:
        """Get link for RSS item."""
        # Prefer DOI
        if metadata and metadata.doi_url:
            return clean_url(metadata.doi_url)

        if entry.doi:
            return f"https://doi.org/{entry.doi}"

        # Try arXiv
        if metadata and metadata.arxiv_url:
            return clean_url(metadata.arxiv_url)

        # Check for URL in entry
        if entry.url:
            return clean_url(entry.url)

        return None
    
    def _get_entry_guid(self, entry: BibEntry) -> str:
        """Generate unique identifier for entry."""
        # Use DOI if available
        if entry.doi:
            return f"doi:{entry.doi}"
        
        # Use entry key as fallback
        return f"bibtex:{entry.key}"
    
    def _get_entry_date(self, entry: BibEntry, metadata: Optional[EnrichedMetadata]) -> Optional[str]:
        """Get publication date in RSS format."""
        iso_date, is_estimated = self._get_entry_date_iso(entry, metadata)
        
        if not iso_date:
            return None
        
        try:
            # Convert ISO date to RSS format
            date_obj = datetime.fromisoformat(iso_date.replace('Z', '+00:00'))
            return date_obj.strftime("%a, %d %b %Y %H:%M:%S %z")
        except (ValueError, TypeError, AttributeError):
            return None
    
    def _get_entry_authors(self, entry: BibEntry) -> Optional[str]:
        """Get formatted authors string."""
        author = ', '.join(entry.authors) if entry.authors else None
        if not author:
            return None
        
        # Simple formatting - replace 'and' with commas
        authors = author.replace(' and ', ', ')
        return authors
    
    def _add_categories(self, item: ET.Element, entry: BibEntry, metadata: Optional[EnrichedMetadata]) -> None:
        """Add categories/keywords to RSS item."""
        categories = set()
        
        # Add keywords from metadata
        if metadata and metadata.keywords:
            categories.update(metadata.keywords)
        
        # Add entry type as category
        categories.add(entry.entry_type.title())
        
        # Add journal as category if available
        if entry.journal:
            categories.add(entry.journal)
        
        # Add keywords from entry if available
        if entry.keywords:
            categories.update(entry.keywords)
        
        # Add category elements
        for category in categories:
            if category:  # Skip empty categories
                ET.SubElement(item, "category").text = category
    
    def _get_entry_content(self, entry: BibEntry, metadata: Optional[EnrichedMetadata]) -> str:
        """Generate detailed content for the entry."""
        content_parts = []

        # Add abstract (strip JATS tags)
        abstract = (metadata.abstract if metadata and metadata.abstract
                   else entry.abstract)
        if abstract:
            abstract = strip_jats_xml_tags(abstract)
            content_parts.append(f"<h3>Abstract</h3><p>{abstract}</p>")
        
        # Add bibliographic details
        details = []
        if entry.authors:
            authors_str = ', '.join(entry.authors)
            details.append(f"<strong>Authors:</strong> {authors_str}")
        
        if entry.journal:
            details.append(f"<strong>Journal:</strong> {entry.journal}")
        
        if entry.year:
            details.append(f"<strong>Year:</strong> {entry.year}")
        
        if entry.volume:
            details.append(f"<strong>Volume:</strong> {entry.volume}")
        
        if entry.pages:
            details.append(f"<strong>Pages:</strong> {entry.pages}")
        
        if details:
            content_parts.append("<h3>Details</h3><ul>" + 
                               "".join(f"<li>{detail}</li>" for detail in details) + 
                               "</ul>")
        
        # Add links (with URL validation)
        links = []
        if metadata:
            doi_url = self._validate_url(metadata.doi_url)
            if doi_url:
                links.append(f'<a href="{doi_url}">DOI</a>')
            arxiv_url = self._validate_url(metadata.arxiv_url)
            if arxiv_url:
                links.append(f'<a href="{arxiv_url}">arXiv</a>')
            pdf_url = self._validate_url(metadata.pdf_url)
            if pdf_url:
                links.append(f'<a href="{pdf_url}">PDF</a>')

        entry_url = self._validate_url(entry.url)
        if entry_url:
            links.append(f'<a href="{entry_url}">URL</a>')

        if links:
            content_parts.append("<h3>Links</h3><p>" + " | ".join(links) + "</p>")

        return "".join(content_parts)
    
    def _prettify_xml(self, element: ET.Element) -> str:
        """Convert XML element to formatted string."""
        from xml.dom import minidom
        
        rough_string = ET.tostring(element, encoding='unicode')
        reparsed = minidom.parseString(rough_string)
        pretty = reparsed.toprettyxml(indent="  ")
        
        # Remove empty lines and fix XML declaration
        lines = [line for line in pretty.split('\n') if line.strip()]
        if lines[0].startswith('<?xml'):
            lines[0] = '<?xml version="1.0" encoding="UTF-8"?>'
        
        return '\n'.join(lines)
    
    def _get_entry_date_iso(self, entry: BibEntry, metadata: Optional[EnrichedMetadata]) -> tuple[Optional[str], bool]:
        """Get publication date in ISO 8601 format for JSON Feed.
        
        Returns:
            tuple: (iso_date_string, is_estimated)
        """
        # Priority 1: Try enriched metadata with precise dates first
        if metadata and metadata.publication_date:
            try:
                if '-' in metadata.publication_date:
                    date_parts = metadata.publication_date.split('-')
                    if len(date_parts) >= 3:
                        # Full date from metadata (precise)
                        return f"{metadata.publication_date}T00:00:00Z", False
                    elif len(date_parts) == 2:
                        # Year-month from metadata (estimated day)
                        return f"{metadata.publication_date}-15T00:00:00Z", True
            except:
                pass
        
        # Priority 2: Try BibTeX month + year combination
        if entry.year and entry.month:
            try:
                # Use BibTeX month information (estimated day)
                return f"{entry.year}-{entry.month}-15T00:00:00Z", True
            except:
                pass
        
        # Priority 3: Try enriched metadata year-only
        if metadata and metadata.publication_date:
            try:
                if len(metadata.publication_date) == 4:  # Year only
                    # Year-only from metadata (estimated month and day)
                    return f"{metadata.publication_date}-01-01T00:00:00Z", True
            except:
                pass
        
        # Priority 4: Fall back to BibTeX year only
        if entry.year:
            try:
                # Year-only from BibTeX (estimated month and day)
                return f"{entry.year}-01-01T00:00:00Z", True
            except:
                pass
        
        return None, False
    
    def _get_entry_authors_list(self, entry: BibEntry, metadata: Optional[EnrichedMetadata]) -> List[Dict[str, str]]:
        """Get authors as a list of objects for JSON Feed."""
        authors = []
        
        # Use enriched metadata first
        if metadata and metadata.authors:
            for author in metadata.authors:
                authors.append({"name": author})
        elif entry.authors:
            for author in entry.authors:
                authors.append({"name": author})
        
        return authors
    
    def _get_entry_tags(self, entry: BibEntry, metadata: Optional[EnrichedMetadata]) -> List[str]:
        """Get tags/categories for JSON Feed."""
        tags = set()
        
        # Add keywords from metadata
        if metadata and metadata.keywords:
            tags.update(metadata.keywords)
        
        # Add keywords from entry
        if entry.keywords:
            tags.update(entry.keywords)
        
        # Add entry type
        tags.add(entry.entry_type.title())
        
        # Add journal/venue
        if metadata and metadata.venue:
            tags.add(metadata.venue)
        elif entry.journal:
            tags.add(entry.journal)
        
        # Add subjects from metadata
        if metadata and metadata.subjects:
            tags.update(metadata.subjects)
        
        return list(tags)
    
    def _get_json_content_html(self, entry: BibEntry, metadata: Optional[EnrichedMetadata]) -> str:
        """Generate rich HTML content for JSON Feed."""
        content_parts = []

        # Abstract (strip JATS tags first, then escape HTML)
        abstract = (metadata.abstract if metadata and metadata.abstract
                   else entry.abstract)
        if abstract:
            abstract = strip_jats_xml_tags(abstract)
            content_parts.append(f"<h3>Abstract</h3><p>{self._escape_html(abstract)}</p>")
        
        # Bibliographic details
        details = []
        
        # Authors
        authors = metadata.authors if metadata and metadata.authors else entry.authors
        if authors:
            author_list = ", ".join(authors)
            details.append(f"<strong>Authors:</strong> {self._escape_html(author_list)}")
        
        # Venue/Journal
        venue = metadata.venue if metadata and metadata.venue else entry.journal
        if venue:
            details.append(f"<strong>Published in:</strong> {self._escape_html(venue)}")
        
        # Year
        year = entry.year
        if year:
            details.append(f"<strong>Year:</strong> {year}")
        
        # Volume and pages
        if entry.volume:
            details.append(f"<strong>Volume:</strong> {entry.volume}")
        if entry.pages:
            details.append(f"<strong>Pages:</strong> {entry.pages}")
        
        # Citation metrics
        if metadata:
            if metadata.citation_count is not None:
                details.append(f"<strong>Citations:</strong> {metadata.citation_count}")
            if metadata.reference_count is not None:
                details.append(f"<strong>References:</strong> {metadata.reference_count}")
        
        if details:
            content_parts.append("<h3>Details</h3><ul>" + 
                               "".join(f"<li>{detail}</li>" for detail in details) + 
                               "</ul>")
        
        # Links (with URL validation)
        links = []
        if metadata:
            doi_url = self._validate_url(metadata.doi_url)
            if doi_url:
                links.append(f'<a href="{doi_url}">DOI</a>')
            arxiv_url = self._validate_url(metadata.arxiv_url)
            if arxiv_url:
                links.append(f'<a href="{arxiv_url}">arXiv</a>')
            pdf_url = self._validate_url(metadata.pdf_url)
            if pdf_url:
                links.append(f'<a href="{pdf_url}">PDF</a>')

        entry_url = self._validate_url(entry.url)
        if entry_url:
            links.append(f'<a href="{entry_url}">URL</a>')

        if links:
            content_parts.append("<h3>Links</h3><p>" + " | ".join(links) + "</p>")

        return "".join(content_parts)
    
    def _get_academic_extensions(self, entry: BibEntry, metadata: Optional[EnrichedMetadata]) -> Dict[str, Any]:
        """Get academic-specific metadata extensions for JSON Feed."""
        extensions = {}

        # DOI
        if metadata and metadata.doi:
            extensions["doi"] = metadata.doi

        # Citation metrics
        if metadata:
            if metadata.citation_count is not None:
                extensions["citation_count"] = metadata.citation_count
            if metadata.reference_count is not None:
                extensions["reference_count"] = metadata.reference_count
            if metadata.is_open_access is not None:
                extensions["open_access"] = metadata.is_open_access

        # Entry type
        extensions["type"] = entry.entry_type

        # Publisher
        if entry.publisher:
            extensions["publisher"] = entry.publisher

        # Volume/Pages
        if entry.volume:
            extensions["volume"] = entry.volume
        if entry.pages:
            extensions["pages"] = entry.pages

        # Keywords/Subjects
        if metadata and metadata.subjects:
            extensions["subjects"] = metadata.subjects

        # Data source
        if metadata and metadata.source:
            extensions["metadata_source"] = metadata.source
            if metadata.confidence_score:
                extensions["confidence_score"] = metadata.confidence_score

        # Data quality indicator
        quality_score, quality_issues = self._calculate_quality_score(entry, metadata)
        extensions["quality_score"] = quality_score
        if quality_issues:
            extensions["quality_issues"] = quality_issues

        return extensions

    def _calculate_quality_score(self, entry: BibEntry, metadata: Optional[EnrichedMetadata]) -> tuple:
        """Calculate a quality score for an entry based on available data.

        Returns:
            tuple: (score 0-100, list of issues)
        """
        score = 0
        issues = []

        # Title quality (25 points)
        if is_valid_title(entry.title):
            score += 25
        else:
            issues.append("missing_title")

        # Authors (20 points)
        if entry.authors:
            score += 20
        else:
            issues.append("missing_authors")

        # Abstract (20 points)
        has_abstract = (entry.abstract or (metadata and metadata.abstract))
        if has_abstract:
            score += 20
        else:
            issues.append("missing_abstract")

        # Publication date (15 points)
        has_date = entry.year or (metadata and metadata.publication_date)
        if has_date:
            score += 15
        else:
            issues.append("missing_date")

        # DOI or URL (10 points)
        has_link = entry.doi or entry.url or (metadata and (metadata.doi_url or metadata.arxiv_url))
        if has_link:
            score += 10
        else:
            issues.append("missing_link")

        # Enrichment success (10 points)
        if metadata and metadata.source not in [None, "url"]:
            score += 10
        else:
            issues.append("not_enriched")

        return score, issues
    
    def _escape_html(self, text: str) -> str:
        """Escape HTML characters in text."""
        if not text:
            return ""

        return (text.replace("&", "&amp;")
                   .replace("<", "&lt;")
                   .replace(">", "&gt;")
                   .replace('"', "&quot;")
                   .replace("'", "&#x27;"))

    def _validate_url(self, url: str) -> Optional[str]:
        """Validate and sanitize URL to prevent XSS.

        Only allows http(s) URLs and rejects javascript: and data: schemes.

        Args:
            url: URL string to validate

        Returns:
            Sanitized URL if valid, None otherwise
        """
        if not url:
            return None

        # Clean LaTeX escapes first
        url = clean_url(url).strip()

        # Check for allowed schemes
        allowed_schemes = ('http://', 'https://')
        if not url.lower().startswith(allowed_schemes):
            # Reject javascript:, data:, and other potentially dangerous schemes
            if '://' in url or url.lower().startswith(('javascript:', 'data:', 'vbscript:')):
                return None
            # Assume https for scheme-less URLs that look like domains
            if '.' in url and not url.startswith('/'):
                url = f'https://{url}'
            else:
                return None

        # Additional XSS prevention - escape any HTML in the URL
        url = url.replace('"', '%22').replace("'", '%27').replace('<', '%3C').replace('>', '%3E')

        return url