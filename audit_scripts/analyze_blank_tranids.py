"""
Prestige Blank TranId Diagnostic & Cleanup Analyzer
====================================================

Analyzes NetSuite journal entries with blank tranIds to determine:
1. What kind of data they contain (accounts, amounts, creators, dates)
2. Whether they DUPLICATE QB-posted entries (same data, different NS internal ID)
3. What likely caused them (integration gaps, manual entries, test data)
4. Which ones are SAFE to delete vs. which must be kept

Uses QB data from server.py memory (port 8001 /results endpoint) — NOT the Azure DB.

Usage:
    # Step 1: Run server.py in SYNC_CHECK_MODE, then queue the range:
    curl "http://127.0.0.1:8001/queue?from_date=2026-04-01&to_date=2026-04-30"
    # Step 2: Click "Update Selected" in QBWC, wait for 100%
    # Step 3: Run this analyzer:
    python analyze_blank_tranids.py --from-date 2026-04-01 --to-date 2026-04-30

Outputs:
    - blank_tranid_analysis_YYYYMMDD.json       (full report with every entry)
    - delete_safe_blank_tranids_YYYYMMDD.py     (auto-generated deletion script)
    - Console summary with actionable recommendations
"""

import argparse
import json
import requests
from collections import defaultdict, Counter
from datetime import datetime, timedelta
from dotenv import dotenv_values
from netsuite_posting import get_jwt_token, generate_access_token
from logger_config import logger

config        = dotenv_values(".env")
NETSUITE_BASE = config.get('NETSITE_BASE_URL').rstrip('/')
JWT_BASE_URL  = config.get('AZURE_NREST_BASE_URL')
SUBSIDIARY_ID = 15  # Prestige
DIAG_HOST     = 'http://127.0.0.1:8001'


def ns_headers(token):
    return {
        'Authorization': f"{token.get('token_type')} {token.get('access_token')}",
        'Content-Type':  'application/json',
        'Prefer':        'transient',
    }


def run_suiteql(token, query: str) -> list:
    base = NETSUITE_BASE
    if base.endswith('/services/rest'):
        base = base[:-len('/services/rest')]
    url = f"{base}/services/rest/query/v1/suiteql"
    all_rows, offset = [], 0
    while True:
        try:
            r = requests.post(
                url, headers=ns_headers(token),
                json={"q": query},
                params={"limit": 500, "offset": offset},
                timeout=(30, 300)
            )
            if r.status_code not in (200, 204):
                logger.error(f"SuiteQL {r.status_code}: {r.text[:300]}")
                break
            data = r.json()
            rows = data.get("items", [])
            all_rows.extend(rows)
            if not data.get("hasMore") or not rows:
                break
            offset += 500
            print(f"  Fetched {len(all_rows)} rows...", end="\r")
        except Exception as e:
            logger.error(f"SuiteQL error: {e}")
            break
    return all_rows


def get_ns_token():
    jwt = get_jwt_token(JWT_BASE_URL)
    token = generate_access_token(NETSUITE_BASE, jwt)
    if not token or not token.get('access_token'):
        raise RuntimeError("NetSuite authentication failed")
    return token


# ═════════════════════════════════════════════════════════════════════════════
# 1. FETCH QB DATA FROM SERVER MEMORY (not DB)
# ═════════════════════════════════════════════════════════════════════════════

def fetch_qb_from_server() -> dict:
    """Fetch QB transactions from server.py sync_results memory."""
    print("\n" + "="*70)
    print("  FETCHING QB data from server.py memory...")
    print("="*70)
    try:
        r = requests.get(f"{DIAG_HOST}/results", timeout=10)
        data = r.json()
        txns = data.get("qb_transactions", {})
        complete = data.get("complete", False)
        pending = data.get("pending_chunks", 0)
        print(f"  QB transactions in server memory: {len(txns)}")
        print(f"  Sync complete: {complete} | Pending chunks: {pending}")
        if not txns:
            print("  [WARN] No QB data — make sure server.py ran sync-check and QBWC finished.")
        return txns
    except requests.ConnectionError:
        print("  [ERROR] Cannot connect to diagnostic server on port 8001.")
        print("  Make sure server.py is running in SYNC_CHECK_MODE.")
        return {}


