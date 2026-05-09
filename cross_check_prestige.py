"""
Prestige NetSuite vs QuickBooks Cross-Check
============================================
Takes all tranIds from NetSuite (subsidiary 15) for a date range
and checks which ones exist in QuickBooks and which don't.

This runs in two steps:
  Step 1 (--queue): Queue the date range for QBWC to collect QB data
  Step 2 (--check): Compare NS tranIds vs QB tranIds after QBWC finishes

Or use --use-db to skip QBWC and use Azure DB as QB source.

Usage:
    # With live QB data:
    python cross_check_prestige.py --from-date 2026-04-01 --to-date 2026-04-30 --queue
    # click Update Selected in QBWC, then:
    python cross_check_prestige.py --from-date 2026-04-01 --to-date 2026-04-30 --check

    # With Azure DB (faster, no QBWC needed):
    python cross_check_prestige.py --from-date 2026-04-01 --to-date 2026-04-30 --use-db
"""

import argparse
import json
import requests
from collections import defaultdict
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
SUBSIDIARY_ID = 15  # Prestige


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_ns_token():
    jwt   = get_jwt_token(JWT_BASE_URL)
    token = generate_access_token(NETSUITE_BASE, jwt)
    if not token or not token.get('access_token'):
        raise RuntimeError("NetSuite auth failed")
    return token


def ns_headers(token):
    return {
        "Authorization": f"{token.get('token_type')} {token.get('access_token')}",
        "Content-Type":  "application/json",
        "Prefer":        "transient",
    }


# ── SuiteQL ───────────────────────────────────────────────────────────────────

def run_suiteql(token, query, limit=500) -> list:
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
                params={"limit": limit, "offset": offset},
                timeout=(30, 300)
            )
            if r.status_code not in (200, 204):
                print(f"  [NS ERROR] {r.status_code}: {r.text[:200]}")
                break
            data = r.json()
            rows = data.get("items", [])
            all_rows.extend(rows)
            if not data.get("hasMore") or not rows:
                break
            offset += limit
            print(f"  Fetched {len(all_rows)} NS entries...", end="\r")
        except requests.exceptions.Timeout:
            print(f"\n  [TIMEOUT] Fetched {len(all_rows)} before timeout")
            break
        except Exception as e:
            print(f"  [ERROR] {e}")
            break
    return all_rows


# ── Fetch NetSuite tranIds (chunked by week to avoid timeout) ─────────────────

def fetch_ns_tranids(token, from_date: str, to_date: str) -> dict:
    """
    Returns { bare_tran_id -> [{ internal_id, tranid, trandate, createddate }] }
    Groups by bare QB number so we can see how many times each was posted.
    """
    print(f"\n  Fetching NetSuite entries (subsidiary {SUBSIDIARY_ID} — Prestige)...")

    from_dt  = datetime.strptime(from_date, "%Y-%m-%d")
    to_dt    = datetime.strptime(to_date,   "%Y-%m-%d")
    all_rows = []

    # Process week by week to avoid timeout
    current = from_dt
    chunk_num = 0
    while current <= to_dt:
        chunk_end = min(current + timedelta(days=6), to_dt)
        fs = current.strftime("%Y-%m-%d")
        ts = chunk_end.strftime("%Y-%m-%d")
        chunk_num += 1
        print(f"  Week {chunk_num}: {fs} → {ts}", end="  ")

        rows = run_suiteql(token, f"""
            SELECT t.id, t.tranid, t.trandate, t.createddate
            FROM transaction t
            WHERE t.recordtype  = 'journalentry'
            AND   t.subsidiary  = {SUBSIDIARY_ID}
            AND   t.trandate   >= TO_DATE('{fs}', 'YYYY-MM-DD')
            AND   t.trandate   <= TO_DATE('{ts}', 'YYYY-MM-DD')
            ORDER BY t.tranid ASC, t.createddate ASC
        """)
        print(f"{len(rows)} entries")
        all_rows.extend(rows)
        current = chunk_end + timedelta(days=1)

    print(f"\n  Total NS entries: {len(all_rows)}")

    # Group by bare QB number
    grouped = defaultdict(list)
    for row in all_rows:
        tranid = str(row.get("tranid", ""))
        # Strip -15 or -14 suffix
        bare = tranid
        for suffix in ["-15", "-14"]:
            if tranid.endswith(suffix):
                bare = tranid[:-len(suffix)]
                break

        if bare.isdigit():
            grouped[bare].append({
                "internal_id": str(row.get("id")),
                "tranid":      tranid,
                "trandate":    str(row.get("trandate")),
                "createddate": str(row.get("createddate")),
            })

    print(f"  Unique QB-numeric tranIds in NS: {len(grouped)}")
    return grouped


