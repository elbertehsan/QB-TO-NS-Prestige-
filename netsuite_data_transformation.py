import json
import Netsuite_mappings
from logger_config import logger
from dotenv import load_dotenv,dotenv_values
import re
import requests
load_dotenv()
config = dotenv_values(".env")
AZURE_LOGIC_APP = config.get('AZURE_LOGIC_APP')


def send_email_via_logic_app(account, original_account):
    try:
        payload = {
            "subject": f"Missing Account Mapping (Prestige): {account}",
            "body": f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background:#f4f4f4;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:24px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr><td style="background:#c0392b;padding:20px 32px;">
          <span style="color:#ffffff;font-size:18px;font-weight:bold;">&#9888; Missing Account Mapping — Prestige</span>
        </td></tr>
        <tr><td style="padding:28px 32px;">
          <p style="margin:0 0 20px;color:#333;font-size:14px;">An account ID was encountered during sync that has no NetSuite mapping. The affected line item was skipped.</p>
          <table cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;font-size:14px;">
            <tr style="background:#fdf3f2;">
              <td style="padding:10px 14px;border:1px solid #f0d0cc;font-weight:bold;color:#555;width:160px;">Account ID</td>
              <td style="padding:10px 14px;border:1px solid #f0d0cc;color:#c0392b;font-weight:bold;">{account}</td>
            </tr>
            <tr>
              <td style="padding:10px 14px;border:1px solid #e8e8e8;font-weight:bold;color:#555;">Original Entry</td>
              <td style="padding:10px 14px;border:1px solid #e8e8e8;color:#333;">{original_account}</td>
            </tr>
          </table>
          <p style="margin:24px 0 0;padding:14px 16px;background:#fff8e1;border-left:4px solid #f39c12;color:#555;font-size:13px;">
            Please add this account to <strong>Netsuite_mappings.py</strong> and re-run the sync for the affected date.
          </p>
        </td></tr>
        <tr><td style="padding:14px 32px;background:#f9f9f9;border-top:1px solid #eee;font-size:12px;color:#999;">
          Prestige QB → NetSuite Sync &nbsp;|&nbsp; Auto-generated alert
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
""",
            "isHtml": True,
            "to": "elbert.ehsan@epikafleet.com",
            "cc": "tijo.ammakuzhiyil@epikafleet.com"
        }

        headers = {"Content-Type": "application/json"}
        response = requests.post(AZURE_LOGIC_APP, json=payload, headers=headers)

        if response.status_code == 202:
            logger.info(f"Notification sent for missing account mapping: {account}")
        else:
            logger.error(f"Failed to send notification. Status: {response.status_code}, Response: {response.text}")

    except Exception as e:
        logger.error(f"ERROR WHILE SENDING EMAIL VIA LOGIC APP: {e}")

def send_post_failure_email_via_logic_app(transaction_id, error_message, data_size_mb=None):
    try:
        size_row = f"""
            <tr style="background:#fdf3f2;">
              <td style="padding:10px 14px;border:1px solid #f0d0cc;font-weight:bold;color:#555;">Payload Size</td>
              <td style="padding:10px 14px;border:1px solid #f0d0cc;color:#c0392b;">{data_size_mb:.2f} MB &nbsp;<span style="color:#888;">(limit: 1 MB)</span></td>
            </tr>""" if data_size_mb else ""

        payload = {
            "subject": f"Failed to Post Journal Entry (Prestige): TxnID {transaction_id}",
            "body": f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background:#f4f4f4;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:24px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr><td style="background:#c0392b;padding:20px 32px;">
          <span style="color:#ffffff;font-size:18px;font-weight:bold;">&#10060; Journal Entry Post Failed — Prestige</span>
        </td></tr>
        <tr><td style="padding:28px 32px;">
          <p style="margin:0 0 20px;color:#333;font-size:14px;">A journal entry could not be posted to NetSuite and requires manual attention.</p>
          <table cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;font-size:14px;">
            <tr style="background:#fdf3f2;">
              <td style="padding:10px 14px;border:1px solid #f0d0cc;font-weight:bold;color:#555;width:160px;">Transaction ID</td>
              <td style="padding:10px 14px;border:1px solid #f0d0cc;color:#c0392b;font-weight:bold;">{transaction_id}</td>
            </tr>
            <tr>
              <td style="padding:10px 14px;border:1px solid #e8e8e8;font-weight:bold;color:#555;vertical-align:top;">Error</td>
              <td style="padding:10px 14px;border:1px solid #e8e8e8;color:#333;word-break:break-word;">{error_message}</td>
            </tr>{size_row}
          </table>
          <p style="margin:24px 0 0;padding:14px 16px;background:#fff8e1;border-left:4px solid #f39c12;color:#555;font-size:13px;">
            Please investigate and use <strong>retry_failed_transactions.py</strong> to repost manually if required.
          </p>
        </td></tr>
        <tr><td style="padding:14px 32px;background:#f9f9f9;border-top:1px solid #eee;font-size:12px;color:#999;">
          Prestige QB → NetSuite Sync &nbsp;|&nbsp; Auto-generated alert
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
""",
            "isHtml": True,
            "to": "elbert.ehsan@epikafleet.com",
            "cc": "tijo.ammakuzhiyil@epikafleet.com"
        }
        headers = {"Content-Type": "application/json"}
        response = requests.post(AZURE_LOGIC_APP, json=payload, headers=headers)
        if response.status_code == 202:
            logger.info(f"Failure notification sent for transaction: {transaction_id}")
        else:
            logger.error(f"Failed to send failure notification. Status: {response.status_code}")
    except Exception as e:
        logger.error(f"ERROR WHILE SENDING FAILURE EMAIL: {e}")


