"""Feed generator module for converting bibliographic entries to RSS and JSON Feed formats."""

import xml.etree.ElementTree as ET
import json
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any
from urllib.parse import quote
from .bibtex_parser import BibEntry
from .metadata_enricher import EnrichedMetadata


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
    
    def generate_json_feed(self, entries: List[BibEntry], 
                          enriched_metadata: Optional[Dict[str, EnrichedMetadata]] = None) -> str:
        """Generate JSON Feed from bibliographic entries (primary format with full metadata)."""
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
        
        # Add items for each entry
        for entry in entries:
            metadata = enriched_metadata.get(entry.key) if enriched_metadata else None
            item = self._create_json_item(entry, metadata)
            feed["items"].append(item)
        
        return json.dumps(feed, indent=2, ensure_ascii=False)
    
    def generate_rss(self, entries: List[BibEntry], 
                    enriched_metadata: Optional[Dict[str, EnrichedMetadata]] = None) -> str:
        """Generate RSS XML from bibliographic entries (simplified format for compatibility)."""
        # Create root RSS element
        rss = ET.Element("rss", version="2.0")
        rss.set("xmlns:dc", "http://purl.org/dc/elements/1.1/")
        rss.set("xmlns:content", "http://purl.org/rss/1.0/modules/content/")
        
        # Create channel
        channel = ET.SubElement(rss, "channel")
        
        # Add channel metadata
        self._add_channel_metadata(channel)
        
        # Add items for each entry
        for entry in entries:
            metadata = enriched_metadata.get(entry.key) if enriched_metadata else None
            item = self._create_rss_item(entry, metadata)
            channel.append(item)
        
        # Convert to string
        return self._prettify_xml(rss)
    
    def _create_json_item(self, entry: BibEntry, metadata: Optional[EnrichedMetadata] = None) -> Dict[str, Any]:
        """Create a JSON Feed item from a bibliographic entry."""
        item = {
            "id": self._get_entry_guid(entry),
            "title": self._get_entry_title(entry),
            "content_text": self._get_entry_description(entry, metadata),
            "date_published": self._get_entry_date_iso(entry, metadata)
        }
        
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
        
        return item
    
    def _get_entry_title(self, entry: BibEntry) -> str:
        """Extract title from entry."""
        title = entry.title or 'Untitled'
        # Clean up LaTeX formatting
        title = title.replace('{', '').replace('}', '')
        return title.strip()
    
    def _get_entry_description(self, entry: BibEntry, metadata: Optional[EnrichedMetadata]) -> Optional[str]:
        """Get description for RSS item."""
        # Prefer enriched abstract
        if metadata and metadata.abstract:
            return metadata.abstract[:500] + "..." if len(metadata.abstract) > 500 else metadata.abstract
        
        # Fall back to entry abstract
        if entry.abstract:
            return entry.abstract[:500] + "..." if len(entry.abstract) > 500 else entry.abstract
        
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
            return metadata.doi_url
        
        if entry.doi:
            return f"https://doi.org/{entry.doi}"
        
        # Try arXiv
        if metadata and metadata.arxiv_url:
            return metadata.arxiv_url
        
        # Check for URL in entry
        if entry.url:
            return entry.url
        
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
        # Try enriched metadata first
        if metadata and metadata.publication_date:
            try:
                # Parse and format date
                if len(metadata.publication_date) == 4:  # Year only
                    date_obj = datetime(int(metadata.publication_date), 1, 1, tzinfo=timezone.utc)
                else:
                    date_obj = datetime.fromisoformat(metadata.publication_date.replace('Z', '+00:00'))
                return date_obj.strftime("%a, %d %b %Y %H:%M:%S %z")
            except:
                pass
        
        # Fall back to entry year
        year = entry.year
        if year:
            try:
                date_obj = datetime(int(year), 1, 1, tzinfo=timezone.utc)
                return date_obj.strftime("%a, %d %b %Y %H:%M:%S %z")
            except:
                pass
        
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
        
        # Add abstract
        abstract = (metadata.abstract if metadata and metadata.abstract 
                   else entry.abstract)
        if abstract:
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
        
        # Add links
        links = []
        if metadata:
            if metadata.doi_url:
                links.append(f'<a href="{metadata.doi_url}">DOI</a>')
            if metadata.arxiv_url:
                links.append(f'<a href="{metadata.arxiv_url}">arXiv</a>')
            if metadata.pdf_url:
                links.append(f'<a href="{metadata.pdf_url}">PDF</a>')
        
        if entry.url:
            links.append(f'<a href="{entry.url}">URL</a>')
        
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
    
    def _get_entry_date_iso(self, entry: BibEntry, metadata: Optional[EnrichedMetadata]) -> Optional[str]:
        """Get publication date in ISO 8601 format for JSON Feed."""
        # Try enriched metadata first
        if metadata and metadata.publication_date:
            try:
                if len(metadata.publication_date) == 4:  # Year only
                    return f"{metadata.publication_date}-01-01T00:00:00Z"
                elif '-' in metadata.publication_date:
                    # Try to parse existing date
                    date_parts = metadata.publication_date.split('-')
                    if len(date_parts) >= 3:
                        return f"{metadata.publication_date}T00:00:00Z"
                    elif len(date_parts) == 2:
                        return f"{metadata.publication_date}-01T00:00:00Z"
            except:
                pass
        
        # Fall back to entry year
        year = entry.year
        if year:
            try:
                return f"{year}-01-01T00:00:00Z"
            except:
                pass
        
        return None
    
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
        
        # Abstract
        abstract = (metadata.abstract if metadata and metadata.abstract 
                   else entry.abstract)
        if abstract:
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
        
        # Links
        links = []
        if metadata:
            if metadata.doi_url:
                links.append(f'<a href="{metadata.doi_url}">DOI</a>')
            if metadata.arxiv_url:
                links.append(f'<a href="{metadata.arxiv_url}">arXiv</a>')
            if metadata.pdf_url:
                links.append(f'<a href="{metadata.pdf_url}">PDF</a>')
        
        if entry.url:
            links.append(f'<a href="{entry.url}">URL</a>')
        
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
        
        return extensions
    
    def _escape_html(self, text: str) -> str:
        """Escape HTML characters in text."""
        if not text:
            return ""
        
        return (text.replace("&", "&amp;")
                   .replace("<", "&lt;")
                   .replace(">", "&gt;")
                   .replace('"', "&quot;")
                   .replace("'", "&#x27;"))