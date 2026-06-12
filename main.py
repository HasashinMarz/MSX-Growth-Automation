"""
MSX Automations — Project A: Growth Automation
Lead Scraper & Enricher Pipeline
================================================
Usage:
    python main.py --query "digital marketing agencies Nairobi" --limit 25
    python main.py --query "fintech startups Lagos" --limit 50 --enricher apollo
    python main.py --query "law firms Cape Town" --sheet "MSX Leads Q3"
"""

import argparse
import sys
import logging
from datetime import datetime

from modules.env_config import load_environment, validate_env
from modules.extractor import LeadExtractor
from modules.enricher import LeadEnricher
from modules.delivery import LeadDelivery

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"logs/pipeline_{datetime.now():%Y%m%d_%H%M%S}.log"),
    ],
)
logger = logging.getLogger("msx.main")


# ── CLI ────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="msx-growth",
        description="MSX Growth Automation — Lead Scraper & Enricher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--query", "-q",
        required=True,
        help='Search query string  e.g. "marketing agencies Nairobi"',
    )
    parser.add_argument(
        "--limit", "-l",
        type=int,
        default=20,
        metavar="N",
        help="Max number of leads to scrape (default: 20)",
    )
    parser.add_argument(
        "--enricher", "-e",
        choices=["apollo", "hunter"],
        default="apollo",
        help="Email-enrichment API to use (default: apollo)",
    )
    parser.add_argument(
        "--sheet", "-s",
        default="MSX Growth Leads",
        metavar="SHEET_NAME",
        help='Google Sheet name to append results to (default: "MSX Growth Leads")',
    )
    parser.add_argument(
        "--worksheet", "-w",
        default="Leads",
        metavar="WORKSHEET",
        help='Worksheet / tab name inside the sheet (default: "Leads")',
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Skip Google Sheets upload — save to CSV only",
    )
    parser.add_argument(
        "--output-csv",
        default="output/leads_export.csv",
        help="Local CSV output path (default: output/leads_export.csv)",
    )
    return parser


# ── Pipeline Orchestrator ───────────────────────────────────────────────────
def run_pipeline(args: argparse.Namespace) -> None:
    logger.info("══════════════════════════════════════════")
    logger.info(" MSX Growth Automation Pipeline — START  ")
    logger.info("══════════════════════════════════════════")
    logger.info(f"Query    : {args.query}")
    logger.info(f"Limit    : {args.limit}")
    logger.info(f"Enricher : {args.enricher}")
    logger.info(f"Sheet    : {args.sheet} / {args.worksheet}")

    # ── Module 1 — Environment ──────────────────────────────────────────────
    logger.info("\n[Module 1] Loading environment…")
    config = load_environment()
    validate_env(config, enricher=args.enricher)

    # ── Module 2 — Extraction ───────────────────────────────────────────────
    logger.info("\n[Module 2] Scraping leads…")
    extractor = LeadExtractor(config=config)
    raw_leads = extractor.scrape(query=args.query, limit=args.limit)
    logger.info(f"  ✓ Extracted {len(raw_leads)} raw leads")

    if not raw_leads:
        logger.warning("No leads found — check your query or scraping target.")
        sys.exit(0)

    # ── Module 3 — Enrichment ───────────────────────────────────────────────
    logger.info("\n[Module 3] Enriching leads…")
    enricher = LeadEnricher(config=config, provider=args.enricher)
    enriched_leads = enricher.enrich_all(raw_leads)
    logger.info(f"  ✓ Enriched {len(enriched_leads)} leads")

    # ── Module 4 — Delivery ─────────────────────────────────────────────────
    logger.info("\n[Module 4] Cleaning & delivering data…")
    delivery = LeadDelivery(config=config, csv_path=args.output_csv)
    df = delivery.prepare(enriched_leads)
    delivery.save_csv(df)
    logger.info(f"  ✓ Saved {len(df)} rows → {args.output_csv}")

    if not args.no_upload:
        rows_added = delivery.upload_to_sheets(
            df=df,
            sheet_name=args.sheet,
            worksheet_name=args.worksheet,
        )
        logger.info(f"  ✓ Uploaded {rows_added} rows → Google Sheet '{args.sheet}'")
    else:
        logger.info("  ↷ Skipped Google Sheets upload (--no-upload flag)")

    logger.info("\n══════════════════════════════════════════")
    logger.info(" MSX Growth Automation Pipeline — DONE   ")
    logger.info("══════════════════════════════════════════\n")


# ── Entry ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import os
    os.makedirs("logs", exist_ok=True)
    os.makedirs("output", exist_ok=True)

    parser = build_parser()
    args = parser.parse_args()
    run_pipeline(args)