# ═════════════════════════════════════════════════════════════════════════════
# 2. FETCH BLANK TRANID ENTRIES WITH FULL DETAIL
# ═════════════════════════════════════════════════════════════════════════════

def fetch_blank_tranid_entries(token, from_date: str, to_date: str) -> list:
    print("\n" + "="*70)
    print("  FETCHING blank-tranid entries from NetSuite...")
    print(f"  Subsidiary: {SUBSIDIARY_ID} (Prestige)")
    print(f"  Period: {from_date} to {to_date}")
    print("="*70)

    # NOTE: Using only standard transaction fields. Removed custbodydowntimedoc
    # and externalid which do not exist on the transaction table.
    headers = run_suiteql(token, f"""
        SELECT
            t.id,
            t.tranid,
            t.trandate,
            t.createddate,
            t.lastmodifieddate,
            t.memo,
            t.status,
            t.postingperiod,
            e.entityid  AS login,
            e.firstname AS first,
            e.lastname  AS last,
            t.createdby
        FROM   transaction t
        LEFT JOIN employee e ON e.id = t.createdby
        WHERE  t.recordtype = 'journalentry'
        AND    t.subsidiary = {SUBSIDIARY_ID}
        AND    t.trandate >= TO_DATE('{from_date}', 'YYYY-MM-DD')
        AND    t.trandate <= TO_DATE('{to_date}', 'YYYY-MM-DD')
        AND    (t.tranid IS NULL OR t.tranid = '')
        ORDER  BY t.trandate DESC, t.createddate DESC
    """)

    print(f"\n  Found {len(headers)} blank-tranid entries")

    enriched = []
    for i, header in enumerate(headers):
        ns_id = str(header.get("id"))
        print(f"  Enriching {i+1}/{len(headers)}: NS id={ns_id}", end="\r")

        lines = run_suiteql(token, f"""
            SELECT
                tl.linesequencenumber,
                tl.account,
                a.acctnumber,
                a.name AS account_name,
                a.type AS account_type,
                tl.debit,
                tl.credit,
                tl.memo AS line_memo,
                tl.entity,
                tl.department,
                tl.location,
                tl.class
            FROM transactionline tl
            JOIN account a ON a.id = tl.account
            WHERE tl.transaction = {ns_id}
            AND tl.mainline = 'F'
            ORDER BY tl.linesequencenumber
        """)

        total_debit = sum(float(l.get("debit") or 0) for l in lines)
        total_credit = sum(float(l.get("credit") or 0) for l in lines)

        first = header.get("first") or ""
        last = header.get("last") or ""
        login = header.get("login") or ""
        createdby = header.get("createdby") or "?"
        creator_name = f"{first} {last}".strip() or login or f"ID:{createdby}"

        enriched.append({
            "ns_id":            ns_id,
            "tranid":           header.get("tranid"),
            "trandate":         str(header.get("trandate")),
            "createddate":      str(header.get("createddate")),
            "lastmodifieddate": str(header.get("lastmodifieddate")),
            "memo":             header.get("memo"),
            "status":           header.get("status"),
            "postingperiod":    header.get("postingperiod"),
            "creator":          creator_name,
            "creator_login":    login,
            "portal_url":       f"https://8579414.app.netsuite.com/app/accounting/transactions/journal.nl?id={ns_id}",
            "line_count":       len(lines),
            "total_debit":      round(total_debit, 2),
            "total_credit":     round(total_credit, 2),
            "net_amount":       round(total_debit - total_credit, 2),
            "is_balanced":      abs(total_debit - total_credit) < 0.01,
            "lines": [
                {
                    "seq":          l.get("linesequencenumber"),
                    "account_id":   l.get("account"),
                    "acctnumber":   l.get("acctnumber"),
                    "account_name": l.get("account_name"),
                    "account_type": l.get("account_type"),
                    "debit":        float(l.get("debit") or 0),
                    "credit":       float(l.get("credit") or 0),
                    "memo":         l.get("line_memo"),
                    "entity":       l.get("entity"),
                    "location":     l.get("location"),
                    "class":        l.get("class"),
                }
                for l in lines
            ]
        })

    print(f"\n  Enriched {len(enriched)} entries with line details")
    return enriched


