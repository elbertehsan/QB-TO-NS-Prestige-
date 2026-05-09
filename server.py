import logging
import threading
import json
from datetime import datetime, timedelta
import xmltodict
from spyne import Application, rpc, ServiceBase, Unicode, Iterable, Integer
from spyne.protocol.soap import Soap11
from spyne.server.wsgi import WsgiApplication
from wsgiref.simple_server import make_server

from master_file import master_function
from QBXML_requests import genral_ledger_enteries_xml_query
from logger_config import logger

def merge_general_detail_responses(parts):
    def extract_rows(parsed):
        return parsed.get('QBXML', {}).get('QBXMLMsgsRs', {}).get('GeneralDetailReportQueryRs', {}).get('ReportRet', {}).get('ReportData', {}).get('DataRow', [])

    def extract_coldesc(parsed):
        return parsed.get('QBXML', {}).get('QBXMLMsgsRs', {}).get('GeneralDetailReportQueryRs', {}).get('ReportRet', {}).get('ColDesc', [])

    def get_max_col_id(col_desc):
        col_ids = [int(col.get('@colID', 0)) for col in col_desc if col.get('@colID')]
        return max(col_ids) if col_ids else 0

    def shift_col_ids(col_desc, data_rows, offset):
        shifted_col_desc = []
        for col in col_desc:
            col_copy = col.copy()
            if '@colID' in col_copy:
                col_copy['@colID'] = str(int(col_copy['@colID']) + offset)
            shifted_col_desc.append(col_copy)

        shifted_rows = []
        for row in data_rows:
            if not isinstance(row, dict):
                continue
            col_data = row.get('ColData', [])
            if isinstance(col_data, dict):
                col_data = [col_data]
            elif not isinstance(col_data, list):
                col_data = []

            new_col_data = []
            for col in col_data:
                col_copy = col.copy()
                if '@colID' in col_copy:
                    col_copy['@colID'] = str(int(col_copy['@colID']) + offset)
                new_col_data.append(col_copy)

            shifted_rows.append({'@rowNumber': row.get('@rowNumber'), 'ColData': new_col_data})

        return shifted_col_desc, shifted_rows

    merged_col_desc = []
    merged_rows_map = {}
    offset = 0

    for part in parts:
        col_desc = extract_coldesc(part)
        rows = extract_rows(part)

        shifted_col_desc, shifted_rows = shift_col_ids(col_desc, rows, offset)
        merged_col_desc.extend(shifted_col_desc)

        for row in shifted_rows:
            row_number = row.get('@rowNumber')
            if not row_number:
                continue
            col_data = row.get('ColData', [])
            merged_rows_map.setdefault(row_number, []).extend(col_data)

        offset = get_max_col_id(merged_col_desc) + 1

    merged_rows = []
    for row_number, col_data_list in sorted(merged_rows_map.items(), key=lambda x: int(x[0])):
        col_data_list.sort(key=lambda col: int(col.get('@colID', 0)))
        merged_rows.append({'@rowNumber': row_number, 'ColData': col_data_list})

    return {
        'response': {
            'QBXML': {
                'QBXMLMsgsRs': {
                    'GeneralDetailReportQueryRs': {
                        'ReportRet': {
                            'ColDesc': merged_col_desc,
                            'ReportData': {'DataRow': merged_rows}
                        }
                    }
                }
            }
        }
    }

