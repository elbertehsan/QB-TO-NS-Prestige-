"""
NetSuite vs QuickBooks Gap Analyzer
=====================================
Finds journal entries in NetSuite that have NO corresponding transaction
in QuickBooks for a given date range. These are the entries causing the
gap between QB and NetSuite totals.

Categorizes findings into:
  1. Manual entries  — tranIds starting with JEA (entered directly in NetSuite)
  2. QB sync entries — numeric tranIds that should exist in QB but may not
  3. Unknown         — anything else

Usage:
    python find_gap_entries.py --from-date 2026-01-01 --to-date 2026-03-31

    # Also pass QB total to see how much of the gap is explained
    python find_gap_entries.py --from-date 2026-01-01 --to-date 2026-03-31 --qb-total 17500000
"""

import argparse
import json
import requests
from collections import defaultdict
from datetime import datetime
from dotenv import dotenv_values

from netsuite_posting import get_jwt_token, generate_access_token
from azure_database_posting import create_db_connection, fetch_all_transaction_ids
from logger_config import logger
from datetime import date, timedelta

config        = dotenv_values(".env")
NETSUITE_BASE = config.get('NETSITE_BASE_URL').rstrip('/')
JWT_BASE_URL  = config.get('AZURE_NREST_BASE_URL')
DB_SERVER     = config.get('AZURE_SERVER')
DB_USER       = config.get('AZURE_USER')
DB_PASSWORD   = config.get('AZURE_PASSWORD')
DB_NAME       = config.get('AZURE_DATABASE')


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_access_token():
    jwt   = get_jwt_token(JWT_BASE_URL)
    token = generate_access_token(NETSUITE_BASE, jwt)
    if not token or not token.get('access_token'):
        raise RuntimeError("Authentication failed")
    return token


def get_headers(token):
    return {
        "Authorization": f"{token.get('token_type')} {token.get('access_token')}",
        "Content-Type":  "application/json",
        "Prefer":        "transient",
    }


# ── SuiteQL ───────────────────────────────────────────────────────────────────

def run_suiteql(token, query: str) -> list:
    base = NETSUITE_BASE
    if base.endswith('/services/rest'):
        base = base[:-len('/services/rest')]
    url = f"{base}/services/rest/query/v1/suiteql"

    all_rows, offset, limit = [], 0, 1000
    while True:
        try:
            r = requests.post(
                url, headers=get_headers(token),
                json={"q": query},
                params={"limit": limit, "offset": offset},
                timeout=(10, 120)
            )
            if r.status_code not in (200, 204):
                logger.error(f"SuiteQL {r.status_code}: {r.text[:300]}")
                break
            data = r.json()
            rows = data.get("items", [])
            all_rows.extend(rows)
            if not data.get("hasMore") or not rows:
                break
            offset += limit
            print(f"  Fetched {len(all_rows)} rows...", end="\r")
        except Exception as e:
            logger.error(f"SuiteQL error: {e}")
            break
    return all_rows


# ── Fetch NetSuite entries with line totals ───────────────────────────────────

def fetch_netsuite_entries_with_totals(token, from_date: str, to_date: str) -> list:
    """
    Fetches all journal entries + their debit totals for subsidiary 14 (Prestige).
    Returns list of { id, tranid, trandate, createddate, memo, total_debit }
    """
    print(f"\n  Fetching NetSuite journal entries for subsidiary 14 (Prestige)...")

    query = f"""
        SELECT
            t.id,
            t.tranid,
            t.trandate,
            t.createddate,
            t.memo,
            SUM(CASE WHEN jel.debit >= 0 THEN jel.debit ELSE 0 END) AS total_debit
        FROM
            transaction t
            INNER JOIN journalentryline jel ON jel.journal = t.id
        WHERE
            t.recordtype  = 'journalentry'
            AND t.subsidiary = 14
            AND t.trandate >= TO_DATE('{from_date}', 'YYYY-MM-DD')
            AND t.trandate <= TO_DATE('{to_date}', 'YYYY-MM-DD')
        GROUP BY
            t.id, t.tranid, t.trandate, t.createddate, t.memo
        ORDER BY
            t.trandate ASC
    """

    rows = run_suiteql(token, query)
    print(f"  Fetched {len(rows)} journal entries from NetSuite          ")
    return rows


# ── Fetch QB transaction IDs from Azure DB ────────────────────────────────────

