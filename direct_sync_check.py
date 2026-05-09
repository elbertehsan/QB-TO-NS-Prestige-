"""
Direct QuickBooks ↔ NetSuite Sync Checker
==========================================
Compares QuickBooks and NetSuite DIRECTLY without using the Azure DB.

Usage:
    # With live QB data (server.py must be running):
    python direct_sync_check.py --from-date 2026-04-01 --to-date 2026-04-30 --queue
    # click Update Selected in QBWC, then:
    python direct_sync_check.py --from-date 2026-04-01 --to-date 2026-04-30 --compare

    # With Azure DB as QB source (fastest, no QBWC needed):
    python direct_sync_check.py --from-date 2026-04-01 --to-date 2026-04-30 --use-db

    # Include financial total for gap analysis:
    python direct_sync_check.py --from-date 2026-04-01 --to-date 2026-04-30 --use-db --qb-total 6058185.21
"""

import argparse
import json
import requests
from datetime import datetime, timedelta
from dotenv import dotenv_values

from netsuite_posting import get_jwt_token, generate_access_token
from azure_database_posting import create_db_connection, fetch_all_transaction_ids
from logger_config import logger

config        = dotenv_values(".env")
NETSUITE_BASE = config.get('NETSITE_BASE_URL').rstrip('/')
JWT_BASE_URL  = config.get('AZURE_NREST_BASE_URL')
DB_SERVER     = config.get('AZURE_SERVER')
DB_USER       = config.get('AZURE_USER')
DB_PASSWORD   = config.get('AZURE_PASSWORD')
DB_NAME       = config.get('AZURE_DATABASE')

DIAG_HOST     = "http://127.0.0.1:8001"
SUBSIDIARY_ID = 15  # Prestige Fleet Services


# ── NetSuite auth ─────────────────────────────────────────────────────────────

def get_ns_token():
    jwt   = get_jwt_token(JWT_BASE_URL)
    token = generate_access_token(NETSUITE_BASE, jwt)
    if not token or not token.get('access_token'):
        raise RuntimeError("NetSuite authentication failed")
    return token


def ns_headers(token):
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
    url      = f"{base}/services/rest/query/v1/suiteql"
    all_rows, offset = [], 0

    while True:
        try:
            r = requests.post(
                url, headers=ns_headers(token),
                json={"q": query},
                params={"limit": 500, "offset": offset},
                timeout=(30, 300)
            )
            if r.status_code not in (200, 204):
                logger.error(f"SuiteQL {r.status_code}: {r.text[:300]}")
                print(f"  [NS ERROR] {r.status_code}: {r.text[:200]}")
                break
            data = r.json()
            rows = data.get("items", [])
            all_rows.extend(rows)
            if not data.get("hasMore") or not rows:
                break
            offset += 500
            print(f"  Fetched {len(all_rows)} NS rows...", end="\r")
        except requests.exceptions.Timeout:
            print(f"\n  [TIMEOUT] Fetched {len(all_rows)} before timeout")
            break
        except Exception as e:
            logger.error(f"SuiteQL error: {e}")
            print(f"  [NS ERROR] {e}")
            break
    return all_rows


# ── NetSuite data (chunked by week to avoid timeout) ─────────────────────────

def fetch_netsuite_transactions(token, from_date: str, to_date: str) -> dict:
    """
    Fetches all QB-originated journal entries from NetSuite for subsidiary 15.
    Processes week by week to avoid timeouts.
    Returns { bare_tran_id -> { internal_id, tranid, trandate } }
    """
    from_dt = datetime.strptime(from_date, "%Y-%m-%d")
    to_dt   = datetime.strptime(to_date,   "%Y-%m-%d")

    print(f"\n  Querying NetSuite (subsidiary {SUBSIDIARY_ID} — Prestige)...")
    all_rows = []
    current  = from_dt
    chunk    = 0

    while current <= to_dt:
        chunk_end = min(current + timedelta(days=6), to_dt)
        fs = current.strftime("%Y-%m-%d")
        ts = chunk_end.strftime("%Y-%m-%d")
        chunk += 1
        print(f"  Week {chunk}: {fs} → {ts}", end="  ")

        rows = run_suiteql(token, f"""
            SELECT t.id, t.tranid, t.trandate
            FROM   transaction t
            WHERE  t.recordtype = 'journalentry'
            AND    t.subsidiary = {SUBSIDIARY_ID}
            AND    t.trandate  >= TO_DATE('{fs}', 'YYYY-MM-DD')
            AND    t.trandate  <= TO_DATE('{ts}', 'YYYY-MM-DD')
            ORDER  BY t.trandate ASC, t.id ASC
        """)
        print(f"{len(rows)} entries")
        all_rows.extend(rows)
        current = chunk_end + timedelta(days=1)

    print(f"\n  Total NS entries: {len(all_rows)}")

    result = {}
    for row in all_rows:
        tranid = str(row.get("tranid") or "")
        # Strip -15 or -14 suffix to get bare QB number
        bare = tranid
        for suffix in ["-15", "-14"]:
            if tranid.endswith(suffix):
                bare = tranid[:-len(suffix)]
                break
        if bare.isdigit():
            result[bare] = {
                "internal_id": str(row.get("id")),
                "tranid":      tranid,
                "trandate":    str(row.get("trandate")),
            }

    print(f"  Unique QB-numeric tranIds in NS: {len(result)}")
    return result