class QuickBooksService(ServiceBase):
    unprocessed_date_chunks = set()
    processed_date_chunks = set()
    in_progress_date_chunks = set()
    failed_date_chunks = set()
    last_chunk_generation_date = None
    chunk_query_phase = {}
    response_cache = {}
    chunk_for_ticket = {}

    @classmethod
    def get_date_range(cls, today):
        if today.day < 11:
            first_day_current_month = today.replace(day=1)
            last_day_previous_month = first_day_current_month - timedelta(days=1)
            from_date = last_day_previous_month.replace(day=1)
        else:
            from_date = today.replace(day=1)

        to_date = today
        while from_date <= to_date:
            yield from_date, from_date
            from_date += timedelta(days=1)

    @classmethod
    def generate_and_store_chunks(cls, today):
        logger.info("Generating chunks...")
        cls.processed_date_chunks.clear()
        cls.last_chunk_generation_date = today
        chunks = list(cls.get_date_range(today))
        for chunk in chunks:
            cls.unprocessed_date_chunks.add(chunk)
        logger.info(f"Chunks generated for {today}: {cls.unprocessed_date_chunks}")

    @rpc(Unicode, Unicode, _returns=Iterable(Unicode))
    def authenticate(ctx, strUserName, strPassword):
        if strUserName == 'elbertehsan' and strPassword == 'Alpha103@':
            return ["85B41BEE-5CD9-427a-A61B-83964F1EB426", ""]
        else:
            return ["", "nvu"]

    @rpc(Unicode, _returns=Unicode)
    def clientVersion(ctx, strVersion):
        return ""

    @rpc(Unicode, Unicode, Unicode, _returns=Unicode)
    def connectionError(ctx, ticket, hresult, message):
        return "done"

    @rpc(Unicode, Unicode, Unicode, Unicode, Integer, Integer, _returns=Unicode)
    def sendRequestXML(ctx, ticket, strHCPResponse, strCompanyFileName, qbXMLCountry, qbXMLMajorVers, qbXMLMinorVers):
        try:
            today = datetime.now().date()
            if QuickBooksService.last_chunk_generation_date != today:
                QuickBooksService.generate_and_store_chunks(today)

            sorted_chunks = sorted(QuickBooksService.unprocessed_date_chunks - QuickBooksService.processed_date_chunks - QuickBooksService.in_progress_date_chunks)
            unprocessed_chunk = sorted_chunks[0] if sorted_chunks else None

            if not unprocessed_chunk:
                return ""

            from_chunk, to_chunk = unprocessed_chunk
            QuickBooksService.chunk_for_ticket[ticket] = unprocessed_chunk
            phase = QuickBooksService.chunk_query_phase.get(unprocessed_chunk, 1)
            print("here")
            print(phase)
            if phase == 1:
                QuickBooksService.chunk_query_phase[unprocessed_chunk] = 2
                return genral_ledger_enteries_xml_query("4a", "Journal", from_chunk, to_chunk, ["TxnNumber","Account"])
            elif phase == 2:
                QuickBooksService.chunk_query_phase[unprocessed_chunk] = 3
                return genral_ledger_enteries_xml_query("4b", "Journal", from_chunk, to_chunk, ["Memo"])
            elif phase == 3:
                QuickBooksService.chunk_query_phase[unprocessed_chunk] = 4
                return genral_ledger_enteries_xml_query("4c", "Journal", from_chunk, to_chunk, ["Name"])
            elif phase == 4:
                QuickBooksService.chunk_query_phase[unprocessed_chunk] = 5
                return genral_ledger_enteries_xml_query("4d", "Journal", from_chunk, to_chunk, ["RefNumber", "TxnNumber", "Debit", "Credit"])
            else:
                QuickBooksService.chunk_query_phase.pop(unprocessed_chunk, None)
                return genral_ledger_enteries_xml_query("4e", "Journal", from_chunk, to_chunk, ["Class","Date", "TxnType","TxnNumber"])

        except Exception as e:
            logger.error(f"Error in sendRequestXML: {e}")
            return ""

    @rpc(Unicode, Unicode, Unicode, Unicode, _returns=Integer)
    def receiveResponseXML(ctx, ticket, response, hresult, message):
        try:
            parsed = xmltodict.parse(response)
            chunk = QuickBooksService.chunk_for_ticket.get(ticket)
            if not chunk:
                return 100

            current_phase = QuickBooksService.chunk_query_phase.get(chunk, 1)
            QuickBooksService.response_cache.setdefault(ticket, {})[f'part{current_phase}'] = parsed

            if current_phase < 5:
                print(current_phase)
                QuickBooksService.chunk_query_phase[chunk] = current_phase + 1
                return 1

            parts = [QuickBooksService.response_cache[ticket].get(f'part{i}') for i in range(1, 6)]
            if all(parts):
                cached_parts = list(parts)
                cached_chunk = chunk  # keep reference
                
                #final_merged = merge_general_detail_responses(parts)
                #master_function(final_merged)

                QuickBooksService.response_cache.pop(ticket, None)
                QuickBooksService.chunk_for_ticket.pop(ticket, None)
                QuickBooksService.chunk_query_phase.pop(chunk, None)
                QuickBooksService.unprocessed_date_chunks.remove(chunk)
                QuickBooksService.in_progress_date_chunks.add(chunk)  # track it

                def process_in_background(parts_to_process, chunk_ref):
                    try:
                        final_merged = merge_general_detail_responses(parts_to_process)
                        master_function(final_merged)
                      
                        # Only mark as processed if master_function succeeded
                        QuickBooksService.in_progress_date_chunks.discard(chunk_ref)
                        QuickBooksService.processed_date_chunks.add(chunk_ref)
                        logger.info(f"Chunk {chunk_ref} successfully processed")
                    except Exception as e:
                        logger.error(f"Background processing error for chunk {chunk_ref}: {e}")
                        logger.info(f"In progress chunk details: {QuickBooksService.in_progress_date_chunks}")
                        # Put it back so it gets retried
                        QuickBooksService.in_progress_date_chunks.discard(chunk_ref)
                        QuickBooksService.failed_date_chunks.add(chunk_ref)
                        QuickBooksService.unprocessed_date_chunks.add(chunk_ref)

                threading.Thread(
                target=process_in_background,
                args=(cached_parts, cached_chunk),
                name=f"chunk-{cached_chunk[0]}",
                daemon=True
                ).start()


            return 100
        except Exception as e:
            logger.error(f"Error in receiveResponseXML: {e}")
            return 500

    @rpc(Unicode, _returns=Iterable(Unicode))
    def closeConnection(ctx, ticket):
        return "OK"

    @rpc(Unicode, _returns=Iterable(Unicode))
    def getLastError(ctx, ticket):
        return ["", ""]

