"""
Module 2 — Lead Extraction (Web Scraper)
==========================================
Scrapes Bing search results for initial lead data:
  • Company name
  • Website / domain
  • Description

Targets: Bing organic results (b_algo) which works globally.
Uses: Playwright (headless Chromium) to bypass basic bot protection.
"""

import time
import logging
import urllib.parse
from dataclasses import dataclass
from typing import Optional

from bs4 import BeautifulSoup

from .env_config import PipelineConfig
from .utils import exponential_backoff, clean_text, extract_domain

logger = logging.getLogger("msx.extractor")

@dataclass
class RawLead:
    company_name: str = ""
    website: str = ""
    domain: str = ""
    phone: str = ""
    address: str = ""
    city: str = ""
    country: str = ""
    industry: str = ""
    description: str = ""
    source_url: str = ""
    decision_maker_name: str = ""
    decision_maker_title: str = ""
    email: str = ""
    email_confidence: float = 0.0
    email_verified: bool = False
    linkedin_url: str = ""

class PlaywrightLeadExtractor:
    """
    Primary Extractor using Playwright to bypass bot blocks.
    Targets Bing to ensure global search coverage.
    """

    def __init__(self, config: PipelineConfig):
        self.config = config

    def scrape(self, query: str, limit: int = 20) -> list[RawLead]:
        logger.info(f"  Executing global search for: '{query}'")
        
        # We use Bing as the primary engine for the MVP
        all_leads = self._scrape_bing(query, limit=limit)
        
        deduped = self._deduplicate(all_leads)
        logger.info(f"  After dedup: {len(deduped)} unique leads")
        return deduped[:limit]

    def _scrape_bing(self, query: str, limit: int = 20) -> list[RawLead]:
        leads: list[RawLead] = []
        # Append "official website" to push direct business links to the top
        search_query = f"{query} official website"
        url = f"https://www.bing.com/search?q={urllib.parse.quote_plus(search_query)}"
        
        soup = self._fetch_html(url)
        if not soup:
            return leads

        # Bing search result cards sit inside <li class="b_algo">
        results = soup.select("li.b_algo")
        if not results:
            logger.warning("  Bing returned no organic results. Might be rate-limited.")
            
        for result in results:
            if len(leads) >= limit:
                break
                
            try:
                title_el = result.select_one("h2 a")
                snippet_el = result.select_one(".b_caption p")
                
                name = clean_text(title_el)
                if not name:
                    continue
                    
                raw_url = title_el.get("href", "")
                domain = extract_domain(raw_url)
                
                # Filter out massive directories so we only pass real companies to Apollo
                if any(directory in domain for directory in ["yellowpages", "facebook", "linkedin", "yelp", "instagram", "yellow.co"]):
                    continue

                leads.append(RawLead(
                    company_name=name,
                    website=raw_url,
                    domain=domain,
                    description=clean_text(snippet_el),
                    source_url=url,
                ))
            except Exception as exc:
                logger.debug(f"  Bing card parse error: {exc}")
                continue

        return leads

    def _fetch_html(self, url: str) -> Optional[BeautifulSoup]:
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(
                    user_agent=self.config.user_agent,
                    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                )
                
                for attempt in range(1, self.config.max_retries + 1):
                    try:
                        logger.debug(f"  [Playwright] GET {url[:80]}… (attempt {attempt})")
                        
                        # CRITICAL FIX: Wait for DOM, not Network Idle
                        page.goto(url, wait_until="domcontentloaded", timeout=self.config.request_timeout * 1000)
                        
                        # Hard wait 2.5 seconds to ensure snippet text renders
                        page.wait_for_timeout(2500)
                        
                        html = page.content()
                        browser.close()
                        return BeautifulSoup(html, "html.parser")
                        
                    except PWTimeout:
                        logger.warning(f"  [PW] Timeout attempt {attempt}/{self.config.max_retries}")
                        if attempt < self.config.max_retries:
                            time.sleep(exponential_backoff(attempt))
                            
                browser.close()
        except ImportError:
            logger.error("Playwright not installed. Run: pip install playwright && playwright install chromium")
        except Exception as exc:
            logger.error(f"  [PW] Unexpected error: {exc}")
            
        return None

    @staticmethod
    def _deduplicate(leads: list[RawLead]) -> list[RawLead]:
        """Remove duplicate companies by domain."""
        seen: set[str] = set()
        unique: list[RawLead] = []
        for lead in leads:
            key = lead.domain or lead.company_name.lower().strip()
            if key and key not in seen:
                seen.add(key)
                unique.append(lead)
        return unique