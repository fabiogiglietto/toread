"""Metadata cache module for persistent storage of enriched bibliographic data."""

import json
import hashlib
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Set
from dataclasses import asdict
from .bibtex_parser import BibEntry


class MetadataCache:
    """Persistent cache for enriched metadata to avoid redundant API calls."""
    
    def __init__(self, cache_file: str = "cache/metadata_cache.json", 
                 cache_duration_days: int = 30):
        self.cache_file = Path(cache_file)
        self.cache_duration = timedelta(days=cache_duration_days)
        self.logger = logging.getLogger(__name__)
        self.cache_data = {}
        self.load_cache()
    
    def _get_entry_hash(self, entry: BibEntry) -> str:
        """Generate a hash for a BibTeX entry based on key identifying fields."""
        # Use title, authors, and year to create a stable hash
        content = f"{entry.title or ''}{','.join(entry.authors)}{entry.year or ''}{entry.doi or ''}"
        return hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]
    
    def load_cache(self) -> None:
        """Load existing cache from disk."""
        if self.cache_file.exists():
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    self.cache_data = json.load(f)
                self.logger.info(f"Loaded cache with {len(self.cache_data)} entries")
            except Exception as e:
                self.logger.warning(f"Failed to load cache: {e}")
                self.cache_data = {}
        else:
            self.cache_data = {}
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
    
    def save_cache(self) -> None:
        """Save current cache to disk."""
        try:
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(self.cache_data, f, indent=2, ensure_ascii=False)
            self.logger.info(f"Saved cache with {len(self.cache_data)} entries")
        except Exception as e:
            self.logger.error(f"Failed to save cache: {e}")
    
    def is_cached(self, entry: BibEntry) -> bool:
        """Check if entry has cached metadata that's still valid."""
        entry_hash = self._get_entry_hash(entry)
        if entry_hash not in self.cache_data:
            return False
        
        cached_item = self.cache_data[entry_hash]
        cache_time = datetime.fromisoformat(cached_item['cached_at'])
        
        # Check if cache is still valid
        if datetime.now() - cache_time > self.cache_duration:
            self.logger.debug(f"Cache expired for entry: {entry.key}")
            del self.cache_data[entry_hash]
            return False
        
        return True
    
    def should_retry_failed_entry(self, entry: BibEntry) -> bool:
        """Check if a failed entry should be retried (once a week limit)."""
        entry_hash = self._get_entry_hash(entry)
        if entry_hash not in self.cache_data:
            return True  # Not cached, should try
        
        cached_item = self.cache_data[entry_hash]
        
        # If it's not marked as failed, check if it has valid metadata
        if not cached_item.get('failed', False):
            return not self.is_cached(entry)
        
        # If it's marked as failed, check if enough time has passed
        last_failure_time = datetime.fromisoformat(cached_item.get('last_failure_at', cached_item['cached_at']))
        retry_interval = timedelta(days=7)  # Retry once a week
        
        if datetime.now() - last_failure_time > retry_interval:
            self.logger.debug(f"Entry {entry.key} failed enrichment {(datetime.now() - last_failure_time).days} days ago, will retry")
            return True
        else:
            days_remaining = 7 - (datetime.now() - last_failure_time).days
            self.logger.debug(f"Entry {entry.key} failed recently, will retry in {days_remaining} days")
            return False
    
    def get_metadata(self, entry: BibEntry) -> Optional[Dict]:
        """Retrieve cached metadata for an entry."""
        if not self.is_cached(entry):
            return None
        
        entry_hash = self._get_entry_hash(entry)
        cached_item = self.cache_data[entry_hash]
        
        try:
            # Return metadata as dict
            return cached_item['metadata']
        except Exception as e:
            self.logger.warning(f"Failed to deserialize cached metadata: {e}")
            return None
    
    def store_metadata(self, entry: BibEntry, metadata) -> None:
        """Store enriched metadata in cache."""
        entry_hash = self._get_entry_hash(entry)
        
        # Convert metadata to dict for JSON serialization
        if hasattr(metadata, '__dict__'):
            # If it's an object, convert to dict
            from dataclasses import asdict
            metadata_dict = asdict(metadata) if hasattr(metadata, '__dataclass_fields__') else metadata.__dict__
        else:
            # Already a dict
            metadata_dict = metadata
        
        self.cache_data[entry_hash] = {
            'entry_key': entry.key,
            'entry_title': entry.title,
            'cached_at': datetime.now().isoformat(),
            'metadata': metadata_dict
        }
        
        self.logger.debug(f"Cached metadata for entry: {entry.key}")
    
    def store_failure(self, entry: BibEntry, error_reason: str = None) -> None:
        """Store failed enrichment attempt with timestamp."""
        entry_hash = self._get_entry_hash(entry)
        
        self.cache_data[entry_hash] = {
            'entry_key': entry.key,
            'entry_title': entry.title,
            'cached_at': datetime.now().isoformat(),
            'metadata': None,
            'failed': True,
            'failure_reason': error_reason or 'Enrichment failed',
            'last_failure_at': datetime.now().isoformat()
        }
        
        self.logger.debug(f"Cached failure for entry: {entry.key} - {error_reason}")
    
    def get_uncached_entries(self, entries: list[BibEntry]) -> list[BibEntry]:
        """Filter entries to return only those without valid cached metadata."""
        uncached = []
        for entry in entries:
            if not self.is_cached(entry):
                uncached.append(entry)
        
        self.logger.info(f"Found {len(uncached)} uncached entries out of {len(entries)} total")
        return uncached
    
    def get_retriable_entries(self, entries: list[BibEntry]) -> list[BibEntry]:
        """Filter entries to return only those that should be retried for enrichment.
        
        This includes:
        - Entries that have never been processed
        - Entries with successful metadata that has expired
        - Entries that failed enrichment more than a week ago
        """
        retriable = []
        for entry in entries:
            if self.should_retry_failed_entry(entry):
                retriable.append(entry)
        
        cached_count = len(entries) - len(retriable)
        self.logger.info(f"Found {len(retriable)} retriable entries out of {len(entries)} total ({cached_count} skipped due to recent cache/failures)")
        return retriable
    
    def get_all_cached_metadata(self, entries: list[BibEntry]) -> Dict[str, Dict]:
        """Get all cached metadata for a list of entries."""
        cached_metadata = {}
        
        for entry in entries:
            metadata = self.get_metadata(entry)
            if metadata:
                cached_metadata[entry.key] = metadata
        
        return cached_metadata
    
    def cleanup_expired(self) -> int:
        """Remove expired cache entries and return count of removed items."""
        current_time = datetime.now()
        expired_keys = []
        
        for key, cached_item in self.cache_data.items():
            cache_time = datetime.fromisoformat(cached_item['cached_at'])
            if current_time - cache_time > self.cache_duration:
                expired_keys.append(key)
        
        for key in expired_keys:
            del self.cache_data[key]
        
        if expired_keys:
            self.logger.info(f"Cleaned up {len(expired_keys)} expired cache entries")
        
        return len(expired_keys)
    
    def get_cache_stats(self) -> Dict[str, int]:
        """Get statistics about the cache."""
        total_entries = len(self.cache_data)
        current_time = datetime.now()
        
        expired_count = 0
        failed_count = 0
        successful_count = 0
        failed_retriable_count = 0
        
        for cached_item in self.cache_data.values():
            cache_time = datetime.fromisoformat(cached_item['cached_at'])
            if current_time - cache_time > self.cache_duration:
                expired_count += 1
            
            if cached_item.get('failed', False):
                failed_count += 1
                # Check if this failed entry can be retried
                last_failure_time = datetime.fromisoformat(cached_item.get('last_failure_at', cached_item['cached_at']))
                if current_time - last_failure_time > timedelta(days=7):
                    failed_retriable_count += 1
            else:
                successful_count += 1
        
        return {
            'total_entries': total_entries,
            'valid_entries': total_entries - expired_count,
            'expired_entries': expired_count,
            'successful_entries': successful_count,
            'failed_entries': failed_count,
            'failed_retriable_entries': failed_retriable_count
        }