def fetch_db_tran_ids(from_date_str: str, to_date_str: str) -> set:
    """Returns set of QB transaction IDs stored in the Azure DB for the range."""
    print(f"\n  Fetching QB transaction IDs from Azure DB...")
    from_dt = datetime.strptime(from_date_str, "%Y-%m-%d").date()
    to_dt   = datetime.strptime(to_date_str,   "%Y-%m-%d").date()

    all_ids = set()
    try:
        conn    = create_db_connection(DB_SERVER, DB_USER, DB_PASSWORD, DB_NAME)
        current = from_dt
        while current <= to_dt:
            ids = fetch_all_transaction_ids(conn, current)
            if ids:
                all_ids.update(map(str, ids))
            current += timedelta(days=1)
        conn.close()
        print(f"  Found {len(all_ids)} QB transaction IDs in DB for range")
    except Exception as e:
        logger.error(f"DB error: {e}")
        print(f"  [ERROR] DB fetch failed: {e}")
    return all_ids


# ── Categorize entries ────────────────────────────────────────────────────────

def categorize_entry(tranid: str) -> str:
    if not tranid:
        return "no_tranid"
    if tranid.upper().startswith("JEA"):
        return "manual_netsuite"       # Manually entered in NetSuite
    if tranid.replace("-", "").isdigit():
        return "qb_numeric"            # QB-style numeric ID
    return "other"


