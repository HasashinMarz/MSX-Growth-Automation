"""
Module 1 — Environment Configuration
======================================
Loads secrets from .env via python-dotenv and validates
all required keys before the pipeline begins.
"""

import os
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

logger = logging.getLogger("msx.env_config")


# ── Config Dataclass ────────────────────────────────────────────────────────
@dataclass
class PipelineConfig:
    # Enricher APIs
    apollo_api_key: Optional[str] = None
    hunter_api_key: Optional[str] = None

    # Google Sheets
    gcp_service_account_path: str = "credentials/gcp_service_account.json"

    # Scraping behaviour
    request_timeout: int = 15          # seconds per HTTP request
    request_delay: float = 1.5         # polite crawl delay in seconds
    max_retries: int = 3               # retry attempts per request

    # Rate-limit back-off
    rate_limit_backoff_base: float = 2.0   # exponential back-off base (seconds)
    rate_limit_backoff_max: float = 60.0   # cap back-off ceiling

    # Extra headers so we look like a real browser
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )


def load_environment(dotenv_path: str = ".env") -> PipelineConfig:
    """
    Load .env file and map values into a PipelineConfig.
    Falls back gracefully if .env is missing (CI/CD environments
    pass secrets via real environment variables instead).
    """
    env_file = Path(dotenv_path)
    if env_file.exists():
        load_dotenv(dotenv_path=env_file, override=False)
        logger.debug(f"Loaded .env from {env_file.resolve()}")
    else:
        logger.warning(
            f".env file not found at '{dotenv_path}'. "
            "Falling back to system environment variables."
        )

    config = PipelineConfig(
        apollo_api_key=os.getenv("APOLLO_API_KEY"),
        hunter_api_key=os.getenv("HUNTER_API_KEY"),
        gcp_service_account_path=os.getenv(
            "GCP_SERVICE_ACCOUNT_PATH", "credentials/gcp_service_account.json"
        ),
        request_timeout=int(os.getenv("REQUEST_TIMEOUT", "15")),
        request_delay=float(os.getenv("REQUEST_DELAY", "1.5")),
        max_retries=int(os.getenv("MAX_RETRIES", "3")),
    )
    return config


def validate_env(config: PipelineConfig, enricher: str = "apollo") -> None:
    """
    Assert that every required credential is present.
    Raises EnvironmentError with a descriptive message listing
    every missing variable so users can fix all issues at once.
    """
    missing: list[str] = []

    # Enricher key check
    if enricher == "apollo" and not config.apollo_api_key:
        missing.append("APOLLO_API_KEY")
    if enricher == "hunter" and not config.hunter_api_key:
        missing.append("HUNTER_API_KEY")

    # Google credentials check
    gcp_path = Path(config.gcp_service_account_path)
    if not gcp_path.exists():
        missing.append(
            f"GCP service account JSON (expected at: {gcp_path.resolve()})"
        )

    if missing:
        bullets = "\n  • ".join(missing)
        raise EnvironmentError(
            f"\n[Module 1] Missing required credentials:\n  • {bullets}\n\n"
            "  Create a .env file based on .env.example and populate the values.\n"
        )

    logger.info("  ✓ All environment variables validated")