soap_app = Application([QuickBooksService],
                       tns='http://developer.intuit.com/',
                       in_protocol=Soap11(validator='lxml'),
                       out_protocol=Soap11())

wsgi_app = WsgiApplication(soap_app)
server = make_server('127.0.0.1', 8000, wsgi_app)
logger.info("Listening on port 8000...")
try:
    server.serve_forever()
except KeyboardInterrupt:
    logger.info("Stopping server...")
    server.shutdown()


# import logging
# import threading
# import json
# from datetime import datetime, timedelta
# import xmltodict
# from spyne import Application, rpc, ServiceBase, Unicode, Iterable, Integer
# from spyne.protocol.soap import Soap11
# from spyne.server.wsgi import WsgiApplication
# from wsgiref.simple_server import make_server, WSGIRequestHandler
# from urllib.parse import parse_qs

# # ── Production imports (commented out while in sync-check mode) ───────────────
# from master_file import master_function

# from QBXML_requests import genral_ledger_enteries_xml_query
# from logger_config import logger


# # ═════════════════════════════════════════════════════════════════════════════
# #  MODE FLAG  —  flip this to switch between modes
# #
# #   SYNC_CHECK_MODE = True   → collect QB txn IDs only, do NOT post to NS/DB
# #   SYNC_CHECK_MODE = False  → normal production (uncomment master_function too)
# # ═════════════════════════════════════════════════════════════════════════════
# SYNC_CHECK_MODE = True


# def merge_general_detail_responses(parts):
#     def extract_rows(parsed):
#         return parsed.get('QBXML', {}).get('QBXMLMsgsRs', {}).get(
#             'GeneralDetailReportQueryRs', {}).get('ReportRet', {}).get(
#             'ReportData', {}).get('DataRow', [])

#     def extract_coldesc(parsed):
#         return parsed.get('QBXML', {}).get('QBXMLMsgsRs', {}).get(
#             'GeneralDetailReportQueryRs', {}).get('ReportRet', {}).get('ColDesc', [])

#     def get_max_col_id(col_desc):
#         col_ids = [int(col.get('@colID', 0)) for col in col_desc if col.get('@colID')]
#         return max(col_ids) if col_ids else 0

#     def shift_col_ids(col_desc, data_rows, offset):
#         shifted_col_desc = []
#         for col in col_desc:
#             col_copy = col.copy()
#             if '@colID' in col_copy:
#                 col_copy['@colID'] = str(int(col_copy['@colID']) + offset)
#             shifted_col_desc.append(col_copy)

#         shifted_rows = []
#         for row in data_rows:
#             if not isinstance(row, dict):
#                 continue
#             col_data = row.get('ColData', [])
#             if isinstance(col_data, dict):
#                 col_data = [col_data]
#             elif not isinstance(col_data, list):
#                 col_data = []

#             new_col_data = []
#             for col in col_data:
#                 col_copy = col.copy()
#                 if '@colID' in col_copy:
#                     col_copy['@colID'] = str(int(col_copy['@colID']) + offset)
#                 new_col_data.append(col_copy)