# ── Fetch QB tranIds ──────────────────────────────────────────────────────────

def fetch_qb_from_server() -> set:
    """Gets QB tranIds already collected by the running server."""
    try:
        r    = requests.get(f"{DIAG_HOST}/results", timeout=10)
        data = r.json()
        txns = data.get("qb_transactions", {})
        print(f"  QB transactions from server: {len(txns)}")
        return set(txns.keys())
    except requests.ConnectionError:
        print(f"  [ERROR] Cannot connect to server on port 8001.")
        return set()


def fetch_qb_from_db(from_date: str, to_date: str) -> set:
    """Gets QB tranIds from Azure DB."""
    print(f"  Fetching QB IDs from Azure DB...")
    from_dt = datetime.strptime(from_date, "%Y-%m-%d").date()
    to_dt   = datetime.strptime(to_date,   "%Y-%m-%d").date()
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
        print(f"  Azure DB QB IDs: {len(all_ids)}")
    except Exception as e:
        print(f"  [DB ERROR] {e}")
    return all_ids


def queue_for_qbwc(from_date: str, to_date: str):
    try:
        r = requests.get(
            f"{DIAG_HOST}/queue?from_date={from_date}&to_date={to_date}",
            timeout=10
        )
        data = r.json()
        print(f"  Queued {data.get('chunks_queued')} chunks")
        print(f"\n  ► Click 'Update Selected' in QBWC")
        print(f"  ► Wait for progress bars to reach 100%")
        print(f"  ► Then run: python cross_check_prestige.py "
              f"--from-date {from_date} --to-date {to_date} --check")
    except requests.ConnectionError:
        print(f"  [ERROR] server.py not running on port 8001")


# ── Analysis ──────────────────────────────────────────────────────────────────