# ═════════════════════════════════════════════════════════════════════════════
# 3. FETCH ALL NS ENTRIES WITH NUMERIC TRANIDS (QB-posted)
# ═════════════════════════════════════════════════════════════════════════════

def fetch_numeric_tranid_entries(token, from_date: str, to_date: str) -> dict:
    """Fetch all numeric tranId entries to compare against blank ones."""
    print("\n" + "="*70)
    print("  FETCHING numeric-tranId entries for comparison...")
    print("="*70)

    rows = run_suiteql(token, f"""
        SELECT
            t.id,
            t.tranid,
            t.trandate,
            t.createddate,
            t.memo,
            SUM(tl.debit) AS total_debit,
            SUM(tl.credit) AS total_credit,
            COUNT(tl.id) AS line_count
        FROM transaction t
        JOIN transactionline tl ON tl.transaction = t.id
        WHERE t.recordtype = 'journalentry'
        AND t.subsidiary = {SUBSIDIARY_ID}
        AND t.trandate >= TO_DATE('{from_date}', 'YYYY-MM-DD')
        AND t.trandate <= TO_DATE('{to_date}', 'YYYY-MM-DD')
        AND t.tranid IS NOT NULL
        AND t.tranid != ''
        AND tl.mainline = 'F'
        GROUP BY t.id, t.tranid, t.trandate, t.createddate, t.memo
        ORDER BY t.trandate DESC
    """)

    result = {}
    for row in rows:
        bare = str(row.get("tranid") or "")
        for suffix in ["-15", "-14"]:
            if bare.endswith(suffix):
                bare = bare[:-len(suffix)]
                break
        if bare.isdigit():
            result[bare] = {
                "ns_id":        str(row.get("id")),
                "tranid":       str(row.get("tranid")),
                "trandate":     str(row.get("trandate")),
                "createddate":  str(row.get("createddate")),
                "memo":         row.get("memo"),
                "total_debit":  float(row.get("total_debit") or 0),
                "total_credit": float(row.get("total_credit") or 0),
                "line_count":   int(row.get("line_count") or 0),
            }

    print(f"  Numeric QB entries in NS: {len(result)}")
    return result


# ═════════════════════════════════════════════════════════════════════════════
# 4. DEEP COMPARISON: Do blank entries match any QB-posted entry?
# ═════════════════════════════════════════════════════════════════════════════

def deep_compare(blank_entries: list, numeric_entries: dict) -> list:
    """
    Check if any blank-tranid entry is a DUPLICATE of a QB-posted entry
    (same date, same amounts, same line count = likely same data).
    """
    print("\n" + "="*70)
    print("  DEEP COMPARISON: Checking for duplicates...")
    print("="*70)

    # Build lookup by (date, net_amount, line_count)
    numeric_lookup = defaultdict(list)
    for tid, data in numeric_entries.items():
        key = (data["trandate"], round(data["total_debit"] - data["total_credit"], 2), data["line_count"])
        numeric_lookup[key].append({"qb_tid": tid, **data})

    for entry in blank_entries:
        key = (entry["trandate"], entry["net_amount"], entry["line_count"])
        matches = numeric_lookup.get(key, [])

        if matches:
            best_match = None
            for match in matches:
                # Use memo as proxy for QB TxnNumber
                if match.get("memo") and any(c.isdigit() for c in str(match.get("memo"))):
                    best_match = match
                    break

            entry["duplicate_analysis"] = {
                "is_likely_duplicate": True,
                "matched_qb_tid": best_match["qb_tid"] if best_match else matches[0]["qb_tid"],
                "matched_ns_id": best_match["ns_id"] if best_match else matches[0]["ns_id"],
                "match_reason": "Same date + net amount + line count",
                "confidence": "HIGH" if best_match else "MEDIUM"
            }
        else:
            entry["duplicate_analysis"] = {
                "is_likely_duplicate": False,
                "match_reason": None,
                "confidence": "NONE"
            }

    dupes = [e for e in blank_entries if e["duplicate_analysis"]["is_likely_duplicate"]]
    print(f"  Likely duplicates of QB entries: {len(dupes)}")
    return blank_entries


