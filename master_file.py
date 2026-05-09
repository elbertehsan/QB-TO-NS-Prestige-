from data_transformation import data_transformation
from netsuite_data_transformation import load_mappings,transform_data_in_netsuite_format, send_post_failure_email_via_logic_app
from netsuite_posting import get_jwt_token,generate_access_token,post_jounral_entry_netsuite,post_updated_jounral_entry_netsuite,check_journal_entry_exists,delete_journal_entry, check_journal_entry_by_tranid
from azure_database_posting import compair_data,post_data_in_database,create_db_connection,fetch_all_transaction_ids,fetch_netsuite_location_for_transaction_ids,delete_data
from dotenv import load_dotenv,dotenv_values
import json
from logger_config import logger
import csv
import requests
from locks import netsuite_lock


load_dotenv()
config = dotenv_values(".env")


username = config.get('WEB_CONNECTOR_USERNAME')
password = config.get('WEB_CONNECTOR_PASSWORD')


database_name = config.get('AZURE_DATABASE')
database_server = config.get('AZURE_SERVER')
database_user = config.get('AZURE_USER')
database_password = config.get('AZURE_PASSWORD')


get_jwt_base_url = config.get('AZURE_NREST_BASE_URL')
netsuite_base_url = config.get('NETSITE_BASE_URL')




