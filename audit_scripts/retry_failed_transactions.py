"""
Log Parser — Find Failed Transactions & Queue for Retry
========================================================
Scans log files for transactions that failed to post to NetSuite
then queues their dates on the diagnostic server for QBWC to retry.

Failure pattern detected:
  "Failure notification sent for transaction: XXXXXXX"

Also detects:
  "NS post failed for XXXXXXX"
  "NS update failed for XXXXXXX"

Usage:
    python retry_failed_transactions.py                        # scan today's log
    python retry_failed_transactions.py --log app.log         # specific log file
    python retry_failed_transactions.py --log app.log --queue # scan + queue dates
    python retry_failed_transactions.py --log-dir logs/       # scan all logs in folder
"""

import re
import os
import argparse
import requests
from datetime import datetime
from collections import defaultdict

DIAG_HOST = "http://127.0.0.1:8001"


# ── Log patterns ──────────────────────────────────────────────────────────────

# Matches: "Failure notification sent for transaction: 1131301"
FAILURE_PATTERN = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+)'   # timestamp
    r'.*?Failure notification sent for transaction:\s*(\d+)'
)

# Matches: "NS post failed for 1131301"
POST_FAILED_PATTERN = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+)'
    r'.*?NS post failed for\s*(\d+)'
)

# Matches: "NS update failed for 1131301"
UPDATE_FAILED_PATTERN = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+)'
    r'.*?NS update failed for\s*(\d+)'
)

# Matches: "Processing transaction ID: 1131301 | Date: 2026-04-29"
# Used to look up the date for each transaction
DATE_PATTERN = re.compile(
    r'Processing transaction ID:\s*(\d+)\s*\|\s*Date:\s*(\d{4}-\d{2}-\d{2})'
)


def scan_log_file(filepath):
    """
    Scan a log file and return:
      - failed_txns: set of transaction IDs that failed
      - txn_dates:   dict of {txn_id -> date}
    """
    failed_txns = set()
    txn_dates   = {}

    print(f"  Scanning: {filepath}")

    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()

        # Build date map first
        for match in DATE_PATTERN.finditer(content):
            txn_id = match.group(1)
            date   = match.group(2)
            txn_dates[txn_id] = date

        # Find failures
        for pattern in [FAILURE_PATTERN, POST_FAILED_PATTERN, UPDATE_FAILED_PATTERN]:
            for match in pattern.finditer(content):
                txn_id = match.group(2)
                failed_txns.add(txn_id)

        print(f"    Found {len(failed_txns)} failed transactions")

    except Exception as e:
        print(f"  [ERROR] Could not read {filepath}: {e}")

    return failed_txns, txn_dates


def scan_log_directory(log_dir):
    """Scan all .log files in a directory."""
    all_failed = set()
    all_dates  = {}

    log_files = [
        f for f in os.listdir(log_dir)
        if f.endswith('.log') or f.endswith('.txt')
    ]

    if not log_files:
        print(f"  No .log files found in {log_dir}")
        return all_failed, all_dates

    for filename in sorted(log_files):
        filepath = os.path.join(log_dir, filename)
        failed, dates = scan_log_file(filepath)
        all_failed.update(failed)
        all_dates.update(dates)

    return all_failed, all_dates


def queue_dates(dates_to_queue):
    """Queue specific dates on the diagnostic server."""
    if not dates_to_queue:
        print("\n  No dates to queue.")
        return

    print(f"\n  Queuing {len(dates_to_queue)} date(s) on server...")
    queued = 0

    for date in sorted(dates_to_queue):
        try:
            r    = requests.get(
                f"{DIAG_HOST}/queue?from_date={date}&to_date={date}",
                timeout=10
            )
            data = r.json()
            chunks = data.get('chunks_queued', 0)
            print(f"    ✅ Queued {date} ({chunks} chunk)")
            queued += 1
        except requests.ConnectionError:
            print(f"\n  [ERROR] Cannot connect to server on port 8001.")
            print(f"  Make sure server.py is running.")
            break
        except Exception as e:
            print(f"    [ERROR] {date}: {e}")

    if queued > 0:
        print(f"\n  ════════════════════════════════════════════════")
        print(f"  NOW: Click 'Update Selected' in QBWC")
        print(f"  WAIT: For all progress bars to reach 100%")
        print(f"  ════════════════════════════════════════════════")