# ═════════════════════════════════════════════════════════════════════════════
# 5. ROOT CAUSE ANALYSIS
# ═════════════════════════════════════════════════════════════════════════════

def analyze_root_causes(entries: list, qb_txns: dict) -> dict:
    """Determine what likely caused each blank-tranid entry."""
    print("\n" + "="*70)
    print("  ROOT CAUSE ANALYSIS...")
    print("="*70)

    causes = Counter()
    creator_patterns = defaultdict(list)
    date_patterns = Counter()

    for entry in entries:
        creator = entry["creator"]
        memo = str(entry.get("memo") or "")
        is_dup = entry.get("duplicate_analysis", {}).get("is_likely_duplicate", False)

        # Detect if memo contains a QB transaction number (indicates integration)
        has_qb_number = any(c.isdigit() for c in memo) and len(memo) < 20

        if is_dup:
            cause = "INTEGRATION_DUPLICATE"
        elif has_qb_number and creator == "Elbert Ehsan":
            cause = "INTEGRATION_MISSING_TRANID"
        elif creator == "Elbert Ehsan":
            cause = "MANUAL_ELBERT"
        elif creator == "Jack Kegermann":
            cause = "MANUAL_JACK"
        elif entry.get("line_count", 0) <= 2 and abs(entry.get("net_amount", 0)) < 1:
            cause = "SYSTEM_ADJUSTMENT"
        else:
            cause = "MANUAL_OTHER"

        entry["likely_cause"] = cause
        causes[cause] += 1
        creator_patterns[creator].append(entry["ns_id"])
        date_patterns[entry["trandate"]] += 1

    creation_hours = Counter()
    for entry in entries:
        cd = entry.get("createddate", "")
        if "T" in str(cd):
            try:
                hour = int(cd.split("T")[1].split(":")[0])
                creation_hours[hour] += 1
            except:
                pass

    return {
        "causes": dict(causes.most_common()),
        "creator_patterns": {k: len(v) for k, v in creator_patterns.items()},
        "date_distribution": dict(date_patterns.most_common(20)),
        "hour_distribution": dict(sorted(creation_hours.items())),
        "total_entries": len(entries)
    }


# ═════════════════════════════════════════════════════════════════════════════
# 6. DELETION RECOMMENDATIONS
# ═════════════════════════════════════════════════════════════════════════════

