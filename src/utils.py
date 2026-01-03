"""Shared utility functions for text matching and similarity calculations."""

import re
from typing import List, Set


# Common stop words to exclude from similarity calculations
STOP_WORDS: Set[str] = {
    'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by'
}


def clean_title_for_search(title: str) -> str:
    """Clean title for better search results.

    Removes LaTeX commands, braces, and excessive punctuation.

    Args:
        title: The title string to clean

    Returns:
        Cleaned title suitable for search queries
    """
    if not title:
        return ""

    # Remove LaTeX commands like \textbf{text} -> text
    clean = re.sub(r'\\[a-zA-Z]+\{([^}]*)\}', r'\1', title)
    # Remove remaining braces
    clean = re.sub(r'[{}]', '', clean)
    # Remove non-word characters except whitespace, hyphens, colons, and apostrophes
    # Apostrophes are important for possessives (e.g., "EU's") and contractions
    clean = re.sub(r"[^\w\s\-:']", ' ', clean)
    # Normalize whitespace
    clean = re.sub(r'\s+', ' ', clean).strip()

    return clean


def calculate_text_similarity(text1: str, text2: str, use_stop_words: bool = True) -> float:
    """Calculate similarity between two texts using Jaccard word overlap.

    Args:
        text1: First text to compare
        text2: Second text to compare
        use_stop_words: Whether to filter out common stop words

    Returns:
        Similarity score between 0.0 and 1.0
    """
    if not text1 or not text2:
        return 0.0

    words1 = set(text1.lower().split())
    words2 = set(text2.lower().split())

    if not words1 or not words2:
        return 0.0

    # Remove very common words that don't help with matching
    if use_stop_words:
        words1 = words1 - STOP_WORDS
        words2 = words2 - STOP_WORDS

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


def calculate_author_similarity(query_author: str, paper_authors: List[str]) -> float:
    """Calculate similarity between query author string and list of paper authors.

    Handles 'and'-separated author lists and finds the best matching author.

    Args:
        query_author: Author string from query (may be 'and'-separated)
        paper_authors: List of author names from the paper

    Returns:
        Maximum similarity score between 0.0 and 1.0
    """
    if not query_author or not paper_authors:
        return 0.0

    # Parse query authors (handle "and" separated list)
    query_authors = [name.strip() for name in query_author.split(' and ')]

    max_similarity = 0.0
    for q_author in query_authors:
        for p_author in paper_authors:
            similarity = calculate_text_similarity(q_author, p_author)
            max_similarity = max(max_similarity, similarity)

    return max_similarity


def calculate_crossref_author_similarity(query_author: str, crossref_authors: List[dict]) -> float:
    """Calculate similarity between query author and Crossref-formatted authors.

    Crossref returns authors as dicts with 'given' and 'family' keys.

    Args:
        query_author: Author string from query
        crossref_authors: List of author dicts from Crossref API

    Returns:
        Maximum similarity score between 0.0 and 1.0
    """
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

    return calculate_author_similarity(query_author, crossref_names)


def extract_first_author(author_str: str) -> str:
    """Extract first author name for search.

    Args:
        author_str: Full author string (possibly 'and'-separated)

    Returns:
        First author's name
    """
    if not author_str:
        return ""

    # Split by 'and' and take first, then take family name if comma-separated
    first = author_str.split(' and ')[0].split(',')[0].strip()
    return first


def strip_jats_xml_tags(text: str) -> str:
    """Strip JATS XML tags from text (commonly found in Crossref abstracts).

    Handles tags like <jats:p>, <jats:title>, <jats:sec>, <jats:italic>, etc.
    Also handles generic XML/HTML tags.

    Args:
        text: Text potentially containing JATS XML tags

    Returns:
        Clean text with tags removed
    """
    if not text:
        return ""

    # Replace closing block-level tags with space to preserve word boundaries
    clean = re.sub(r'</jats:(?:p|title|sec|abstract)>', ' ', text)

    # Remove remaining JATS namespace tags: <jats:p>, </jats:p>, <jats:italic>, etc.
    clean = re.sub(r'</?jats:[^>]+>', '', clean)

    # Replace closing block-level HTML tags with space
    clean = re.sub(r'</(?:p|title|div|section|br)>', ' ', clean, flags=re.IGNORECASE)

    # Remove other common XML/HTML tags
    clean = re.sub(r'</?(?:p|title|sec|italic|bold|sub|sup|br|span|div|em|strong)[^>]*>', '', clean, flags=re.IGNORECASE)

    # Remove any remaining XML-style tags
    clean = re.sub(r'<[^>]+>', '', clean)

    # Normalize whitespace (multiple spaces, newlines, etc.)
    clean = re.sub(r'\s+', ' ', clean).strip()

    return clean