def send_no_transactions_email_via_logic_app(date):
    try:
        payload = {
            "subject": f"No Transactions Posted to NetSuite (Prestige) — {date}",
            "body": f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background:#f4f4f4;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:24px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr><td style="background:#e67e22;padding:20px 32px;">
          <span style="color:#ffffff;font-size:18px;font-weight:bold;">&#9888; No Transactions Posted — Prestige</span>
        </td></tr>
        <tr><td style="padding:28px 32px;">
          <p style="margin:0 0 20px;color:#333;font-size:14px;">The end-of-day check detected that <strong>no transactions</strong> were posted to NetSuite for the following date:</p>
          <table cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;font-size:14px;">
            <tr style="background:#fef9f0;">
              <td style="padding:10px 14px;border:1px solid #f5e6cc;font-weight:bold;color:#555;width:160px;">Date</td>
              <td style="padding:10px 14px;border:1px solid #f5e6cc;color:#e67e22;font-weight:bold;">{date}</td>
            </tr>
          </table>
          <p style="margin:24px 0 0;padding:14px 16px;background:#fff8e1;border-left:4px solid #f39c12;color:#555;font-size:13px;">
            This may indicate a sync issue with the QuickBooks Web Connector or the NetSuite API. Please investigate promptly.
          </p>
        </td></tr>
        <tr><td style="padding:14px 32px;background:#f9f9f9;border-top:1px solid #eee;font-size:12px;color:#999;">
          Prestige QB → NetSuite Sync &nbsp;|&nbsp; Auto-generated alert
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
""",
            "isHtml": True,
            "to": "elbert.ehsan@epikafleet.com",
            "cc": "tijo.ammakuzhiyil@epikafleet.com"
        }
        headers = {"Content-Type": "application/json"}
        response = requests.post(AZURE_LOGIC_APP, json=payload, headers=headers)
        if response.status_code == 202:
            logger.info(f"No-transactions alert sent for {date}")
        else:
            logger.error(f"Failed to send no-transactions alert. Status: {response.status_code}")
    except Exception as e:
        logger.error(f"ERROR WHILE SENDING NO-TRANSACTIONS EMAIL: {e}")

        
def send_location_email_via_logic_app(location, original_location):
    try:
        payload = {
            "subject": f"Missing Location Mapping (Prestige): {location}",
            "body": f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background:#f4f4f4;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:24px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr><td style="background:#8e44ad;padding:20px 32px;">
          <span style="color:#ffffff;font-size:18px;font-weight:bold;">&#9888; Missing Location Mapping — Prestige</span>
        </td></tr>
        <tr><td style="padding:28px 32px;">
          <p style="margin:0 0 20px;color:#333;font-size:14px;">A QB class/location was encountered during sync that has no NetSuite mapping. The affected line item was skipped.</p>
          <table cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;font-size:14px;">
            <tr style="background:#f9f0ff;">
              <td style="padding:10px 14px;border:1px solid #e0c8f0;font-weight:bold;color:#555;width:160px;">Location</td>
              <td style="padding:10px 14px;border:1px solid #e0c8f0;color:#8e44ad;font-weight:bold;">{location}</td>
            </tr>
            <tr>
              <td style="padding:10px 14px;border:1px solid #e8e8e8;font-weight:bold;color:#555;">Original Entry</td>
              <td style="padding:10px 14px;border:1px solid #e8e8e8;color:#333;">{original_location}</td>
            </tr>
          </table>
          <p style="margin:24px 0 0;padding:14px 16px;background:#fff8e1;border-left:4px solid #f39c12;color:#555;font-size:13px;">
            Please add this location to <strong>Netsuite_mappings.py</strong> (Location_mappings) and re-run the sync for the affected date.
          </p>
        </td></tr>
        <tr><td style="padding:14px 32px;background:#f9f9f9;border-top:1px solid #eee;font-size:12px;color:#999;">
          Prestige QB → NetSuite Sync &nbsp;|&nbsp; Auto-generated alert
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
""",
            "isHtml": True,
            "to": "elbert.ehsan@epikafleet.com",
            "cc": "tijo.ammakuzhiyil@epikafleet.com,Jack.Kegermann@epikafleet.com,mlarson@pfstruck.com,lgeske@managedmobile.com"
        }

        headers = {"Content-Type": "application/json"}
        response = requests.post(AZURE_LOGIC_APP, json=payload, headers=headers)

        if response.status_code == 202:
            logger.info(f"Notification sent for missing location mapping: {location}")
        else:
            logger.error(f"Failed to send notification. Status: {response.status_code}, Response: {response.text}")

    except Exception as e:
        logger.error(f"ERROR WHILE SENDING EMAIL VIA LOGIC APP: {e}")

