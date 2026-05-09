from data_transformation import data_transformation
from netsuite_data_transformation import (
    load_mappings,
    transform_data_in_netsuite_format,
    send_post_failure_email_via_logic_app,
    send_batch_summary_email_via_logic_app,
)
from netsuite_posting import (
    get_jwt_token,
    generate_access_token,
    post_jounral_entry_netsuite,
    post_updated_jounral_entry_netsuite,
    check_journal_entry_exists,
    delete_journal_entry,
    check_journal_entry_by_tranid,
)
from azure_database_posting import (
    compair_data,
    confirm_hash_update,
    post_data_in_database,
    create_db_connection,
    fetch_all_transaction_ids,
    fetch_netsuite_location_for_transaction_ids,
    delete_data,
)
from dotenv import load_dotenv, dotenv_values
from datetime import datetime
import json
from logger_config import logger
from locks import netsuite_lock


load_dotenv()
config = dotenv_values(".env")

database_name     = config.get('AZURE_DATABASE')
database_server   = config.get('AZURE_SERVER')
database_user     = config.get('AZURE_USER')
database_password = config.get('AZURE_PASSWORD')

get_jwt_base_url  = config.get('AZURE_NREST_BASE_URL')
netsuite_base_url = config.get('NETSITE_BASE_URL')


