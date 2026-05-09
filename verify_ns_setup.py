"""
Verify NetSuite subsidiary ID and check what tranIds exist for April 2026.
    python verify_ns_setup.py
"""
import requests
from dotenv import dotenv_values
from netsuite_posting import get_jwt_token, generate_access_token

config        = dotenv_values(".env")
NETSUITE_BASE = config.get('NETSITE_BASE_URL').rstrip('/')
JWT_BASE_URL  = config.get('AZURE_NREST_BASE_URL')


def run_suiteql(token, query, limit=25):
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
                      params={"limit": limit}, timeout=(10, 60))
    if r.status_code == 200:
        return r.json().get("items", [])
    print(f"  [ERROR] {r.status_code}: {r.text[:300]}")
    return []


def main():
    print("\nAuthenticating...")
    jwt   = get_jwt_token(JWT_BASE_URL)
    token = generate_access_token(NETSUITE_BASE, jwt)
    print("OK\n")

    # ── 1. List ALL subsidiaries ──────────────────────────────────────────────
    print("="*60)
    print("  ALL SUBSIDIARIES in NetSuite")
    print("="*60)
    rows = run_suiteql(token, "SELECT id, name, fullname FROM subsidiary ORDER BY id", limit=50)
    for r in rows:
        print(f"  id={r.get('id'):<6}  name={r.get('name')}  ({r.get('fullname')})")

    # ── 2. What tranIds exist in April 2026 without subsidiary filter ─────────
    print("\n" + "="*60)
    print("  JOURNAL ENTRIES — April 2026 — NO subsidiary filter (first 10)")
    print("="*60)
    rows = run_suiteql(token, """
        SELECT t.id, t.tranid, t.trandate, t.subsidiary
        FROM transaction t
        WHERE t.recordtype = 'journalentry'
        AND   t.trandate  >= TO_DATE('2026-04-01', 'YYYY-MM-DD')
        AND   t.trandate  <= TO_DATE('2026-04-30', 'YYYY-MM-DD')
        ORDER BY t.trandate ASC, t.id ASC
    """, limit=10)
    for r in rows:
        print(f"  id={r.get('id'):<12} tranid={r.get('tranid'):<20} "
              f"date={r.get('trandate'):<14} subsidiary={r.get('subsidiary')}")

    # ── 3. Count by subsidiary for April 2026 ────────────────────────────────
    print("\n" + "="*60)
    print("  COUNT BY SUBSIDIARY — April 2026 journal entries")
    print("="*60)
    rows = run_suiteql(token, """
        SELECT t.subsidiary, COUNT(*) AS cnt
        FROM transaction t
        WHERE t.recordtype = 'journalentry'
        AND   t.trandate  >= TO_DATE('2026-04-01', 'YYYY-MM-DD')
        AND   t.trandate  <= TO_DATE('2026-04-30', 'YYYY-MM-DD')
        GROUP BY t.subsidiary
        ORDER BY cnt DESC
    """, limit=20)
    for r in rows:
        print(f"  subsidiary={r.get('subsidiary'):<6}  count={r.get('cnt')}")

    # ── 4. Look for the specific QB tranIds we know exist ────────────────────
    print("\n" + "="*60)
    print("  LOOKING FOR QB TRANIDS 1289302, 1289303 in NetSuite")
    print("="*60)
    rows = run_suiteql(token, """
        SELECT t.id, t.tranid, t.trandate, t.subsidiary
        FROM transaction t
        WHERE t.recordtype = 'journalentry'
        AND   t.tranid IN ('1289302-14', '1289303-14', '1289302', '1289303')
    """, limit=10)
    if rows:
        for r in rows:
            print(f"  FOUND: id={r.get('id')} tranid={r.get('tranid')} "
                  f"date={r.get('trandate')} subsidiary={r.get('subsidiary')}")
    else:
        print("  NOT FOUND — these QB tranIds do not exist in NetSuite at all")
        print("  This means April data was never posted to NetSuite successfully")


if __name__ == "__main__":
    main()