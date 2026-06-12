"""
Module 3 — Lead Enrichment
============================
Enriches raw leads with verified decision-maker contact info
via Apollo.io or Hunter.io REST APIs.

Apollo.io  → People Search API (finds contacts by domain/company)
Hunter.io  → Domain Search + Email Verifier API

Both providers apply exponential back-off on HTTP 429 responses
and gracefully skip leads that cannot be enriched.
"""

import time
import logging
from typing import Optional

import requests

from .env_config import PipelineConfig
from .extractor import RawLead
from .utils import exponential_backoff, clean_text

logger = logging.getLogger("msx.enricher")

# ── Decision-maker seniority filter ─────────────────────────────────────────
DECISION_MAKER_TITLES = {
    "ceo", "chief executive", "founder", "co-founder", "owner",
    "president", "managing director", "md", "director", "vp",
    "vice president", "head of", "chief marketing", "cmo",
    "chief growth", "business development", "bd manager",
    "marketing manager", "growth manager",
}


# ── Base Enricher Interface ──────────────────────────────────────────────────
class BaseEnricher:
    def __init__(self, config: PipelineConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def enrich(self, lead: RawLead) -> RawLead:
        raise NotImplementedError

    def enrich_all(self, leads: list[RawLead]) -> list[RawLead]:
        enriched: list[RawLead] = []
        total = len(leads)
        for idx, lead in enumerate(leads, 1):
            logger.info(
                f"  Enriching [{idx}/{total}]: {lead.company_name} ({lead.domain})"
            )
            try:
                enriched_lead = self.enrich(lead)
                enriched.append(enriched_lead)
            except Exception as exc:
                logger.warning(f"    ↷ Skipped enrichment: {exc}")
                enriched.append(lead)  # keep raw data even if enrichment fails
            # Polite delay between API calls
            time.sleep(self.config.request_delay)
        return enriched

    # ── Shared HTTP helper ──────────────────────────────────────────────────
    def _get(self, url: str, params: dict = None, headers: dict = None) -> Optional[dict]:
        for attempt in range(1, self.config.max_retries + 1):
            try:
                resp = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self.config.request_timeout,
                )
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 429:
                    wait = exponential_backoff(
                        attempt,
                        base=self.config.rate_limit_backoff_base,
                        ceiling=self.config.rate_limit_backoff_max,
                    )
                    logger.warning(f"    Rate-limited (429). Waiting {wait:.1f}s…")
                    time.sleep(wait)
                    continue
                if resp.status_code in (401, 403):
                    logger.error(
                        f"    Auth error {resp.status_code} — check API key."
                    )
                    return None
                logger.debug(f"    HTTP {resp.status_code} from {url[:60]}")
                return None
            except requests.exceptions.Timeout:
                logger.warning(f"    Timeout attempt {attempt}/{self.config.max_retries}")
                if attempt < self.config.max_retries:
                    time.sleep(exponential_backoff(attempt))
            except requests.exceptions.ConnectionError as exc:
                logger.error(f"    Connection error: {exc}")
                return None
        return None

    def _post(self, url: str, payload: dict, headers: dict = None) -> Optional[dict]:
        for attempt in range(1, self.config.max_retries + 1):
            try:
                resp = self.session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=self.config.request_timeout,
                )
                if resp.status_code in (200, 201):
                    return resp.json()
                if resp.status_code == 429:
                    wait = exponential_backoff(
                        attempt,
                        base=self.config.rate_limit_backoff_base,
                        ceiling=self.config.rate_limit_backoff_max,
                    )
                    logger.warning(f"    Rate-limited (429). Waiting {wait:.1f}s…")
                    time.sleep(wait)
                    continue
                logger.debug(f"    POST HTTP {resp.status_code} from {url[:60]}")
                return None
            except requests.exceptions.Timeout:
                logger.warning(f"    Timeout attempt {attempt}/{self.config.max_retries}")
                if attempt < self.config.max_retries:
                    time.sleep(exponential_backoff(attempt))
            except requests.exceptions.ConnectionError as exc:
                logger.error(f"    Connection error: {exc}")
                return None
        return None


