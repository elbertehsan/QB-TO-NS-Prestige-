import requests
from logger_config import logger
from dotenv import load_dotenv,dotenv_values


load_dotenv()

config = dotenv_values(".env")
JWT_CODE = config.get('JWT_CODE')


def get_jwt_token(base_url):
    try:
        url =f'{base_url}/GetJWT?code={JWT_CODE}'
        response = requests.get(url)
        return response.text
    except Exception as e:
        logger.error(f"ERROR WHILE GETTING JWT TOKEN: {e}")


def generate_access_token(base_url,generated_jwt_signed_token):


    try:
        url = f'{base_url}/auth/oauth2/v1/token'
        body = {
            'grant_type': 'client_credentials',
            'client_assertion_type': 'urn:ietf:params:oauth:client-assertion-type:jwt-bearer',
            'client_assertion': generated_jwt_signed_token
        }
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded'
        }


        response = requests.post(url, data=body, headers=headers)
        return response.json()


    except Exception as e:
        logger.error(f"ERROR WHILE GENERATING ACCESS TOKEN: {e}")


def post_jounral_entry_netsuite(base_url,access_token,body):
   
    try:
        url = f'{base_url}/record/v1/journalEntry/'
        headers = {
            'Authorization': f"{access_token.get('token_type')} {access_token.get('access_token')}",
        }
        response = requests.post(url, data=body, headers=headers)
        return(response)
    except Exception as e:
        logger.error(f"ERROR WHILE POSTING JOURNAL ENTERIES: {e} \n JOURNAL ENTRY: {body}")


def post_updated_jounral_entry_netsuite(base_url,access_token,body):


    try:
        url = f'{base_url}?replaceSelectedFields=true&replace=Line'
        headers = {
            'Authorization': f"{access_token.get('token_type')} {access_token.get('access_token')}",
        }
        response = requests.patch(url, data=body, headers=headers)
        logger.info("POSTING UPDATED JOURNAL ENTRIES")
        return response
    except Exception as e:
        logger.info(f"ERROR WHILE UPDATING JOURNAL ENTRY: {e} \n JOURNAL ENTRY: {body}")

def check_journal_entry_exists(location_url, access_token):
    try:
        headers = {
            'Authorization': f"{access_token.get('token_type')} {access_token.get('access_token')}",
            'Content-Type': 'application/json'
        }
        response = requests.get(location_url, headers=headers)
        return response
    except Exception as e:
        logger.error(f"ERROR WHILE CHECKING JOURNAL ENTRY EXISTENCE: {e}")
        return None

def delete_journal_entry(location_url, access_token):
    try:
        headers = {
            'Authorization': f"{access_token.get('token_type')} {access_token.get('access_token')}",
        }
        response = requests.delete(location_url, headers=headers)
        return response
    except Exception as e:
        logger.error(f"ERROR WHILE DELETING JOURNAL ENTRY: {e}")
        return None


def check_journal_entry_by_tranid(netsuite_base_url, access_token, tran_id, timeout=(10, 60)):
    try:
        # Strip trailing /services/rest if already in base URL
        if not tran_id:
            logger.error("check_journal_entry_by_tranid called with empty tranId — skipping")
            return None
        base = netsuite_base_url.rstrip('/')
        if base.endswith('/services/rest'):
            base = base[:-len('/services/rest')]

        headers = {
            'Authorization': f"{access_token.get('token_type')} {access_token.get('access_token')}",
            'Content-Type': 'application/json',
            'Prefer': 'transient'
        }

        search_url = f"{base}/services/rest/query/v1/suiteql"
        query = {
            "q": f"""SELECT t.id, t.tranId, t.subsidiary
        FROM transaction t
        WHERE t.tranId = '{tran_id}'
        AND t.recordtype = 'journalentry'
        AND t.subsidiary = 15"""
        }

        logger.info(f"Search URL: {search_url}")

        response = requests.post(
            search_url,
            headers=headers,
            json=query,
            timeout=timeout
        )

        if response.status_code == 200:
            data = response.json()
            items = data.get('items', [])
            if items:
                netsuite_id = items[0].get('id')
                location = f"{netsuite_base_url.rstrip('/')}/record/v1/journalentry/{netsuite_id}"
                logger.info(f"Found in NetSuite: tranId={tran_id} → {location}")
                return location
            else:
                logger.info(f"Not found in NetSuite: tranId={tran_id}")
                return None
        else:
            logger.error(f"NetSuite search failed: {response.status_code} - {response.text}")
            return None

    except requests.exceptions.Timeout:
        logger.error(f"Timeout searching NetSuite for tranId={tran_id}")
        return None
    except Exception as e:
        logger.error(f"Error searching NetSuite for tranId={tran_id}: {e}")
        return None


