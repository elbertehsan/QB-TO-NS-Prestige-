"""
Find Prestige (subsidiary 15) data range in NetSuite.
Shows earliest and latest journal entries so you know what date range to check.

    python find_prestige_range.py
"""
import requests
from dotenv import dotenv_values
from netsuite_posting import get_jwt_token, generate_access_token

config        = dotenv_values(".env")
NETSUITE_BASE = config.get('NETSITE_BASE_URL').rstrip('/')
JWT_BASE_URL  = config.get('AZURE_NREST_BASE_URL')


def run_suiteql(token, query, limit=10):
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
    print("\nAuthenticating...")
    jwt   = get_jwt_token(JWT_BASE_URL)
    token = generate_access_token(NETSUITE_BASE, jwt)
    print("OK\n")

    print("="*65)
    print("  PRESTIGE (Subsidiary 15) — Data Range in NetSuite")
    print("="*65)

    # Earliest 5 entries
    print("\n  Earliest 5 journal entries:")
    print("  " + "─"*55)
    rows = run_suiteql(token, """
        SELECT t.id, t.tranid, t.trandate, t.createddate, t.memo
        FROM transaction t
        WHERE t.recordtype = 'journalentry'
        AND   t.subsidiary = 15
        ORDER BY t.trandate ASC, t.id ASC
    """, limit=5)
    for r in rows:
        print(f"  id={r.get('id'):<12} tranid={r.get('tranid'):<20} "
              f"date={r.get('trandate'):<14} created={str(r.get('createddate',''))[:10]}")

    if not rows:
        print("  No entries found for Prestige (subsidiary 15)")
        return

    # Latest 5 entries
    print("\n  Latest 5 journal entries:")
    print("  " + "─"*55)
    rows = run_suiteql(token, """
        SELECT t.id, t.tranid, t.trandate, t.createddate, t.memo
        FROM transaction t
        WHERE t.recordtype = 'journalentry'
        AND   t.subsidiary = 15
        ORDER BY t.trandate DESC, t.id DESC
    """, limit=5)
    for r in rows:
        print(f"  id={r.get('id'):<12} tranid={r.get('tranid'):<20} "
              f"date={r.get('trandate'):<14} created={str(r.get('createddate',''))[:10]}")

    # Total count
    print("\n  Total count by month:")
    print("  " + "─"*55)
    rows = run_suiteql(token, """
        SELECT t.trandate, COUNT(*) AS cnt
        FROM transaction t
        WHERE t.recordtype = 'journalentry'
        AND   t.subsidiary = 15
        GROUP BY t.trandate
        ORDER BY t.trandate DESC
    """, limit=50)

    # Group by month manually
    from collections import defaultdict
    monthly = defaultdict(int)
    for r in rows:
        date_str = str(r.get("trandate", ""))
        # parse M/D/YYYY or YYYY-MM-DD
        try:
            if "/" in date_str:
                parts = date_str.split("/")
                month_key = f"{parts[2]}-{parts[0].zfill(2)}"
            else:
                month_key = date_str[:7]
            monthly[month_key] += int(r.get("cnt", 0))
        except:
            pass

    for month in sorted(monthly.keys(), reverse=True):
        print(f"  {month}  →  {monthly[month]:>6} entries")

    print("\n" + "="*65)
    print("  Use the earliest and latest dates above to set your")
    print("  date range when running check_netsuite_duplicates.py")
    print("="*65 + "\n")


if __name__ == "__main__":
    main()