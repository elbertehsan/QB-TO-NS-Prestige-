# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is a **QuickBooks Desktop → NetSuite sync integration** for the Prestige subsidiary (subsidiary ID `15`). A SOAP server listens for the QuickBooks Web Connector (QBWC), fetches General Ledger journal entries day-by-day, transforms them into NetSuite format, and posts them — tracking state in an Azure SQL database to skip unchanged records and detect deletions.

## Running the Server

```bash
# Install dependencies
pip install -r requirements.txt

# Start the SOAP + diagnostic server (requires a .env file)
python server.py
```

The server starts two listeners:
- **Port 8000** — Spyne SOAP server (QBWC connects here)
- **Port 8001** — Diagnostic HTTP server (control endpoints below)

## Environment Variables (.env)

The following keys must be present in `.env`:

| Key | Purpose |
|---|---|
| `WEB_CONNECTOR_USERNAME` / `WEB_CONNECTOR_PASSWORD` | QBWC auth credentials |
| `AZURE_DATABASE`, `AZURE_SERVER`, `AZURE_USER`, `AZURE_PASSWORD` | Azure SQL connection |
| `AZURE_NREST_BASE_URL` | Azure Function URL used to mint a JWT for NetSuite |
| `NETSITE_BASE_URL` | NetSuite REST API base (e.g. `https://<account>.suitetalk.api.netsuite.com/services/rest`) |
| `JWT_CODE` | Code passed to the JWT-minting Azure Function |
| `AZURE_LOGIC_APP` | Azure Logic App webhook URL for alert emails |

## Diagnostic API (port 8001)

Used to drive sync-check runs without touching production code:

| Endpoint | Action |
|---|---|
| `GET /queue?from_date=YYYY-MM-DD&to_date=YYYY-MM-DD` | Queue a date range; then click "Update Selected" in QBWC |
| `GET /results` | Poll until `complete=true`; returns `qb_transactions` and `qb_line_items` |
| `GET /status` | Chunk progress overview |
| `GET /clear` | Reset all state |

## Operating Modes

`SYNC_CHECK_MODE` in [server.py](server.py) (line 294) controls behavior:

- **`True` (default)** — Collects QB transaction IDs and line items only; does **not** post to NetSuite or the DB. Use `/queue` + `/results` to inspect what QB has.
- **`False`** — Production mode: calls `master_function` which posts to NetSuite and syncs the DB.

## Data Flow (Production Mode)

```
QBWC → server.py (SOAP)
  → 5-phase QB query per day-chunk (Account, Memo, Name, RefNumber/Debit/Credit, Class/Date/TxnType)
  → merge_general_detail_responses()       # stitch 5 partial responses into one
  → data_transformation()                  # parse QBXML → sliced journal entries keyed by TxnNumber
  → transform_data_in_netsuite_format()    # map QB account codes + locations → NS IDs
  → compair_data()                         # hash-compare against Azure SQL; skip if unchanged
  → post_jounral_entry_netsuite() / post_updated_jounral_entry_netsuite()
  → post_data_in_database()               # store hash + NS Location URL
  → (after all entries) deletion sweep: QB txns missing from DB → delete from NS + DB
```

## Key Files and Their Roles

| File | Role |
|---|---|
| [server.py](server.py) | SOAP service (`QuickBooksService`), 5-phase chunk state machine, diagnostic HTTP server |
| [master_file.py](master_file.py) | Orchestration: calls transformation → DB compare → NS post/update/delete |
| [data_transformation.py](data_transformation.py) | Parses merged QBXML response into `{journal_entry_raw_N: [rows]}` dict |
| [netsuite_data_transformation.py](netsuite_data_transformation.py) | Maps QB account/location codes to NS IDs; builds JSON payload; sends alert emails |
| [netsuite_posting.py](netsuite_posting.py) | NetSuite REST calls: JWT auth, POST/PATCH/DELETE journal entries, SuiteQL search by tranId |
| [azure_database_posting.py](azure_database_posting.py) | Azure SQL operations: hash compare, insert, update, delete, fetch transaction IDs |
| [Netsuite_mappings.py](Netsuite_mappings.py) | Static JSON strings: QB account code → NS internal ID, QB location string → NS location ID |
| [QBXML_requests.py](QBXML_requests.py) | Builds QBXML `GeneralDetailReportQueryRq` strings for each phase |
| [locks.py](locks.py) | Shared `netsuite_lock` (threading.Lock) used in master_file to serialize NS API calls |
| [logger_config.py](logger_config.py) | Singleton logger writing to `prod_live_35.log` + stdout |

## 5-Phase QB Query Protocol

Each day-chunk is queried in 5 sequential SOAP round-trips because the QB QBXML API has column limits per request. The phases fetch: `[Account, TxnNumber]` → `[Memo]` → `[Name]` → `[RefNumber, TxnNumber, Debit, Credit]` → `[Class, Date, TxnType, TxnNumber]`. All 5 responses are cached in `response_cache[ticket]` and merged by `merge_general_detail_responses()` before processing.

## DB Schema (Azure SQL)

Table: `[dbo].[Quickbooks_Netsuite_Sync_prestige]`

| Column | Type | Notes |
|---|---|---|
| `transaction_id` | int | QB TxnNumber |
| `transaction_date` | date | Journal entry date |
| `hashed_data` | varchar | SHA-256 fingerprint of the `line` block |
| `netsuite_location` | varchar | Full REST URL of the NS journal entry |

Hash comparison (`json_fingerprint` / SHA-256) determines whether a re-encountered transaction needs an update PATCH or can be skipped.

## Mappings

`Netsuite_mappings.py` contains two hardcoded JSON strings:
- `Netsuite_mappings` — QB 5-digit account code → NS internal account ID
- `Location_mappings` — QB class string (e.g. `"15--Mid-South"`) → NS location ID

When an unknown account or location is encountered, an alert email is sent via the Azure Logic App webhook, and that line item is skipped.

## Utility / Audit Scripts

The repo contains standalone scripts for investigation and remediation — none are imported by the main server:

- `sync_check.py` / `direct_sync_check.py` / `reconcile_qb_vs_ns.py` — compare QB vs NS vs DB state
- `check_netsuite_duplicates.py` / `verify_dupliactes.py` — find duplicate NS journal entries
- `delete_april_entries.py` / `delete_blank_tranids.py` — targeted cleanup scripts
- `retry_failed_transactions.py` — re-post transactions listed in a failed-txn file
- `find_gap_entries.py` / `find_prestige_range.py` — find date gaps or range boundaries
- `generate_ns_links.py` — bulk-generate NS record URLs from a txn ID list
