from netsuite_posting import get_jwt_token, generate_access_token, check_journal_entry_by_tranid
from dotenv import load_dotenv, dotenv_values
from logger_config import logger

load_dotenv()
config = dotenv_values(".env")

get_jwt_base_url = config.get('AZURE_NREST_BASE_URL')
netsuite_base_url = config.get('NETSITE_BASE_URL')

TEST_TRAN_ID = "1102082"

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info(f"AZURE_NREST_BASE_URL: {get_jwt_base_url}")
    logger.info(f"NETSITE_BASE_URL:     {netsuite_base_url}")

    logger.info("=" * 60)
    logger.info("STEP 1: Generating JWT token...")
    jwt_token = get_jwt_token(get_jwt_base_url)
    logger.info(f"JWT Token (first 50 chars): {str(jwt_token)[:50]}...")

    logger.info("=" * 60)
    logger.info("STEP 2: Generating Access token...")
    access_token = generate_access_token(netsuite_base_url, jwt_token)
    logger.info(f"Access Token (first 50 chars): {str(access_token)[:50]}...")

    logger.info("=" * 60)
    logger.info(f"STEP 3: Searching NetSuite for tranId={TEST_TRAN_ID}...")
    result = check_journal_entry_by_tranid(netsuite_base_url, access_token, TEST_TRAN_ID)

    logger.info("=" * 60)
    logger.info(f"FINAL RESULT: {result}")