def master_function(responce_json):
    sliced_jounral_enteries_raw = data_transformation(responce_json)
    if not sliced_jounral_enteries_raw:
        logger.error("data_transformation returned empty — nothing to process for this chunk")
        return

    mapping_of_net, locations_mapping = load_mappings()
    total = len(sliced_jounral_enteries_raw)

    # ── Single DB connection + single token for the entire batch ─────────────
    connection = create_db_connection(database_server, database_user, database_password, database_name)
    if not connection:
        logger.error("Cannot start batch — DB connection failed")
        return

    generated_jwt = get_jwt_token(get_jwt_base_url)
    access_token  = generate_access_token(netsuite_base_url, generated_jwt)
    if not access_token:
        logger.error("Cannot start batch — NetSuite access token could not be obtained")
        return

    # ── Result collector (Phase 3) ────────────────────────────────────────────
    results = {
        'created':   [],   # new NS POST + DB INSERT both succeeded
        'updated':   [],   # changed NS PATCH + DB hash update both succeeded
        'no_change': [],   # hash matched — skipped
        'failed':    [],   # any step failed
        'ns_orphans': [],  # NS POST succeeded but DB + rollback both failed
    }

    current_qb_transaction_ids = set()
    all_dates = set()

    logger.info(f"Batch started — {total} journal entries to process")

    for i, entry_key in enumerate(sliced_jounral_enteries_raw, 1):
        transacton_id = entry_key  # safe fallback for except block
        date = ''
        try:
            result = transform_data_in_netsuite_format(
                sliced_jounral_enteries_raw[entry_key], mapping_of_net, locations_mapping
            )
            if result is None:
                logger.error(f"[{i}/{total}] Skipping {entry_key} — transformation returned None")
                results['failed'].append({
                    'txn_id': entry_key, 'step': 'transformation',
                    'error': 'transform returned None', 'date': '',
                })
                continue

            data_to_post, transacton_id, date = result
            if data_to_post is None or transacton_id is None:
                logger.error(f"[{i}/{total}] Skipping {entry_key} — missing payload or txn_id after transformation")
                results['failed'].append({
                    'txn_id': entry_key, 'step': 'transformation',
                    'error': 'missing data_to_post or txn_id', 'date': date,
                })
                continue

            current_qb_transaction_ids.add(transacton_id)
            all_dates.add(date)
            logger.info(f"[{i}/{total}][txn:{transacton_id}] Processing | date:{date}")

            with netsuite_lock:
                comparison_result = compair_data(data_to_post, transacton_id, connection)

                # ── BRANCH A: Not in DB (new transaction) ─────────────────────
                if comparison_result and comparison_result.get('query_status') == False:
                    logger.info(f"[txn:{transacton_id}] Not in DB — checking NetSuite via SuiteQL")

                    existing_ns_location = check_journal_entry_by_tranid(
                        netsuite_base_url, access_token, transacton_id
                    )

                    if existing_ns_location:
                        # Exists in NS but missing from DB — reconcile both
                        logger.info(f"[txn:{transacton_id}] Found in NS but missing from DB — patching NS and inserting DB record")
                        patch_response = post_updated_jounral_entry_netsuite(
                            existing_ns_location, access_token, data_to_post
                        )
                        if patch_response is None or patch_response.status_code not in (200, 201, 204):
                            error_msg = patch_response.text if patch_response else "No response from NetSuite on reconcile PATCH"
                            logger.error(f"[txn:{transacton_id}] NS reconcile PATCH failed ({patch_response.status_code if patch_response else 'N/A'}): {error_msg}")
                            results['failed'].append({
                                'txn_id': transacton_id, 'step': 'ns_reconcile_patch',
                                'error': error_msg, 'date': date,
                            })
                            send_post_failure_email_via_logic_app(
                                transacton_id,
                                f"NS reconcile PATCH failed — entry exists in NS but DB is missing it. Error: {error_msg}"
                            )
                            continue

                        logger.info(f"[txn:{transacton_id}] NS reconcile PATCH OK ({patch_response.status_code}) — inserting into DB")
                        db_ok = post_data_in_database(data_to_post, transacton_id, existing_ns_location, connection)
                        if db_ok:
                            logger.info(f"[txn:{transacton_id}] Reconciled — NS updated and DB record created")
                            results['created'].append({'txn_id': transacton_id, 'date': date, 'action': 'reconciled'})
                        else:
                            logger.error(f"[txn:{transacton_id}] NS PATCH succeeded but DB INSERT failed — NS has correct data but DB record is still missing")
                            results['failed'].append({
                                'txn_id': transacton_id, 'step': 'db_insert_after_reconcile',
                                'error': 'DB INSERT failed after NS reconcile PATCH — NS is correct, DB needs manual update',
                                'date': date,
                            })
                            send_post_failure_email_via_logic_app(
                                transacton_id,
                                "DB INSERT failed after NS reconcile PATCH. NetSuite has the correct entry but the DB record is missing. Manual DB insert required."
                            )

                    else:
                        # Brand new transaction — POST to NS then INSERT to DB
                        logger.info(f"[txn:{transacton_id}] New transaction — posting to NetSuite")
                        ns_response = post_jounral_entry_netsuite(netsuite_base_url, access_token, data_to_post)

                        if ns_response is None or ns_response.status_code not in (200, 201, 204):
                            error_msg = ns_response.text if ns_response else "No response from NetSuite on POST"
                            logger.error(f"[txn:{transacton_id}] NS POST failed ({ns_response.status_code if ns_response else 'N/A'}): {error_msg}")
                            results['failed'].append({
                                'txn_id': transacton_id, 'step': 'ns_post',
                                'error': error_msg, 'date': date,
                            })
                            send_post_failure_email_via_logic_app(
                                transacton_id,
                                f"NS POST failed — journal entry could not be created in NetSuite. Error: {error_msg}"
                            )
                            continue

                        ns_location = ns_response.headers.get('Location')
                        logger.info(f"[txn:{transacton_id}] NS POST OK ({ns_response.status_code}) | NS location: {ns_location}")

                        # ── Phase 2: compensate if DB INSERT fails after NS POST ──
                        db_ok = post_data_in_database(data_to_post, transacton_id, ns_location, connection)
                        if db_ok:
                            logger.info(f"[txn:{transacton_id}] DB INSERT OK — transaction fully synced (NS + DB)")
                            results['created'].append({'txn_id': transacton_id, 'date': date, 'action': 'created'})
                        else:
                            # NS has the entry, DB doesn't — attempt rollback to keep systems in sync
                            logger.error(f"[txn:{transacton_id}] DB INSERT failed after NS POST — attempting NS rollback to avoid orphan")
                            rollback = delete_journal_entry(ns_location, access_token)
                            if rollback and rollback.status_code in (200, 204):
                                logger.info(f"[txn:{transacton_id}] NS rollback succeeded — NS entry removed cleanly. Will retry on next sync.")
                                results['failed'].append({
                                    'txn_id': transacton_id, 'step': 'db_insert',
                                    'error': 'DB INSERT failed after NS POST — NS entry rolled back. Will retry on next sync.',
                                    'date': date,
                                })
                                send_post_failure_email_via_logic_app(
                                    transacton_id,
                                    "DB INSERT failed after NS POST. The NS entry was automatically rolled back to keep systems in sync. This transaction will be retried on the next sync."
                                )
                            else:
                                # Worst case: NS has it, DB doesn't, rollback also failed
                                rollback_error = rollback.text if rollback else "No response on NS rollback DELETE"
                                logger.error(f"[txn:{transacton_id}] NS ORPHAN — DB INSERT failed AND NS rollback failed. NS location: {ns_location} | Rollback error: {rollback_error}. Manual cleanup required.")
                                results['ns_orphans'].append({
                                    'txn_id': transacton_id,
                                    'ns_location': ns_location,
                                    'note': f"DB INSERT failed, NS rollback also failed: {rollback_error}",
                                    'date': date,
                                })
                                send_post_failure_email_via_logic_app(
                                    transacton_id,
                                    f"CRITICAL — NS ORPHAN DETECTED: Entry was posted to NetSuite but the DB record could not be saved, and the automatic NS rollback also failed.\n\nNS Location: {ns_location}\nRollback Error: {rollback_error}\n\nManual action required: either delete this entry from NetSuite or manually insert the DB record."
                                )

                # ── BRANCH B: In DB, hash changed (updated transaction) ────────
                elif comparison_result and comparison_result.get('query_status') == True:
                    ns_update_url = comparison_result.get('update_url')
                    new_hash = comparison_result.get('new_hash')
                    logger.info(f"[txn:{transacton_id}] Change detected — patching NetSuite | update_url: {ns_update_url}")

                    patch_response = post_updated_jounral_entry_netsuite(ns_update_url, access_token, data_to_post)
                    if patch_response is None or patch_response.status_code not in (200, 201, 204):
                        error_msg = patch_response.text if patch_response else "No response from NetSuite on PATCH"
                        # DB hash was NOT changed (Phase 1 fix) so next sync will detect the change again and retry
                        logger.error(f"[txn:{transacton_id}] NS PATCH failed ({patch_response.status_code if patch_response else 'N/A'}): {error_msg} — DB hash preserved, will retry on next sync")
                        results['failed'].append({
                            'txn_id': transacton_id, 'step': 'ns_patch',
                            'error': error_msg, 'date': date,
                        })
                        send_post_failure_email_via_logic_app(
                            transacton_id,
                            f"NS PATCH failed — journal entry update could not be applied to NetSuite. The DB hash has NOT been changed so this transaction will be retried automatically on the next sync. Error: {error_msg}"
                        )
                        continue

                    logger.info(f"[txn:{transacton_id}] NS PATCH OK ({patch_response.status_code}) — committing updated hash to DB")
                    # ── Phase 1 fix: update DB hash only after NS confirms success ──
                    db_ok = confirm_hash_update(transacton_id, new_hash, connection)
                    if db_ok:
                        logger.info(f"[txn:{transacton_id}] Updated — NS PATCH and DB hash both confirmed in sync")
                        results['updated'].append({'txn_id': transacton_id, 'date': date})
                    else:
                        # NS was patched correctly. DB hash is stale but this is safe:
                        # next sync will re-detect the change and re-patch NS (idempotent)
                        logger.error(f"[txn:{transacton_id}] NS PATCH OK but DB hash update failed — next sync will re-patch NS (safe but wasteful)")
                        results['failed'].append({
                            'txn_id': transacton_id, 'step': 'db_hash_update',
                            'error': 'NS PATCH succeeded but DB hash update failed — will re-patch on next sync',
                            'date': date,
                        })

                # ── BRANCH C: No change ────────────────────────────────────────
                elif comparison_result and comparison_result.get('query_status') == "No change":
                    logger.info(f"[txn:{transacton_id}] No change — skipping")
                    results['no_change'].append({'txn_id': transacton_id, 'date': date})

        except Exception as e:
            txn_label = locals().get('transacton_id', entry_key)
            logger.error(f"[{i}/{total}][txn:{txn_label}] Unhandled exception: {e}")
            results['failed'].append({
                'txn_id': txn_label, 'step': 'unhandled_exception',
                'error': str(e), 'date': locals().get('date', ''),
            })
            continue

    # ── DELETION SWEEP ────────────────────────────────────────────────────────
    logger.info(f"Deletion sweep starting — checking DB records for dates: {sorted(all_dates)}")
    db_records_set = set()
    for date in all_dates:
        db_records = fetch_all_transaction_ids(connection, date)
        db_records_set.update(map(str, db_records))

    deleted_ids = db_records_set - current_qb_transaction_ids

    if deleted_ids:
        logger.info(f"Deletion sweep found {len(deleted_ids)} transaction(s) in DB absent from QB: {deleted_ids}")
        deleted_locations = fetch_netsuite_location_for_transaction_ids(connection, deleted_ids)

        for tid, location in deleted_locations.items():
            ns_check = check_journal_entry_exists(location, access_token)
            if ns_check and ns_check.status_code == 200:
                logger.info(f"[txn:{tid}] Confirmed in NS — deleting from NS and DB")
                del_response = delete_journal_entry(location, access_token)

                if del_response is None or del_response.status_code not in (200, 201, 204):
                    error_msg = del_response.text if del_response else "No response from NetSuite on DELETE"
                    logger.error(f"[txn:{tid}] NS DELETE failed ({del_response.status_code if del_response else 'N/A'}): {error_msg}")
                    results['failed'].append({
                        'txn_id': tid, 'step': 'ns_delete',
                        'error': error_msg, 'date': '',
                    })
                    send_post_failure_email_via_logic_app(
                        tid,
                        f"NS DELETE failed during deletion sweep — entry should have been removed from NetSuite but wasn't. Error: {error_msg}"
                    )
                    continue

                logger.info(f"[txn:{tid}] NS DELETE OK ({del_response.status_code}) — removing from DB")
                db_del_ok = delete_data(tid, connection)
                if db_del_ok:
                    logger.info(f"[txn:{tid}] Deleted from NS and DB successfully")
                else:
                    logger.error(f"[txn:{tid}] NS DELETE succeeded but DB DELETE failed — stale DB record remains. Manual cleanup required.")
                    results['failed'].append({
                        'txn_id': tid, 'step': 'db_delete',
                        'error': 'NS DELETE succeeded but DB DELETE failed — stale DB record remains',
                        'date': '',
                    })
                    send_post_failure_email_via_logic_app(
                        tid,
                        "NS DELETE succeeded but DB DELETE failed during deletion sweep. A stale DB record remains and must be manually removed."
                    )
            else:
                logger.warning(f"[txn:{tid}] Not found in NS (already removed?) — removing stale DB record")
                delete_data(tid, connection)
    else:
        logger.info("Deletion sweep complete — no stale transactions found")

    # ── BATCH SUMMARY (Phase 3) ───────────────────────────────────────────────
    logger.info(
        f"Batch complete — "
        f"{len(results['created'])} created | "
        f"{len(results['updated'])} updated | "
        f"{len(results['no_change'])} no_change | "
        f"{len(results['failed'])} failed | "
        f"{len(results['ns_orphans'])} NS orphans"
    )

    if results['failed'] or results['ns_orphans']:
        failed_ids = [r['txn_id'] for r in results['failed']]
        orphan_ids = [r['txn_id'] for r in results['ns_orphans']]
        logger.error(f"Failed txn IDs: {failed_ids}")
        if orphan_ids:
            logger.error(f"NS orphan txn IDs (manual cleanup required): {orphan_ids}")

        # Write failed IDs to timestamped file for retry_failed_transactions.py
        all_failed = results['failed'] + results['ns_orphans']
        if all_failed:
            filename = f"failed_txns_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            with open(filename, 'w') as f:
                for r in all_failed:
                    f.write(f"{r['txn_id']}\t{r['step']}\t{r['error']}\n")
            logger.info(f"Failed transaction details written to {filename}")

        send_batch_summary_email_via_logic_app(all_dates, results, total)
