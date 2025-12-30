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
    # Remove non-word characters except whitespace, hyphens, and colons
    clean = re.sub(r'[^\w\s\-:]', ' ', clean)
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
