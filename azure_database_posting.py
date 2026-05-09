import json
import json_fingerprint
import pymssql
from json_fingerprint import hash_functions
from datetime import datetime
from logger_config import setup_logger


logger = setup_logger('app.log')


def generate_and_compair_hash_of_journal_entery(journal_entery,op_type,hashed_value:str = None):
    try:
        loaded_entery = json.loads(journal_entery)
        jsonify_loaded_entery = json.dumps(loaded_entery.get('line'))
       
        if op_type == 'hash':
            hashed_entery = json_fingerprint.create(input=jsonify_loaded_entery, hash_function=hash_functions.SHA256, version=1)
            return hashed_entery,loaded_entery.get('tranDate')
       
        elif op_type == 'compare':
            comparison_result = json_fingerprint.match(input=jsonify_loaded_entery, target_fingerprint=hashed_value)
            return comparison_result
    except Exception as e:
        logger.error(f"ERROR WHILE {op_type} DATA : {e}")


   
def get_sql_query(query_type:str):
   
    queries = {
        "fetch_data" : """
    SELECT * FROM [dbo].[Quickbooks_Netsuite_Sync_prestige] WHERE transaction_id = %s
    """,
        "add_value":"""SET IDENTITY_INSERT [dbo].[Quickbooks_Netsuite_Sync_prestige] ON;
    INSERT INTO [dbo].[Quickbooks_Netsuite_Sync_prestige] (transaction_date, transaction_id, hashed_data,netsuite_location) VALUES (%s, %s, %s, %s)
    """,
        "update_value" : """
    UPDATE [dbo].[Quickbooks_Netsuite_Sync_prestige] SET hashed_data = %s WHERE transaction_id = %s
    """,
    "fetch_all_transaction_ids" : """
    SELECT transaction_id FROM [dbo].[Quickbooks_Netsuite_Sync_prestige] WHERE transaction_date = %s
    """,
        "update_value" : """
    UPDATE [dbo].[Quickbooks_Netsuite_Sync_prestige] SET hashed_data = %s WHERE transaction_id = %s
    """,
    "delete_value": """
            DELETE FROM [dbo].[Quickbooks_Netsuite_Sync_prestige] WHERE transaction_id = %s
        """

    }


    return queries.get(query_type)


def create_db_connection(database_server, database_user, database_password, database_name):
    try:
        conn = pymssql.connect(database_server, database_user, database_password, database_name)
        return conn
    except Exception as e:
        logger.error(f"DB Connection Failed: {e}")


def store_data(cursor_object):


    data = []


    for row in cursor_object:
        data.append(row)
   
    return data


def query_data(query_type:str,data:tuple,connection,close_connection:bool = False):


    try:
       
        cursorr = connection.cursor(as_dict=True)
        query_to_execute = get_sql_query(query_type)
        cursorr.execute(query_to_execute,data)
       
        if query_type == 'fetch_data' :
            data = store_data(cursorr)
            return data
        elif query_type == 'fetch_all_transaction_ids':
            data = store_data(cursorr)
            return [record['transaction_id'] for record in data]
       
        return True


    except Exception as e:
        print(f"{e}")
   
    finally:
        connection.commit()
        cursorr.close()


        if close_connection:
            connection.close()




def compair_data(journal_entery,transaction_id,connection):
   
    try:
   
        data_base_data = query_data("fetch_data",(int(transaction_id)),connection)
       
        if data_base_data:
           
            hashed_value_in_db = data_base_data[0].get('hashed_data')
            comparison_result =  generate_and_compair_hash_of_journal_entery(journal_entery,'compare',hashed_value_in_db)
           
            if not comparison_result:
                           
                new_hashed_value, _ = generate_and_compair_hash_of_journal_entery(journal_entery,'hash')
                print(f' VALUE CHANGED \n PREVIOUS HASH: {hashed_value_in_db} \n NEW HASH VALUE:{new_hashed_value}')


                query_status = query_data("update_value",(new_hashed_value,int(transaction_id)),connection,close_connection=True)
                return {'query_status':query_status,'update_url':data_base_data[0].get('netsuite_location')}
       
        if not data_base_data:
            return {'query_status': False}
        return {'query_status': "No change"}
   
    except Exception as e :
        logger.error(f"ERROR WHILE COMPAIRING DATA: {e}")


def post_data_in_database(journal_entery,transaction_id,url,connection):


    try:
        logger.info(f"Posting Data to DB {journal_entery}")


        generated_hash_value,transactin_date = generate_and_compair_hash_of_journal_entery(journal_entery,'hash')
        query_data("add_value",(datetime.strptime(transactin_date,"%Y-%m-%d"),int(transaction_id),generated_hash_value,url),connection,close_connection=True)
        return True
   
    except Exception as e:
        logger.error(f"ERROR WHILE POSTING DATA IN DATABASE: {e}")

def fetch_all_transaction_ids(connection, date):
    try:
        transaction_ids_data = query_data("fetch_all_transaction_ids", (date), connection)
        logger.info(transaction_ids_data)
       
        return transaction_ids_data
    except Exception as e:
        logger.error(f"Error fetching transaction IDs: {e}")
        return []



def fetch_netsuite_location_for_transaction_ids(connection, transaction_ids):
    cursor = connection.cursor()
    format_ids = ','.join(f"'{tid}'" for tid in transaction_ids)
    query = f"""
        SELECT transaction_id, netsuite_location
        FROM Quickbooks_Netsuite_Sync_prestige
        WHERE transaction_id IN ({format_ids})
    """
    cursor.execute(query)
    rows = cursor.fetchall()
    return {row[0]: row[1] for row in rows}

def fetch_integration_status(connection):
    cursor = connection.cursor()
    query = f"""
        SELECT is_up, last_success_at,last_failure_at,last_failure_message,down_email_sent
        FROM integration_status_prestige
        WHERE id = 1
    """
    cursor.execute(query)
    rows = cursor.fetchall()
    return {row[0]: row[1] for row in rows}

def update_integration_status(connection,is_up,last_success_at,last_failure_at,last_failure_message,down_email_sent):
    try:
        cursor= connection.cursor()
        query= """
        UPDATE integration_status_prestige
        SET
            is_up = %s,
            last_success_at= %s,
            last_failure_at = %s,
            last_failure_message= %s,
            down_email_sent= %s
        WHERE id=1
        """
        connection.commit()
        return True
    except Exception as e:
        logger.error(f"ERROR while updating integration status: {e}")
        return False
    finally:
        cursor.close()

def delete_data(transaction_id: str, connection, close_connection: bool = False):
    try:
        cursor = connection.cursor()
        query_to_execute = get_sql_query("delete_value")
        cursor.execute(query_to_execute, (int(transaction_id),))
        connection.commit()
        return True
    except Exception as e:
        logger.error(f"ERROR WHILE DELETING DATA: {e}")
        return False
    finally:
        cursor.close()
        if close_connection:
            connection.close()