def generate_deletion_recommendations(entries: list) -> dict:
    """Classify each entry as SAFE_TO_DELETE, REVIEW_REQUIRED, or KEEP."""
    print("\n" + "="*70)
    print("  GENERATING DELETION RECOMMENDATIONS...")
    print("="*70)

    safe = []
    review = []
    keep = []

    for entry in entries:
        cause = entry.get("likely_cause", "UNKNOWN")
        is_dup = entry.get("duplicate_analysis", {}).get("is_likely_duplicate", False)
        is_balanced = entry.get("is_balanced", False)
        net_amount = abs(entry.get("net_amount", 0))
        line_count = entry.get("line_count", 0)
        memo = str(entry.get("memo") or "").lower()

        if is_dup:
            entry["recommendation"] = "SAFE_TO_DELETE"
            entry["reason"] = "Duplicate of QB-posted entry (same data, different NS internal ID)"
            safe.append(entry)
        elif cause == "SYSTEM_ADJUSTMENT" and net_amount < 0.01:
            entry["recommendation"] = "SAFE_TO_DELETE"
            entry["reason"] = "Zero-amount system adjustment with no financial impact"
            safe.append(entry)
        elif cause == "INTEGRATION_MISSING_TRANID":
            entry["recommendation"] = "REVIEW_REQUIRED"
            entry["reason"] = "Posted by integration but missing tranId — verify if QB has this TxnNumber"
            review.append(entry)
        elif not is_balanced:
            entry["recommendation"] = "REVIEW_REQUIRED"
            entry["reason"] = (
                f"UNBALANCED journal (debit={entry['total_debit']}, credit={entry['total_credit']})"
            )
            review.append(entry)
        elif net_amount > 1000 or line_count > 5:
            entry["recommendation"] = "KEEP"
            entry["reason"] = "Material manual journal entry — likely legitimate accounting adjustment"
            keep.append(entry)
        elif "accrual" in memo or "adjustment" in memo or "reclass" in memo:
            entry["recommendation"] = "KEEP"
            entry["reason"] = "Manual accrual/adjustment/reclassification — legitimate accounting entry"
            keep.append(entry)
        else:
            entry["recommendation"] = "REVIEW_REQUIRED"
            entry["reason"] = "Manual entry — review memo and account distribution before deleting"
            review.append(entry)

    print(f"  SAFE TO DELETE:  {len(safe)}")
    print(f"  REVIEW REQUIRED: {len(review)}")
    print(f"  KEEP:            {len(keep)}")

    return {
        "safe_to_delete": safe,
        "review_required": review,
        "keep": keep,
        "counts": {"safe": len(safe), "review": len(review), "keep": len(keep)}
    }


# ═════════════════════════════════════════════════════════════════════════════
# 7. SUMMARY REPORT
# ═════════════════════════════════════════════════════════════════════════════

def print_summary(entries: list, root_causes: dict, recommendations: dict, qb_txns: dict):
    """Print actionable console summary."""
    div = "─" * 70
    eq = "=" * 70

    print("\n" + eq)
    print(f"  BLANK TRANID ANALYSIS SUMMARY — Prestige (subsidiary {SUBSIDIARY_ID})")
    print(f"  Generated: {datetime.now().isoformat()}")
    print(eq)

    print("\n  OVERVIEW")
    print(div)
    print(f"  Total blank-tranid entries:     {len(entries):>6}")
    print(f"  QB transactions in server:      {len(qb_txns):>6}")

    print("\n  ROOT CAUSES")
    print(div)
    for cause, count in root_causes["causes"].items():
        icon = {
            "INTEGRATION_DUPLICATE": "🔁",
            "INTEGRATION_MISSING_TRANID": "⚠️",
            "MANUAL_ELBERT": "✍️",
            "MANUAL_JACK": "✍️",
            "MANUAL_OTHER": "✍️",
            "SYSTEM_ADJUSTMENT": "⚙️"
        }.get(cause, "❓")
        print(f"  {icon} {cause:<35} {count:>6}")

    print("\n  DELETION RECOMMENDATIONS")
    print(div)
    print(f"  🗑️  SAFE TO DELETE:   {recommendations['counts']['safe']:>6}")
    print(f"  🔍  REVIEW REQUIRED:  {recommendations['counts']['review']:>6}")
    print(f"  ✅  KEEP:             {recommendations['counts']['keep']:>6}")

    if recommendations["safe_to_delete"]:
        print("\n  SAFE TO DELETE (first 10):")
        print(div)
        for e in recommendations["safe_to_delete"][:10]:
            dup = e.get("duplicate_analysis", {})
            print(f"  NS id={e['ns_id']:<12}  date={e['trandate']}  " +
                  f"net=${e['net_amount']:>10.2f}  " +
                  f"dup_of={dup.get('matched_qb_tid', 'N/A')}")

    if recommendations["review_required"]:
        print("\n  REVIEW REQUIRED (first 10):")
        print(div)
        for e in recommendations["review_required"][:10]:
            print(f"  NS id={e['ns_id']:<12}  date={e['trandate']}  " +
                  f"creator={e['creator']:<20}  " +
                  f"net=${e['net_amount']:>10.2f}")
            print(f"       Reason: {e['reason']}")
            print(f"       URL: {e['portal_url']}")

    safe_total = sum(e["net_amount"] for e in recommendations["safe_to_delete"])
    review_total = sum(e["net_amount"] for e in recommendations["review_required"])
    keep_total = sum(e["net_amount"] for e in recommendations["keep"])

    print("\n  FINANCIAL IMPACT IF DELETED")
    print(div)
    print(f"  Safe to delete total net amount:  ${safe_total:>12,.2f}")
    print(f"  Review required total net amount: ${review_total:>12,.2f}")
    print(f"  Keep total net amount:            ${keep_total:>12,.2f}")

    print("\n  LIKELY CAUSE OF THE PROBLEM")
    print(div)
    if root_causes["causes"].get("INTEGRATION_DUPLICATE", 0) > 0:
        print("  🔴 INTEGRATION DUPLICATES: Your QB→NS integration posted entries")
        print("     but failed to set the tranId field, creating phantom duplicates.")
        print("     These are SAFE to delete — the real entries have numeric tranIds.")
    if root_causes["causes"].get("INTEGRATION_MISSING_TRANID", 0) > 0:
        print("  🟡 INTEGRATION GAP: Entries were posted by integration but tranId")
        print("     was not populated. These need investigation — they may be valid")
        print("     QB entries that need their tranId backfilled.")
    if root_causes["causes"].get("MANUAL_ELBERT", 0) > 1000:
        print("  🟢 MANUAL ENTRIES: Elbert Ehsan created many manual journals directly")
        print("     in NetSuite. These are likely legitimate month-end adjustments.")
        print("     DO NOT delete without confirming with accounting team.")

    print("\n" + eq)
    print("  NEXT STEPS:")
    print("  1. Review the JSON report for full details")
    print("  2. Open a few REVIEW_REQUIRED entries in NetSuite to verify")
    print("  3. Delete SAFE_TO_DELETE entries using the generated delete script")
    print("  4. Fix integration to prevent future blank-tranId entries")
    print(eq + "\n")