#             shifted_rows.append({'@rowNumber': row.get('@rowNumber'), 'ColData': new_col_data})

#         return shifted_col_desc, shifted_rows

#     merged_col_desc = []
#     merged_rows_map = {}
#     offset = 0

#     for part in parts:
#         col_desc = extract_coldesc(part)
#         rows = extract_rows(part)
#         shifted_col_desc, shifted_rows = shift_col_ids(col_desc, rows, offset)
#         merged_col_desc.extend(shifted_col_desc)

#         for row in shifted_rows:
#             row_number = row.get('@rowNumber')
#             if not row_number:
#                 continue
#             col_data = row.get('ColData', [])
#             merged_rows_map.setdefault(row_number, []).extend(col_data)

#         offset = get_max_col_id(merged_col_desc) + 1

#     merged_rows = []
#     for row_number, col_data_list in sorted(merged_rows_map.items(), key=lambda x: int(x[0])):
#         col_data_list.sort(key=lambda col: int(col.get('@colID', 0)))
#         merged_rows.append({'@rowNumber': row_number, 'ColData': col_data_list})

#     return {
#         'response': {
#             'QBXML': {
#                 'QBXMLMsgsRs': {
#                     'GeneralDetailReportQueryRs': {
#                         'ReportRet': {
#                             'ColDesc': merged_col_desc,
#                             'ReportData': {'DataRow': merged_rows}
#                         }
#                     }
#                 }
#             }
#         }
#     }


# class QuickBooksService(ServiceBase):
#     unprocessed_date_chunks    = set()
#     processed_date_chunks      = set()
#     in_progress_date_chunks    = set()
#     failed_date_chunks         = set()
#     last_chunk_generation_date = None
#     chunk_query_phase          = {}
#     response_cache             = {}
#     chunk_for_ticket           = {}

#     # Sync-check state
#     sync_results = {}           # { txn_id (str) -> date (str) }
#     qb_line_items = []
#     sync_lock    = threading.Lock()
#     sync_complete = False


#     @classmethod
#     def get_date_range(cls, today):
#         if today.day < 11:
#             first_day_current_month = today.replace(day=1)
#             last_day_previous_month = first_day_current_month - timedelta(days=1)
#             from_date = last_day_previous_month.replace(day=1)
#         else:
#             from_date = today.replace(day=1)

#         to_date = today
#         while from_date <= to_date:
#             yield from_date, from_date
#             from_date += timedelta(days=1)


#     @classmethod
#     def generate_and_store_chunks(cls, today):
#         logger.info("Generating chunks...")
#         cls.processed_date_chunks.clear()
#         cls.last_chunk_generation_date = today
#         chunks = list(cls.get_date_range(today))
#         for chunk in chunks:
#             cls.unprocessed_date_chunks.add(chunk)
#         logger.info(f"Chunks generated for {today}: {chunks}")


#     @classmethod
#     def queue_sync_check(cls, from_date, to_date):
#         """Queue a custom date range for sync checking."""
#         with cls.sync_lock:
#             cls.unprocessed_date_chunks.clear()
#             cls.processed_date_chunks.clear()
#             cls.in_progress_date_chunks.clear()
#             cls.failed_date_chunks.clear()
#             cls.chunk_query_phase.clear()
#             cls.response_cache.clear()
#             cls.chunk_for_ticket.clear()
#             cls.sync_results.clear()
#             cls.qb_line_items = []  
#             cls.sync_complete = False

#             current = from_date
#             while current <= to_date:
#                 cls.unprocessed_date_chunks.add((current, current))
#                 current += timedelta(days=1)

#             cls.last_chunk_generation_date = to_date

#         count = len(cls.unprocessed_date_chunks)
#         logger.info(f"[SYNC-CHECK] Queued {count} chunks: {from_date} → {to_date}")
#         return count


#     @rpc(Unicode, Unicode, _returns=Iterable(Unicode))
#     def authenticate(ctx, strUserName, strPassword):
#         if strUserName == 'elbertehsan' and strPassword == 'Alpha103@':
#             return ["85B41BEE-5CD9-427a-A61B-83964F1EB426", ""]
#         else:
#             return ["", "nvu"]

#     @rpc(Unicode, _returns=Unicode)
#     def clientVersion(ctx, strVersion):
#         return ""

