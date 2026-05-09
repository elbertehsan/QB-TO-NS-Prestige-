from datetime import datetime

def genral_ledger_enteries_xml_query(request_id: str, report_type: str, from_date: datetime, to_date: datetime, columns: list[str]):
    def format_qb_date(date):
        return date.strftime('%Y-%m-%d')

    from_report_date = format_qb_date(from_date)
    to_report_date = format_qb_date(to_date)

    include_columns_xml = "\n".join(f"<IncludeColumn>{col}</IncludeColumn>" for col in columns)

    query = f"""
        <?qbxml version="8.0"?>
        <QBXML>
            <QBXMLMsgsRq onError="stopOnError">
                <GeneralDetailReportQueryRq requestID="{request_id}">
                    <GeneralDetailReportType>{report_type}</GeneralDetailReportType>
                    <ReportPeriod>
                        <FromReportDate>{from_report_date}</FromReportDate>
                        <ToReportDate>{to_report_date}</ToReportDate>
                    </ReportPeriod>
                    {include_columns_xml}
                </GeneralDetailReportQueryRq>
            </QBXMLMsgsRq>
        </QBXML>
    """
    return query