# ═════════════════════════════════════════════════════════════════════════════
# 8. GENERATE DELETE SCRIPT
# ═════════════════════════════════════════════════════════════════════════════

DELETE_SCRIPT_TEMPLATE = """
# Auto-generated deletion script for blank-tranid entries
# Period: {period}
# Generated: {generated_at}
#
# DELETES {count} entries classified as SAFE_TO_DELETE

import requests
from netsuite_posting import get_jwt_token, generate_access_token
from dotenv import dotenv_values

config = dotenv_values(".env")
NETSUITE_BASE = config.get('NETSITE_BASE_URL').rstrip('/')
JWT_BASE_URL  = config.get('AZURE_NREST_BASE_URL')

SAFE_TO_DELETE_NS_IDS = {ns_ids}

def delete_entry(ns_id: str, token: dict):
    url = f"{NETSUITE_BASE}/record/v1/journalentry/{ns_id}"
    headers = {{
        'Authorization': f"{{token.get('token_type')}} {{token.get('access_token')}}",
    }}
    r = requests.delete(url, headers=headers)
    return r.status_code == 204

def main():
    jwt = get_jwt_token(JWT_BASE_URL)
    token = generate_access_token(NETSUITE_BASE, jwt)

    success = 0
    failed = 0

    for ns_id in SAFE_TO_DELETE_NS_IDS:
        if delete_entry(ns_id, token):
            print(f"Deleted {{ns_id}}")
            success += 1
        else:
            print(f"Failed to delete {{ns_id}}")
            failed += 1

    print(f"Done: {{success}} deleted, {{failed}} failed")

if __name__ == '__main__':
    main()
"""