# ── QB data via diagnostic endpoint ──────────────────────────────────────────

def queue_qb_range(from_date: str, to_date: str):
    try:
        r    = requests.get(f"{DIAG_HOST}/queue?from_date={from_date}&to_date={to_date}", timeout=10)
        data = r.json()
        if r.status_code == 200:
            print(f"\n  ✅ Queued {data.get('chunks_queued')} chunks on diagnostic server")
            print(f"\n  ════════════════════════════════════════════════")
            print(f"  NOW:  Click 'Update Selected' in QBWC")
            print(f"  WAIT: For the progress bars to reach 100%")
            print(f"  THEN: Run with --compare to see results")
            print(f"  ════════════════════════════════════════════════\n")
        else:
            print(f"  [ERROR] {data.get('error')}")
    except requests.ConnectionError:
        print(f"\n  [ERROR] Cannot connect to diagnostic server on port 8001.")
        print(f"  Make sure server.py is running.")


def fetch_qb_from_server() -> dict:
    try:
        r    = requests.get(f"{DIAG_HOST}/results", timeout=10)
        data = r.json()
        txns = data.get("qb_transactions", {})
        print(f"  QB transactions from server: {len(txns)}")
        if not txns:
            print(f"  [WARN] No QB data collected yet — make sure QBWC finished.")
        return txns
    except requests.ConnectionError:
        print(f"  [ERROR] Cannot connect to diagnostic server on port 8001.")
        return {}


# ── QB data via Azure DB ──────────────────────────────────────────────────────

def fetch_qb_from_db(from_date_str: str, to_date_str: str) -> dict:
    print(f"\n  Fetching QB transactions from Azure DB...")
    from_dt = datetime.strptime(from_date_str, "%Y-%m-%d").date()
    to_dt   = datetime.strptime(to_date_str,   "%Y-%m-%d").date()

    result  = {}
    try:
        conn    = create_db_connection(DB_SERVER, DB_USER, DB_PASSWORD, DB_NAME)
        current = from_dt
        while current <= to_dt:
            ids = fetch_all_transaction_ids(conn, current)
            if ids:
                for tid in ids:
                    result[str(tid)] = str(current)
            current += timedelta(days=1)
        conn.close()
        print(f"  Azure DB returned {len(result)} QB transaction IDs")
    except Exception as e:
        logger.error(f"DB error: {e}")
        print(f"  [ERROR] DB: {e}")
    return result


# ── Compare ───────────────────────────────────────────────────────────────────