def analyze_gap(ns_entries: list, db_qb_ids: set) -> dict:
    """
    Compares NetSuite entries against DB QB IDs.
    Returns categorized breakdown of entries with no QB counterpart.
    """
    results = {
        "in_both":         [],   # In NetSuite and in QB DB — synced correctly
        "only_netsuite":   [],   # In NetSuite but NOT in QB DB
        "manual_entries":  [],   # JEA* entries — manually created in NetSuite
        "unknown":         [],   # Other patterns
    }

    for entry in ns_entries:
        tranid   = str(entry.get("tranid") or "")
        ns_id    = str(entry.get("id"))
        debit    = float(entry.get("total_debit") or 0)
        category = categorize_entry(tranid)

        record = {
            "internal_id": ns_id,
            "tranid":       tranid,
            "trandate":     entry.get("trandate"),
            "createddate":  entry.get("createddate"),
            "memo":         entry.get("memo", ""),
            "total_debit":  debit,
            "category":     category,
        }

        # Strip the '-14' suffix your code appends before comparing
        bare_id = tranid.replace("-14", "").strip()

        if bare_id in db_qb_ids:
            results["in_both"].append(record)
        elif category == "manual_netsuite":
            results["manual_entries"].append(record)
        elif category == "qb_numeric":
            results["only_netsuite"].append(record)
        else:
            results["unknown"].append(record)

    return results


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(results: dict, from_date, to_date, qb_total: float = None):
    div = "─" * 70

    synced         = results["in_both"]
    only_ns        = results["only_netsuite"]
    manual         = results["manual_entries"]
    unknown        = results["unknown"]

    synced_total   = sum(e["total_debit"] for e in synced)
    only_ns_total  = sum(e["total_debit"] for e in only_ns)
    manual_total   = sum(e["total_debit"] for e in manual)
    unknown_total  = sum(e["total_debit"] for e in unknown)
    ns_grand_total = synced_total + only_ns_total + manual_total + unknown_total

    print(f"\n{'='*70}")
    print(f"  NETSUITE vs QB GAP ANALYSIS")
    print(f"  Period : {from_date}  to  {to_date}")
    print(f"{'='*70}")
    print(f"\n  {'Category':<35} {'Count':>6}  {'Total Debit':>14}")
    print(div)
    print(f"  {'Synced (in QB + NetSuite):':<35} {len(synced):>6}  ${synced_total:>13,.2f}")
    print(f"  {'QB numeric IDs only in NetSuite:':<35} {len(only_ns):>6}  ${only_ns_total:>13,.2f}")
    print(f"  {'Manual NetSuite entries (JEA*):':<35} {len(manual):>6}  ${manual_total:>13,.2f}")
    print(f"  {'Other/Unknown:':<35} {len(unknown):>6}  ${unknown_total:>13,.2f}")
    print(div)
    print(f"  {'NetSuite grand total:':<35} {'':>6}  ${ns_grand_total:>13,.2f}")

    if qb_total:
        gap = ns_grand_total - qb_total
        print(f"\n  {'QuickBooks total (provided):':<35} {'':>6}  ${qb_total:>13,.2f}")
        print(f"  {'Gap (NetSuite - QB):':<35} {'':>6}  ${gap:>13,.2f}")
        print(f"\n  Gap breakdown:")
        print(f"    QB numeric IDs only in NetSuite : ${only_ns_total:>13,.2f}")
        print(f"    Manual NetSuite entries (JEA*)  : ${manual_total:>13,.2f}")
        print(f"    Other/Unknown                   : ${unknown_total:>13,.2f}")
        explained = only_ns_total + manual_total + unknown_total
        print(f"    ─────────────────────────────────────────────")
        print(f"    Total explained                 : ${explained:>13,.2f}")
        print(f"    Still unexplained               : ${gap - explained:>13,.2f}")

    # QB numeric IDs only in NetSuite (missing from QB DB)
    if only_ns:
        print(f"\n  QB-STYLE ENTRIES ONLY IN NETSUITE (missing from QB DB)")
        print(div)
        print(f"  {'tranId':<20} {'internal_id':<14} {'trandate':<14} {'Debit':>12}  memo")
        print(f"  {'─'*19} {'─'*13} {'─'*13} {'─'*12}  {'─'*20}")
        for e in sorted(only_ns, key=lambda x: float(x['total_debit']), reverse=True):
            memo = (e['memo'] or '')[:25]
            print(f"  {e['tranid']:<20} {e['internal_id']:<14} "
                  f"{str(e['trandate']):<14} ${float(e['total_debit']):>11,.2f}  {memo}")

    # Manual JEA entries
    if manual:
        print(f"\n  MANUAL NETSUITE ENTRIES (JEA* — not from QB sync)")
        print(div)
        print(f"  {'tranId':<30} {'internal_id':<12} {'trandate':<14} {'Debit':>12}  memo")
        print(f"  {'─'*29} {'─'*11} {'─'*13} {'─'*12}  {'─'*25}")
        for e in sorted(manual, key=lambda x: float(x['total_debit']), reverse=True):
            memo = (e['memo'] or '')[:30]
            print(f"  {e['tranid']:<30} {e['internal_id']:<12} "
                  f"{str(e['trandate']):<14} ${float(e['total_debit']):>11,.2f}  {memo}")

    # Unknown
    if unknown:
        print(f"\n  UNKNOWN PATTERN ENTRIES")
        print(div)
        for e in unknown:
            print(f"  tranId={e['tranid']}  id={e['internal_id']}  "
                  f"date={e['trandate']}  debit=${float(e['total_debit']):,.2f}  memo={e['memo']}")

    print(f"\n{'='*70}")

    # Save JSON
    report = {
        "generated_at": datetime.now().isoformat(),
        "date_range":   {"from": str(from_date), "to": str(to_date)},
        "summary": {
            "synced_count":          len(synced),
            "synced_total":          round(synced_total, 2),
            "only_netsuite_count":   len(only_ns),
            "only_netsuite_total":   round(only_ns_total, 2),
            "manual_entries_count":  len(manual),
            "manual_entries_total":  round(manual_total, 2),
            "unknown_count":         len(unknown),
            "unknown_total":         round(unknown_total, 2),
            "netsuite_grand_total":  round(ns_grand_total, 2),
            "qb_total_provided":     qb_total,
            "gap":                   round(ns_grand_total - qb_total, 2) if qb_total else None,
        },
        "only_in_netsuite":  only_ns,
        "manual_entries":    manual,
        "unknown_entries":   unknown,
    }

    fname = f"gap_analysis_{str(from_date).replace('-','')}_{str(to_date).replace('-','')}.json"
    with open(fname, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n  Report saved → {fname}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Find NetSuite journal entries with no corresponding QB transaction."
    )
    parser.add_argument("--from-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to-date",   required=True, help="YYYY-MM-DD")
    parser.add_argument("--qb-total",  type=float, default=None,
                        help="QuickBooks total income for the period (for gap analysis)")
    args = parser.parse_args()

    print(f"\n{'='*70}")
    print(f"  NetSuite vs QB Gap Analyzer")
    print(f"{'='*70}")

    # Auth
    print("\n  Authenticating...")
    token = get_access_token()
    print("  Authenticated OK")

    # Fetch NetSuite entries
    ns_entries = fetch_netsuite_entries_with_totals(token, args.from_date, args.to_date)
    if not ns_entries:
        print("  No NetSuite entries returned.")
        return

    # Fetch QB IDs from DB
    db_ids = fetch_db_tran_ids(args.from_date, args.to_date)

    # Analyze
    print(f"\n  Analyzing {len(ns_entries)} NetSuite entries against {len(db_ids)} QB DB records...")
    results = analyze_gap(ns_entries, db_ids)

    # Report
    print_report(results, args.from_date, args.to_date, args.qb_total)


if __name__ == "__main__":
    main()


    