def send_batch_summary_email_via_logic_app(all_dates, results, total):
    """Sends one summary email at the end of a batch when there are failures or NS orphans."""
    try:
        failed    = results.get('failed', [])
        orphans   = results.get('ns_orphans', [])
        created   = results.get('created', [])
        updated   = results.get('updated', [])
        no_change = results.get('no_change', [])

        dates_str = ', '.join(sorted(str(d) for d in all_dates)) if all_dates else 'unknown'

        failed_rows_html = ''.join(
            f"""<tr>
              <td style="padding:9px 12px;border:1px solid #f0d0cc;color:#c0392b;font-weight:bold;">{r['txn_id']}</td>
              <td style="padding:9px 12px;border:1px solid #f0d0cc;color:#555;">{r.get('date','')}</td>
              <td style="padding:9px 12px;border:1px solid #f0d0cc;color:#555;">{r['step']}</td>
              <td style="padding:9px 12px;border:1px solid #f0d0cc;color:#333;word-break:break-word;">{r['error']}</td>
            </tr>"""
            for r in failed
        ) if failed else '<tr><td colspan="4" style="padding:9px 12px;border:1px solid #e8e8e8;color:#999;text-align:center;">None</td></tr>'

        orphan_rows_html = ''.join(
            f"""<tr>
              <td style="padding:9px 12px;border:1px solid #f0d0cc;color:#c0392b;font-weight:bold;">{r['txn_id']}</td>
              <td style="padding:9px 12px;border:1px solid #f0d0cc;color:#555;word-break:break-all;">{r.get('ns_location','')}</td>
              <td style="padding:9px 12px;border:1px solid #f0d0cc;color:#333;">{r['note']}</td>
            </tr>"""
            for r in orphans
        ) if orphans else '<tr><td colspan="3" style="padding:9px 12px;border:1px solid #e8e8e8;color:#999;text-align:center;">None</td></tr>'

        subject = f"[Prestige Sync] Batch completed with issues — {len(failed)} failed, {len(orphans)} NS orphans | dates: {dates_str}"

        body = f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;font-family:Arial,sans-serif;background:#f4f4f4;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:24px 0;">
    <tr><td align="center">
      <table width="700" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">

        <!-- Header -->
        <tr><td style="background:#2c3e50;padding:20px 32px;">
          <span style="color:#ffffff;font-size:18px;font-weight:bold;">Prestige QB &#8594; NetSuite Sync — Batch Report</span><br>
          <span style="color:#95a5a6;font-size:13px;">Completed with issues &nbsp;|&nbsp; Dates: {dates_str}</span>
        </td></tr>

        <!-- Summary table -->
        <tr><td style="padding:28px 32px 0;">
          <p style="margin:0 0 14px;font-size:15px;font-weight:bold;color:#2c3e50;">Summary</p>
          <table cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;font-size:14px;">
            <tr style="background:#eaf4fb;">
              <td style="padding:10px 14px;border:1px solid #d0e8f5;font-weight:bold;color:#555;width:200px;">Total entries</td>
              <td style="padding:10px 14px;border:1px solid #d0e8f5;font-weight:bold;color:#2980b9;">{total}</td>
            </tr>
            <tr>
              <td style="padding:10px 14px;border:1px solid #e8e8e8;font-weight:bold;color:#555;">Created (new)</td>
              <td style="padding:10px 14px;border:1px solid #e8e8e8;color:#27ae60;font-weight:bold;">{len(created)}</td>
            </tr>
            <tr style="background:#fafafa;">
              <td style="padding:10px 14px;border:1px solid #e8e8e8;font-weight:bold;color:#555;">Updated</td>
              <td style="padding:10px 14px;border:1px solid #e8e8e8;color:#2980b9;font-weight:bold;">{len(updated)}</td>
            </tr>
            <tr>
              <td style="padding:10px 14px;border:1px solid #e8e8e8;font-weight:bold;color:#555;">No change (skipped)</td>
              <td style="padding:10px 14px;border:1px solid #e8e8e8;color:#888;">{len(no_change)}</td>
            </tr>
            <tr style="background:#fdf3f2;">
              <td style="padding:10px 14px;border:1px solid #f0d0cc;font-weight:bold;color:#555;">Failed</td>
              <td style="padding:10px 14px;border:1px solid #f0d0cc;color:#c0392b;font-weight:bold;">{len(failed)}</td>
            </tr>
            <tr style="background:#fdf3f2;">
              <td style="padding:10px 14px;border:1px solid #f0d0cc;font-weight:bold;color:#555;">NS Orphans</td>
              <td style="padding:10px 14px;border:1px solid #f0d0cc;color:#c0392b;font-weight:bold;">{len(orphans)}</td>
            </tr>
          </table>
        </td></tr>

        <!-- Failed transactions -->
        <tr><td style="padding:28px 32px 0;">
          <p style="margin:0 0 14px;font-size:15px;font-weight:bold;color:#2c3e50;">Failed Transactions</p>
          <table cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;font-size:13px;">
            <tr style="background:#e8e8e8;">
              <th style="padding:9px 12px;border:1px solid #ddd;text-align:left;color:#444;width:100px;">TxnID</th>
              <th style="padding:9px 12px;border:1px solid #ddd;text-align:left;color:#444;width:90px;">Date</th>
              <th style="padding:9px 12px;border:1px solid #ddd;text-align:left;color:#444;width:120px;">Step</th>
              <th style="padding:9px 12px;border:1px solid #ddd;text-align:left;color:#444;">Error</th>
            </tr>
            {failed_rows_html}
          </table>
        </td></tr>

        <!-- NS Orphans -->
        <tr><td style="padding:28px 32px 0;">
          <p style="margin:0 0 6px;font-size:15px;font-weight:bold;color:#2c3e50;">NS Orphans</p>
          <p style="margin:0 0 14px;font-size:12px;color:#888;">NetSuite entry exists but DB record is missing — manual cleanup required</p>
          <table cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;font-size:13px;">
            <tr style="background:#e8e8e8;">
              <th style="padding:9px 12px;border:1px solid #ddd;text-align:left;color:#444;width:100px;">TxnID</th>
              <th style="padding:9px 12px;border:1px solid #ddd;text-align:left;color:#444;width:220px;">NS Location</th>
              <th style="padding:9px 12px;border:1px solid #ddd;text-align:left;color:#444;">Note</th>
            </tr>
            {orphan_rows_html}
          </table>
        </td></tr>

        <!-- Action required -->
        <tr><td style="padding:28px 32px;">
          <p style="margin:0 0 10px;font-size:15px;font-weight:bold;color:#2c3e50;">Action Required</p>
          <ul style="margin:0;padding:0 0 0 20px;color:#555;font-size:14px;line-height:1.8;">
            <li>Review failed transactions above and use <strong>retry_failed_transactions.py</strong> to repost.</li>
            <li>NS orphans must be manually deleted from NetSuite or re-synced.</li>
            <li>Failed transaction IDs have been written to a <strong>failed_txns_*.txt</strong> file on the server.</li>
          </ul>
        </td></tr>

        <!-- Footer -->
        <tr><td style="padding:14px 32px;background:#f9f9f9;border-top:1px solid #eee;font-size:12px;color:#999;">
          Prestige QB → NetSuite Sync &nbsp;|&nbsp; Auto-generated batch report
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>
"""

        payload = {
            "subject": subject,
            "body": body,
            "isHtml": True,
            "to": "elbert.ehsan@epikafleet.com",
            "cc": "tijo.ammakuzhiyil@epikafleet.com",
        }
        headers = {"Content-Type": "application/json"}
        response = requests.post(AZURE_LOGIC_APP, json=payload, headers=headers)
        if response.status_code == 202:
            logger.info(f"Batch summary alert sent — {len(failed)} failures, {len(orphans)} orphans")
        else:
            logger.error(f"Failed to send batch summary alert. Status: {response.status_code}")
    except Exception as e:
        logger.error(f"ERROR WHILE SENDING BATCH SUMMARY EMAIL: {e}")


def load_mappings():
    try:
        data = json.loads(Netsuite_mappings.Netsuite_mappings)
        locations_data = json.loads(Netsuite_mappings.Location_mappings)
        return data, locations_data
    except Exception as e:
        logger.error(f"ERROR WHILE LOADING ACCOUNT MAPPINGS:  {e}")

def find_mapped_value(account_id, mapping):
    for key in mapping.keys():
        if key.startswith(account_id):
            return mapping[key]
    return None


def transform_data_in_netsuite_format(journal_entry, mapping, location_mapping):
    items_journal_entry = []
    post_journal_entry = {}
    date = ''
    journal_entry_id = None
    number = 'none'
    try:
        for entries in journal_entry:
            account_data= entries.get('Account')
            account_id_match = re.match(r'^\d+', account_data.strip())  # Match digits at the start of the string
            account_id = account_id_match.group(0) if account_id_match else None
           
            account = find_mapped_value(account_id,mapping)
            txn_number = entries.get('TxnNumber')
            if entries.get('Name'):
                name = entries.get('Name')

            
            if account is None:
                logger.error(f"[txn:{journal_entry_id}] No NS mapping for QB account '{entries.get('Account')}' (parsed id: {account_id})")
                if account_id not in ("89000","71300"):
                    send_email_via_logic_app(account_id, entries.get('Account')) 
                continue


            if txn_number:
                journal_entry_id = txn_number
            
            # After building items_journal_entry, before returning:
            if journal_entry_id is None:
                logger.error(f"No TxnNumber found in journal entry — skipping to prevent blank tranId in NetSuite {journal_entery}")
                raise ValueError("No TxnNumber found — cannot post without tranId")
            memo = entries.get('Memo', '')
            class_value = entries.get('Class')
            location= None
            if class_value:
                location = location_mapping.get(class_value)

            if class_value and location is None:
                logger.error(f"[txn:{journal_entry_id}] No NS mapping for QB location '{class_value}'")
                send_location_email_via_logic_app(class_value, entries.get('Class'))  
                continue
            
            if entries.get('RefNumber'):
                number = entries.get('RefNumber')

            if entries.get('Debit'):
                debit = float(entries.get('Debit'))
            elif entries.get('Credit'):
                debit = -float(entries.get('Credit'))
            else:
                debit = 0.0


            extracted_date = entries.get('Date')
            if extracted_date:
                date = extracted_date
           
            line_item = {
                "account": {"id": account},
                "memo": memo,
                "debit": debit,
                "custcol1": entries.get('Name'),
                "custcol2": number,
            }

            if location:
                line_item["location"] = {"id": location}

            items_journal_entry.append(line_item)
        if not items_journal_entry:
            logger.error("No valid journal entries found.")
            raise ValueError("No valid journal entries found.")


        post_journal_entry["line"] = {"items": items_journal_entry}
        post_journal_entry["subsidiary"] = {"id": "15"}
        post_journal_entry["tranDate"] = date
        post_journal_entry["tranId"] = journal_entry_id
        post_journal_entry["custbodydowntimedoc"] = journal_entry_id

        json_post_journal_entry = json.dumps(post_journal_entry)
        
        return json_post_journal_entry, journal_entry_id, date


    except ValueError as ve:
        logger.error(f"ValueError: {ve}")
    except Exception as e:
        logger.error(f"ERROR WHILE TRANSFORMING DATA IN NETSUITE FORMAT: {e}")