#     @rpc(Unicode, Unicode, Unicode, _returns=Unicode)
#     def connectionError(ctx, ticket, hresult, message):
#         return "done"


#     @rpc(Unicode, Unicode, Unicode, Unicode, Integer, Integer, _returns=Unicode)
#     def sendRequestXML(ctx, ticket, strHCPResponse, strCompanyFileName, qbXMLCountry, qbXMLMajorVers, qbXMLMinorVers):
#         try:
#             today = datetime.now().date()

#             if not SYNC_CHECK_MODE:
#                 if QuickBooksService.last_chunk_generation_date != today:
#                     QuickBooksService.generate_and_store_chunks(today)

#             sorted_chunks = sorted(
#                 QuickBooksService.unprocessed_date_chunks
#                 - QuickBooksService.processed_date_chunks
#                 - QuickBooksService.in_progress_date_chunks
#             )
#             unprocessed_chunk = sorted_chunks[0] if sorted_chunks else None

#             if not unprocessed_chunk:
#                 if SYNC_CHECK_MODE:
#                     QuickBooksService.sync_complete = True
#                     logger.info("[SYNC-CHECK] All chunks done")
#                 return ""

#             from_chunk, to_chunk = unprocessed_chunk
#             QuickBooksService.chunk_for_ticket[ticket] = unprocessed_chunk
#             phase = QuickBooksService.chunk_query_phase.get(unprocessed_chunk, 1)

#             if phase == 1:
#                 QuickBooksService.chunk_query_phase[unprocessed_chunk] = 2
#                 return genral_ledger_enteries_xml_query("4a", "Journal", from_chunk, to_chunk, ["TxnNumber", "Account"])
#             elif phase == 2:
#                 QuickBooksService.chunk_query_phase[unprocessed_chunk] = 3
#                 return genral_ledger_enteries_xml_query("4b", "Journal", from_chunk, to_chunk, ["Memo"])
#             elif phase == 3:
#                 QuickBooksService.chunk_query_phase[unprocessed_chunk] = 4
#                 return genral_ledger_enteries_xml_query("4c", "Journal", from_chunk, to_chunk, ["Name"])
#             elif phase == 4:
#                 QuickBooksService.chunk_query_phase[unprocessed_chunk] = 5
#                 return genral_ledger_enteries_xml_query("4d", "Journal", from_chunk, to_chunk, ["RefNumber", "TxnNumber", "Debit", "Credit"])
#             else:
#                 QuickBooksService.chunk_query_phase.pop(unprocessed_chunk, None)
#                 return genral_ledger_enteries_xml_query("4e", "Journal", from_chunk, to_chunk, ["Class", "Date", "TxnType", "TxnNumber"])

#         except Exception as e:
#             logger.error(f"Error in sendRequestXML: {e}")
#             return ""


#     @rpc(Unicode, Unicode, Unicode, Unicode, _returns=Integer)
#     def receiveResponseXML(ctx, ticket, response, hresult, message):
#         try:
#             parsed = xmltodict.parse(response)
#             chunk  = QuickBooksService.chunk_for_ticket.get(ticket)
#             if not chunk:
#                 return 100

#             current_phase = QuickBooksService.chunk_query_phase.get(chunk, 1)
#             QuickBooksService.response_cache.setdefault(ticket, {})[f'part{current_phase}'] = parsed

#             if current_phase < 5:
#                 QuickBooksService.chunk_query_phase[chunk] = current_phase + 1
#                 return 1

#             parts = [QuickBooksService.response_cache[ticket].get(f'part{i}') for i in range(1, 6)]
#             if all(parts):
#                 cached_parts = list(parts)
#                 cached_chunk = chunk

#                 QuickBooksService.response_cache.pop(ticket, None)
#                 QuickBooksService.chunk_for_ticket.pop(ticket, None)
#                 QuickBooksService.chunk_query_phase.pop(chunk, None)
#                 QuickBooksService.unprocessed_date_chunks.discard(chunk)
#                 QuickBooksService.in_progress_date_chunks.add(chunk)

#                 def process_in_background(parts_to_process, chunk_ref):
#                     try:
#                         final_merged = merge_general_detail_responses(parts_to_process)

#                         if SYNC_CHECK_MODE:
#                             # ── SYNC CHECK MODE: collect txn IDs only ─────────
#                             _collect_sync_txns(final_merged)
#                         else:
#                             # ── PRODUCTION MODE: post to NetSuite + DB ────────
#                             master_function(final_merged)
#                             #pass  # remove this line when uncommenting above

