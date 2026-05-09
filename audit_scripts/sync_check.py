"""
Immediate QB ↔ NetSuite Sync Checker
======================================
Run this ONE command and it does everything automatically:
  1. Queues the date range on the diagnostic server (port 8001)
  2. Tells you to click Update Selected in QBWC
  3. Polls every 10 seconds until QB data collection is complete
  4. Queries NetSuite for the same range
  5. Compares both and shows exactly what differs

Subsidiary 14 (Prestige Fleet Services) only.

Usage:
    python sync_check.py --from-date 2026-04-01 --to-date 2026-04-30

    # With QB financial total for gap analysis
    python sync_check.py --from-date 2026-04-01 --to-date 2026-04-30 --qb-total 6058185.21

    # Skip QB live fetch and use Azure DB instead (no QBWC needed)
    python sync_check.py --from-date 2026-04-01 --to-date 2026-04-30 --use-db
"""

import argparse
import json
import sys
import time
import requests
from datetime import datetime, date, timedelta
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
SUBSIDIARY_ID = 15
POLL_INTERVAL = 10   # seconds between polls


# ── NetSuite auth ─────────────────────────────────────────────────────────────

def get_ns_token():
    jwt   = get_jwt_token(JWT_BASE_URL)
    token = generate_access_token(NETSUITE_BASE, jwt)
    if not token or not token.get('access_token'):
        raise RuntimeError("NetSuite authentication failed — check .env credentials")
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
    all_rows, offset, limit = [], 0, 1000

    while True:
        try:
            r = requests.post(
                url, headers=ns_headers(token),
                json={"q": query},
                params={"limit": limit, "offset": offset},
                timeout=(10, 120)
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
            offset += limit
            print(f"  Fetched {len(all_rows)} NS entries...", end="\r")
        except Exception as e:
            logger.error(f"SuiteQL error: {e}")
            print(f"  [NS ERROR] {e}")
            break
    return all_rows


# ── Fetch NetSuite transactions ───────────────────────────────────────────────

def fetch_netsuite(token, from_date: str, to_date: str) -> dict:
    """
    Returns { bare_tran_id -> { internal_id, tranid, trandate, total_debit } }
    Only QB-originated numeric tranIds. Subsidiary 14 only.
    """
    print(f"\n  Querying NetSuite (subsidiary {SUBSIDIARY_ID} — Prestige)...")

    query = f"""
        SELECT
            t.id,
            t.tranid,
            t.trandate,
            SUM(CASE WHEN jel.debit >= 0 THEN jel.debit ELSE 0 END) AS total_debit
        FROM
            transaction t
            INNER JOIN journalentryline jel ON jel.journal = t.id
        WHERE
            t.recordtype  = 'journalentry'
            AND t.subsidiary = {SUBSIDIARY_ID}
            AND t.trandate >= TO_DATE('{from_date}', 'YYYY-MM-DD')
            AND t.trandate <= TO_DATE('{to_date}', 'YYYY-MM-DD')
        GROUP BY
            t.id, t.tranid, t.trandate
        ORDER BY
            t.trandate ASC
    """

    rows = run_suiteql(token, query)
    print(f"  NetSuite returned {len(rows)} journal entries          ")

    result = {}
    for row in rows:
        tranid = str(row.get("tranid") or "")
        bare   = tranid.replace("-14", "").strip()
        if bare.isdigit():
            result[bare] = {
                "internal_id": str(row.get("id")),
                "tranid":      tranid,
                "trandate":    str(row.get("trandate")),
                "total_debit": float(row.get("total_debit") or 0),
            }

    print(f"  QB-style entries in NetSuite: {len(result)}")
    return result


# ── Fetch QB via diagnostic server (live QBWC) ───────────────────────────────

def fetch_qb_live(from_date: str, to_date: str, fetch_only: bool = False) -> dict:
    """
    If fetch_only=True: just grabs whatever is already in /results (no re-queue).
    Otherwise: queues the range, waits for QBWC, then returns collected transactions.
    """
    print(f"\n  Connecting to diagnostic server on port 8001...")

    # fetch_only mode — just grab what the server already has
    if fetch_only:
        try:
            r    = requests.get(f"{DIAG_HOST}/results", timeout=10)
            data = r.json()
            txns = data.get("qb_transactions", {})
            collected = data.get("collected", 0)
            complete  = data.get("complete", False)
            if not txns:
                print(f"  [WARN] Server has no QB transactions yet.")
                print(f"  Run with --queue first, click Update Selected in QBWC, then run again with --fetch.")
                return {}
            status = "complete" if complete else "still processing"
            print(f"  Fetched {collected} transactions from server ({status})")
            return txns
        except requests.ConnectionError:
            print(f"  [ERROR] Cannot connect to diagnostic server on port 8001.")
            return {}

    # Normal mode — queue first, then wait
    try:
        r = requests.get(
            f"{DIAG_HOST}/queue?from_date={from_date}&to_date={to_date}",
            timeout=10
        )
        data = r.json()
        if r.status_code != 200:
            print(f"  [ERROR] {data.get('error')}")
            return {}
        chunks = data.get('chunks_queued', 0)
        print(f"  Queued {chunks} chunks ({from_date} → {to_date})")
    except requests.ConnectionError:
        print(f"\n  [ERROR] Cannot connect to diagnostic server on port 8001.")
        print(f"  Make sure server.py is running. Use --use-db to skip QB live fetch.")
        return {}

    print(f"\n  ╔══════════════════════════════════════════════════════════════╗")
    print(f"  ║  ACTION REQUIRED                                             ║")
    print(f"  ║  1. Open QuickBooks Web Connector                           ║")
    print(f"  ║  2. Click  'Update Selected'                                ║")
    print(f"  ║  3. Wait for the progress bars to reach 100%                ║")
    print(f"  ║  This script will automatically continue when done.         ║")
    print(f"  ╚══════════════════════════════════════════════════════════════╝\n")

    start    = time.time()
    last_pct = -1

    while True:
        time.sleep(POLL_INTERVAL)
        try:
            r    = requests.get(f"{DIAG_HOST}/results", timeout=10)
            data = r.json()
        except Exception as e:
            print(f"  Poll error: {e} — retrying...")
            continue

        collected = data.get("collected", 0)
        pending   = data.get("pending_chunks", 0)
        complete  = data.get("complete", False)
        elapsed   = int(time.time() - start)

        total_chunks = chunks
        done_chunks  = max(0, total_chunks - pending)
        pct          = int((done_chunks / total_chunks) * 100) if total_chunks else 0

        if pct != last_pct:
            print(f"  [{elapsed:>4}s]  Chunks: {done_chunks}/{total_chunks}  "
                  f"Transactions collected: {collected}  ({pct}%)", end="\r")
            last_pct = pct

        if complete:
            print(f"\n  ✅ QB sync complete — {collected} transactions collected in {elapsed}s")
            return data.get("qb_transactions", {})


# ── Fetch QB via Azure DB (fallback) ─────────────────────────────────────────

def fetch_qb_db(from_date_str: str, to_date_str: str) -> dict:
    print(f"\n  Fetching QB transactions from Azure DB...")
    from_dt = datetime.strptime(from_date_str, "%Y-%m-%d").date()
    to_dt   = datetime.strptime(to_date_str,   "%Y-%m-%d").date()

    result = {}
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
        print(f"  Azure DB: {len(result)} QB transaction IDs found")
    except Exception as e:
        logger.error(f"DB error: {e}")
        print(f"  [ERROR] DB: {e}")
    return result


# ── Compare ───────────────────────────────────────────────────────────────────

def compare(qb_txns: dict, ns_data: dict) -> dict:
    qb_ids = set(qb_txns.keys())
    ns_ids = set(ns_data.keys())
    in_both  = qb_ids & ns_ids
    only_qb  = qb_ids - ns_ids   # In QB, missing from NetSuite
    only_ns  = ns_ids - qb_ids   # In NetSuite, not in QB (over-posted)

    return {
        "in_both":          sorted(in_both),
        "only_in_qb":       sorted(only_qb),
        "only_in_netsuite": sorted(only_ns),
        "synced_total":     sum(ns_data[i]["total_debit"] for i in in_both),
        "extra_ns_total":   sum(ns_data[i]["total_debit"] for i in only_ns),
        "qb_txns":          qb_txns,
        "ns_data":          ns_data,
    }


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(result: dict, from_date, to_date, qb_source: str, qb_total: float = None):
    div     = "─" * 70
    in_both = result["in_both"]
    only_qb = result["only_in_qb"]
    only_ns = result["only_in_netsuite"]
    ns_data = result["ns_data"]
    qb_txns = result["qb_txns"]
    synced_total  = result["synced_total"]
    extra_ns_total = result["extra_ns_total"]
    is_synced = len(only_qb) == 0 and len(only_ns) == 0

    print(f"\n{'='*70}")
    print(f"  QB ↔ NETSUITE SYNC REPORT")
    print(f"  Subsidiary {SUBSIDIARY_ID} — Prestige Fleet Services")
    print(f"  Period    : {from_date}  →  {to_date}")
    print(f"  QB source : {qb_source}")
    print(f"{'='*70}")
    print(f"\n  Overall : {'✅  IN SYNC' if is_synced else '❌  OUT OF SYNC'}")
    print(f"\n  {'Category':<45} {'Count':>6}  {'Amount':>14}")
    print(div)
    print(f"  {'✅ Synced (in QB + NetSuite):':<45} {len(in_both):>6}  ${synced_total:>13,.2f}")
    print(f"  {'❌ In QB — MISSING from NetSuite:':<45} {len(only_qb):>6}")
    print(f"  {'⚠  In NetSuite — NOT in QB (over-posted):':<45} {len(only_ns):>6}  ${extra_ns_total:>13,.2f}")

    if qb_total:
        ns_total = synced_total + extra_ns_total
        gap      = ns_total - qb_total
        print(f"\n  Financial Summary")
        print(div)
        print(f"  QB total (provided)          : ${qb_total:>14,.2f}")
        print(f"  NetSuite QB-sync total       : ${ns_total:>14,.2f}")
        print(f"  Gap (NetSuite − QB)          : ${gap:>14,.2f}  "
              f"{'✅ MATCH' if abs(gap) < 1 else '❌ MISMATCH'}")

    # Missing from NetSuite
    if only_qb:
        print(f"\n  ❌ IN QB — MISSING FROM NETSUITE")
        print(div)
        print(f"  {'tranId':<15}  QB Date")
        print(f"  {'─'*14}  {'─'*12}")
        for tid in sorted(only_qb, key=lambda x: int(x) if x.isdigit() else x):
            print(f"  {tid:<15}  {qb_txns.get(tid, '')}")

    # Over-posted in NetSuite
    if only_ns:
        print(f"\n  ⚠  IN NETSUITE — NOT IN QB  (over-posted)")
        print(div)
        print(f"  {'tranId':<15}  {'NS id':<12}  {'Date':<14}  {'Debit':>12}")
        print(f"  {'─'*14}  {'─'*11}  {'─'*13}  {'─'*12}")
        for tid in sorted(only_ns, key=lambda x: int(x) if x.isdigit() else x):
            e = ns_data[tid]
            print(f"  {tid:<15}  {e['internal_id']:<12}  {e['trandate']:<14}  ${e['total_debit']:>11,.2f}")
        print(f"\n  Over-posted total : ${extra_ns_total:,.2f}")

    if is_synced:
        print(f"\n  All {len(in_both)} QB transactions are in NetSuite. No issues. ✅")

    print(f"\n{'='*70}")

    # Save JSON
    report = {
        "generated_at": datetime.now().isoformat(),
        "subsidiary":   f"{SUBSIDIARY_ID} (Prestige Fleet Services)",
        "date_range":   {"from": str(from_date), "to": str(to_date)},
        "qb_source":    qb_source,
        "status":       "IN_SYNC" if is_synced else "OUT_OF_SYNC",
        "summary": {
            "synced_count":       len(in_both),
            "synced_total":       round(synced_total, 2),
            "missing_from_ns":    len(only_qb),
            "extra_in_ns_count":  len(only_ns),
            "extra_in_ns_total":  round(extra_ns_total, 2),
            "qb_total_provided":  qb_total,
            "gap": round((synced_total + extra_ns_total) - qb_total, 2) if qb_total else None,
        },
        "missing_from_netsuite": [
            {"tran_id": t, "qb_date": qb_txns.get(t, "")} for t in sorted(only_qb)
        ],
        "extra_in_netsuite": [
            {**ns_data[t], "bare_tran_id": t} for t in sorted(only_ns)
        ],
    }

    fname = f"sync_check_{str(from_date).replace('-','')}_{str(to_date).replace('-','')}.json"
    with open(fname, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n  Report saved → {fname}\n")
    return is_synced


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Immediately compare QB vs NetSuite for Prestige (subsidiary 14)."
    )
    parser.add_argument("--from-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to-date",   required=True, help="YYYY-MM-DD")
    parser.add_argument("--qb-total",  type=float, default=None,
                        help="QuickBooks Total Income for the period")
    parser.add_argument("--use-db",  action="store_true",
                        help="Use Azure DB as QB source instead of live QBWC (no QBWC needed)")
    parser.add_argument("--fetch",   action="store_true",
                        help="Fetch QB data already collected by server.py — no re-queue, no QBWC click needed")
    args = parser.parse_args()

    print(f"\n{'='*70}")
    print(f"  QB ↔ NetSuite Sync Checker  |  Subsidiary {SUBSIDIARY_ID} (Prestige)")
    print(f"{'='*70}")

    # ── Step 1: Get QB data ───────────────────────────────────────────────────
    if args.use_db:
        qb_txns   = fetch_qb_db(args.from_date, args.to_date)
        qb_source = "Azure DB"
    elif args.fetch:
        qb_txns   = fetch_qb_live(args.from_date, args.to_date, fetch_only=True)
        qb_source = "QBWC live sync (already collected)"
    else:
        qb_txns   = fetch_qb_live(args.from_date, args.to_date, fetch_only=False)
        qb_source = "QBWC live sync"

    if not qb_txns:
        print("\n  No QB transactions found. Cannot run comparison.")
        sys.exit(1)

    # ── Step 2: Get NetSuite data ─────────────────────────────────────────────
    print("\n  Authenticating with NetSuite...")
    token   = get_ns_token()
    print("  Authenticated OK")
    ns_data = fetch_netsuite(token, args.from_date, args.to_date)

    # ── Step 3: Compare and report ────────────────────────────────────────────
    print(f"\n  Comparing {len(qb_txns)} QB transactions vs {len(ns_data)} NetSuite entries...")
    result  = compare(qb_txns, ns_data)
    synced  = print_report(result, args.from_date, args.to_date, qb_source, args.qb_total)
    sys.exit(0 if synced else 1)


if __name__ == "__main__":
    main()