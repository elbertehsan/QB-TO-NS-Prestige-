"""
Run from the repo root with the venv active:
    python -m tests.test_emails

Each test sends a real email via the Azure Logic App webhook.
All tests run by default. Pass a test name to run just one:
    python -m tests.test_emails missing_account
    python -m tests.test_emails post_failure
    python -m tests.test_emails no_transactions
    python -m tests.test_emails missing_location
    python -m tests.test_emails batch_summary
    python -m tests.test_emails all
"""

import sys
import os

# Allow imports from repo root regardless of where the script is called from
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netsuite_data_transformation import (
    send_email_via_logic_app,
    send_post_failure_email_via_logic_app,
    send_no_transactions_email_via_logic_app,
    send_location_email_via_logic_app,
    send_batch_summary_email_via_logic_app,
)


def test_missing_account():
    """Fires when a QB account code has no mapping in Netsuite_mappings."""
    print("\n--- TEST: Missing Account Mapping ---")
    send_email_via_logic_app(
        account="99999",
        original_account="99999 - Unknown Test Account",
    )
    print("Done. Check inbox for: 'Missing Account Mapping (Prestige): 99999'")


def test_post_failure():
    """Fires when a journal entry POST to NetSuite fails."""
    print("\n--- TEST: Post Failure ---")
    send_post_failure_email_via_logic_app(
        transaction_id="TEST-TXN-001",
        error_message="[TEST] NetSuite returned 500 Internal Server Error",
    )
    print("Done. Check inbox for: 'Failed to Post Journal Entry (Prestige): TxnID TEST-TXN-001'")


def test_post_failure_with_payload_size():
    """Same as post failure but includes an oversized payload size note."""
    print("\n--- TEST: Post Failure (with payload size) ---")
    send_post_failure_email_via_logic_app(
        transaction_id="TEST-TXN-002",
        error_message="[TEST] Payload too large",
        data_size_mb=1.45,
    )
    print("Done. Check inbox for: 'Failed to Post Journal Entry (Prestige): TxnID TEST-TXN-002'")


def test_no_transactions():
    """Fires when no transactions were posted to NetSuite for a given date."""
    print("\n--- TEST: No Transactions Posted ---")
    send_no_transactions_email_via_logic_app(date="2026-05-09")
    print("Done. Check inbox for: 'No Transactions Posted to NetSuite (Prestige) — 2026-05-09'")


def test_missing_location():
    """Fires when a QB class/location string has no mapping in Location_mappings."""
    print("\n--- TEST: Missing Location Mapping ---")
    send_location_email_via_logic_app(
        location="99--TestLocation",
        original_location="99--TestLocation (test entry)",
    )
    print("Done. Check inbox for: 'Missing Location Mapping (Prestige): 99--TestLocation'")


def test_batch_summary():
    """Fires at end of a batch when there are failures or NS orphans."""
    print("\n--- TEST: Batch Summary ---")
    test_results = {
        "created":   [{"txn_id": "1001", "date": "2026-04-06", "action": "created"},
                      {"txn_id": "1002", "date": "2026-04-06", "action": "reconciled"}],
        "updated":   [{"txn_id": "1003", "date": "2026-04-06"}],
        "no_change": [{"txn_id": str(i), "date": "2026-04-06"} for i in range(1004, 1079)],
        "failed": [
            {"txn_id": "journal_entry_raw_1", "date": "",           "step": "transformation", "error": "transform returned None"},
            {"txn_id": "1080",                "date": "2026-04-06", "step": "ns_post",        "error": "[TEST] NetSuite returned 503 Service Unavailable"},
        ],
        "ns_orphans": [
            {
                "txn_id": "1081",
                "ns_location": "https://account.suitetalk.api.netsuite.com/services/rest/record/v1/journalentry/88421",
                "note": "DB INSERT failed, NS rollback also failed: Connection timeout",
                "date": "2026-04-06",
            }
        ],
    }
    send_batch_summary_email_via_logic_app(
        all_dates={"2026-04-06"},
        results=test_results,
        total=1440,
    )
    print("Done. Check inbox for: '[Prestige Sync] Batch completed with issues'")


TESTS = {
    "missing_account":        test_missing_account,
    "post_failure":           test_post_failure,
    "post_failure_with_size": test_post_failure_with_payload_size,
    "no_transactions":        test_no_transactions,
    "missing_location":       test_missing_location,
    "batch_summary":          test_batch_summary,
}


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "all"

    if target == "all":
        print(f"Running all {len(TESTS)} email tests...")
        for name, fn in TESTS.items():
            try:
                fn()
            except Exception as e:
                print(f"  ERROR in {name}: {e}")
    elif target in TESTS:
        try:
            TESTS[target]()
        except Exception as e:
            print(f"ERROR: {e}")
    else:
        print(f"Unknown test '{target}'. Available: {', '.join(TESTS)} | all")
        sys.exit(1)

    print("\nAll done.")