#                         QuickBooksService.in_progress_date_chunks.discard(chunk_ref)
#                         QuickBooksService.processed_date_chunks.add(chunk_ref)
#                         logger.info(f"Chunk {chunk_ref} processed")

#                         # Mark complete when all chunks done
#                         pending = (len(QuickBooksService.unprocessed_date_chunks) +
#                                    len(QuickBooksService.in_progress_date_chunks))
#                         if pending == 0:
#                             QuickBooksService.sync_complete = True
#                             logger.info(f"[SYNC-CHECK] Complete — {len(QuickBooksService.sync_results)} transactions collected")

#                     except Exception as e:
#                         logger.error(f"Background error for chunk {chunk_ref}: {e}")
#                         QuickBooksService.in_progress_date_chunks.discard(chunk_ref)
#                         QuickBooksService.failed_date_chunks.add(chunk_ref)
#                         QuickBooksService.unprocessed_date_chunks.add(chunk_ref)

#                 threading.Thread(
#                     target=process_in_background,
#                     args=(cached_parts, cached_chunk),
#                     daemon=True
#                 ).start()

#             return 100

#         except Exception as e:
#             logger.error(f"Error in receiveResponseXML: {e}")
#             return 100


#     @rpc(Unicode, _returns=Iterable(Unicode))
#     def closeConnection(ctx, ticket):
#         return "OK"

#     @rpc(Unicode, _returns=Iterable(Unicode))
#     def getLastError(ctx, ticket):
#         return ["", ""]


# # ── Collect txn IDs from merged QB response ───────────────────────────────────

# def _collect_sync_txns(merged_response):
#         try:
#             from data_transformation import data_transformation
#             sliced = data_transformation(merged_response)
#             if not sliced:
#                 return
#             with QuickBooksService.sync_lock:
#                 for _, rows in sliced.items():
#                     for row in rows:
#                         txn_id = row.get('TxnNumber')
#                         txn_dt = row.get('Date', '')
#                         if txn_id:
#                             QuickBooksService.sync_results[str(txn_id)] = txn_dt
 
#                             # ── Store full line item data for financial reconciliation ──
#                             account  = row.get('Account', '')
#                             memo     = row.get('Memo', '')
#                             location = row.get('Name', '')    # Name column = location/customer
#                             debit_v  = row.get('Debit', '')
#                             credit_v = row.get('Credit', '')
 
#                             try:
#                                 debit  = float(debit_v)  if debit_v  else 0.0
#                             except (ValueError, TypeError):
#                                 debit  = 0.0
#                             try:
#                                 credit = float(credit_v) if credit_v else 0.0
#                             except (ValueError, TypeError):
#                                 credit = 0.0
 
#                             QuickBooksService.qb_line_items.append({
#                                 'txn_id':   str(txn_id),
#                                 'date':     txn_dt,
#                                 'account':  account,
#                                 'location': location,
#                                 'memo':     memo,
#                                 'debit':    debit,
#                                 'credit':   credit,
#                             })
 
#             logger.info(f"[SYNC-CHECK] {len(QuickBooksService.sync_results)} transactions "
#                         f"| {len(QuickBooksService.qb_line_items)} line items collected so far")
#         except Exception as e:
#             logger.error(f"[SYNC-CHECK] Collection error: {e}")


# # ── Diagnostic HTTP server on port 8001 ───────────────────────────────────────

# class SilentHandler(WSGIRequestHandler):
#     def log_message(self, format, *args):
#         pass


# def diagnostic_app(environ, start_response):
#     path = environ.get('PATH_INFO', '')
#     qs   = parse_qs(environ.get('QUERY_STRING', ''))

#     def respond(data, status='200 OK'):
#         body = json.dumps(data, default=str).encode('utf-8')
#         start_response(status, [
#             ('Content-Type',   'application/json'),
#             ('Content-Length', str(len(body))),
#         ])
#         return [body]

