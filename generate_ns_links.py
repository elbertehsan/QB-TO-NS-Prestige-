"""
Generate NetSuite Portal Links
================================
Reads the cross-check report JSON and generates direct clickable URLs
for both phantom entries (in NS but not QB) and missing entries (in QB but not NS).
Saves a clean JSON with links you can open directly in the NetSuite portal.

    python generate_ns_links.py --report prestige_crosscheck_20260401_20260430.json
"""

import argparse
import json
import requests
from datetime import datetime
from dotenv import dotenv_values

from netsuite_posting import get_jwt_token, generate_access_token

config        = dotenv_values(".env")
NETSUITE_BASE = config.get('NETSITE_BASE_URL').rstrip('/')
JWT_BASE_URL  = config.get('AZURE_NREST_BASE_URL')
SUBSIDIARY_ID = 15


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
                          timeout=(30, 120))
        if r.status_code not in (200, 204):
            print(f"  [ERROR] {r.status_code}: {r.text[:200]}")
            break
        data = r.json()
        rows = data.get("items", [])
        all_rows.extend(rows)
        if not data.get("hasMore") or not rows:
            break
        offset += limit
    return all_rows


def get_account_id_from_base_url():
    """Extracts the NetSuite account ID from the base URL for portal links."""
    # NetSuite URLs look like: https://1234567.suitetalk.api.netsuite.com/...
    # Portal URL looks like: https://1234567.app.netsuite.com/app/accounting/transactions/journal.nl?id=XXXXX
    import re
    match = re.search(r'(\d{6,8})', NETSUITE_BASE)
    return match.group(1) if match else None


def build_portal_url(account_id, internal_id):
    """Builds a direct NetSuite portal link for a journal entry."""
    if account_id:
        return f"https://{account_id}.app.netsuite.com/app/accounting/transactions/journal.nl?id={internal_id}"
    return f"https://app.netsuite.com/app/accounting/transactions/journal.nl?id={internal_id}"


def main():
    parser = argparse.ArgumentParser(
        description="Generate NetSuite portal links from cross-check report."
    )
    parser.add_argument("--report", required=True,
                        help="Path to prestige_crosscheck_*.json")
    args = parser.parse_args()

    with open(args.report) as f:
        report = json.load(f)

    phantom_ids  = report.get("phantom_in_ns", [])
    missing_ids  = report.get("missing_from_ns", [])

    print(f"\n  Phantom in NS (not in QB): {len(phantom_ids)}")
    print(f"  Missing from NS (in QB):   {len(missing_ids)}")

    # Get account ID for portal URLs
    account_id = get_account_id_from_base_url()
    print(f"\n  NetSuite account ID: {account_id}")

    # Authenticate
    print("\n  Authenticating...")
    token = get_ns_token()
    print("  OK")

    # ── Fetch details for phantom entries ─────────────────────────────────────
    phantom_links = []
    if phantom_ids:
        print(f"\n  Fetching details for {len(phantom_ids)} phantom entries...")
        ids_str = ", ".join(f"'{i}'" for i in phantom_ids)
        rows = run_suiteql(token, f"""
            SELECT
                t.id,
                t.tranid,
                t.trandate,
                t.createddate,
                t.memo,
                t.createdby,
                e.entityid      AS created_by_login,
                e.firstname     AS created_by_first,
                e.lastname      AS created_by_last
            FROM
                transaction t
                LEFT JOIN employee e ON e.id = t.createdby
            WHERE
                t.recordtype = 'journalentry'
                AND t.subsidiary = {SUBSIDIARY_ID}
                AND t.tranid IN ({ids_str})
            ORDER BY t.trandate ASC
        """)

        for row in rows:
            iid        = str(row.get("id"))
            created_by = row.get("createdby", "?")
            first      = row.get("created_by_first") or ""
            last       = row.get("created_by_last") or ""
            login      = row.get("created_by_login") or ""
            full_name  = f"{first} {last}".strip() or login or f"Employee ID {created_by}"

            phantom_links.append({
                "internal_id":   iid,
                "tranid":        row.get("tranid"),
                "trandate":      str(row.get("trandate")),
                "createddate":   str(row.get("createddate")),
                "memo":          row.get("memo", ""),
                "created_by_id": created_by,
                "created_by":    full_name,
                "created_by_login": login,
                "portal_url":    build_portal_url(account_id, iid),
                "api_url":       f"{NETSUITE_BASE}/record/v1/journalentry/{iid}",
                "note":          "EXISTS in NetSuite but NOT in QuickBooks — verify manually"
            })

    # ── Build links for missing entries (no NS internal ID available) ──────────
    missing_links = []
    for qb_id in missing_ids:
        missing_links.append({
            "qb_tranid":  qb_id,
            "ns_tranid":  f"{qb_id}-15",
            "status":     "NOT in NetSuite — needs to be posted",
            "note":       "Search for this tranId in NetSuite to confirm it's missing"
        })

    # ── Save output ────────────────────────────────────────────────────────────
    output = {
        "generated_at":  datetime.now().isoformat(),
        "source_report": args.report,
        "netsuite_account_id": account_id,

        "phantom_entries": {
            "count":       len(phantom_links),
            "description": "These exist in NetSuite (subsidiary 15) but have NO matching QB transaction. Review each link and decide if they should be deleted.",
            "entries":     phantom_links
        },

        "missing_from_netsuite": {
            "count":       len(missing_links),
            "description": "These QB transactions have NOT been posted to NetSuite. They need to be synced.",
            "entries":     missing_links
        }
    }

    fname = f"ns_links_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(fname, "w") as f:
        json.dump(output, f, indent=2)

    # Print phantom links to console for quick review
    print(f"\n{'='*65}")
    print(f"  PHANTOM ENTRIES — Direct NetSuite Links")
    print(f"  (exist in NS but not in QB — review before deleting)")
    print(f"{'='*65}")
    for e in phantom_links:
        print(f"\n  tranId     : {e['tranid']}")
        print(f"  Date       : {e['trandate']}")
        print(f"  Created on : {str(e['createddate'])[:10]}")
        print(f"  Created by : {e.get('created_by', '?')}  (login: {e.get('created_by_login', '?')})")
        print(f"  Memo       : {e['memo'] or '(none)'}")
        print(f"  Portal URL : {e['portal_url']}")

    print(f"\n{'='*65}")
    print(f"  Full report saved → {fname}")
    print(f"  Open the portal URLs above to verify each entry in NetSuite")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()