# ── Apollo.io Enricher ───────────────────────────────────────────────────────
class ApolloEnricher(BaseEnricher):
    """
    Uses Apollo.io People Search API to find decision-makers by domain.
    Docs: https://apolloio.github.io/apollo-api-docs/
    """

    BASE_URL = "https://api.apollo.io/v1"

    def __init__(self, config: PipelineConfig):
        super().__init__(config)
        self.api_key = config.apollo_api_key

    def enrich(self, lead: RawLead) -> RawLead:
        if not lead.domain:
            logger.debug(f"    No domain for '{lead.company_name}' — skipping.")
            return lead

        contacts = self._search_people(lead.domain, lead.company_name)
        if not contacts:
            logger.debug(f"    No Apollo contacts for {lead.domain}")
            return lead

        # Pick the highest-ranking decision maker
        decision_maker = self._pick_decision_maker(contacts)
        if not decision_maker:
            # Fall back to first contact if no clear DM found
            decision_maker = contacts[0]

        lead.decision_maker_name = (
            f"{decision_maker.get('first_name', '')} "
            f"{decision_maker.get('last_name', '')}".strip()
        )
        lead.decision_maker_title = decision_maker.get("title", "")
        lead.linkedin_url = decision_maker.get("linkedin_url", "")

        # Email — Apollo may return it directly or require a reveal call
        email = decision_maker.get("email", "")
        if email and "@" in email:
            lead.email = email
            lead.email_confidence = float(
                decision_maker.get("email_confidence", 0.75)
            )
            lead.email_verified = lead.email_confidence >= 0.7
            logger.info(
                f"    ✓ Email found: {lead.email} "
                f"(confidence {lead.email_confidence:.0%})"
            )
        else:
            # Attempt to reveal via /people/match
            lead = self._reveal_email(lead, decision_maker)

        return lead

    def _search_people(self, domain: str, company_name: str) -> list[dict]:
        """POST /mixed_people/search — returns a list of person objects."""
        payload = {
            "api_key": self.api_key,
            "q_organization_domains": domain,
            "person_titles": list(DECISION_MAKER_TITLES),
            "page": 1,
            "per_page": 5,
        }
        data = self._post(f"{self.BASE_URL}/mixed_people/search", payload)
        if not data:
            return []
        return data.get("people", [])

    def _reveal_email(self, lead: RawLead, person: dict) -> RawLead:
        """POST /people/match — reveals email for a specific person ID."""
        person_id = person.get("id")
        if not person_id:
            return lead

        payload = {
            "api_key": self.api_key,
            "id": person_id,
            "reveal_personal_emails": False,
        }
        data = self._post(f"{self.BASE_URL}/people/match", payload)
        if not data:
            return lead

        person_data = data.get("person", {})
        email = person_data.get("email", "")
        if email and "@" in email:
            lead.email = email
            lead.email_confidence = float(
                person_data.get("email_confidence", 0.70)
            )
            lead.email_verified = lead.email_confidence >= 0.7
            logger.info(
                f"    ✓ Revealed email: {lead.email} "
                f"(confidence {lead.email_confidence:.0%})"
            )
        return lead

    @staticmethod
    def _pick_decision_maker(contacts: list[dict]) -> Optional[dict]:
        """Return the most senior contact based on title keywords."""
        priority_order = [
            "ceo", "founder", "owner", "president",
            "managing director", "cmo", "vp", "director",
        ]
        for keyword in priority_order:
            for contact in contacts:
                title = (contact.get("title") or "").lower()
                if keyword in title:
                    return contact
        return None


# ── Hunter.io Enricher ───────────────────────────────────────────────────────
class HunterEnricher(BaseEnricher):
    """
    Uses Hunter.io Domain Search + Email Verifier APIs.
    Docs: https://hunter.io/api-documentation/v2
    """

    BASE_URL = "https://api.hunter.io/v2"

    def __init__(self, config: PipelineConfig):
        super().__init__(config)
        self.api_key = config.hunter_api_key

    def enrich(self, lead: RawLead) -> RawLead:
        if not lead.domain:
            logger.debug(f"    No domain for '{lead.company_name}' — skipping.")
            return lead

        # Step 1: Find emails via domain search
        emails_data = self._domain_search(lead.domain)
        if not emails_data:
            return lead

        emails = emails_data.get("emails", [])
        if not emails:
            logger.debug(f"    No Hunter emails for {lead.domain}")
            return lead

        # Step 2: Pick best contact (decision-maker heuristic)
        contact = self._pick_decision_maker(emails) or emails[0]

        lead.decision_maker_name = (
            f"{contact.get('first_name', '')} "
            f"{contact.get('last_name', '')}".strip()
        )
        lead.decision_maker_title = contact.get("position", "")
        lead.linkedin_url = contact.get("linkedin", "")

        raw_email = contact.get("value", "")
        if raw_email and "@" in raw_email:
            confidence = contact.get("confidence", 0) / 100.0
            # Step 3: Verify the email explicitly
            verified, score = self._verify_email(raw_email)
            lead.email = raw_email
            lead.email_confidence = score if score > 0 else confidence
            lead.email_verified = verified
            logger.info(
                f"    ✓ Email: {lead.email} "
                f"| verified={lead.email_verified} "
                f"| confidence={lead.email_confidence:.0%}"
            )

        return lead

    def _domain_search(self, domain: str) -> Optional[dict]:
        params = {
            "domain": domain,
            "api_key": self.api_key,
            "limit": 5,
            "type": "personal",  # focus on work emails
        }
        data = self._get(f"{self.BASE_URL}/domain-search", params=params)
        return data.get("data") if data else None

    def _verify_email(self, email: str) -> tuple[bool, float]:
        """
        Returns (is_deliverable, confidence_score).
        Hunter verification result: 'deliverable' / 'risky' / 'undeliverable'
        """
        params = {"email": email, "api_key": self.api_key}
        data = self._get(f"{self.BASE_URL}/email-verifier", params=params)
        if not data:
            return False, 0.0

        result = data.get("data", {})
        status = result.get("status", "unknown")
        score = result.get("score", 0) / 100.0

        is_deliverable = status == "deliverable"
        return is_deliverable, score

    @staticmethod
    def _pick_decision_maker(contacts: list[dict]) -> Optional[dict]:
        for keyword in ["ceo", "founder", "director", "owner", "manager"]:
            for c in contacts:
                pos = (c.get("position") or "").lower()
                if keyword in pos:
                    return c
        return None


# ── Factory ──────────────────────────────────────────────────────────────────
def LeadEnricher(config: PipelineConfig, provider: str = "apollo") -> BaseEnricher:
    """
    Factory function — returns the correct enricher implementation.
    Usage: enricher = LeadEnricher(config, provider="apollo")
    """
    if provider == "apollo":
        logger.info("  Using Apollo.io enricher")
        return ApolloEnricher(config)
    elif provider == "hunter":
        logger.info("  Using Hunter.io enricher")
        return HunterEnricher(config)
    else:
        raise ValueError(
            f"Unknown enricher provider '{provider}'. "
            "Choose 'apollo' or 'hunter'."
        )
