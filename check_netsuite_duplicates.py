"""
Chunked Duplicate Finder
=========================
Runs check_netsuite_duplicates week-by-week to avoid NetSuite timeouts
on large date ranges. Merges all results into a single report.

Usage:
    python find_duplicates_chunked.py --from-date 2026-04-01 --to-date 2026-04-30
    python find_duplicates_chunked.py --from-date 2026-04-01 --to-date 2026-04-30 --delete
"""

import argparse
import json
import requests
from collections import defaultdict
from datetime import datetime, timedelta
from dotenv import dotenv_values

from netsuite_posting import get_jwt_token, generate_access_token, delete_journal_entry
from logger_config import logger

config        = dotenv_values(".env")
NETSUITE_BASE = config.get('NETSITE_BASE_URL').rstrip('/')
JWT_BASE_URL  = config.get('AZURE_NREST_BASE_URL')
SUBSIDIARY_ID = 15  # Prestige


def get_headers(token):
    return {
        "Authorization": f"{token.get('token_type')} {token.get('access_token')}",
        "Content-Type":  "application/json",
        "Prefer":        "transient",
    }


def run_suiteql(token, query: str) -> list:
    base = NETSUITE_BASE
    if base.endswith('/services/rest'):
        base = base[:-len('/services/rest')]
    url      = f"{base}/services/rest/query/v1/suiteql"
    all_rows, offset, limit = [], 0, 500

    while True:
        try:
            r = requests.post(
                url, headers=get_headers(token),
                json={"q": query},
                params={"limit": limit, "offset": offset},
                timeout=(30, 300)
            )
            if r.status_code not in (200, 204):
                logger.error(f"SuiteQL {r.status_code}: {r.text[:300]}")
                print(f"\n  [ERROR] {r.status_code}: {r.text[:200]}")
                break
            data = r.json()
            rows = data.get("items", [])
            all_rows.extend(rows)
            if not data.get("hasMore") or not rows:
                break
            offset += limit
            print(f"  Fetched {len(all_rows)} rows...", end="\r")
        except requests.exceptions.Timeout:
            print(f"\n  [TIMEOUT] Try a smaller date range")
            break
        except Exception as e:
            logger.error(f"SuiteQL error: {e}")
            print(f"\n  [ERROR] {e}")
            break
    return all_rows


def fetch_week(token, from_date: str, to_date: str) -> list:
    query = f"""
        SELECT
            t.id,
            t.tranid,
            t.trandate,
            t.createddate,
            t.memo
        FROM
            transaction t
        WHERE
            t.recordtype  = 'journalentry'
            AND t.subsidiary = {SUBSIDIARY_ID}
            AND t.trandate >= TO_DATE('{from_date}', 'YYYY-MM-DD')
            AND t.trandate <= TO_DATE('{to_date}',   'YYYY-MM-DD')
        ORDER BY
            t.tranid ASC,
            t.createddate ASC
    """
    return run_suiteql(token, query)


def find_duplicates(entries: list) -> dict:
    grouped = defaultdict(list)
    for e in entries:
        tid = e.get("tranid")
        if tid:
            grouped[tid].append(e)
    return {k: v for k, v in grouped.items() if len(v) > 1}


def delete_by_id(token, internal_id: str) -> bool:
    location = f"{NETSUITE_BASE}/record/v1/journalentry/{internal_id}"
    r = delete_journal_entry(location, token)
    if r and r.status_code == 204:
        logger.info(f"Deleted id={internal_id}")
        return True
    status = r.status_code if r else "no response"
    print(f"    [ERROR] Delete failed id={internal_id} — HTTP {status}")
    return False


def week_chunks(from_date: datetime, to_date: datetime):
    """Yields (week_start, week_end) pairs."""
    current = from_date
    while current <= to_date:
        week_end = min(current + timedelta(days=6), to_date)
        yield current, week_end
        current = week_end + timedelta(days=1)


