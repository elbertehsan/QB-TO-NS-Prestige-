"""
Delete Blank TranId Entries — Prestige April 2026
==================================================
Deletes all journal entries with blank/null tranId for subsidiary 15
in the April 2026 date range.

44,223 total NS entries - 11,458 blank tranId = 32,765 = matches QB exactly

    python delete_blank_tranids.py                    # count only (safe)
    python delete_blank_tranids.py --delete           # count then delete
    python delete_blank_tranids.py --delete --chunk 100  # delete in batches of 100
"""

import argparse
import json
import requests
from datetime import datetime, timedelta
from dotenv import dotenv_values
import time
from netsuite_posting import get_jwt_token, generate_access_token
from logger_config import logger

# Token expires after ~60 minutes — refresh every 45 mins to be safe
TOKEN_REFRESH_INTERVAL = 45 * 60   # seconds
DELAY_BETWEEN_DELETES  = 0.3       # seconds between each delete (avoid NS rate limit)

config        = dotenv_values(".env")
NETSUITE_BASE = config.get('NETSITE_BASE_URL').rstrip('/')
JWT_BASE_URL  = config.get('AZURE_NREST_BASE_URL')
SUBSIDIARY_ID = 15
FROM_DATE     = "2026-04-01"
TO_DATE       = "2026-04-30"
# NOTE: Filter is on t.trandate (the QB transaction date stored in NetSuite).
# Entries with March invoice dates processed in April will have trandate=March
# and are EXCLUDED from this query — so you are safe.


class TokenManager:
    """Automatically refreshes the NetSuite access token before it expires."""

    def __init__(self):
        self.token      = None
        self.fetched_at = None
        self.refresh()

    def refresh(self):
        print("  🔑 Refreshing NetSuite access token...")
        jwt         = get_jwt_token(JWT_BASE_URL)
        self.token  = generate_access_token(NETSUITE_BASE, jwt)
        self.fetched_at = time.time()
        if self.token and self.token.get("access_token"):
            print("  ✅ Token refreshed OK")
        else:
            raise RuntimeError("Failed to get NetSuite access token")

    def get(self):
        """Return current token, refreshing if it's close to expiry."""
        elapsed = time.time() - self.fetched_at
        if elapsed >= TOKEN_REFRESH_INTERVAL:
            self.refresh()
        return self.token


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
            print(f"  [ERROR] {r.status_code}: {r.text[:200]}")
            break
        data = r.json()
        rows = data.get("items", [])
        all_rows.extend(rows)
        if not data.get("hasMore") or not rows:
            break
        offset += limit
        print(f"  Fetched {len(all_rows)} rows...", end="\r")
    return all_rows


def fetch_blank_tranid_ids(token, from_date=FROM_DATE, to_date=TO_DATE) -> list:
    from_dt  = datetime.strptime(from_date, "%Y-%m-%d")
    to_dt    = datetime.strptime(to_date,   "%Y-%m-%d")
    all_ids  = []
    current  = from_dt
    chunk    = 0

    print(f"\n  Fetching blank-tranId entries week by week...")
    print(f"  Filter: t.trandate (QB transaction date) — NOT invoice/created date")
    print(f"  March invoices processed in April will have trandate=March → excluded ✅")
    while current <= to_dt:
        chunk_end = min(current + timedelta(days=6), to_dt)
        fs = current.strftime("%Y-%m-%d")
        ts = chunk_end.strftime("%Y-%m-%d")
        chunk += 1
        print(f"  Week {chunk}: {fs} → {ts}", end="  ")

        rows = run_suiteql(token, f"""
            SELECT t.id, t.trandate, t.createddate,
                   e.firstname AS first, e.lastname AS last
            FROM   transaction t
            LEFT JOIN employee e ON e.id = t.createdby
            WHERE  t.recordtype = 'journalentry'
            AND    t.subsidiary = {SUBSIDIARY_ID}
            AND    t.trandate  >= TO_DATE('{fs}', 'YYYY-MM-DD')
            AND    t.trandate  <= TO_DATE('{ts}', 'YYYY-MM-DD')
            AND    (t.tranid IS NULL OR t.tranid = '')
            ORDER  BY t.trandate ASC, t.id ASC
        """)

        print(f"{len(rows)} blank entries")
        for row in rows:
            first   = row.get("first") or ""
            last    = row.get("last")  or ""
            creator = f"{first} {last}".strip() or "unknown"
            all_ids.append({
                "id":       str(row.get("id")),
                "trandate": str(row.get("trandate")),
                "created":  str(row.get("createddate",""))[:10],
                "creator":  creator,
            })

        current = chunk_end + timedelta(days=1)

    return all_ids