def master_function(responce_json):
    sliced_jounral_enteries_raw = data_transformation(responce_json)
    current_qb_transaction_ids = set()
    # logger.info(sliced_jounral_enteries_raw)
    i = 0
    mapping_of_net, locations_mapping = load_mappings()
    all_dates = set()
    for enteries in sliced_jounral_enteries_raw:
        i+=1
        try:
            logger.info(sliced_jounral_enteries_raw[enteries])
            result = transform_data_in_netsuite_format(sliced_jounral_enteries_raw[enteries],mapping_of_net,locations_mapping)
            if result is None:
                logger.error(f"Skipping entry {enteries} — transformation returned None")
                continue
            data_to_post, transacton_id, date = result  # safe to unpack now
            if data_to_post is None or transacton_id is None:
                logger.error(f"Skipping {enteries} — missing data_to_post or transacton_id")
                continue
            logger.info(f"Processing transaction ID: {transacton_id} | Date: {date}")

            current_qb_transaction_ids.add(transacton_id)
            all_dates.add(date)
            if data_to_post is None or transacton_id is None:
                logger.error(f"transform_data_in_netsuite_format returned None")
                raise ValueError("transform_data_in_netsuite_format returned None")
           
            connection = create_db_connection(database_server, database_user, database_password, database_name)
            comparison_result = compair_data(data_to_post,transacton_id,connection)
           
            logger.info(comparison_result)
            with netsuite_lock:
                if comparison_result and comparison_result.get('query_status')== False:
                    logger.info(f"Transaction {transacton_id} not in DB — checking NetSuite...")
                    generated_jwt_signed_token = get_jwt_token(get_jwt_base_url)
                    generated_access_token = generate_access_token(netsuite_base_url,generated_jwt_signed_token)
                    # NEW: Check NetSuite first before posting
                    existing_netsuite_location = check_journal_entry_by_tranid(
                        netsuite_base_url, 
                        generated_access_token, 
                        transacton_id
                    )
                    if existing_netsuite_location:
                        # EXISTS in NetSuite but NOT in DB
                        # Update in Netsuite
                        post_updated_jounral_entry_netsuite(existing_netsuite_location,generated_access_token, data_to_post)
                        # Post to database
                        logger.info(f"Transaction {transacton_id} found in NetSuite but missing from DB — adding to DB only")
                        data_base_posting_status = post_data_in_database(
                            data_to_post, 
                            transacton_id, 
                            existing_netsuite_location, 
                            connection
                        )
                        if data_base_posting_status:
                            logger.info(f"Transaction {transacton_id} added to DB successfully")
                        else:
                            logger.error(f"Failed to add {transacton_id} to DB")
                    else:

                        logger.info("Posting Record to Database")
                        logger.info(data_to_post)
                        response = post_jounral_entry_netsuite(netsuite_base_url,generated_access_token,data_to_post)
                        if response is None or response.status_code not in (200, 201, 204):
                            error_msg = response.text if response else "No response from NetSuite. Failed to insert entry into nesuite"
                            logger.error(f"NS post failed for {transacton_id}: {error_msg} {response}")  # ← ADD THIS
                            send_post_failure_email_via_logic_app(transacton_id, error_msg)
                            continue
                        entery_location = response.headers.get('Location')
                        logger.info(entery_location)
                        data_base_posting_status = post_data_in_database(data_to_post,transacton_id,entery_location,connection)
                        logger.info(data_base_posting_status)
                        logger.info("Posted Record Successfully")


                if comparison_result and comparison_result.get('query_status')== True:
                    generated_jwt_signed_token = get_jwt_token(get_jwt_base_url)
                    generated_access_token = generate_access_token(netsuite_base_url,generated_jwt_signed_token)
                    logger.info("Updating Record to Database")
                    update_entery = post_updated_jounral_entry_netsuite(comparison_result.get('update_url'),generated_access_token,data_to_post)
                    if update_entery is None or update_entery.status_code not in (200, 201, 204):

                        error_msg = update_entery.text if update_entery else "No response from NetSuite Failed to update entry in netsuite."
                        logger.error(f"NS update failed for {transacton_id}: {error_msg} {update_entery}")  # ← ADD THIS
                        send_post_failure_email_via_logic_app(transacton_id, error_msg)
                        continue
                    logger.info(update_entery)
                
                # if comparison_result and comparison_result.get('query_status')== 'No change':
                #     logger.info("No Change Reported by DB Still updating the netsuite")
                #     generated_jwt_signed_token = get_jwt_token(get_jwt_base_url)
                #     generated_access_token = generate_access_token(netsuite_base_url,generated_jwt_signed_token)
                #      # NEW: Check NetSuite first before posting
                #     existing_netsuite_location = check_journal_entry_by_tranid(
                #         netsuite_base_url, 
                #         generated_access_token, 
                #         transacton_id
                #     )

                #     logger.info("Updating Record to Netsuite")
                #     update_entery = post_updated_jounral_entry_netsuite(existing_netsuite_location,generated_access_token,data_to_post)
                #     if update_entery is None or update_entery.status_code not in (200, 201, 204):
                #         error_msg = update_entery.text if update_entery else "No response from NetSuite Failed to update entry in netsuite."
                #         logger.error(f"NS update failed for {transacton_id}: {error_msg} {update_entery}")  # ← ADD THIS
                #         send_post_failure_email_via_logic_app(transacton_id, error_msg)
                #         continue
                #     logger.info(update_entery)
                
        except Exception as e:
            logger.error(f"Error processing entry {enteries}: {e}")
            continue
    print(all_dates)
    connection_again = create_db_connection(database_server, database_user, database_password, database_name)
    db_records_set = set()
    for date in all_dates:
        db_records = fetch_all_transaction_ids(connection_again, date)
        db_records_set.update(map(str, db_records))
 
    deleted_ids = db_records_set - current_qb_transaction_ids


   
    if deleted_ids:
        print("Deleted ids")
        print(deleted_ids)
        logger.info(f"Deleted transaction IDs from QuickBooks but still in NetSuite: {deleted_ids}")
       
        # Get location URLs from DB
        deleted_locations = fetch_netsuite_location_for_transaction_ids(connection_again, deleted_ids)
       
        # # Generate token for API call
        generated_jwt_signed_token = get_jwt_token(get_jwt_base_url)
        generated_access_token = generate_access_token(netsuite_base_url, generated_jwt_signed_token)


        for tid, location in deleted_locations.items():
            response = check_journal_entry_exists(location, generated_access_token)
            if response and response.status_code == 200:
                logger.info(f"Transaction {tid} exists — attempting delete...")
                delete_response = delete_journal_entry(location, generated_access_token)
                if delete_response is None or delete_response.status_code not in (200, 201, 204):
                        error_msg = delete_response.text if delete_response else "No response from NetSuite Failed to delete entry in netsuite."
                        logger.error(f"Failed to delete from NS error: {error} body: {delete_response}")
                        send_post_failure_email_via_logic_app(transacton_id, error_msg)
                        continue
                
                if delete_response and delete_response.status_code == 204:
                    logger.info(f"Successfully deleted transaction {tid} from NetSuite")
                    delete_from_db= delete_data(tid,connection_again)
                    if delete_from_db:
                        logger.info(f"Successfully deleted transaction {tid} from DB")
                    else:
                        logger.info(f"Error deleting {tid} from db")
                else:
                    logger.warning(f"Failed to delete {tid}: {delete_response.status_code} - {delete_response.text}")
           


    print("Done")
    logger.info(f"Processed {i} entries")