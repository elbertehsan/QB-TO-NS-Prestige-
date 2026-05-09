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
            The following account ID was not found in the Netsuite mappings:


            - Account: {account}
            - Original Entry: {original_account}


            Please update the mappings accordingly.
            """,
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
        size_info = f"\n- Payload Size: {data_size_mb:.2f} MB (limit: 1 MB)" if data_size_mb else ""
        payload = {
            "subject": f"Failed to Post Journal Entry (Prestige): TxnID {transaction_id}",
            "body": f"""
            A journal entry failed to post to NetSuite:

            - Transaction ID: {transaction_id}
            - Error: {error_message}

            Please investigate and repost manually if required.
            """,
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
            The end-of-day check has detected that no transactions were posted to NetSuite today:

            - Date: {date}

            This may indicate a sync issue with the QuickBooks Web Connector or NetSuite API.
            Please investigate.
            """,
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
            The following location was not found in the Netsuite mappings:


            - Location: {location}
            - Original Entry: {original_location}


            Please update the mappings accordingly.
            """,
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
        failed  = results.get('failed', [])
        orphans = results.get('ns_orphans', [])
        created = results.get('created', [])
        updated = results.get('updated', [])
        no_change = results.get('no_change', [])

        dates_str = ', '.join(sorted(str(d) for d in all_dates)) if all_dates else 'unknown'

        failed_rows = '\n'.join(
            f"  - TxnID {r['txn_id']} | date: {r.get('date','')} | step: {r['step']} | error: {r['error']}"
            for r in failed
        ) or '  None'

        orphan_rows = '\n'.join(
            f"  - TxnID {r['txn_id']} | NS location: {r.get('ns_location','')} | note: {r['note']}"
            for r in orphans
        ) or '  None'

        subject = f"[Prestige Sync] Batch completed with issues — {len(failed)} failed, {len(orphans)} NS orphans | dates: {dates_str}"

        body = f"""
Prestige QuickBooks → NetSuite sync batch completed with issues.

DATES PROCESSED: {dates_str}

SUMMARY
-------
  Total entries  : {total}
  Created (new)  : {len(created)}
  Updated        : {len(updated)}
  No change      : {len(no_change)}
  Failed         : {len(failed)}
  NS Orphans     : {len(orphans)}

FAILED TRANSACTIONS
-------------------
{failed_rows}

NS ORPHANS (NetSuite entry exists but DB record missing — manual cleanup required)
-----------
{orphan_rows}

ACTION REQUIRED
---------------
- Review failed transactions above and repost manually if needed.
- NS orphans must be manually deleted from NetSuite or re-synced.
- Failed transaction IDs have been written to a failed_txns_*.txt file on the server.
        """

        payload = {
            "subject": subject,
            "body": body,
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