def compare_and_report(qb_txns: dict, ns_data: dict,
                       from_date, to_date, qb_source: str, qb_total: float = None):
    qb_ids  = set(qb_txns.keys())
    ns_ids  = set(ns_data.keys())

    in_both  = qb_ids & ns_ids
    only_qb  = qb_ids - ns_ids   # ❌ Missing from NS — need posting
    only_ns  = ns_ids - qb_ids   # ⚠  In NS but not in QB — phantom

    is_synced = len(only_qb) == 0 and len(only_ns) == 0
    div       = "─" * 70

    print(f"\n{'='*70}")
    print(f"  DIRECT QB ↔ NETSUITE SYNC REPORT")
    print(f"  Subsidiary {SUBSIDIARY_ID} — Prestige Fleet Services")
    print(f"  Period    : {from_date}  to  {to_date}")
    print(f"  QB source : {qb_source}")
    print(f"{'='*70}")
    print(f"\n  Status: {'✅  IN SYNC' if is_synced else '❌  OUT OF SYNC'}\n")
    print(f"  {'Category':<45} {'Count':>7}")
    print(div)
    print(f"  {'QB transactions total:':<45} {len(qb_ids):>7}")
    print(f"  {'NS unique QB tranIds:':<45} {len(ns_ids):>7}")
    print(div)
    print(f"  {'✅ Synced (in both QB and NS):':<45} {len(in_both):>7}")
    print(f"  {'❌ In QB — MISSING from NetSuite:':<45} {len(only_qb):>7}")
    print(f"  {'⚠  In NetSuite — NOT in QB (phantom):':<45} {len(only_ns):>7}")

    if qb_total:
        print(f"\n  QB total provided: ${qb_total:,.2f}")

    # Missing from NS
    if only_qb:
        print(f"\n  ❌ IN QB — MISSING FROM NETSUITE (need to be posted)")
        print(div)
        for tid in sorted(only_qb, key=lambda x: int(x) if x.isdigit() else x):
            print(f"  {tid}   QB date: {qb_txns.get(tid,'')}")

    # Phantom in NS
    if only_ns:
        print(f"\n  ⚠  IN NETSUITE — NOT IN QB (phantom entries)")
        print(div)
        for tid in sorted(only_ns, key=lambda x: int(x) if x.isdigit() else x):
            e = ns_data[tid]
            print(f"  {tid:<15}  NS id={e['internal_id']}  date={e['trandate']}")

    if is_synced:
        print(f"\n  ✅ All {len(in_both)} QB transactions are present in NetSuite.")

    print(f"\n{'='*70}")

    # Save JSON
    report = {
        "generated_at": datetime.now().isoformat(),
        "subsidiary":   f"{SUBSIDIARY_ID} (Prestige Fleet Services)",
        "date_range":   {"from": str(from_date), "to": str(to_date)},
        "qb_source":    qb_source,
        "status":       "IN_SYNC" if is_synced else "OUT_OF_SYNC",
        "summary": {
            "qb_total_provided":  qb_total,
            "qb_transactions":    len(qb_ids),
            "ns_unique_tranids":  len(ns_ids),
            "synced":             len(in_both),
            "missing_from_ns":    len(only_qb),
            "phantom_in_ns":      len(only_ns),
        },
        "missing_from_ns": sorted(only_qb),
        "phantom_in_ns":   [
            {"tran_id": tid, **ns_data[tid]}
            for tid in sorted(only_ns)
        ],
    }

    fname = f"direct_sync_{str(from_date).replace('-','')}_{str(to_date).replace('-','')}.json"
    with open(fname, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n  Report saved → {fname}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=f"Compare QB ↔ NetSuite directly for Prestige (subsidiary {SUBSIDIARY_ID})."
    )
    parser.add_argument("--from-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to-date",   required=True, help="YYYY-MM-DD")

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--queue",   action="store_true",
                      help="Queue date range for QBWC (server.py must be running)")
    mode.add_argument("--compare", action="store_true",
                      help="Compare after QBWC has finished collecting QB data")
    mode.add_argument("--use-db",  action="store_true",
                      help="Use Azure DB as QB source — fastest, no QBWC needed")

    parser.add_argument("--qb-total", type=float, default=None,
                        help="QB Total Income for the period (optional, for gap analysis)")
    args = parser.parse_args()

    print(f"\n{'='*70}")
    print(f"  Direct QB ↔ NetSuite Sync Checker")
    print(f"  Subsidiary {SUBSIDIARY_ID} — Prestige Fleet Services")
    print(f"{'='*70}")

    if args.queue:
        print(f"\n  Queuing {args.from_date} → {args.to_date}...")
        queue_qb_range(args.from_date, args.to_date)
        return

    # Get QB data
    if args.compare:
        print(f"\n  Getting QB data from server...")
        qb_txns   = fetch_qb_from_server()
        qb_source = "QBWC live sync (server collected)"
    else:
        qb_txns   = fetch_qb_from_db(args.from_date, args.to_date)
        qb_source = "Azure DB"

    if not qb_txns:
        print("  No QB transactions found. Exiting.")
        return

    # Get NetSuite data
    print("\n  Authenticating with NetSuite...")
    token = get_ns_token()
    print("  OK")

    ns_data = fetch_netsuite_transactions(token, args.from_date, args.to_date)

    # Compare
    print(f"\n  Comparing {len(qb_txns)} QB vs {len(ns_data)} NS entries...")
    compare_and_report(qb_txns, ns_data,
                       args.from_date, args.to_date,
                       qb_source, args.qb_total)


if __name__ == "__main__":
    main()