def main():
    parser = argparse.ArgumentParser(
        description="Find duplicates in Prestige (sub 15) week by week to avoid timeouts."
    )
    parser.add_argument("--from-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to-date",   required=True, help="YYYY-MM-DD")
    parser.add_argument("--delete",    action="store_true",
                        help="Delete duplicates after finding them")
    parser.add_argument("--chunk-days", type=int, default=7,
                        help="Days per chunk (default 7, reduce if still timing out)")
    args = parser.parse_args()

    from_dt = datetime.strptime(args.from_date, "%Y-%m-%d")
    to_dt   = datetime.strptime(args.to_date,   "%Y-%m-%d")

    print(f"\n{'='*70}")
    print(f"  Chunked Duplicate Finder — Prestige (subsidiary {SUBSIDIARY_ID})")
    print(f"  Period     : {args.from_date}  to  {args.to_date}")
    print(f"  Chunk size : {args.chunk_days} day(s)")
    print(f"{'='*70}")

    print("\n  Authenticating...")
    jwt   = get_jwt_token(JWT_BASE_URL)
    token = generate_access_token(NETSUITE_BASE, jwt)
    print("  OK\n")

    # Generate chunks
    chunks = []
    current = from_dt
    while current <= to_dt:
        chunk_end = min(current + timedelta(days=args.chunk_days - 1), to_dt)
        chunks.append((current, chunk_end))
        current = chunk_end + timedelta(days=1)

    print(f"  Scanning in {len(chunks)} chunk(s) of {args.chunk_days} day(s) each...\n")

    # Collect all entries across all chunks
    all_entries = []
    for i, (chunk_from, chunk_to) in enumerate(chunks, 1):
        fs = chunk_from.strftime("%Y-%m-%d")
        ts = chunk_to.strftime("%Y-%m-%d")
        print(f"  Chunk {i}/{len(chunks)}: {fs} → {ts}", end="  ")
        rows = fetch_week(token, fs, ts)
        print(f"fetched {len(rows)} entries          ")
        all_entries.extend(rows)

    print(f"\n  Total entries fetched: {len(all_entries)}")

    # Find duplicates across full dataset
    print(f"  Scanning for duplicates...")
    duplicates = find_duplicates(all_entries)
    total_excess = sum(len(v) - 1 for v in duplicates.values())

    print(f"\n{'='*70}")
    print(f"  RESULTS — Prestige (subsidiary {SUBSIDIARY_ID})")
    print(f"  Period: {args.from_date}  to  {args.to_date}")
    print(f"{'='*70}")
    print(f"  Total entries scanned    : {len(all_entries)}")
    print(f"  Duplicate tranIds found  : {len(duplicates)}")
    print(f"  Excess records to remove : {total_excess}")

    if not duplicates:
        print(f"\n  ✅ No duplicates found — Prestige NetSuite data is clean.")
    else:
        div = "─" * 70
        print(f"\n  {'tranId':<22} {'Count':<8} {'Internal IDs'}")
        print(div)
        for tran_id, entries in sorted(duplicates.items()):
            ids = ", ".join(
                f"{e['id']}[KEPT]" if i == 0 else f"{e['id']}[DUP]"
                for i, e in enumerate(entries)
            )
            print(f"  {tran_id:<22} {len(entries):<8} {ids}")

    # Delete
    deleted_ids = set()
    if args.delete and duplicates:
        print(f"\n  Strategy: keep LOWEST internal ID (oldest), delete the rest.")
        confirm = input(f"\n  Delete {total_excess} duplicate(s) from NetSuite? Type 'yes': ")
        if confirm.strip().lower() == "yes":
            for tran_id, entries_list in duplicates.items():
                for entry in entries_list[1:]:
                    iid = str(entry.get("id"))
                    print(f"  Deleting tranId={tran_id}  id={iid}...")
                    if delete_by_id(token, iid):
                        deleted_ids.add(iid)
            print(f"\n  Deleted {len(deleted_ids)} of {total_excess} duplicate(s).")
        else:
            print("  Aborted.")

    # Save JSON
    report = {
        "generated_at": datetime.now().isoformat(),
        "subsidiary":   f"{SUBSIDIARY_ID} (Prestige)",
        "date_range":   {"from": args.from_date, "to": args.to_date},
        "summary": {
            "total_entries":     len(all_entries),
            "duplicate_tranids": len(duplicates),
            "excess_records":    total_excess,
            "records_deleted":   len(deleted_ids),
        },
        "duplicates": {
            tran_id: [
                {
                    "internal_id": str(e.get("id")),
                    "trandate":    str(e.get("trandate")),
                    "createddate": str(e.get("createddate")),
                    "memo":        e.get("memo", ""),
                    "action":      "kept" if i == 0
                                   else "deleted" if str(e.get("id")) in deleted_ids
                                   else "duplicate_not_deleted",
                }
                for i, e in enumerate(entries)
            ]
            for tran_id, entries in duplicates.items()
        }
    }

    fname = f"prestige_duplicates_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(fname, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n  Report saved → {fname}\n")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()