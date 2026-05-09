import string
import pandas as pd
from logger_config import logger


def get_columns(response_json):
    try:
        # Map colID to ColType, e.g., "1": "Date"
        return {
            col.get('@colID'): col.get('ColType')
            for col in response_json.get('response', {})
                                  .get('QBXML', {})
                                  .get('QBXMLMsgsRs', {})
                                  .get('GeneralDetailReportQueryRs', {})
                                  .get('ReportRet', {})
                                  .get('ColDesc', [])
        }
    except Exception as e:
        logger.error(f"ERROR WHILE GETTING COLUMN NAMES: {e}")
        return {}

def normalizing_responce_data(response_data):
    try:
        if not response_data:
            return []

        string_digits = list(string.digits)[1:]  # ['1', '2', ..., '9']
        for data in response_data:
            col_data = data.get('ColData', [])
            if isinstance(col_data, dict):
                col_data = [col_data]
                data['ColData'] = col_data

            columns_id = [col_id.get('@colID') for col_id in col_data if isinstance(col_id, dict)]
            missing_columns_id = [value for value in string_digits if value not in columns_id]

            for column_id in missing_columns_id:
                data['ColData'].insert(int(column_id)-1, {'@colID': column_id, '@value': ''})

        return response_data
    except Exception as e:
        logger.error(f"ERROR WHILE NORMALIZING RESPONSE: {e}")
        return []


def flatten_data(response_data):
    try:
        flattened_data = []
        for row in response_data:
            if not row or 'ColData' not in row:
                continue

            flattened_row = {'@rowNumber': row.get('@rowNumber', '')}
            for col_data in row['ColData']:
                col_id = col_data.get('@colID')
                value = col_data.get('@value')
                if col_id:
                    flattened_row[col_id] = value
            flattened_data.append(flattened_row)
        return flattened_data
    except Exception as e:
        logger.error(f"ERROR WHILE FLATTENING DATA: {e}")
        return []


def transform_flatten_data(flattened_data):
    try:
        return [
            {k: v for k, v in row.items() if k != '@rowNumber'}
            for row in flattened_data
        ]
    except Exception as e:
        logger.error(f"ERROR WHILE TRANSFORMING FLATTENED DATA: {e}")
        return []


def add_columns_values(flattened_data, col_id_to_name):
    try:
        journal = []
        for row in flattened_data:
            journal_entry = {}
            for col_id, value in row.items():
                col_name = col_id_to_name.get(col_id)
                if col_name:
                    journal_entry[col_name] = value
            journal.append(journal_entry)
        return journal
    except Exception as e:
        logger.error(f"ERROR WHILE ADDING COLUMN VALUES: {e}")

def slice_jounra_enteries(transformed_cleaned_data):
    try:
        sliced_entries = {}
        entry_number = 1
        previous_index = 0
        current_index = 0
        print(len(transformed_cleaned_data))
        for data in transformed_cleaned_data:
            current_index = transformed_cleaned_data.index(data)
            txn_number = data.get('TxnNumber')
            txn_type = data.get('TxnType')

            if txn_number and txn_type:
                sliced_entries[f"journal_entry_raw_{entry_number}"] = transformed_cleaned_data[previous_index:current_index]
                previous_index = current_index
                entry_number += 1

        if previous_index != current_index:
            sliced_entries[f"journal_entry_raw_{entry_number}"] = transformed_cleaned_data[previous_index:]

        return sliced_entries
    except Exception as e:
        logger.error(f"ERROR WHILE SLICING JOURNAL ENTRIES: {e}")
        return {}


def generate_csv(transformed_cleaned_data):
    try:
        if not transformed_cleaned_data:
            return

        # Convert to DataFrame
        df = pd.DataFrame(transformed_cleaned_data)

        # Determine current month and year (based on today's date)
        now = datetime.now()
        month_str = now.strftime("%Y_%m")  # e.g., 2025_10

        # Construct filename like journal_entries_2025_10.csv
        filename = f"journal_entries_{month_str}.csv"
        file_path = os.path.join(os.path.dirname(__file__), filename)

        # If file exists, append without headers; else create with headers
        if os.path.exists(file_path):
            df.to_csv(file_path, mode='a', header=False, index=False)
        else:
            df.to_csv(file_path, index=False)

        print(f"Data appended to {file_path}")

    except Exception as e:
        logger.error(f"ERROR WHILE GENERATING CSV: {e}")


def data_transformation(response_json):
    try:
        logger.info("Beginning transformation...")
        columns = get_columns(response_json)
        response_data = (
            response_json.get('response', {})
                         .get('QBXML', {})
                         .get('QBXMLMsgsRs', {})
                         .get('GeneralDetailReportQueryRs', {})
                         .get('ReportRet', {})
                         .get('ReportData', {})
                         .get('DataRow', [])
        )

        # Remove rows that are completely empty (optional but helps clean)
        response_data = [
            row for row in response_data
            if any(col.get('@value') for col in row.get('ColData', []))
        ]
        normalized_data = normalizing_responce_data(response_data)
        flattened_data = flatten_data(normalized_data)
        transformed_flattened_data = transform_flatten_data(flattened_data)
        transformed_cleaned_data = add_columns_values(transformed_flattened_data, columns)
        # generate_csv(transformed_cleaned_data)
        if not transformed_cleaned_data:
            logger.error("No valid rows after transformation.")
            return {}

        sliced_entries = slice_jounra_enteries(transformed_cleaned_data)
        return sliced_entries

    except Exception as e:
        logger.error(f"ERROR WHILE TRANSFORMING DATA: {e}")
        return {}