def generate_delete_script(recommendations: dict, from_date: str, to_date: str):
    """Generate a Python script to delete SAFE entries."""
    safe_entries = recommendations["safe_to_delete"]
    if not safe_entries:
        return None

    ns_ids = [e["ns_id"] for e in safe_entries]
    script = DELETE_SCRIPT_TEMPLATE.format(
        period=f"{from_date} to {to_date}",
        generated_at=datetime.now().isoformat(),
        count=len(ns_ids),
        ns_ids=ns_ids
    )
    fname = f"delete_safe_blank_tranids_{from_date.replace(chr(45),chr(95))}_{to_date.replace(chr(45),chr(95))}.py"
    with open(fname, "w") as f:
        f.write(script)
    print(f"  Delete script generated: {fname}")
    return fname


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Analyze blank-tranid NetSuite entries for Prestige"
    )
    parser.add_argument("--from-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--deep-compare", action="store_true",
                        help="Compare line-level data with QB-posted entries")
    parser.add_argument("--suggest-deletions", action="store_true",
                        help="Generate deletion recommendations and script")
    args = parser.parse_args()

    print("\n" + "="*70)
    print("  PRESTIGE BLANK TRANID DIAGNOSTIC")
    print(f"  Period: {args.from_date} to {args.to_date}")
    print("="*70)

    print("\n  Authenticating with NetSuite...")
    token = get_ns_token()
    print("  OK")

    # 1. Fetch blank entries from NS
    blank_entries = fetch_blank_tranid_entries(token, args.from_date, args.to_date)
    if not blank_entries:
        print("\n  No blank-tranid entries found. Exiting.")
        return

    # 2. Fetch QB data from SERVER MEMORY (not DB)
    qb_txns = fetch_qb_from_server()

    # 3. Fetch numeric entries for comparison
    numeric_entries = {}
    if args.deep_compare or args.suggest_deletions:
        numeric_entries = fetch_numeric_tranid_entries(token, args.from_date, args.to_date)

    # 4. Deep comparison
    if args.deep_compare or args.suggest_deletions:
        blank_entries = deep_compare(blank_entries, numeric_entries)

    # 5. Root cause analysis
    root_causes = analyze_root_causes(blank_entries, qb_txns)

    # 6. Deletion recommendations
    recommendations = {"safe_to_delete": [], "review_required": [], "keep": [], "counts": {"safe":0,"review":0,"keep":0}}
    if args.suggest_deletions:
        recommendations = generate_deletion_recommendations(blank_entries)
        generate_delete_script(recommendations, args.from_date, args.to_date)

    # 7. Print summary
    print_summary(blank_entries, root_causes, recommendations, qb_txns)

    # 8. Save full JSON report
    report = {
        "generated_at": datetime.now().isoformat(),
        "subsidiary": f"{SUBSIDIARY_ID} (Prestige)",
        "date_range": {"from": args.from_date, "to": args.to_date},
        "root_causes": root_causes,
        "recommendations": {
            "counts": recommendations["counts"],
            "safe_to_delete": [
                {"ns_id": e["ns_id"], "tranid": e["tranid"], "trandate": e["trandate"],
                 "creator": e["creator"], "net_amount": e["net_amount"],
                 "reason": e.get("reason"), "portal_url": e["portal_url"]}
                for e in recommendations["safe_to_delete"]
            ],
            "review_required": [
                {"ns_id": e["ns_id"], "tranid": e["tranid"], "trandate": e["trandate"],
                 "creator": e["creator"], "net_amount": e["net_amount"],
                 "reason": e.get("reason"), "portal_url": e["portal_url"]}
                for e in recommendations["review_required"]
            ],
            "keep": [
                {"ns_id": e["ns_id"], "tranid": e["tranid"], "trandate": e["trandate"],
                 "creator": e["creator"], "net_amount": e["net_amount"],
                 "reason": e.get("reason"), "portal_url": e["portal_url"]}
                for e in recommendations["keep"]
            ]
        },
        "entries": blank_entries
    }

    fname = f"blank_tranid_analysis_{args.from_date.replace(chr(45),chr(95))}_{args.to_date.replace(chr(45),chr(95))}.json"
    with open(fname, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"  Full report saved -> {fname}")


if __name__ == '__main__':
    main()