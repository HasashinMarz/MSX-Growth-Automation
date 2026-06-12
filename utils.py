"""
Shared Utilities
=================
Small helper functions used across all pipeline modules.
"""

import re
import time
import math
import urllib.parse
from typing import Optional
from bs4 import Tag


def clean_text(element) -> str:
    """
    Extract and normalise text from a BeautifulSoup Tag or raw string.
    Returns an empty string if element is None.
    """
    if element is None:
        return ""
    if isinstance(element, Tag):
        text = element.get_text(separator=" ", strip=True)
    else:
        text = str(element)
    # Collapse internal whitespace
    return re.sub(r"\s+", " ", text).strip()


def extract_domain(url: str) -> str:
    """
    Extract the bare domain (e.g. 'acme.com') from any URL or
    domain-like string.  Returns empty string on failure.
    """
    if not url:
        return ""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        # Strip 'www.' prefix
        domain = re.sub(r"^www\.", "", host).lower()
        return domain
    except Exception:
        return ""


def exponential_backoff(
    attempt: int,
    base: float = 2.0,
    ceiling: float = 60.0,
    jitter: bool = True,
) -> float:
    """
    Calculate back-off wait time: base^attempt seconds, capped at ceiling.
    Adds ±10 % random jitter by default to avoid thundering-herd.
    """
    import random
    wait = min(base ** attempt, ceiling)
    if jitter:
        wait *= 0.9 + random.random() * 0.2   # ±10%
    return wait


def is_valid_email(email: str) -> bool:
    """Lightweight structural email validation."""
    pattern = r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
    return bool(re.match(pattern, email or ""))


def slugify(text: str) -> str:
    """Convert a string to a lowercase, hyphen-separated slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text
