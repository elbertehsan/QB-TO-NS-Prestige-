"""
Delete All NS Journal Entries — Prestige April 2026
====================================================
Fetches all journal entries for subsidiary 15 in April 2026
then deletes each one using a FRESH token per delete call.

Usage:
    python delete_april_entries.py              # dry run — shows what would be deleted
    python delete_april_entries.py --delete     # actually deletes

Safety:
    - Dry run by default (must pass --delete to actually delete)
    - Prints progress every 10 deletes
    - Logs all failures to delete_failures.log
    - Skips entries with blank/JEA tranIds if --skip-manual is passed
"""

import argparse
import time
import requests
from datetime import datetime
from dotenv import dotenv_values
from netsuite_posting import get_jwt_token, generate_access_token
from logger_config import logger

config        = dotenv_values(".env")
NETSUITE_BASE = config.get('NETSITE_BASE_URL').rstrip('/')
JWT_BASE_URL  = config.get('AZURE_NREST_BASE_URL')

SUBSIDIARY_ID = 15
FROM_DATE     = "2026-04-01"
TO_DATE       = "2026-04-30"


# ── Auth — fresh token every call ─────────────────────────────────────────────

def get_fresh_token():
    jwt   = get_jwt_token(JWT_BASE_URL)
    token = generate_access_token(NETSUITE_BASE, jwt)
    return token


def auth_headers(token):
    return {
        "Authorization": f"{token.get('token_type')} {token.get('access_token')}",
        "Content-Type":  "application/json",
        "Prefer":        "transient",
    }


# ── SuiteQL — fetch all April journal entries ─────────────────────────────────

def fetch_all_april_entries():
    """
    Returns list of { id, tran_id, tran_date } for all journal entries
    in subsidiary 15 for April 2026.
    """
    print(f"\n  Fetching all April 2026 journal entries from NS (subsidiary {SUBSIDIARY_ID})...")

    base = NETSUITE_BASE
    if base.endswith('/services/rest'):
        base = base[:-len('/services/rest')]
    url = f"{base}/services/rest/query/v1/suiteql"

    query = f"""
        SELECT
            t.id        AS id,
            t.tranid    AS tran_id,
            t.trandate  AS tran_date
        FROM transaction t
        WHERE t.recordtype = 'journalentry'
        AND   t.subsidiary = {SUBSIDIARY_ID}
        AND   t.trandate  >= TO_DATE('{FROM_DATE}', 'YYYY-MM-DD')
        AND   t.trandate  <= TO_DATE('{TO_DATE}',   'YYYY-MM-DD')
        ORDER BY t.trandate ASC, t.id ASC
    """

    all_entries = []
    offset      = 0
    limit       = 500

    # Use one token just for fetching — deletes each get their own
    fetch_token = get_fresh_token()

    while True:
        r = requests.post(
            url,
            headers=auth_headers(fetch_token),
            json={"q": query},
            params={"limit": limit, "offset": offset},
            timeout=(30, 120),
        )

        if r.status_code not in (200, 204):
            print(f"\n  [ERROR] SuiteQL fetch failed: HTTP {r.status_code}")
            print(f"  Detail: {r.text[:500]}")
            break

        data  = r.json()
        rows  = data.get("items", [])
        all_entries.extend(rows)

        print(f"  Fetched {len(all_entries)} entries...", end="\r")

        if not data.get("hasMore") or not rows:
            break

        offset += limit

    print(f"  Fetched {len(all_entries)} total entries.          ")
    return all_entries


# ── Delete one entry — fresh token every time ─────────────────────────────────

