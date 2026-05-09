"""
Debug tranId formats from both QB (via server) and NetSuite.
Run this to see exactly what IDs are on each side before comparing.

    python debug_tranids.py --from-date 2026-04-01 --to-date 2026-04-30
"""
import argparse
import requests
from dotenv import dotenv_values
from netsuite_posting import get_jwt_token, generate_access_token

config        = dotenv_values(".env")
NETSUITE_BASE = config.get('NETSITE_BASE_URL').rstrip('/')
JWT_BASE_URL  = config.get('AZURE_NREST_BASE_URL')
DIAG_HOST     = "http://127.0.0.1:8001"


def run_suiteql(token, query):
    base = NETSUITE_BASE
    if base.endswith('/services/rest'):
        base = base[:-len('/services/rest')]
    url = f"{base}/services/rest/query/v1/suiteql"
    headers = {
        "Authorization": f"{token.get('token_type')} {token.get('access_token')}",
        "Content-Type": "application/json",
        "Prefer": "transient",
    }
    r = requests.post(url, headers=headers, json={"q": query},
                      params={"limit": 20}, timeout=(10, 60))
    if r.status_code == 200:
        return r.json().get("items", [])
    print(f"  SuiteQL error {r.status_code}: {r.text[:200]}")
    return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-date", required=True)
    parser.add_argument("--to-date",   required=True)
    args = parser.parse_args()

    print("\n" + "="*60)
    print("  TRANID FORMAT DEBUGGER")
    print("="*60)

    # ── QB side: first 10 IDs from server ────────────────────────
    print("\n  QB transactions (first 10 from server /results):")
    print("  " + "─"*50)
    try:
        r    = requests.get(f"{DIAG_HOST}/results", timeout=10)
        data = r.json()
        txns = data.get("qb_transactions", {})
        sample_qb = list(txns.items())[:10]
        for tid, dt in sample_qb:
            print(f"    QB tranId = '{tid}'  (type={type(tid).__name__})  date={dt}")
        print(f"\n  Total QB transactions in server: {len(txns)}")
    except Exception as e:
        print(f"  [ERROR] {e}")

    # ── NetSuite side: first 10 tranIds ──────────────────────────
    print("\n  NetSuite tranIds (first 20 raw from SuiteQL):")
    print("  " + "─"*50)
    try:
        jwt   = get_jwt_token(JWT_BASE_URL)
        token = generate_access_token(NETSUITE_BASE, jwt)

        query = f"""
            SELECT t.id, t.tranid, t.trandate
            FROM transaction t
            WHERE t.recordtype  = 'journalentry'
            AND   t.subsidiary  = 14
            AND   t.trandate   >= TO_DATE('{args.from_date}', 'YYYY-MM-DD')
            AND   t.trandate   <= TO_DATE('{args.to_date}',   'YYYY-MM-DD')
            ORDER BY t.trandate ASC
        """
        rows = run_suiteql(token, query)
        for row in rows:
            raw_tranid = row.get("tranid", "")
            bare       = str(raw_tranid).replace("-14", "").strip()
            is_numeric = bare.isdigit()
            print(f"    NS tranId = '{raw_tranid}'  →  bare='{bare}'  numeric={is_numeric}")

    except Exception as e:
        print(f"  [ERROR] {e}")

    print("\n" + "="*60)
    print("  Compare the QB tranId format vs the NS bare tranId.")
    print("  They must match exactly for the sync check to work.")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()