#     # /queue?from_date=YYYY-MM-DD&to_date=YYYY-MM-DD
#     if path == '/queue':
#         from_date_str = qs.get('from_date', [None])[0]
#         to_date_str   = qs.get('to_date',   [None])[0]
#         if not from_date_str or not to_date_str:
#             return respond({"error": "Pass ?from_date=YYYY-MM-DD&to_date=YYYY-MM-DD"}, '400 Bad Request')
#         try:
#             from_dt = datetime.strptime(from_date_str, "%Y-%m-%d").date()
#             to_dt   = datetime.strptime(to_date_str,   "%Y-%m-%d").date()
#             count   = QuickBooksService.queue_sync_check(from_dt, to_dt)
#             return respond({
#                 "status":        "queued",
#                 "from_date":     from_date_str,
#                 "to_date":       to_date_str,
#                 "chunks_queued": count,
#                 "next":          "Click 'Update Selected' in QBWC, then poll /results",
#             })
#         except Exception as e:
#             return respond({"error": str(e)}, '500 Internal Server Error')

#     # /results — returns collected QB txns and whether sync is complete
#     elif path == '/results':
#         pending = (len(QuickBooksService.unprocessed_date_chunks) +
#                    len(QuickBooksService.in_progress_date_chunks))
#         return respond({
#             "complete":        QuickBooksService.sync_complete or pending == 0,
#             "pending_chunks":  pending,
#             "processed_chunks": len(QuickBooksService.processed_date_chunks),
#             "failed_chunks":   len(QuickBooksService.failed_date_chunks),
#             "collected":       len(QuickBooksService.sync_results),
#             "qb_transactions": QuickBooksService.sync_results,
#             "qb_line_items":   QuickBooksService.qb_line_items,
#         })

#     # /status — progress overview
#     elif path == '/status':
#         total = (len(QuickBooksService.unprocessed_date_chunks) +
#                  len(QuickBooksService.in_progress_date_chunks) +
#                  len(QuickBooksService.processed_date_chunks))
#         done  = len(QuickBooksService.processed_date_chunks)
#         pct   = round((done / total) * 100) if total else 0
#         return respond({
#             "sync_check_mode":    SYNC_CHECK_MODE,
#             "complete":           QuickBooksService.sync_complete,
#             "progress":           f"{done}/{total} chunks ({pct}%)",
#             "unprocessed_chunks": len(QuickBooksService.unprocessed_date_chunks),
#             "in_progress_chunks": len(QuickBooksService.in_progress_date_chunks),
#             "processed_chunks":   done,
#             "failed_chunks":      len(QuickBooksService.failed_date_chunks),
#             "collected_txns":     len(QuickBooksService.sync_results),
#         })

#     # /clear — reset sync state
#     elif path == '/clear':
#         QuickBooksService.sync_results.clear()
#         QuickBooksService.sync_complete = False
#         QuickBooksService.unprocessed_date_chunks.clear()
#         QuickBooksService.processed_date_chunks.clear()
#         QuickBooksService.in_progress_date_chunks.clear()
#         return respond({"status": "cleared"})

#     else:
#         return respond({
#             "service":      "QB Sync-Check Server — port 8001",
#             "mode":         "SYNC_CHECK" if SYNC_CHECK_MODE else "PRODUCTION",
#             "endpoints": {
#                 "/queue?from_date=YYYY-MM-DD&to_date=YYYY-MM-DD":
#                     "Queue a date range, then click Update Selected in QBWC",
#                 "/results":
#                     "Poll this — complete=true when done, returns qb_transactions",
#                 "/status":
#                     "Check progress (chunks done / total)",
#                 "/clear":
#                     "Reset everything",
#             }
#         })


# # ── Spyne SOAP app ────────────────────────────────────────────────────────────

# soap_app = Application(
#     [QuickBooksService],
#     tns='http://developer.intuit.com/',
#     in_protocol=Soap11(validator='lxml'),
#     out_protocol=Soap11()
# )
# wsgi_app = WsgiApplication(soap_app)


# # ── Start both servers ────────────────────────────────────────────────────────

# diag_server = make_server('127.0.0.1', 8001, diagnostic_app, handler_class=SilentHandler)
# diag_thread = threading.Thread(target=diag_server.serve_forever, daemon=True)
# diag_thread.start()

# mode_label = "SYNC-CHECK MODE (production posting disabled)" if SYNC_CHECK_MODE else "PRODUCTION MODE"
# logger.info(f"Starting in {mode_label}")
# logger.info("Diagnostic server listening on port 8001")

# main_server = make_server('127.0.0.1', 8000, wsgi_app)
# logger.info("SOAP server listening on port 8000")
# try:
#     main_server.serve_forever()
# except KeyboardInterrupt:
#     logger.info("Stopping servers...")
#     main_server.shutdown()
#     diag_server.shutdown()