def main():
    parser = argparse.ArgumentParser(
        description="Find failed NS transactions in logs and queue for retry"
    )

    source = parser.add_mutually_exclusive_group()
    source.add_argument("--log",     default=None,
                        help="Path to a single log file (default: app.log)")
    source.add_argument("--log-dir", default=None,
                        help="Directory containing log files to scan")

    parser.add_argument("--queue", action="store_true",
                        help="Queue the failed transaction dates for QBWC retry")
    args = parser.parse_args()

    print(f"\n{'='*65}")
    print(f"  Log Scanner — Failed Transaction Finder")
    print(f"{'='*65}")

    # Determine source
    if args.log_dir:
        print(f"\n  Scanning directory: {args.log_dir}")
        failed_txns, txn_dates = scan_log_directory(args.log_dir)
    else:
        # Default — look for common log file names
        log_file = args.log or "prod_live_31.log"
        if not os.path.exists(log_file):
            # Try common names
            for candidate in ["prod_live_31.log"]:
                if os.path.exists(candidate):
                    log_file = candidate
                    break
            else:
                print(f"\n  [ERROR] Log file not found: {log_file}")
                print(f"  Specify with: --log path/to/your.log")
                return

        print(f"\n  Scanning log file: {log_file}")
        failed_txns, txn_dates = scan_log_file(log_file)

    if not failed_txns:
        print(f"\n  ✅ No failed transactions found in logs.")
        return

    # Group by date
    date_to_txns = defaultdict(list)
    no_date      = []

    for txn_id in sorted(failed_txns):
        date = txn_dates.get(txn_id)
        if date:
            date_to_txns[date].append(txn_id)
        else:
            no_date.append(txn_id)

    # Report
    print(f"\n{'='*65}")
    print(f"  FAILED TRANSACTIONS FOUND: {len(failed_txns)}")
    print(f"{'='*65}")
    print(f"\n  {'Date':<14} {'Count':>6}   Transaction IDs")
    print(f"  {'─'*13} {'─'*6}   {'─'*35}")

    for date in sorted(date_to_txns.keys()):
        txns = date_to_txns[date]
        ids_preview = ", ".join(txns[:5])
        if len(txns) > 5:
            ids_preview += f" ... +{len(txns)-5} more"
        print(f"  {date:<14} {len(txns):>6}   {ids_preview}")

    if no_date:
        print(f"\n  ⚠  {len(no_date)} transactions with no date found in log:")
        print(f"     {', '.join(no_date[:10])}")
        if len(no_date) > 10:
            print(f"     ... +{len(no_date)-10} more")

    # Save full list
    report_file = f"failed_txns_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    with open(report_file, "w") as f:
        f.write(f"Failed transactions found: {len(failed_txns)}\n")
        f.write(f"Generated: {datetime.now().isoformat()}\n\n")
        for date in sorted(date_to_txns.keys()):
            f.write(f"\n{date}:\n")
            for txn in sorted(date_to_txns[date]):
                f.write(f"  {txn}\n")
        if no_date:
            f.write(f"\nNo date found:\n")
            for txn in sorted(no_date):
                f.write(f"  {txn}\n")

    print(f"\n  Full list saved → {report_file}")
    print(f"\n  Dates to queue: {sorted(date_to_txns.keys())}")

    if not args.queue:
        print(f"\n  To queue these dates for retry run:")
        print(f"    python retry_failed_transactions.py --log {args.log or 'app.log'} --queue")
        return

    queue_dates(set(date_to_txns.keys()))
    print(f"\n{'='*65}\n")


if __name__ == "__main__":
    main()