def delete_entry(ns_id, tran_id, tran_date, dry_run):
    """
    Deletes a single journal entry by its internal NS id.
    Gets a fresh token before every delete call.
    Returns (success: bool, error_msg: str or None)
    """
    record_url = f"{NETSUITE_BASE}/record/v1/journalentry/{ns_id}"

    if dry_run:
        return True, None

    # Fresh token for every delete
    token = get_fresh_token()

    try:
        r = requests.delete(
            record_url,
            headers=auth_headers(token),
            timeout=(15, 60),
        )

        if r.status_code == 204:
            return True, None
        else:
            return False, f"HTTP {r.status_code}: {r.text[:200]}"

    except requests.exceptions.Timeout:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--delete",       action="store_true",
                        help="Actually delete. Without this flag it's a dry run.")
    parser.add_argument("--skip-manual",  action="store_true",
                        help="Skip entries where tranId starts with JEA (manual NS entries)")
    args = parser.parse_args()

    dry_run = not args.delete

    print(f"\n{'='*65}")
    print(f"  NS Delete — Prestige April 2026")
    print(f"  Subsidiary {SUBSIDIARY_ID} | {FROM_DATE} → {TO_DATE}")
    print(f"  Mode: {'DRY RUN (pass --delete to actually delete)' if dry_run else '⚠  LIVE DELETE'}")
    print(f"  Token: fresh per delete")
    if args.skip_manual:
        print(f"  Skipping: JEA manual entries")
    print(f"{'='*65}")

    if not dry_run:
        confirm = input("\n  Type YES to confirm live deletion: ")
        if confirm.strip() != "YES":
            print("  Cancelled.")
            return

    # Step 1 — Fetch all entries
    entries = fetch_all_april_entries()

    if not entries:
        print("\n  No entries found. Nothing to delete.")
        return

    # Step 2 — Filter if requested
    to_delete   = []
    skipped_jea = []

    for e in entries:
        tran_id = str(e.get("tran_id") or "").strip()
        if args.skip_manual and (not tran_id or tran_id.upper().startswith("JEA")):
            skipped_jea.append(e)
        else:
            to_delete.append(e)

    print(f"\n  Total found       : {len(entries):>6,}")
    print(f"  To delete         : {len(to_delete):>6,}")
    if args.skip_manual:
        print(f"  Skipped (JEA/blank): {len(skipped_jea):>6,}")

    if not to_delete:
        print("\n  Nothing to delete after filters.")
        return

    if dry_run:
        print(f"\n  [DRY RUN] Would delete {len(to_delete):,} entries.")
        print(f"  First 10:")
        for e in to_delete[:10]:
            print(f"    NS id={e['id']}  tranId={e.get('tran_id') or 'BLANK':<25}  date={e.get('tran_date')}")
        print(f"\n  Run with --delete to actually delete.")
        return

    # Step 3 — Delete one by one, fresh token each time
    print(f"\n  Deleting {len(to_delete):,} entries (fresh token per delete)...\n")

    success_count = 0
    fail_count    = 0
    failures      = []
    start_time    = time.time()

    for i, entry in enumerate(to_delete, 1):
        ns_id    = entry["id"]
        tran_id  = str(entry.get("tran_id") or "BLANK")
        tran_date= str(entry.get("tran_date") or "")

        ok, err = delete_entry(ns_id, tran_id, tran_date, dry_run=False)

        if ok:
            success_count += 1
        else:
            fail_count += 1
            failures.append({"id": ns_id, "tran_id": tran_id, "date": tran_date, "error": err})
            logger.error(f"Delete failed: NS id={ns_id} tranId={tran_id} | {err}")

        # Progress every 10
        if i % 10 == 0 or i == len(to_delete):
            elapsed = time.time() - start_time
            rate    = i / elapsed if elapsed > 0 else 0
            eta_s   = (len(to_delete) - i) / rate if rate > 0 else 0
            eta_m   = int(eta_s // 60)
            eta_s   = int(eta_s % 60)
            print(
                f"  [{i:>5}/{len(to_delete)}]  "
                f"✅ {success_count}  ❌ {fail_count}  "
                f"ETA {eta_m}m {eta_s:02d}s",
                end="\r"
            )

    elapsed_total = time.time() - start_time
    print(f"\n\n{'='*65}")
    print(f"  DONE")
    print(f"  Deleted successfully : {success_count:,}")
    print(f"  Failed               : {fail_count:,}")
    print(f"  Total time           : {int(elapsed_total//60)}m {int(elapsed_total%60):02d}s")
    print(f"{'='*65}")

    # Save failures to file
    if failures:
        fname = f"delete_failures_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        with open(fname, "w") as f:
            for fail in failures:
                f.write(f"NS id={fail['id']}  tranId={fail['tran_id']}  date={fail['date']}  error={fail['error']}\n")
        print(f"\n  Failed entries saved → {fname}")
        print(f"  Re-run the script and it will retry only the failed ones.")


if __name__ == "__main__":
    main()