# MSX Automations — Project A: Growth Automation
## Lead Scraper & Enricher Pipeline

```
Terminal Query → Web Scraper → Email Enricher → Google Sheets
```

---

## Project Structure

```
msx_growth_automation/
├── main.py                    ← Pipeline entry point (CLI)
├── modules/
│   ├── __init__.py
│   ├── env_config.py          ← Module 1: Environment & secrets
│   ├── extractor.py           ← Module 2: Web scraping (BS4 + Playwright)
│   ├── enricher.py            ← Module 3: Apollo.io / Hunter.io enrichment
│   ├── delivery.py            ← Module 4: pandas cleaning + gspread upload
│   └── utils.py               ← Shared helpers
├── credentials/
│   └── gcp_service_account.json  ← (you add this — never commit)
├── output/                    ← Auto-created: CSV exports
├── logs/                      ← Auto-created: run logs
├── requirements.txt
├── .env.example               ← Copy to .env and fill in keys
└── README.md
```

---

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure secrets
```bash
cp .env.example .env
# Edit .env and add your API keys
```

### 3. Add Google Service Account
1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a project → Enable **Google Sheets API** and **Google Drive API**
3. Create a **Service Account** → Download the JSON key
4. Place it at `credentials/gcp_service_account.json`
5. Share your target Google Sheet with the service account email (Editor role)

### 4. Run the pipeline
```bash
# Basic usage
python main.py --query "digital marketing agencies Nairobi" --limit 25

# Use Hunter.io instead of Apollo
python main.py --query "fintech startups Lagos" --enricher hunter --limit 50

# Custom sheet name
python main.py --query "law firms Cape Town" --sheet "MSX Client Leads" --worksheet "Q3 2025"

# Skip Google Sheets upload (CSV only)
python main.py --query "tech companies Accra" --no-upload
```

---

## CLI Reference

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--query` | `-q` | *(required)* | Search query e.g. `"agencies Nairobi"` |
| `--limit` | `-l` | `20` | Max leads to scrape |
| `--enricher` | `-e` | `apollo` | `apollo` or `hunter` |
| `--sheet` | `-s` | `MSX Growth Leads` | Google Sheet name |
| `--worksheet` | `-w` | `Leads` | Worksheet/tab name |
| `--no-upload` | | `False` | Skip Sheets, save CSV only |
| `--output-csv` | | `output/leads_export.csv` | Local CSV path |

---

## Output Columns

| Column | Source |
|--------|--------|
| Company Name | Scraped |
| Domain | Scraped |
| Website | Scraped |
| Industry | Scraped |
| City / Country | Scraped |
| Phone | Scraped |
| Address | Scraped |
| Decision Maker Name | Apollo / Hunter |
| Decision Maker Title | Apollo / Hunter |
| Email | Apollo / Hunter |
| Email Confidence % | Apollo / Hunter |
| Email Verified | Apollo / Hunter |
| LinkedIn URL | Apollo / Hunter |
| Description | Scraped |
| Source URL | Scraped |
| Scraped At | System |

---

## Architecture Notes

### Error Handling Strategy
- **HTTP 429 (rate limit):** Exponential back-off (2^n seconds, max 60s) with ±10% jitter
- **Timeouts:** Configurable per-request timeout with retry loop
- **Missing HTML nodes:** `try/except` on every card parse; failed cards are skipped, not crashed
- **Missing API data:** Leads without enrichment are preserved with empty email fields
- **Auth errors (401/403):** Immediate halt with descriptive error message

### Scraping Approach
1. **Primary:** YellowPages HTML (static, reliable structure)
2. **Fallback:** DuckDuckGo HTML results (no API key needed)
3. **JS Fallback:** `PlaywrightLeadExtractor` drop-in (uncomment in `main.py`)

### Deduplication
- By domain (Module 2) — prevents scraping the same company twice
- By domain + company name (Module 4) — pandas-level final dedup

---

## Extending the Pipeline

**Add a new scraping target:**
Add a new entry to `SCRAPE_TARGETS` in `extractor.py` and implement
`_scrape_<source>()` following the YellowPages pattern.

**Add a new enricher:**
Subclass `BaseEnricher` in `enricher.py` and register it in the
`LeadEnricher` factory function.

**Add new output columns:**
Add to `COLUMN_ORDER` and `SHEET_HEADERS` in `delivery.py`,
and populate the field in `_lead_to_dict()`.