def delete_entry(token, ns_id: str):
    base = NETSUITE_BASE
    if base.endswith('/services/rest'):
        url = f"{base}/record/v1/journalentry/{ns_id}"
    else:
        url = f"{base}/services/rest/record/v1/journalentry/{ns_id}"

    try:
        r = requests.delete(
            url,
            headers={"Authorization": f"{token.get('token_type')} {token.get('access_token')}"},
            timeout=(15, 60)
        )
        if r.status_code == 204:
            return "deleted"
        if r.status_code == 401:
            return "failed_401"
        try:
            reason = r.json().get("o:errorDetails", [{}])[0].get("detail", "")
            if "closed period" in reason.lower():
                return "closed_period"
        except:
            pass
        return f"failed_{r.status_code}"
    except requests.exceptions.Timeout:
        return "failed_timeout"
    except Exception as e:
        logger.error(f"Delete exception id={ns_id}: {e}")
        return "failed_exception"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--delete",     action="store_true",
                        help="Delete the blank-tranId entries (default: count only)")
    parser.add_argument("--chunk",      type=int, default=50,
                        help="Show progress every N deletions (default 50)")
    parser.add_argument("--from-date",  default=None,
                        help=f"Start date YYYY-MM-DD (default {FROM_DATE})")
    parser.add_argument("--to-date",    default=None,
                        help=f"End date YYYY-MM-DD (default {TO_DATE})")
    args = parser.parse_args()

    # Use args if provided, otherwise fall back to module defaults
    from_date = args.from_date if args.from_date else FROM_DATE
    to_date   = args.to_date   if args.to_date   else TO_DATE

    print(f"\n{'='*65}")
    print(f"  Blank TranId Cleanup — Prestige (subsidiary {SUBSIDIARY_ID})")
    print(f"  Period: {from_date} → {to_date}")
    print(f"{'='*65}")

    print("\n  Authenticating...")
    token = get_ns_token()
    print("  OK")

    entries = fetch_blank_tranid_ids(token, from_date, to_date)

    if not entries:
        print("\n  ✅ No blank-tranId entries found.")
        return

    from collections import Counter
    creator_counts = Counter(e["creator"] for e in entries)

    print(f"\n{'='*65}")
    print(f"  SUMMARY")
    print(f"{'='*65}")
    print(f"  Blank tranId entries found : {len(entries)}")
    print(f"\n  By creator:")
    for creator, count in creator_counts.most_common():
        print(f"    {creator:<40} {count:>6}")

    print(f"\n  📊 Count verification:")
    print(f"    NS total entries        : 44,223")
    print(f"    Blank tranId entries    : {len(entries)}")
    print(f"    Remaining after delete  : {44223 - len(entries)}")
    print(f"    QB transactions (April) : 32,765")
    diff = (44223 - len(entries)) - 32765
    match = "✅ WILL MATCH" if diff == 0 else f"⚠ diff={diff:+d}"
    print(f"    Result                  : {match}")

    # Save list
    fname = f"blank_tranid_ids_{FROM_DATE.replace('-','')}.json"
    with open(fname, "w") as f:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "count":   len(entries),
            "entries": entries,
        }, f, indent=2)
    print(f"\n  Entry list saved → {fname}")

    if not args.delete:
        print(f"\n  DRY RUN — nothing deleted.")
        print(f"  To delete all {len(entries)} entries run:")
        print(f"    python delete_blank_tranids.py --delete")
        return

    # Confirm
    print(f"\n  ⚠  About to permanently delete {len(entries)} entries from NetSuite.")
    print(f"  Token auto-refreshes every {TOKEN_REFRESH_INTERVAL//60} minutes.")
    print(f"  Delay between deletes: {DELAY_BETWEEN_DELETES}s (avoids NS rate limiting)")
    confirm = input(f"\n  Type 'yes' to confirm: ")
    if confirm.strip().lower() != "yes":
        print("  Aborted.")
        return

    # Use token manager for auto-refresh
    tm = TokenManager()

    deleted, closed_period, failed = [], [], []
    total    = len(entries)
    start_ts = time.time()

    print(f"\n  Deleting {total} entries sequentially...\n")

    for i, entry in enumerate(entries, 1):
        # Auto-refresh token if needed
        current_token = tm.get()

        # Delete with one retry on failure
        result = delete_entry(current_token, entry["id"])

        if result == "failed_401":
            # Token may have just expired — force refresh and retry once
            print(f"\n  ⚠  401 on id={entry['id']} — forcing token refresh and retrying...")
            tm.refresh()
            current_token = tm.get()
            result = delete_entry(current_token, entry["id"])

        if result == "deleted":
            deleted.append(entry["id"])
        elif result == "closed_period":
            closed_period.append(entry["id"])
        else:
            failed.append(entry["id"])
            logger.warning(f"Delete failed id={entry['id']} result={result}")

        # Rate limit — wait between each delete
        time.sleep(DELAY_BETWEEN_DELETES)

        # Progress every chunk
        if i % args.chunk == 0 or i == total:
            elapsed  = int(time.time() - start_ts)
            rate     = i / elapsed if elapsed > 0 else 0
            eta_secs = int((total - i) / rate) if rate > 0 else 0
            eta_mins = eta_secs // 60
            eta_s    = eta_secs % 60
            token_age = int(time.time() - tm.fetched_at) // 60

            pct = round((i / total) * 100)
            print(f"  {i:>6}/{total} ({pct:>3}%)  "
                  f"✅ {len(deleted):>5} deleted  "
                  f"🔒 {len(closed_period):>4} closed  "
                  f"❌ {len(failed):>3} failed  "
                  f"⏱ ETA {eta_mins}m{eta_s:02d}s  "
                  f"🔑 token age {token_age}m")

    print(f"\n{'='*65}")
    print(f"  DONE")
    print(f"{'='*65}")
    print(f"  ✅ Deleted         : {len(deleted)}")
    print(f"  🔒 Closed period   : {len(closed_period)}")
    print(f"  ❌ Failed          : {len(failed)}")
    remaining = 44223 - len(deleted)
    diff = remaining - 32765
    print(f"\n  NS entries remaining : {remaining}")
    print(f"  QB transactions      : 32,765")
    print(f"  Difference           : {diff:+d}  {'✅ MATCH' if diff == 0 else '(closed period entries)'}")

    if closed_period:
        print(f"\n  🔒 {len(closed_period)} entries in closed periods — cannot be deleted via API.")
        print(f"  Ask your NetSuite admin to unlock the period, or accept this small difference.")

    print(f"\n{'='*65}\n")


if __name__ == "__main__":
    main()