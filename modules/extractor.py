"""
Module 2 — Lead Extraction (Web Scraper)
==========================================
Scrapes business directory pages for initial lead data:
  • Company name
  • Website / domain
  • Phone number
  • Address / location
  • Industry / category

Targets: YellowPages-style directories, Google Maps snippets,
         and generic business listing pages — chosen based on
         the search query supplied via CLI.

Uses: requests + BeautifulSoup4 (lightweight, no JS required).
For JS-heavy pages, a Playwright fallback is also provided.
"""

import re
import time
import logging
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup

from .env_config import PipelineConfig
from .utils import exponential_backoff, clean_text, extract_domain

logger = logging.getLogger("msx.extractor")

# ── Lead Dataclass ──────────────────────────────────────────────────────────
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
    # Populated later by Module 3
    decision_maker_name: str = ""
    decision_maker_title: str = ""
    email: str = ""
    email_confidence: float = 0.0
    email_verified: bool = False
    linkedin_url: str = ""


# ── Scraping Targets Configuration ─────────────────────────────────────────
SCRAPE_TARGETS = {
    "yellow_pages": {
        "search_url": "https://www.yellowpages.com/search?search_terms={query}&geo_location_terms={location}",
        "result_selector": ".result",
        "name_selector": ".business-name span",
        "phone_selector": ".phones",
        "address_selector": ".street-address",
        "city_selector": ".locality",
        "website_selector": "a.track-visit-website",
        "description_selector": ".snippet",
    },
    "yelp": {
        "search_url": "https://www.yelp.com/search?find_desc={query}&find_loc={location}",
        "result_selector": '[data-testid="serp-ia-card"]',
        "name_selector": "a.css-166la90",
        "phone_selector": "p.css-1p9ibgf",
        "address_selector": "address",
        "city_selector": None,
        "website_selector": 'a[href*="biz_redir"]',
        "description_selector": "p.css-1b3XXXX",  # update per Yelp version
    },
}