def clean_url(url: str) -> str:
    """Clean LaTeX escapes and other artifacts from URLs.

    Handles common issues like:
    - LaTeX escaped underscores: \\_ -> _
    - LaTeX escaped ampersands: \\& -> &
    - LaTeX escaped percent: \\% -> %
    - Escaped braces: \\{ \\} -> { }
    - URL wrapper: \\url{...} -> ...

    Args:
        url: URL string potentially containing LaTeX escapes

    Returns:
        Clean URL
    """
    if not url:
        return ""

    url = url.strip()

    # Remove LaTeX \url{} wrapper
    url = re.sub(r'\\url\{([^}]*)\}', r'\1', url)

    # Remove LaTeX escapes (backslash before special chars)
    url = url.replace('\\_', '_')
    url = url.replace('\\&', '&')
    url = url.replace('\\%', '%')
    url = url.replace('\\#', '#')
    url = url.replace('\\{', '{')
    url = url.replace('\\}', '}')
    url = url.replace('\\~', '~')

    # Handle double backslashes that might remain
    url = url.replace('\\\\', '\\')

    # Remove any remaining single backslashes before alphanumeric chars
    # (but preserve %XX encoding)
    url = re.sub(r'\\(?=[a-zA-Z_])', '', url)

    return url


def extract_title_from_url(url: str) -> str:
    """Try to extract a meaningful title from a URL.

    Handles common patterns from academic sites like:
    - /papers/title-of-paper
    - /article/title_of_paper.pdf
    - ?title=some-title

    Args:
        url: URL to extract title from

    Returns:
        Extracted title or empty string if no title found
    """
    if not url:
        return ""

    from urllib.parse import urlparse, parse_qs, unquote

    try:
        parsed = urlparse(url)

        # Try query parameters first (some sites use ?title=)
        query_params = parse_qs(parsed.query)
        for param in ['title', 'name', 't']:
            if param in query_params:
                return unquote(query_params[param][0]).replace('-', ' ').replace('_', ' ')

        # Try path - get the last meaningful segment
        path = parsed.path

        # Remove file extension
        path = re.sub(r'\.(pdf|html?|aspx?|php|xml)$', '', path, flags=re.IGNORECASE)

        # Get last path segment
        segments = [s for s in path.split('/') if s and not s.isdigit()]
        if segments:
            last_segment = segments[-1]

            # Skip common non-title segments
            skip_patterns = ['article', 'paper', 'abstract', 'view', 'content',
                           'doi', 'full', 'download', 'index']
            if last_segment.lower() not in skip_patterns:
                # Clean up the segment
                title = unquote(last_segment)
                title = title.replace('-', ' ').replace('_', ' ')

                # Only return if it looks like a title (has multiple words, not just numbers)
                words = title.split()
                if len(words) >= 2 and not all(w.isdigit() for w in words):
                    # Capitalize first letter of each word
                    return ' '.join(word.capitalize() for word in words)

        return ""
    except Exception:
        return ""


def is_valid_title(title: str) -> bool:
    """Check if a title is meaningful (not just placeholder text).

    Args:
        title: Title to validate

    Returns:
        True if title appears to be valid
    """
    if not title:
        return False

    # Titles that are essentially empty
    invalid_titles = {
        'untitled', 'no title', 'unknown', 'n/a', 'na', 'none',
        'title', 'paper', 'article', 'document', 'pdf',
    }

    title_lower = title.lower().strip()

    # Check against known invalid titles
    if title_lower in invalid_titles:
        return False

    # Too short to be meaningful
    if len(title_lower) < 5:
        return False

    # Just numbers or special characters
    if re.match(r'^[\d\W]+$', title):
        return False

    return True
