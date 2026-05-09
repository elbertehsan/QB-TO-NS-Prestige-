"""
Check for duplicate tranIds in NetSuite Prestige (sub 15) April 2026.
Shows how many times each QB tranId was posted.

    python check_ns_duplicates_prestige.py
"""

import requests
from collections import defaultdict
from datetime import datetime, timedelta
from dotenv import dotenv_values
from netsuite_posting import get_jwt_token, generate_access_token
from logger_config import logger

config        = dotenv_values(".env")
NETSUITE_BASE = config.get('NETSITE_BASE_URL').rstrip('/')
JWT_BASE_URL  = config.get('AZURE_NREST_BASE_URL')
SUBSIDIARY_ID = 15


def ns_headers(token):
    return {
        "Authorization": f"{token.get('token_type')} {token.get('access_token')}",
        "Content-Type":  "application/json",
        "Prefer":        "transient",
    }


def run_suiteql(token, query):
    base = NETSUITE_BASE
    if base.endswith('/services/rest'):
        base = base[:-len('/services/rest')]
    url      = f"{base}/services/rest/query/v1/suiteql"
    all_rows, offset = [], 0
    while True:
        r = requests.post(url, headers=ns_headers(token), json={"q": query},
                          params={"limit": 500, "offset": offset},
                          timeout=(30, 300))
        if r.status_code not in (200, 204):
            print(f"  [ERROR] {r.status_code}: {r.text[:200]}")
            break
        data = r.json()
        rows = data.get("items", [])
        all_rows.extend(rows)
        if not data.get("hasMore") or not rows:
            break
        offset += 500
        print(f"  Fetched {len(all_rows)}...", end="\r")
    return all_rows


