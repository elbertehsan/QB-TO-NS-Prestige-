"""
Duplicate Value Verifier
=========================
Loads the duplicate report JSON, fetches the actual line items for every
duplicate entry from NetSuite, sums up the total value being double-posted,
and compares it to the gap between QuickBooks and NetSuite.

Usage:
    python verify_duplicate_values.py --report netsuite_duplicates_20260504_112344.json

    # Also pass the QB total income so it calculates the gap for you
    python verify_duplicate_values.py --report netsuite_duplicates_20260504_112344.json --qb-total 6058185.21
"""

import argparse
import json
import requests
import time
from collections import defaultdict
from datetime import datetime
from dotenv import dotenv_values

from netsuite_posting import get_jwt_token, generate_access_token
from logger_config import logger

config        = dotenv_values(".env")
NETSUITE_BASE = config.get('NETSITE_BASE_URL').rstrip('/')
JWT_BASE_URL  = config.get('AZURE_NREST_BASE_URL')


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_access_token():
    jwt   = get_jwt_token(JWT_BASE_URL)
    token = generate_access_token(NETSUITE_BASE, jwt)
    if not token or not token.get('access_token'):
        raise RuntimeError("Authentication failed — check .env credentials")
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

    all_rows = []
    offset   = 0
    limit    = 1000

    while True:
        try:
            r = requests.post(
                url,
                headers=get_headers(token),
                json={"q": query},
                params={"limit": limit, "offset": offset},
                timeout=(10, 120)
            )
            if r.status_code not in (200, 204):
                logger.error(f"SuiteQL {r.status_code}: {r.text[:300]}")
                break

            data     = r.json()
            rows     = data.get("items", [])
            all_rows.extend(rows)

            if not data.get("hasMore") or len(rows) == 0:
                break
            offset += limit

        except Exception as e:
            logger.error(f"SuiteQL error: {e}")
            break

    return all_rows


# ── Fetch line items for a list of internal IDs ───────────────────────────────

def fetch_line_totals_for_ids(token, internal_ids: list) -> dict:
    """
    Fetches all journal entry lines for the given internal IDs in one
    SuiteQL query. Returns:
        { internal_id (str) -> { "debit_total": float, "credit_total": float } }
    """
    if not internal_ids:
        return {}

    # Confirmed columns: journal (FK back to journal entry), debit (positive=debit, negative=credit)
    ids_str = ", ".join(str(i) for i in internal_ids)

    query = f"""
        SELECT
            journal                                          AS journal_id,
            SUM(CASE WHEN debit >= 0 THEN debit ELSE 0 END) AS total_debit,
            SUM(CASE WHEN debit < 0  THEN debit ELSE 0 END) AS total_credit
        FROM
            journalentryline
        WHERE
            journal IN ({ids_str})
        GROUP BY
            journal
    """

    print(f"  Fetching line totals for {len(internal_ids)} entries...", end="\r")
    rows = run_suiteql(token, query)
    print(f"  Fetched line totals for {len(rows)} entries          ")

    result = {}
    for row in rows:
        txn_id = str(row.get("journal_id") or row.get("journalentry") or row.get("transaction"))
        result[txn_id] = {
            "debit_total":  float(row.get("total_debit")  or 0),
            "credit_total": float(row.get("total_credit") or 0),
        }
    return result


# ── Main logic ────────────────────────────────────────────────────────────────

