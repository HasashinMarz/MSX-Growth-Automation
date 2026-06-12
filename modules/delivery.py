"""
Module 4 — Data Cleaning & Delivery
======================================
Cleans enriched leads with pandas and uploads rows
to a live Google Sheet via gspread.

Google Sheets authentication uses a Service Account JSON key
(OAuth2 server-to-server — no browser prompt required).
"""

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import gspread
from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound
from google.oauth2.service_account import Credentials

from .env_config import PipelineConfig
from .extractor import RawLead
from .utils import exponential_backoff

logger = logging.getLogger("msx.delivery")

# ── Google API scopes ────────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# ── Final column order for the sheet ────────────────────────────────────────
COLUMN_ORDER = [
    "company_name",
    "domain",
    "website",
    "industry",
    "city",
    "country",
    "phone",
    "address",
    "decision_maker_name",
    "decision_maker_title",
    "email",
    "email_confidence_pct",
    "email_verified",
    "linkedin_url",
    "description",
    "source_url",
    "scraped_at",
]

SHEET_HEADERS = [col.replace("_", " ").title() for col in COLUMN_ORDER]


# ── Delivery Class ───────────────────────────────────────────────────────────
class LeadDelivery:
    def __init__(self, config: PipelineConfig, csv_path: str = "output/leads_export.csv"):
        self.config = config
        self.csv_path = csv_path
        self._gc: Optional[gspread.Client] = None  # lazy-initialised

    # ── Data Preparation ─────────────────────────────────────────────────────
    def prepare(self, leads: list[RawLead]) -> pd.DataFrame:
        """
        Convert RawLead objects → clean, de-duped pandas DataFrame.
        """
        if not leads:
            logger.warning("  No leads to prepare — returning empty DataFrame.")
            return pd.DataFrame(columns=COLUMN_ORDER)

        rows = [self._lead_to_dict(lead) for lead in leads]
        df = pd.DataFrame(rows)

        df = self._clean(df)
        logger.info(f"  DataFrame shape after cleaning: {df.shape}")
        return df

    def _lead_to_dict(self, lead: RawLead) -> dict:
        return {
            "company_name": lead.company_name,
            "domain": lead.domain,
            "website": lead.website,
            "industry": lead.industry,
            "city": lead.city,
            "country": lead.country,
            "phone": lead.phone,
            "address": lead.address,
            "decision_maker_name": lead.decision_maker_name,
            "decision_maker_title": lead.decision_maker_title,
            "email": lead.email,
            "email_confidence_pct": (
                round(lead.email_confidence * 100, 1) if lead.email_confidence else None
            ),
            "email_verified": lead.email_verified,
            "linkedin_url": lead.linkedin_url,
            "description": lead.description,
            "source_url": lead.source_url,
            "scraped_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        }

    def _clean(self, df: pd.DataFrame) -> pd.DataFrame:
        """Apply all cleaning transformations."""
        # ── 1. Strip whitespace from all string columns ──────────────────────
        str_cols = df.select_dtypes(include="object").columns
        for col in str_cols:
            df[col] = df[col].astype(str).str.strip()
            df[col] = df[col].replace({"None": "", "nan": "", "NaN": ""})

        # ── 2. Normalise company name: title case ────────────────────────────
        df["company_name"] = df["company_name"].str.title()

        # ── 3. Normalise email: lowercase ────────────────────────────────────
        df["email"] = df["email"].str.lower()

        # ── 4. Remove rows without a company name ────────────────────────────
        df = df[df["company_name"].str.len() > 0].copy()

        # ── 5. Drop rows with obviously invalid emails ───────────────────────
        mask_email = df["email"].str.contains("@", na=False)
        invalid_email_count = (~mask_email & df["email"].str.len().gt(0)).sum()
        if invalid_email_count:
            logger.debug(f"  Dropping {invalid_email_count} rows with malformed emails")
        df.loc[~mask_email, "email"] = ""

        # ── 6. Deduplicate by domain; keep row with email if possible ─────────
        df = df.sort_values(
            ["domain", "email"],
            key=lambda s: s.str.len(),
            ascending=False,
        )
        df = df.drop_duplicates(subset=["domain"], keep="first")
        df = df.drop_duplicates(subset=["company_name"], keep="first")
        df = df.reset_index(drop=True)

        # ── 7. Ensure column order ────────────────────────────────────────────
        for col in COLUMN_ORDER:
            if col not in df.columns:
                df[col] = ""
        df = df[COLUMN_ORDER]

        # ── 8. Fill remaining NaN ─────────────────────────────────────────────
        df = df.fillna("")

        return df

    # ── CSV Export ───────────────────────────────────────────────────────────
    def save_csv(self, df: pd.DataFrame) -> None:
        Path(self.csv_path).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(self.csv_path, index=False, encoding="utf-8-sig")
        logger.info(f"  ✓ CSV saved: {self.csv_path} ({len(df)} rows)")

    # ── Google Sheets Upload ─────────────────────────────────────────────────
    def upload_to_sheets(
        self,
        df: pd.DataFrame,
        sheet_name: str,
        worksheet_name: str = "Leads",
    ) -> int:
        """
        Append all rows in df to the target Google Sheet.
        Creates the sheet/worksheet if they don't exist.
        Returns the number of rows appended.
        """
        gc = self._get_gspread_client()
        spreadsheet = self._get_or_create_spreadsheet(gc, sheet_name)
        worksheet = self._get_or_create_worksheet(spreadsheet, worksheet_name)

        self._ensure_header_row(worksheet)

        rows_to_append = df.values.tolist()
        if not rows_to_append:
            logger.warning("  No rows to append.")
            return 0

        # Batch append with retry on API errors
        appended = self._append_rows_with_retry(worksheet, rows_to_append)
        return appended

    def _get_gspread_client(self) -> gspread.Client:
        if self._gc is not None:
            return self._gc
        try:
            creds = Credentials.from_service_account_file(
                self.config.gcp_service_account_path,
                scopes=SCOPES,
            )
            self._gc = gspread.authorize(creds)
            logger.info("  ✓ Google Sheets client authenticated")
            return self._gc
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Service account JSON not found at: "
                f"{self.config.gcp_service_account_path}\n"
                "Download it from Google Cloud Console → "
                "IAM & Admin → Service Accounts."
            )
        except Exception as exc:
            raise RuntimeError(f"gspread auth failed: {exc}") from exc

    def _get_or_create_spreadsheet(
        self, gc: gspread.Client, sheet_name: str
    ) -> gspread.Spreadsheet:
        try:
            spreadsheet = gc.open(sheet_name)
            logger.info(f"  ✓ Opened existing sheet: '{sheet_name}'")
        except SpreadsheetNotFound:
            spreadsheet = gc.create(sheet_name)
            # Make it accessible to anyone with the link (optional)
            spreadsheet.share(
                None,
                perm_type="anyone",
                role="writer",
                notify=False,
                with_link=True,
            )
            logger.info(f"  ✓ Created new sheet: '{sheet_name}'")
        return spreadsheet

    def _get_or_create_worksheet(
        self, spreadsheet: gspread.Spreadsheet, name: str
    ) -> gspread.Worksheet:
        try:
            ws = spreadsheet.worksheet(name)
            logger.info(f"  ✓ Using worksheet: '{name}'")
        except WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=name, rows=5000, cols=len(COLUMN_ORDER))
            logger.info(f"  ✓ Created worksheet: '{name}'")
        return ws

    def _ensure_header_row(self, worksheet: gspread.Worksheet) -> None:
        """
        Write SHEET_HEADERS to row 1 only if the sheet is empty.
        Bold + frozen headers for readability.
        """
        try:
            existing = worksheet.row_values(1)
            if not existing:
                worksheet.append_row(
                    SHEET_HEADERS,
                    value_input_option="USER_ENTERED",
                )
                # Freeze header row
                worksheet.freeze(rows=1)
                # Bold header row via Sheets API batch update
                spreadsheet = worksheet.spreadsheet
                spreadsheet.batch_update({
                    "requests": [{
                        "repeatCell": {
                            "range": {
                                "sheetId": worksheet.id,
                                "startRowIndex": 0,
                                "endRowIndex": 1,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "textFormat": {"bold": True},
                                    "backgroundColor": {
                                        "red": 0.18,
                                        "green": 0.18,
                                        "blue": 0.18,
                                    },
                                    "textFormat": {
                                        "bold": True,
                                        "foregroundColor": {
                                            "red": 1.0,
                                            "green": 1.0,
                                            "blue": 1.0,
                                        },
                                    },
                                }
                            },
                            "fields": "userEnteredFormat(textFormat,backgroundColor)",
                        }
                    }]
                })
                logger.info("  ✓ Header row written and styled")
        except APIError as exc:
            logger.warning(f"  Could not write headers: {exc}")

    def _append_rows_with_retry(
        self, worksheet: gspread.Worksheet, rows: list[list]
    ) -> int:
        """
        Append rows in chunks of 500; retry on quota/API errors.
        """
        CHUNK_SIZE = 500
        total_appended = 0

        for i in range(0, len(rows), CHUNK_SIZE):
            chunk = rows[i: i + CHUNK_SIZE]

            for attempt in range(1, self.config.max_retries + 1):
                try:
                    worksheet.append_rows(
                        chunk,
                        value_input_option="USER_ENTERED",
                        insert_data_option="INSERT_ROWS",
                    )
                    total_appended += len(chunk)
                    logger.debug(
                        f"  Appended chunk {i // CHUNK_SIZE + 1} "
                        f"({len(chunk)} rows)"
                    )
                    break

                except APIError as exc:
                    status = getattr(exc.response, "status_code", 0)
                    if status == 429:
                        wait = exponential_backoff(
                            attempt,
                            base=self.config.rate_limit_backoff_base,
                            ceiling=self.config.rate_limit_backoff_max,
                        )
                        logger.warning(
                            f"  Sheets quota hit. Backing off {wait:.1f}s…"
                        )
                        time.sleep(wait)
                    else:
                        logger.error(f"  Sheets APIError: {exc}")
                        break

                except Exception as exc:
                    logger.error(f"  Unexpected Sheets error: {exc}")
                    break

            # Small sleep between chunks to stay within Sheets write quota
            time.sleep(1.5)

        logger.info(f"  ✓ Total rows appended: {total_appended}")
        return total_appended
