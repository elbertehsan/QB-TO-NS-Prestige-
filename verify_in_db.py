"""
Azure DB ↔ NetSuite Subsidiary Audit
======================================
Checks every DB record that has a saved NetSuite location URL and verifies
the NetSuite entry it points to belongs to the CORRECT subsidiary (15 = Prestige).

If a DB record saved a location pointing to subsidiary 14 (Managed Mobile)
or any other wrong subsidiary, it means check_journal_entry_by_tranid found
a false match and the DB now points to the wrong company's entry.

Usage:
    python verify_in_db.py --from-date 2026-04-01 --to-date 2026-04-30
    python verify_in_db.py --from-date 2026-04-01 --to-date 2026-04-30 --fix
"""

import argparse
import json
import re
import requests
from datetime import datetime, timedelta
from dotenv import dotenv_values

from netsuite_posting import get_jwt_token, generate_access_token
from azure_database_posting import create_db_connection
from logger_config import logger

config        = dotenv_values(".env")
NETSUITE_BASE = config.get('NETSITE_BASE_URL').rstrip('/')
JWT_BASE_URL  = config.get('AZURE_NREST_BASE_URL')
DB_SERVER     = config.get('AZURE_SERVER')
DB_USER       = config.get('AZURE_USER')
DB_PASSWORD   = config.get('AZURE_PASSWORD')
DB_NAME       = config.get('AZURE_DATABASE')

CORRECT_SUBSIDIARY = 15   # Prestige


# ── NetSuite auth ─────────────────────────────────────────────────────────────

def get_ns_token():
    jwt   = get_jwt_token(JWT_BASE_URL)
    token = generate_access_token(NETSUITE_BASE, jwt)
    return token


def ns_headers(token):
    return {
        "Authorization": f"{token.get('token_type')} {token.get('access_token')}",
        "Content-Type":  "application/json",
        "Prefer":        "transient",
    }


def run_suiteql(token, query, limit=500):
    base = NETSUITE_BASE
    if base.endswith('/services/rest'):
        base = base[:-len('/services/rest')]
    url      = f"{base}/services/rest/query/v1/suiteql"
    all_rows, offset = [], 0
    while True:
        r = requests.post(url, headers=ns_headers(token),
                          json={"q": query},
                          params={"limit": limit, "offset": offset},
                          timeout=(30, 300))
        if r.status_code not in (200, 204):
            print(f"  [NS ERROR] {r.status_code}: {r.text[:200]}")
            break
        data = r.json()
        rows = data.get("items", [])
        all_rows.extend(rows)
        if not data.get("hasMore") or not rows:
            break
        offset += limit
    return all_rows


# ── DB fetch ──────────────────────────────────────────────────────────────────

def fetch_db_records(from_date, to_date):
    """
    Fetch all DB records in the date range that have a netsuite_location saved.
    Table: Quickbooks_Netsuite_Sync_prestige
    Columns: transaction_id, transaction_date, netsuite_location, hashed_data
    """
    conn    = create_db_connection(DB_SERVER, DB_USER, DB_PASSWORD, DB_NAME)
    cursor  = conn.cursor()
    records = []

    try:
        from_str = str(from_date)
        to_str   = str(to_date)
        cursor.execute(f"""
            SELECT
                transaction_id,
                transaction_date,
                netsuite_location
            FROM Quickbooks_Netsuite_Sync_prestige
            WHERE transaction_date >= '{from_str}'
            AND   transaction_date <  DATEADD(day, 1, CAST('{to_str}' AS DATE))
            AND   netsuite_location IS NOT NULL
            AND   netsuite_location != ''
            ORDER BY transaction_date ASC, transaction_id ASC
        """)

        rows = cursor.fetchall()
        for row in rows:
            txn_id   = str(row[0])
            txn_date = str(row[1])
            location = str(row[2])

            # Extract NS internal ID from URL
            # Format: .../record/v1/journalentry/2596017
            match = re.search(r'/journalentry/(\d+)', location)
            ns_id = match.group(1) if match else None

            records.append({
                "txn_id":            txn_id,
                "date":              txn_date,
                "netsuite_location": location,
                "ns_internal_id":    ns_id,
            })

        print(f"  Fetched {len(records)} DB records with NS locations")

    except Exception as e:
        logger.error(f"DB fetch error: {e}")
        print(f"  [DB ERROR] {e}")
    finally:
        cursor.close()
        conn.close()

    return records