def analyze(ns_grouped: dict, qb_ids: set, from_date: str, to_date: str):
    """
    Compares NS tranIds against QB tranIds.
    Also detects entries posted multiple times to NS.
    """
    ns_ids   = set(ns_grouped.keys())
    in_both  = ns_ids & qb_ids       # In NS + in QB ✅
    only_ns  = ns_ids - qb_ids       # In NS but NOT in QB ⚠
    only_qb  = qb_ids - ns_ids       # In QB but NOT in NS ❌

    # Entries posted more than once to NS (same bare tranId, multiple NS records)
    multi_posted = {k: v for k, v in ns_grouped.items() if len(v) > 1}

    div = "─" * 70

    print(f"\n{'='*70}")
    print(f"  PRESTIGE CROSS-CHECK REPORT")
    print(f"  Period : {from_date}  to  {to_date}")
    print(f"{'='*70}")
    print(f"\n  {'Category':<45} {'Count':>8}")
    print(div)
    print(f"  {'QB transactions total:':<45} {len(qb_ids):>8}")
    print(f"  {'NS entries total (all postings):':<45} {len(sum(ns_grouped.values(), [])):>8}")
    print(f"  {'NS unique QB tranIds:':<45} {len(ns_ids):>8}")
    print(div)
    print(f"  {'✅ In both QB and NS (synced):':<45} {len(in_both):>8}")
    print(f"  {'❌ In QB — MISSING from NS:':<45} {len(only_qb):>8}")
    print(f"  {'⚠  In NS — NOT in QB (phantom):':<45} {len(only_ns):>8}")
    print(f"  {'🔁 Posted multiple times to NS:':<45} {len(multi_posted):>8}")

    # Multi-posted entries — same QB tranId in NS more than once
    if multi_posted:
        total_extra = sum(len(v) - 1 for v in multi_posted.values())
        print(f"\n  🔁 POSTED MULTIPLE TIMES (same QB tranId, multiple NS entries)")
        print(div)
        print(f"  Total excess NS entries from re-posting: {total_extra}")
        print(f"\n  Sample (first 20):")
        print(f"  {'QB tranId':<15} {'Times posted':<14} {'NS internal IDs'}")
        print(f"  {'─'*14} {'─'*13} {'─'*30}")
        for bare, entries in sorted(multi_posted.items(),
                                    key=lambda x: -len(x[1]))[:20]:
            ids = ", ".join(e["internal_id"] for e in entries)
            created_dates = sorted(set(e["createddate"][:10] for e in entries))
            print(f"  {bare:<15} {len(entries):<14} posted on: {', '.join(created_dates)}")

    # Missing from NS
    if only_qb:
        print(f"\n  ❌ IN QB — MISSING FROM NETSUITE (first 20)")
        print(div)
        for tid in sorted(only_qb, key=lambda x: int(x) if x.isdigit() else x)[:20]:
            print(f"  {tid}")
        if len(only_qb) > 20:
            print(f"  ... and {len(only_qb) - 20} more")

    # Phantom entries (in NS but not in QB)
    if only_ns:
        print(f"\n  ⚠  IN NETSUITE — NOT IN QB (first 20)")
        print(div)
        for tid in sorted(only_ns, key=lambda x: int(x) if x.isdigit() else x)[:20]:
            entries = ns_grouped[tid]
            print(f"  {tid:<15} NS id={entries[0]['internal_id']}  "
                  f"date={entries[0]['trandate']}")
        if len(only_ns) > 20:
            print(f"  ... and {len(only_ns) - 20} more")

    print(f"\n{'='*70}")

    # Save
    report = {
        "generated_at": datetime.now().isoformat(),
        "subsidiary":   f"{SUBSIDIARY_ID} (Prestige)",
        "date_range":   {"from": from_date, "to": to_date},
        "summary": {
            "qb_total":            len(qb_ids),
            "ns_total_entries":    len(sum(ns_grouped.values(), [])),
            "ns_unique_tranids":   len(ns_ids),
            "synced":              len(in_both),
            "missing_from_ns":     len(only_qb),
            "phantom_in_ns":       len(only_ns),
            "multi_posted_count":  len(multi_posted),
            "multi_posted_excess": sum(len(v)-1 for v in multi_posted.values()),
        },
        "missing_from_ns":  sorted(only_qb),
        "phantom_in_ns":    sorted(only_ns),
        "multi_posted": {
            k: [{"internal_id": e["internal_id"],
                 "tranid":      e["tranid"],
                 "trandate":    e["trandate"],
                 "createddate": e["createddate"]}
                for e in v]
            for k, v in multi_posted.items()
        }
    }

    fname = f"prestige_crosscheck_{from_date.replace('-','')}_{to_date.replace('-','')}.json"
    with open(fname, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n  Report saved → {fname}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Cross-check Prestige NS tranIds against QuickBooks."
    )
    parser.add_argument("--from-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to-date",   required=True, help="YYYY-MM-DD")

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--queue",   action="store_true",
                      help="Queue date range for QBWC collection")
    mode.add_argument("--check",   action="store_true",
                      help="Run cross-check using QB data already collected by server")
    mode.add_argument("--use-db",  action="store_true",
                      help="Use Azure DB as QB source (fastest, no QBWC needed)")
    args = parser.parse_args()

    print(f"\n{'='*70}")
    print(f"  Prestige QB ↔ NetSuite Cross-Check")
    print(f"  Subsidiary {SUBSIDIARY_ID} — Prestige")
    print(f"{'='*70}")

    if args.queue:
        print(f"\n  Queuing {args.from_date} → {args.to_date}...")
        queue_for_qbwc(args.from_date, args.to_date)
        return

    # Get QB IDs
    if args.use_db:
        print(f"\n  Getting QB IDs from Azure DB...")
        qb_ids = fetch_qb_from_db(args.from_date, args.to_date)
    else:
        print(f"\n  Getting QB IDs from server...")
        qb_ids = fetch_qb_from_server()

    if not qb_ids:
        print("  No QB IDs found. Exiting.")
        return

    # Get NS tranIds
    print("\n  Authenticating with NetSuite...")
    token = get_ns_token()
    print("  OK")

    ns_grouped = fetch_ns_tranids(token, args.from_date, args.to_date)

    # Analyze
    analyze(ns_grouped, qb_ids, args.from_date, args.to_date)


if __name__ == "__main__":
    main()