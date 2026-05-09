"""
Delete 6 wrong subsidiary records from Quickbooks_Netsuite_Sync_prestige.
These QB transaction IDs were matched to NetSuite entries belonging to
other companies (TopTech sub 12, CS Truck sub 11) — not Prestige (sub 15).

Deleting them from the DB so the next sync re-posts them correctly to Prestige.
NetSuite entries are NOT touched (they belong to other brands in closed periods).

    python delete_wrong_db_records.py           # preview only
    python delete_wrong_db_records.py --confirm # actually delete
"""

import argparse
from dotenv import dotenv_values
from azure_database_posting import create_db_connection
from logger_config import logger

config      = dotenv_values(".env")
DB_SERVER   = config.get('AZURE_SERVER')
DB_USER     = config.get('AZURE_USER')
DB_PASSWORD = config.get('AZURE_PASSWORD')
DB_NAME     = config.get('AZURE_DATABASE')

# The 6 DB records pointing to the wrong NetSuite subsidiary
WRONG_TXN_IDS = [
    "1126331",   # → NS id=1115275, subsidiary 12 (TopTech)
    "1131301",   # → NS id=1115276, subsidiary 12 (TopTech)
    "1132025",   # → NS id=1410404, subsidiary 11 (CS Truck)
    "1135617",   # → NS id=1115279, subsidiary 12 (TopTech)
    "1134428",   # → NS id=1115277, subsidiary 12 (TopTech)
    "1134570",   # → NS id=1115278, subsidiary 12 (TopTech)
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true",
                        help="Actually delete the records (default is preview only)")
    args = parser.parse_args()

    print(f"\n{'='*65}")
    print(f"  DB Cleanup — Wrong Subsidiary Records")
    print(f"  Table: Quickbooks_Netsuite_Sync_prestige")
    print(f"{'='*65}")

    conn   = create_db_connection(DB_SERVER, DB_USER, DB_PASSWORD, DB_NAME)
    cursor = conn.cursor()

    # Preview — show what will be deleted
    ids_str = ", ".join(WRONG_TXN_IDS)
    cursor.execute(f"""
        SELECT transaction_id, transaction_date, netsuite_location
        FROM   Quickbooks_Netsuite_Sync_prestige
        WHERE  transaction_id IN ({ids_str})
        ORDER  BY transaction_date ASC
    """)
    rows = cursor.fetchall()

    print(f"\n  Records to delete from DB ({len(rows)} found):")
    print("  " + "─"*60)
    print(f"  {'TxnId':<12} {'Date':<14} {'NS Location (wrong subsidiary)'}")
    print(f"  {'─'*11} {'─'*13} {'─'*35}")
    for row in rows:
        txn_id   = str(row[0])
        date     = str(row[1])[:10]
        location = str(row[2])
        ns_id    = location.split("/")[-1] if location else "?"
        print(f"  {txn_id:<12} {date:<14} NS id={ns_id} (wrong company)")

    if not rows:
        print("  No matching records found — already cleaned up.")
        cursor.close()
        conn.close()
        return

    if not args.confirm:
        print(f"\n  Preview only — nothing deleted.")
        print(f"  To delete these {len(rows)} records run:")
        print(f"    python delete_wrong_db_records.py --confirm")
        cursor.close()
        conn.close()
        return

    # Delete
    print(f"\n  Deleting {len(rows)} records from DB...")
    cursor.execute(f"""
        DELETE FROM Quickbooks_Netsuite_Sync_prestige
        WHERE  transaction_id IN ({ids_str})
    """)
    deleted = cursor.rowcount
    conn.commit()

    print(f"  ✅ Deleted {deleted} records from Quickbooks_Netsuite_Sync_prestige")
    print(f"\n  These transactions will be re-posted correctly to")
    print(f"  Prestige (subsidiary 15) on the next sync run.")

    cursor.close()
    conn.close()
    print(f"\n{'='*65}\n")


if __name__ == "__main__":
    main()