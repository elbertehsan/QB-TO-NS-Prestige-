"""
Check which subsidiaries the April duplicate entries belonged to.
Uses the saved duplicate report JSON to look up the kept entries in NetSuite.

    python check_deleted_subsidiary.py
"""
import json
import requests
from dotenv import dotenv_values
from netsuite_posting import get_jwt_token, generate_access_token

config        = dotenv_values(".env")
NETSUITE_BASE = config.get('NETSITE_BASE_URL').rstrip('/')
JWT_BASE_URL  = config.get('AZURE_NREST_BASE_URL')

SUBSIDIARY_MAP = {
    "1":  "Epika Fleet Services",
    "4":  "LubeZone",
    "7":  "All Star Truck Services",
    "9":  "Deaton Fleet Solutions",
    "10": "Truckers",
    "11": "CS Truck & Trailer Repair",
    "12": "TopTech",
    "13": "Fleet Mobile Maintenance",
    "14": "Managed Mobile, Inc.",
    "15": "Prestige",
    "16": "C&R Fleet Services",
    "17": "Penn Jersey Diesel",
    "18": "Push & Pull",
    "19": "Meso Inc",
    "21": "Accelerated Fleet Services",
    "25": "Freeway",
}


def run_suiteql(token, query, limit=500):
    base = NETSUITE_BASE
    if base.endswith('/services/rest'):
        base = base[:-len('/services/rest')]
    url     = f"{base}/services/rest/query/v1/suiteql"
    headers = {
        "Authorization": f"{token.get('token_type')} {token.get('access_token')}",
        "Content-Type":  "application/json",
        "Prefer":        "transient",
    }
    r = requests.post(url, headers=headers, json={"q": query},
                      params={"limit": limit}, timeout=(10, 120))
    if r.status_code == 200:
        return r.json().get("items", [])
    print(f"  [ERROR] {r.status_code}: {r.text[:300]}")
    return []


def main():
    import glob, os

    # Find the April duplicate report
    reports = sorted(glob.glob("netsuite_duplicates_20260504_1153*.json"))
    if not reports:
        reports = sorted(glob.glob("netsuite_duplicates_*.json"))

    if not reports:
        print("[ERROR] No duplicate report JSON found in current directory.")
        return

    report_file = reports[-1]
    print(f"\n  Using report: {report_file}")

    with open(report_file) as f:
        report = json.load(f)

    duplicates = report.get("duplicates", {})
    
    # Collect all kept internal IDs (first 20 as sample)
    kept_ids = []
    for tran_id, entries in list(duplicates.items())[:20]:
        kept_ids.append((tran_id, str(entries[0]["internal_id"])))

    ids_str = ", ".join(f"'{iid}'" for _, iid in kept_ids)

    print(f"\n  Authenticating...")
    jwt   = get_jwt_token(JWT_BASE_URL)
    token = generate_access_token(NETSUITE_BASE, jwt)
    print(f"  OK\n")

    # Look up subsidiary for these entries
    query = f"""
        SELECT t.id, t.tranid, t.trandate, t.subsidiary
        FROM transaction t
        WHERE t.id IN ({ids_str})
        ORDER BY t.id ASC
    """
    rows = run_suiteql(token, query)

    print("="*65)
    print("  SUBSIDIARY CHECK FOR DELETED APRIL DUPLICATES (sample of 20)")
    print("="*65)
    print(f"  {'tranId':<15} {'NS id':<12} {'Date':<14} {'Sub ID':<8} {'Company'}")
    print("  " + "─"*60)

    sub_counts = {}
    for row in rows:
        sub_id   = str(row.get("subsidiary", "?"))
        sub_name = SUBSIDIARY_MAP.get(sub_id, f"Unknown ({sub_id})")
        sub_counts[sub_name] = sub_counts.get(sub_name, 0) + 1
        print(f"  {row.get('tranid',''):<15} {str(row.get('id','')):<12} "
              f"{str(row.get('trandate','')):<14} {sub_id:<8} {sub_name}")

    print(f"\n  Summary:")
    for company, count in sorted(sub_counts.items(), key=lambda x: -x[1]):
        print(f"    {company}: {count} entries")

    print("\n" + "="*65)
    if len(rows) == 0:
        print("  ⚠  None of the kept entries found — they may have been deleted too!")
        print("  This would mean both the original AND duplicate were removed.")
    elif "Managed Mobile, Inc." in sub_counts:
        print("  ✅ Deleted entries were from Managed Mobile (id=14) — correct")
    elif "Prestige" in sub_counts:
        print("  ✅ Entries belong to Prestige (id=15) — these are the right ones")
    else:
        print(f"  ⚠  Entries belong to: {list(sub_counts.keys())}")
        print(f"  Verify this is the correct company before proceeding")
    print("="*65 + "\n")


if __name__ == "__main__":
    main()