# find_missing_5.py
from azure_database_posting import create_db_connection, post_data_in_database
from netsuite_posting import get_jwt_token, generate_access_token
from dotenv import dotenv_values, load_dotenv
import ast

load_dotenv()
config = dotenv_values(".env")

missing_ids = ['1125348', '1127469', '1127518', '1127544', '1127564']

log_files = [
    'prod_live_25.log'
]
for log_file in log_files:
    try:
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        
        for i, line in enumerate(lines):
            for txn_id in missing_ids:
                if txn_id in line:
                    print(f"\nFile: {log_file} | Line {i}:")
                    print(f"  {line.strip()}")
    except FileNotFoundError:
        pass