def verify_duplicates(report_path: str, qb_total: float = None):

    # Load duplicate report
    with open(report_path) as f:
        report = json.load(f)

    duplicates = report.get("duplicates", {})
    if not duplicates:
        print("No duplicates found in report.")
        return

    print(f"\n{'='*70}")
    print(f"  DUPLICATE VALUE VERIFICATION")
    print(f"  Report : {report_path}")
    print(f"  Period : {report['date_range']['from']}  to  {report['date_range']['to']}")
    print(f"{'='*70}")
    print(f"\n  Loading {len(duplicates)} duplicate tranIds from report...")

    # Collect all internal IDs — both kept and duplicate
    kept_ids      = []
    duplicate_ids = []

    for tran_id, entries in duplicates.items():
        for i, e in enumerate(entries):
            iid = str(e["internal_id"])
            if i == 0:
                kept_ids.append(iid)
            else:
                duplicate_ids.append(iid)

    all_ids = kept_ids + duplicate_ids
    print(f"  Kept entries      : {len(kept_ids)}")
    print(f"  Duplicate entries : {len(duplicate_ids)}")

    # Authenticate
    print(f"\n  Authenticating with NetSuite...")
    token = get_access_token()
    print(f"  Authenticated OK")

    # Fetch line totals — batch in chunks of 500 to stay within SuiteQL limits
    print(f"\n  Fetching line item totals from NetSuite...")
    line_totals = {}
    batch_size  = 500

    for i in range(0, len(all_ids), batch_size):
        batch = all_ids[i:i + batch_size]
        batch_totals = fetch_line_totals_for_ids(token, batch)
        line_totals.update(batch_totals)
        if len(all_ids) > batch_size:
            time.sleep(0.5)  # be kind to the API

    # ── Calculate totals ──────────────────────────────────────────────────────

    kept_debit_total      = 0.0
    kept_credit_total     = 0.0
    duplicate_debit_total = 0.0
    duplicate_credit_total = 0.0
    missing_from_api      = []
    line_comparison       = []

    for tran_id, entries in duplicates.items():
        kept_id  = str(entries[0]["internal_id"])
        dupe_ids = [str(e["internal_id"]) for e in entries[1:]]

        kept_data = line_totals.get(kept_id, {})
        k_debit   = kept_data.get("debit_total", 0.0)
        k_credit  = kept_data.get("credit_total", 0.0)
        kept_debit_total  += k_debit
        kept_credit_total += k_credit

        if not kept_data:
            missing_from_api.append(kept_id)

        for dupe_id in dupe_ids:
            dupe_data = line_totals.get(dupe_id, {})
            d_debit   = dupe_data.get("debit_total", 0.0)
            d_credit  = dupe_data.get("credit_total", 0.0)
            duplicate_debit_total  += d_debit
            duplicate_credit_total += d_credit

            if not dupe_data:
                missing_from_api.append(dupe_id)

            amounts_match = (
                abs(k_debit  - d_debit)  < 0.01 and
                abs(k_credit - d_credit) < 0.01
            )

            line_comparison.append({
                "tran_id":       tran_id,
                "kept_id":       kept_id,
                "dupe_id":       dupe_id,
                "kept_debit":    k_debit,
                "kept_credit":   k_credit,
                "dupe_debit":    d_debit,
                "dupe_credit":   d_credit,
                "amounts_match": amounts_match,
            })

    # ── Print results ─────────────────────────────────────────────────────────

    div = "─" * 70
    matching     = sum(1 for r in line_comparison if r["amounts_match"])
    not_matching = sum(1 for r in line_comparison if not r["amounts_match"])

    print(f"\n{'='*70}")
    print(f"  RESULTS")
    print(f"{'='*70}")

    print(f"\n  Line Amount Comparison (kept vs duplicate)")
    print(div)
    print(f"  Identical amounts  : {matching} / {len(line_comparison)} pairs")
    print(f"  Different amounts  : {not_matching} / {len(line_comparison)} pairs")
    if missing_from_api:
        print(f"  Missing from API   : {len(missing_from_api)} entries (no lines found)")

    print(f"\n  Debit/Credit Totals")
    print(div)
    print(f"  {'':30} {'Debit':>15} {'Credit':>15}")
    print(f"  {'Kept entries (correct)':30} ${kept_debit_total:>14,.2f} ${kept_credit_total:>14,.2f}")
    print(f"  {'Duplicate entries (extra)':30} ${duplicate_debit_total:>14,.2f} ${duplicate_credit_total:>14,.2f}")
    print(f"  {'─'*30} {'─'*15} {'─'*15}")
    print(f"  {'INFLATION in NetSuite':30} ${duplicate_debit_total:>14,.2f} ${duplicate_credit_total:>14,.2f}")

    if qb_total:
        netsuite_apparent = qb_total + duplicate_debit_total
        print(f"\n  Gap Analysis")
        print(div)
        print(f"  QuickBooks total income (you provided) : ${qb_total:>14,.2f}")
        print(f"  Duplicate inflation (debits)           : ${duplicate_debit_total:>14,.2f}")
        print(f"  Expected NetSuite total (QB + dupes)   : ${netsuite_apparent:>14,.2f}")
        print(f"\n  If NetSuite shows ~${netsuite_apparent:,.2f}, the duplicates")
        print(f"  fully explain the gap. Run the delete script to fix it.")

    # ── Flag any non-matching pairs ───────────────────────────────────────────
    if not_matching > 0:
        print(f"\n  ⚠ MISMATCHED PAIRS  (duplicate has different amounts — review before deleting)")
        print(div)
        print(f"  {'tranId':<15} {'Kept ID':<12} {'Dupe ID':<12} {'Kept Debit':>12} {'Dupe Debit':>12}")
        for r in line_comparison:
            if not r["amounts_match"]:
                print(
                    f"  {r['tran_id']:<15} {r['kept_id']:<12} {r['dupe_id']:<12} "
                    f"${r['kept_debit']:>11,.2f} ${r['dupe_debit']:>11,.2f}"
                )

    # ── Save detailed JSON ────────────────────────────────────────────────────
    output = {
        "generated_at": datetime.now().isoformat(),
        "report_source": report_path,
        "summary": {
            "total_pairs":              len(line_comparison),
            "amounts_match":            matching,
            "amounts_differ":           not_matching,
            "kept_debit_total":         round(kept_debit_total, 2),
            "kept_credit_total":        round(kept_credit_total, 2),
            "duplicate_debit_total":    round(duplicate_debit_total, 2),
            "duplicate_credit_total":   round(duplicate_credit_total, 2),
            "netsuite_inflation":       round(duplicate_debit_total, 2),
            "qb_total_provided":        qb_total,
            "expected_netsuite_total":  round(qb_total + duplicate_debit_total, 2) if qb_total else None,
        },
        "line_comparison": line_comparison,
    }

    fname = f"duplicate_verification_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(fname, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n{'='*70}")
    print(f"  Detailed report saved → {fname}")
    print(f"{'='*70}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Verify duplicate journal entry values and calculate NetSuite inflation."
    )
    parser.add_argument(
        "--report", required=True,
        help="Path to the JSON file produced by check_netsuite_duplicates.py"
    )
    parser.add_argument(
        "--qb-total", type=float, default=None,
        help="QuickBooks Total Income for the same period (e.g. 6058185.21) "
             "— used to calculate the expected gap"
    )
    args = parser.parse_args()

    verify_duplicates(args.report, args.qb_total)


if __name__ == "__main__":
    main()