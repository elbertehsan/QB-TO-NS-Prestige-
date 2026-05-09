"""
Recovery Queue — Posts only the dates with missing transactions.
Queues specific dates on the diagnostic server so QBWC picks them up
and master_function posts them to NetSuite.

Run AFTER:
  1. subsidiary fixed to 15 in netsuite_data_transformation.py
  2. check_journal_entry_by_tranid fixed with subsidiary filter
  3. SYNC_CHECK_MODE = False in server.py
  4. master_function(final_merged) uncommented

    python queue_missing.py
"""

import requests
from datetime import datetime, timedelta

DIAG_HOST = "http://127.0.0.1:8001"

# Dates extracted from the 367 missing transactions
# Grouped to minimize QBWC sessions
MISSING_DATES = sorted(set([
    "2026-04-01", "2026-04-02", "2026-04-03",
    "2026-04-07", "2026-04-10", "2026-04-11",
    "2026-04-12", "2026-04-13", "2026-04-14",
    "2026-04-15", "2026-04-16", "2026-04-17",
    "2026-04-20", "2026-04-22", "2026-04-23",
    "2026-04-24", "2026-04-25", "2026-04-26",
    "2026-04-27", "2026-04-28", "2026-04-29",
    "2026-04-30",
]))


def queue_range(from_date: str, to_date: str):
    try:
        r    = requests.get(
            f"{DIAG_HOST}/queue?from_date={from_date}&to_date={to_date}",
            timeout=10
        )
        data = r.json()
        print(f"  ✅ Queued {data.get('chunks_queued')} chunks: {from_date} → {to_date}")
        return True
    except requests.ConnectionError:
        print(f"  [ERROR] Cannot connect to server on port 8001. Is server.py running?")
        return False
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


def main():
    print(f"\n{'='*65}")
    print(f"  Recovery Queue — Missing Transactions")
    print(f"  Queuing {len(MISSING_DATES)} days that have missing transactions")
    print(f"{'='*65}")

    print(f"\n  ⚠  BEFORE RUNNING THIS:")
    print(f"  1. subsidiary = '15' in netsuite_data_transformation.py ✓?")
    print(f"  2. check_journal_entry_by_tranid has subsidiary filter ✓?")
    print(f"  3. SYNC_CHECK_MODE = False in server.py ✓?")
    print(f"  4. master_function(final_merged) uncommented ✓?")
    confirm = input(f"\n  All confirmed? Type 'yes' to queue: ")
    if confirm.strip().lower() != "yes":
        print("  Aborted.")
        return

    # Queue as one continuous range — server handles day-by-day chunking
    # Use the full range since master_function checks existing tranIds before posting
    from_date = MISSING_DATES[0]   # 2026-04-01
    to_date   = MISSING_DATES[-1]  # 2026-04-30

    print(f"\n  Queuing {from_date} → {to_date}...")
    print(f"  (master_function will skip already-posted transactions automatically)")

    success = queue_range(from_date, to_date)

    if success:
        print(f"\n{'='*65}")
        print(f"  NEXT STEPS:")
        print(f"  1. Click 'Update Selected' in QBWC")
        print(f"  2. Wait for progress bars to reach 100%")
        print(f"     (30 chunks × ~15s each ≈ 7-8 minutes)")
        print(f"  3. Check server.py terminal for posting logs")
        print(f"  4. Run sync check again to verify:")
        print(f"     python direct_sync_check.py --from-date 2026-04-01 --to-date 2026-04-30 --compare")
        print(f"{'='*65}\n")


if __name__ == "__main__":
    main()