def fetch_db_schema(conn):
    """Show schema for Quickbooks_Netsuite_Sync_prestige."""
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = 'Quickbooks_Netsuite_Sync_prestige'
            ORDER BY ORDINAL_POSITION
        """)
        rows = cursor.fetchall()
        print("\n  Table: Quickbooks_Netsuite_Sync_prestige")
        print("  " + "─"*50)
        for row in rows:
            length = f"({row[2]})" if row[2] else ""
            print(f"    {row[0]:<30} {row[1]}{length}")

        # Also show row count
        cursor.execute("SELECT COUNT(*) FROM Quickbooks_Netsuite_Sync_prestige")
        count = cursor.fetchone()[0]
        print(f"\n  Total rows: {count:,}")
        return rows
    except Exception as e:
        print(f"  [DB SCHEMA ERROR] {e}")
        return []
    finally:
        cursor.close()


# ── NetSuite verification ─────────────────────────────────────────────────────

def check_subsidiaries_in_bulk(token, ns_internal_ids: list) -> dict:
    """
    Look up subsidiary for a batch of NS internal IDs.
    Returns { internal_id (str) -> subsidiary_id (str) }
    """
    if not ns_internal_ids:
        return {}

    result = {}
    batch_size = 200   # SuiteQL IN clause limit

    for i in range(0, len(ns_internal_ids), batch_size):
        batch    = ns_internal_ids[i:i+batch_size]
        ids_str  = ", ".join(f"'{iid}'" for iid in batch)
        rows     = run_suiteql(token, f"""
            SELECT t.id, t.tranid, t.subsidiary
            FROM transaction t
            WHERE t.id IN ({ids_str})
            AND   t.recordtype = 'journalentry'
        """)
        for row in rows:
            result[str(row.get("id"))] = str(row.get("subsidiary", "?"))

        print(f"  Checked {min(i+batch_size, len(ns_internal_ids))}/{len(ns_internal_ids)} NS entries...", end="\r")

    print()
    return result


def fix_db_location(conn, txn_id, correct_location):
    """
    Clear the netsuite_location for a DB record so it gets re-posted correctly.
    Table: Quickbooks_Netsuite_Sync_prestige
    """
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            UPDATE Quickbooks_Netsuite_Sync_prestige
            SET    netsuite_location = '{correct_location}'
            WHERE  transaction_id    = {txn_id}
        """)
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        logger.error(f"DB fix error for txn_id={txn_id}: {e}")
        return False
    finally:
        cursor.close()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Audit DB records to find those pointing to the wrong NS subsidiary."
    )
    parser.add_argument("--from-date",  required=True, help="YYYY-MM-DD")
    parser.add_argument("--to-date",    required=True, help="YYYY-MM-DD")
    parser.add_argument("--schema",     action="store_true",
                        help="Just show DB schema info and exit")
    parser.add_argument("--fix",        action="store_true",
                        help="Clear wrong NS locations from DB so they get re-posted correctly")
    args = parser.parse_args()

    from_dt = datetime.strptime(args.from_date, "%Y-%m-%d").date()
    to_dt   = datetime.strptime(args.to_date,   "%Y-%m-%d").date()

    print(f"\n{'='*70}")
    print(f"  DB ↔ NetSuite Subsidiary Audit")
    print(f"  Correct subsidiary: {CORRECT_SUBSIDIARY} (Prestige)")
    print(f"  Period: {args.from_date}  to  {args.to_date}")
    print(f"{'='*70}")

    # Schema discovery
    if args.schema:
        print("\n  Connecting to DB to discover schema...")
        conn = create_db_connection(DB_SERVER, DB_USER, DB_PASSWORD, DB_NAME)
        fetch_db_schema(conn)
        conn.close()
        return

    # ── Step 1: Fetch DB records ──────────────────────────────────────────────
    print("\n  Step 1: Fetching DB records with saved NS locations...")
    db_records = fetch_db_records(from_dt, to_dt)

    if not db_records:
        print("\n  No DB records found with NS locations for this period.")
        print("  Either the DB uses different column names or nothing was posted.")
        print(f"\n  TIP: Run with --schema to see your DB column names:")
        print(f"       python verify_in_db.py --from-date {args.from_date} --to-date {args.to_date} --schema")
        return

    # Collect NS IDs to check
    ns_ids_to_check = [r["ns_internal_id"] for r in db_records if r["ns_internal_id"]]
    print(f"  Found {len(ns_ids_to_check)} DB records pointing to NS entries")

    # ── Step 2: Check subsidiary in NetSuite ─────────────────────────────────
    print("\n  Step 2: Authenticating with NetSuite...")
    token = get_ns_token()
    print("  OK")

    print(f"\n  Step 3: Verifying subsidiary for {len(ns_ids_to_check)} NS entries...")
    subsidiary_map = check_subsidiaries_in_bulk(token, ns_ids_to_check)

    # ── Step 3: Classify records ──────────────────────────────────────────────
    correct    = []
    wrong_sub  = []
    not_found  = []

    for rec in db_records:
        ns_id = rec["ns_internal_id"]
        if not ns_id:
            not_found.append(rec)
            continue

        sub = subsidiary_map.get(ns_id)
        if sub is None:
            not_found.append(rec)
        elif sub == str(CORRECT_SUBSIDIARY):
            correct.append(rec)
        else:
            rec["actual_subsidiary"] = sub
            wrong_sub.append(rec)

    # ── Report ────────────────────────────────────────────────────────────────
    div = "─" * 70
    print(f"\n{'='*70}")
    print(f"  AUDIT RESULTS")
    print(f"{'='*70}")
    print(f"  Total DB records checked     : {len(db_records)}")
    print(f"  ✅ Correct subsidiary ({CORRECT_SUBSIDIARY})     : {len(correct)}")
    print(f"  ❌ Wrong subsidiary          : {len(wrong_sub)}")
    print(f"  ⚠  Not found in NS           : {len(not_found)}")

    if wrong_sub:
        print(f"\n  ❌ WRONG SUBSIDIARY — DB points to wrong company's NS entry")
        print(div)
        print(f"  {'QB TxnId':<15} {'NS Internal ID':<16} {'Actual Sub':<12} {'Should be'}")
        print(f"  {'─'*14} {'─'*15} {'─'*11} {'─'*10}")
        for rec in wrong_sub[:50]:
            print(f"  {rec['txn_id']:<15} {rec['ns_internal_id']:<16} "
                  f"{rec.get('actual_subsidiary','?'):<12} {CORRECT_SUBSIDIARY}")
        if len(wrong_sub) > 50:
            print(f"  ... and {len(wrong_sub)-50} more")

        print(f"\n  These {len(wrong_sub)} DB records have NS locations pointing to a different")
        print(f"  company's journal entry. The integration saved the wrong NS ID.")

    if not_found:
        print(f"\n  ⚠  NOT FOUND IN NS — NS entry was deleted or never existed")
        print(div)
        for rec in not_found[:20]:
            print(f"  QB TxnId={rec['txn_id']}  NS id={rec.get('ns_internal_id','?')}")
        if len(not_found) > 20:
            print(f"  ... and {len(not_found)-20} more")

    # ── Fix ───────────────────────────────────────────────────────────────────
    if args.fix and (wrong_sub or not_found):
        print(f"\n  --fix flag set.")
        print(f"  This will CLEAR the netsuite_location for {len(wrong_sub)+len(not_found)}")
        print(f"  records pointing to the wrong subsidiary or missing entries.")
        print(f"  Once cleared, the next sync run will re-post them to the correct subsidiary.")
        confirm = input(f"\n  Type 'yes' to proceed: ")
        if confirm.strip().lower() != "yes":
            print("  Aborted.")
        else:
            conn    = create_db_connection(DB_SERVER, DB_USER, DB_PASSWORD, DB_NAME)
            fixed   = 0
            failed  = 0
            for rec in wrong_sub + not_found:
                if fix_db_location(conn, rec["txn_id"], ""):
                    fixed += 1
                else:
                    failed += 1
                    print(f"  [FAIL] Could not clear txn_id={rec['txn_id']}")
            conn.close()
            print(f"\n  Fixed {fixed} records ({failed} failures)")
            print(f"  These transactions will be re-posted on the next sync run.")

    # Save report
    report = {
        "generated_at":       datetime.now().isoformat(),
        "correct_subsidiary": CORRECT_SUBSIDIARY,
        "date_range":         {"from": args.from_date, "to": args.to_date},
        "summary": {
            "total_checked":    len(db_records),
            "correct":          len(correct),
            "wrong_subsidiary": len(wrong_sub),
            "not_found_in_ns":  len(not_found),
        },
        "wrong_subsidiary_records": [
            {
                "qb_txn_id":        r["txn_id"],
                "date":             r["date"],
                "ns_internal_id":   r["ns_internal_id"],
                "actual_subsidiary": r.get("actual_subsidiary"),
                "netsuite_location": r["netsuite_location"],
            }
            for r in wrong_sub
        ],
        "not_found_in_ns": [
            {
                "qb_txn_id":       r["txn_id"],
                "date":            r["date"],
                "ns_internal_id":  r["ns_internal_id"],
                "netsuite_location": r["netsuite_location"],
            }
            for r in not_found
        ],
    }

    fname = f"db_subsidiary_audit_{args.from_date.replace('-','')}_{args.to_date.replace('-','')}.json"
    with open(fname, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n  Report saved → {fname}")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()