# ── Main Extractor ──────────────────────────────────────────────────────────
class LeadExtractor:
    """
    Orchestrates multi-source scraping and deduplicates results.
    """

    def __init__(self, config: PipelineConfig):
        self.config = config
        self.session = self._build_session()

    # ── Session ─────────────────────────────────────────────────────────────
    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "User-Agent": self.config.user_agent,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        return session

    # ── Public Interface ─────────────────────────────────────────────────────
    def scrape(self, query: str, limit: int = 20) -> list[RawLead]:
        """
        Main entry point. Parses query into (terms, location),
        fans out to multiple scrapers, deduplicates by domain.
        """
        terms, location = self._parse_query(query)
        logger.info(f"  Search terms: '{terms}' | Location: '{location}'")

        all_leads: list[RawLead] = []

        # Primary: Yellow Pages (reliable HTML structure)
        yp_leads = self._scrape_yellowpages(terms, location, limit=limit)
        all_leads.extend(yp_leads)
        logger.info(f"  YellowPages → {len(yp_leads)} leads")

        # Secondary: Generic Google-snippet fallback if YP under-delivers
        if len(all_leads) < limit:
            remaining = limit - len(all_leads)
            google_leads = self._scrape_google_snippets(query, limit=remaining)
            all_leads.extend(google_leads)
            logger.info(f"  Google snippets → {len(google_leads)} leads")

        deduped = self._deduplicate(all_leads)
        logger.info(f"  After dedup: {len(deduped)} unique leads")
        return deduped[:limit]

    # ── Query Parser ─────────────────────────────────────────────────────────
    @staticmethod
    def _parse_query(query: str) -> tuple[str, str]:
        """
        Heuristic: last 1-2 words that look like a city/country
        become `location`; everything else is `terms`.
        Pattern: "<business type> <city>" or "<type> in <city>".
        """
        query = query.strip()
        # "X in Y" pattern
        if " in " in query.lower():
            parts = re.split(r"\s+in\s+", query, maxsplit=1, flags=re.IGNORECASE)
            return parts[0].strip(), parts[1].strip()

        # Fallback: last word = location
        tokens = query.split()
        if len(tokens) >= 2:
            return " ".join(tokens[:-1]), tokens[-1]
        return query, ""

    # ── Yellow Pages Scraper ─────────────────────────────────────────────────
    def _scrape_yellowpages(
        self, terms: str, location: str, limit: int = 20
    ) -> list[RawLead]:
        leads: list[RawLead] = []
        page = 1
        cfg = SCRAPE_TARGETS["yellow_pages"]

        while len(leads) < limit:
            url = cfg["search_url"].format(
                query=urllib.parse.quote_plus(terms),
                location=urllib.parse.quote_plus(location),
            )
            if page > 1:
                url += f"&page={page}"

            soup = self._fetch_html(url, source="yellowpages")
            if soup is None:
                break

            results = soup.select(cfg["result_selector"])
            if not results:
                logger.debug("  YP: No more results found.")
                break

            for card in results:
                if len(leads) >= limit:
                    break
                lead = self._parse_yp_card(card, cfg, base_url=url)
                if lead:
                    leads.append(lead)

            page += 1
            time.sleep(self.config.request_delay)

        return leads

    def _parse_yp_card(
        self, card: BeautifulSoup, cfg: dict, base_url: str
    ) -> Optional[RawLead]:
        try:
            name = clean_text(card.select_one(cfg["name_selector"]))
            if not name:
                return None

            phone_el = card.select_one(cfg["phone_selector"])
            address_el = card.select_one(cfg["address_selector"])
            city_el = card.select_one(cfg["city_selector"]) if cfg["city_selector"] else None
            website_el = card.select_one(cfg["website_selector"])
            desc_el = card.select_one(cfg["description_selector"])

            website = ""
            if website_el:
                href = website_el.get("href", "")
                # YP wraps outbound clicks; extract real URL from query param
                if "redirect" in href or "track" in href:
                    match = re.search(r"url=([^&]+)", href)
                    website = urllib.parse.unquote(match.group(1)) if match else href
                else:
                    website = href

            domain = extract_domain(website)
            city_text = clean_text(city_el) if city_el else ""

            return RawLead(
                company_name=name,
                website=website,
                domain=domain,
                phone=clean_text(phone_el),
                address=clean_text(address_el),
                city=city_text,
                description=clean_text(desc_el),
                source_url=base_url,
            )

        except Exception as exc:
            logger.debug(f"  Error parsing YP card: {exc}")
            return None

    # ── Google Snippet Scraper (fallback / enrichment) ───────────────────────
    def _scrape_google_snippets(
        self, query: str, limit: int = 10
    ) -> list[RawLead]:
        """
        Scrapes DuckDuckGo HTML results as a Google-free fallback.
        Extracts title + snippet + domain from organic results.
        """
        leads: list[RawLead] = []
        url = (
            "https://html.duckduckgo.com/html/?q="
            + urllib.parse.quote_plus(query + " official website")
        )

        soup = self._fetch_html(url, source="duckduckgo")
        if soup is None:
            return leads

        results = soup.select(".result__body")
        for result in results[:limit]:
            try:
                title_el = result.select_one(".result__title a")
                snippet_el = result.select_one(".result__snippet")
                url_el = result.select_one(".result__url")

                name = clean_text(title_el)
                if not name:
                    continue

                raw_url = clean_text(url_el)
                domain = extract_domain(raw_url)

                leads.append(RawLead(
                    company_name=name,
                    website=f"https://{domain}" if domain else "",
                    domain=domain,
                    description=clean_text(snippet_el),
                    source_url=url,
                ))
            except Exception as exc:
                logger.debug(f"  DDG card parse error: {exc}")
                continue

        return leads

    # ── HTTP Fetch Helper ────────────────────────────────────────────────────
    def _fetch_html(
        self, url: str, source: str = "web"
    ) -> Optional[BeautifulSoup]:
        """
        GET with retry + exponential back-off on 429/5xx.
        Returns a BeautifulSoup object or None on failure.
        """
        for attempt in range(1, self.config.max_retries + 1):
            try:
                logger.debug(f"  [{source}] GET {url[:80]}… (attempt {attempt})")
                response = self.session.get(
                    url,
                    timeout=self.config.request_timeout,
                    allow_redirects=True,
                )

                if response.status_code == 429:
                    wait = exponential_backoff(
                        attempt,
                        base=self.config.rate_limit_backoff_base,
                        ceiling=self.config.rate_limit_backoff_max,
                    )
                    logger.warning(
                        f"  [{source}] Rate-limited (429). "
                        f"Backing off {wait:.1f}s…"
                    )
                    time.sleep(wait)
                    continue

                if response.status_code == 200:
                    return BeautifulSoup(response.text, "html.parser")

                logger.warning(
                    f"  [{source}] HTTP {response.status_code} for {url[:60]}"
                )
                return None

            except requests.exceptions.Timeout:
                logger.warning(
                    f"  [{source}] Timeout on attempt {attempt}/{self.config.max_retries}"
                )
                if attempt < self.config.max_retries:
                    time.sleep(exponential_backoff(attempt))

            except requests.exceptions.ConnectionError as exc:
                logger.error(f"  [{source}] Connection error: {exc}")
                return None

            except Exception as exc:
                logger.error(f"  [{source}] Unexpected error: {exc}")
                return None

        logger.error(f"  [{source}] All {self.config.max_retries} attempts failed.")
        return None

    # ── Deduplication ────────────────────────────────────────────────────────
    @staticmethod
    def _deduplicate(leads: list[RawLead]) -> list[RawLead]:
        """Remove duplicate companies by domain, falling back to company name."""
        seen: set[str] = set()
        unique: list[RawLead] = []
        for lead in leads:
            key = lead.domain or lead.company_name.lower().strip()
            if key and key not in seen:
                seen.add(key)
                unique.append(lead)
        return unique


# ── Playwright Fallback (for JS-rendered pages) ─────────────────────────────
class PlaywrightLeadExtractor(LeadExtractor):
    """
    Drop-in replacement for JS-heavy directories.
    Install: pip install playwright && playwright install chromium
    """

    def _fetch_html(
        self, url: str, source: str = "playwright"
    ) -> Optional[BeautifulSoup]:
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
                        page.goto(url, timeout=self.config.request_timeout * 1000)
                        page.wait_for_load_state("networkidle", timeout=15_000)
                        html = page.content()
                        browser.close()
                        return BeautifulSoup(html, "html.parser")
                    except PWTimeout:
                        logger.warning(
                            f"  [PW] Timeout attempt {attempt}/{self.config.max_retries}"
                        )
                        if attempt < self.config.max_retries:
                            time.sleep(exponential_backoff(attempt))
                browser.close()
        except ImportError:
            logger.error(
                "Playwright not installed. "
                "Run: pip install playwright && playwright install chromium"
            )
        except Exception as exc:
            logger.error(f"  [PW] Unexpected error: {exc}")
        return None