def main():
    print(f"\n{'='*65}")
    print(f"  Duplicate tranId Check — Prestige (subsidiary {SUBSIDIARY_ID})")
    print(f"  Period: 2026-04-01 to 2026-04-30")
    print(f"{'='*65}")

    print("\n  Authenticating...")
    jwt   = get_jwt_token(JWT_BASE_URL)
    token = generate_access_token(NETSUITE_BASE, jwt)
    print("  OK")

    # Fetch all entries week by week
    from_dt  = datetime(2026, 4, 1)
    to_dt    = datetime(2026, 4, 30)
    all_rows = []
    current  = from_dt
    chunk    = 0

    print("\n  Fetching all NS entries for April...")
    while current <= to_dt:
        chunk_end = min(current + timedelta(days=6), to_dt)
        fs = current.strftime("%Y-%m-%d")
        ts = chunk_end.strftime("%Y-%m-%d")
        chunk += 1
        print(f"  Week {chunk}: {fs} → {ts}", end="  ")
        rows = run_suiteql(token, f"""
            SELECT
                t.id,
                t.tranid,
                t.trandate,
                t.createddate,
                t.createdby,
                e.entityid  AS login,
                e.firstname AS first,
                e.lastname  AS last
            FROM   transaction t
            LEFT JOIN employee e ON e.id = t.createdby
            WHERE  t.recordtype = 'journalentry'
            AND    t.subsidiary = {SUBSIDIARY_ID}
            AND    t.trandate  >= TO_DATE('{fs}', 'YYYY-MM-DD')
            AND    t.trandate  <= TO_DATE('{ts}', 'YYYY-MM-DD')
            ORDER  BY t.tranid ASC, t.createddate ASC
        """)
        print(f"{len(rows)} entries")
        all_rows.extend(rows)
        current = chunk_end + timedelta(days=1)

    print(f"\n  Total entries fetched: {len(all_rows)}")

    # Group by bare tranId
    numeric_groups  = defaultdict(list)   # bare QB number → [entries]
    non_numeric     = []

    for row in all_rows:
        tranid = str(row.get("tranid") or "")
        bare   = tranid
        for suffix in ["-15", "-14"]:
            if tranid.endswith(suffix):
                bare = tranid[:-len(suffix)]
                break

        # Enrich with creator name
        first = row.get("first") or ""
        last  = row.get("last")  or ""
        login = row.get("login") or ""
        row["creator"] = f"{first} {last}".strip() or login or f"ID:{row.get('createdby','?')}"

        if bare.isdigit():
            numeric_groups[bare].append(row)
        else:
            non_numeric.append(row)

    # Find duplicates
    duplicates = {k: v for k, v in numeric_groups.items() if len(v) > 1}
    clean      = {k: v for k, v in numeric_groups.items() if len(v) == 1}

    total_excess = sum(len(v) - 1 for v in duplicates.values())

    div = "─" * 65
    print(f"\n{'='*65}")
    print(f"  RESULTS")
    print(f"{'='*65}")
    print(f"  Total NS entries           : {len(all_rows):>7}")
    print(f"  Numeric (QB) tranIds       : {len(numeric_groups):>7}")
    print(f"  Non-numeric (JEA*/manual)  : {len(non_numeric):>7}")
    print(div)
    print(f"  ✅ Clean (posted once)     : {len(clean):>7}")
    print(f"  🔁 Duplicated tranIds      : {len(duplicates):>7}")
    print(f"  🔁 Excess duplicate entries: {total_excess:>7}  ← these need deleting")

    if duplicates:
        print(f"\n  Duplicate tranIds (posted more than once):")
        print(div)
        print(f"  {'tranId':<15} {'Times posted':<14} {'NS internal IDs (keep first)'}")
        print(f"  {'─'*14} {'─'*13} {'─'*30}")

        for bare, entries in sorted(duplicates.items(),
                                    key=lambda x: -len(x[1])):
            print(f"\n  tranId: {bare}  ({len(entries)}x posted)")
            print(f"  {'─'*60}")
            for i, e in enumerate(entries):
                action  = "KEEP  " if i == 0 else "DELETE"
                ns_id   = str(e.get("id"))
                created = str(e.get("createddate",""))[:19]
                creator = e.get("creator", "unknown")
                portal  = f"https://8579414.app.netsuite.com/app/accounting/transactions/journal.nl?id={ns_id}"
                print(f"  [{action}] NS id={ns_id:<12} created={created}  by={creator}")
                print(f"           {portal}")

        print(f"\n  To delete these {total_excess} duplicates run:")
        print(f"    python find_duplicates_chunked.py "
              f"--from-date 2026-04-01 --to-date 2026-04-30 --delete")
    else:
        print(f"\n  ✅ No duplicate tranIds found — NetSuite is clean.")
        print(f"  The {len(non_numeric):,} non-numeric entries are manual/JEA* "
              f"entries — untouched by integration.")

    # Always show who posted numeric entries (top creators)
    if numeric_groups:
        from collections import Counter
        all_numeric = [e for entries in numeric_groups.values() for e in entries]
        creator_counts = Counter(e.get("creator", "unknown") for e in all_numeric)
        print(f"\n  Top creators of numeric (QB) entries in NS:")
        print("  " + "─"*50)
        for creator, count in creator_counts.most_common(10):
            print(f"  {creator:<40} {count:>6} entries")

    # ── Analyze non-numeric entries ──────────────────────────────────────────
    if non_numeric:
        print(f"\n  NON-NUMERIC (JEA*/manual) ENTRY BREAKDOWN")
        print("  " + "─"*60)

        from collections import Counter
        # Pattern breakdown
        patterns = Counter()
        creators = Counter()
        for row in non_numeric:
            tranid = str(row.get("tranid") or "")
            if tranid.startswith("JEA"):
                patterns["JEA* (system auto)"] += 1
            elif tranid.startswith("JE"):
                patterns["JE* (manual journal)"] += 1
            elif "Prestige" in tranid:
                patterns["Prestige* (manual)"] += 1
            else:
                patterns[f"Other: {tranid[:10]}"] += 1
            creators[row.get("creator", "unknown")] += 1

        print(f"\n  By tranId pattern:")
        for pattern, count in patterns.most_common():
            print(f"    {pattern:<35} {count:>6}")

        print(f"\n  By creator:")
        for creator, count in creators.most_common(10):
            print(f"    {creator:<40} {count:>6}")

        # Sample entries
        print(f"\n  Sample non-numeric entries (first 10):")
        print(f"  {'tranId':<30} {'Date':<14} {'Created':<12} {'By'}")
        print("  " + "─"*80)
        for row in non_numeric[:10]:
            print(f"  {str(row.get('tranid','')):<30} "
                  f"{str(row.get('trandate','')):<14} "
                  f"{str(row.get('createddate',''))[:10]:<12} "
                  f"{row.get('creator','?')}")

    # ── Save full JSON report ─────────────────────────────────────────────────
    import json
    from datetime import datetime as dt

    report = {
        "generated_at":   dt.now().isoformat(),
        "subsidiary":     f"{SUBSIDIARY_ID} (Prestige)",
        "date_range":     {"from": "2026-04-01", "to": "2026-04-30"},
        "summary": {
            "total_ns_entries":        len(all_rows),
            "numeric_qb_tranids":      len(numeric_groups),
            "non_numeric_manual":      len(non_numeric),
            "clean_posted_once":       len(clean),
            "duplicate_tranids":       len(duplicates),
            "excess_duplicate_entries": total_excess,
        },
        "top_creators_numeric": dict(
            Counter(e.get("creator","?") for entries in numeric_groups.values()
                    for e in entries).most_common(10)
        ),
        "non_numeric_by_pattern": dict(
            Counter(
                "JEA*" if str(r.get("tranid","")).startswith("JEA")
                else "JE*" if str(r.get("tranid","")).startswith("JE")
                else "Prestige*" if "Prestige" in str(r.get("tranid",""))
                else "Other"
                for r in non_numeric
            ).most_common()
        ),
        "non_numeric_by_creator": dict(
            Counter(r.get("creator","?") for r in non_numeric).most_common(10)
        ),
        "non_numeric_entries": [
            {
                "tranid":      str(r.get("tranid","")),
                "ns_id":       str(r.get("id","")),
                "trandate":    str(r.get("trandate","")),
                "createddate": str(r.get("createddate",""))[:10],
                "creator":     r.get("creator","?"),
                "portal_url":  f"https://8579414.app.netsuite.com/app/accounting/transactions/journal.nl?id={r.get('id','')}",
            }
            for r in non_numeric
        ],
        "duplicates": {
            bare: [
                {
                    "ns_id":       str(e.get("id")),
                    "tranid":      str(e.get("tranid","")),
                    "trandate":    str(e.get("trandate","")),
                    "createddate": str(e.get("createddate",""))[:10],
                    "creator":     e.get("creator","?"),
                    "action":      "keep" if i == 0 else "delete",
                }
                for i, e in enumerate(entries)
            ]
            for bare, entries in duplicates.items()
        }
    }

    fname = f"ns_full_analysis_prestige_april.json"
    with open(fname, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n  Full report saved → {fname}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()