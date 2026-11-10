"""How real results releases open, as filed with SEC's 8-Ks in 2026, for learning how each issuer
names its fiscal years. Each list is newest first: (when SEC accepted it, its opening lines).
SEC gives each of these issuers' fiscal year end as 0131, Target's as 0201."""

# Name a fiscal year for the calendar year it starts in: fiscal 2026 ends in January 2027
HD_RELEASES = [
    (
        "2026-08-18T10:10:33.000Z",
        (
            "Exhibit 99.1\n"
            "The Home Depot Announces Second Quarter Fiscal 2026 Results;\n"
            "Reaffirms Fiscal 2026 Guidance\n"
            "ATLANTA, August 18, 2026 -- The Home Depot®, the world's largest home improvement "
            "retailer, today reported sales of $47.9 billion for the second quarter of fiscal "
            "2026, an increase of $2.6 billion, or 5.7% from the second quarter of fiscal 2025."
        ),
    ),
    (
        "2026-05-19T10:06:14.000Z",
        (
            "Exhibit 99.1\n"
            "The Home Depot Announces First Quarter Fiscal 2026 Results;\n"
            "Reaffirms Fiscal 2026 Guidance\n"
            "ATLANTA, May 19, 2026 -- The Home Depot®, the world's largest home improvement "
            "retailer, today reported sales of $41.8 billion for the first quarter of fiscal 2026, "
            "an increase of $1.9 billion, or 4.8% from the first quarter of fiscal 2025."
        ),
    ),
    (
        "2026-02-24T11:07:18.000Z",
        (
            "Exhibit 99.1\n"
            "The Home Depot Announces Fourth Quarter and Fiscal 2025 Results;\n"
            "Increases Quarterly Dividend by 1.3%;\n"
            "Provides Fiscal 2026 Guidance\n"
            "ATLANTA, February 24, 2026 -- The Home Depot®, the world's largest home improvement "
            "retailer, today reported fourth quarter and fiscal 2025 results."
        ),
    ),
]
LOW_RELEASES = [
    (
        "2026-08-19T12:45:07.000Z",
        (
            "Exhibit 99.1\n"
            "August 19, 2026\n"
            "For 6:00 a.m. ET Release\n"
            "LOWE\u2019S REPORTS SECOND QUARTER 2026 SALES AND EARNINGS RESULTS\n"
            "— Diluted EPS of $4.27; Adjusted Diluted EPS1 of $4.40 —\n"
            "MOORESVILLE, N.C., August 19, 2026 \u2013 Lowe\u2019s Companies, Inc. (NYSE: LOW) "
            "today reported net earnings of $2.4 billion and diluted earnings per share (EPS) of "
            "$4.27 for the quarter ended July 31, 2026, compared to diluted EPS of $4.27 in the "
            "second quarter of 2025."
        ),
    ),
    (
        "2026-05-20T12:45:10.000Z",
        (
            "Exhibit 99.1\n"
            "May 20, 2026\n"
            "For 6:00 a.m. ET Release\n"
            "LOWE\u2019S REPORTS FIRST QUARTER 2026 SALES AND EARNINGS RESULTS\n"
            "— Diluted EPS of $2.90; Adjusted Diluted EPS1 of $3.03 —\n"
            "MOORESVILLE, N.C., May 20, 2026 \u2013 Lowe\u2019s Companies, Inc. (NYSE: LOW) today "
            "reported net earnings of $1.6 billion and diluted earnings per share (EPS) of $2.90 "
            "for the quarter ended May 1, 2026, compared to diluted EPS of $2.92 in the first "
            "quarter of 2025."
        ),
    ),
    (
        "2026-02-25T13:44:53.000Z",
        (
            "Exhibit 99.1\n"
            "February 25, 2026\n"
            "For 6:00 a.m. ET Release\n"
            "LOWE\u2019S REPORTS FOURTH QUARTER 2025 SALES AND EARNINGS RESULTS\n"
            "— Comparable Sales Increased 1.3% —\n"
            "MOORESVILLE, N.C., Feb. 25, 2026 \u2013 Lowe\u2019s Companies, Inc. (NYSE: LOW) today "
            "reported net earnings of $1.0 billion and diluted earnings per share (EPS) of $1.78 "
            "for the quarter ended Jan. 30, 2026, compared to diluted EPS of $1.99 in the fourth "
            "quarter of 2024."
        ),
    ),
]
# Its headlines name no year, and a bullet ahead of the dateline compares with last year's
TGT_RELEASES = [
    (
        "2026-08-19T11:51:13.000Z",
        (
            "Exhibit 99\n"
            "FOR IMMEDIATE RELEASE\n"
            "Target Corporation Reports Second Quarter Earnings\n"
            "•Second quarter net sales grew 5.3 percent over last year, with comparable sales "
            "growth of 3.8 percent driven by a 3.6 percent increase in comparable traffic.\n"
            "MINNEAPOLIS (August 19, 2026) \u2013 Target Corporation (NYSE: TGT) today announced "
            "its second quarter 2026 financial results."
        ),
    ),
    (
        "2026-05-20T11:57:30.000Z",
        (
            "Exhibit 99\n"
            "FOR IMMEDIATE RELEASE\n"
            "Target Corporation Reports First Quarter Earnings\n"
            "•First quarter net sales grew 6.7 percent over last year, well above expectations.\n"
            "▪Comparable traffic grew 4.4 percent compared with Q1 2025.\n"
            "MINNEAPOLIS (May 20, 2026) \u2013 Target Corporation (NYSE: TGT) today announced its "
            "first quarter 2026 financial results."
        ),
    ),
    (
        "2026-03-03T12:12:05.000Z",
        (
            "Exhibit 99\n"
            "FOR IMMEDIATE RELEASE\n"
            "Target Corporation Reports Fourth Quarter and Full-Year 2025 Earnings\n"
            "•Fourth quarter net sales of $30.5 billion were in line with company expectations.\n"
            "MINNEAPOLIS (March 3, 2026) \u2013 Target Corporation (NYSE: TGT) today announced its "
            "fourth-quarter and full-year 2025 results."
        ),
    ),
]

# Name a fiscal year for the calendar year it ends in: fiscal 2027 ends in January 2027
NVDA_RELEASES = [
    (
        "2026-08-26T20:21:19.000Z",
        (
            "NVIDIA Announces Financial Results for Second Quarter Fiscal 2027\n"
            "•Revenue of $96.2 billion, up 106% from a year ago\n"
            "SANTA CLARA, Calif.—Aug. 26, 2026―NVIDIA (NASDAQ: NVDA) today reported revenue for "
            "the second quarter ended July 26, 2026, of $96.2 billion, up 18% from the previous "
            "quarter and up 106% from a year ago."
        ),
    ),
    (
        "2026-05-20T20:21:19.000Z",
        (
            "NVIDIA Announces Financial Results for First Quarter Fiscal 2027\n"
            "•Record revenue of $81.6 billion, up 85% from a year ago\n"
            "SANTA CLARA, Calif.—May 20, 2026―NVIDIA (NASDAQ: NVDA) today reported record revenue "
            "for the first quarter ended April 26, 2026, of $81.6 billion, up 20% from the "
            "previous quarter and up 85% from a year ago."
        ),
    ),
    (
        "2026-02-25T21:31:25.000Z",
        (
            "NVIDIA Announces Financial Results for Fourth Quarter and Fiscal 2026\n"
            "•Record quarterly revenue of $68.1 billion, up 20% from Q3 and up 73% from a year "
            "ago\n"
            "SANTA CLARA, Calif.—Feb. 25, 2026―NVIDIA (NASDAQ: NVDA) today reported record revenue "
            "for the fourth quarter ended January 25, 2026, of $68.1 billion, up 20% from the "
            "previous quarter and up 73% from a year ago."
        ),
    ),
]
CRM_RELEASES = [
    (
        "2026-08-26T20:03:53.000Z",
        (
            "Exhibit 99.1\n"
            "Salesforce Delivers Record Second Quarter Fiscal 2027 Results\n"
            "cRPO growth accelerates to 14% Y/Y CC; Raises FY27 revenue guidance by $200M, $300M "
            "in CC\n"
            "SAN FRANCISCO, Calif. - August 26, 2026 - Salesforce (NYSE: CRM), the world's #1 AI "
            "CRM, today announced results for its second quarter fiscal 2027 ended July 31, 2026."
        ),
    ),
    (
        "2026-05-27T20:18:26.000Z",
        (
            "Exhibit 99.1\n"
            "Salesforce Delivers Record First Quarter Fiscal 2027 Results\n"
            "GAAP EPS $2.42, up 52% Y/Y, Non-GAAP EPS $3.88, up 50% Y/Y\n"
            "SAN FRANCISCO, Calif. - May 27, 2026 - Salesforce (NYSE: CRM), the world's #1 AI CRM, "
            "today announced results for its first quarter fiscal 2027 ended April 30, 2026."
        ),
    ),
    (
        "2026-02-25T21:03:12.000Z",
        (
            "Exhibit 99.1\n"
            "Salesforce Delivers Record Fourth Quarter Fiscal 2026 Results\n"
            "RPO Exceeds $72B, Up 14% Y/Y; $15B OCF\n"
            "SAN FRANCISCO, Calif. - February 25, 2026 - Salesforce (NYSE: CRM), the world's #1 AI "
            "CRM, today announced results for its fourth quarter and full fiscal year ended "
            "January 31, 2026."
        ),
    ),
]
# Its releases name no fiscal year for their own quarter, though one looks ahead to "Q1 and FY27"
WMT_RELEASES = [
    (
        "2026-08-20T10:59:01.000Z",
        (
            "| Walmart reports second quarter results |\n"
            "| •Revenue growth of 5.9%, up 5.1% in constant currency (cc)1 •Operating income "
            "growth of 28.8%, up 17.4% adjusted (cc)1 •eCommerce sales up 23% globally •GAAP EPS "
            "of $0.80; Adjusted EPS1 of $0.81 •Company issues guidance for Q3; raises outlook for "
            "FY27\n"
            "| BENTONVILLE, Ark., August 20, 2026 \u2013 Walmart Inc. (NASDAQ: WMT) announces "
            "second-quarter results with strong growth in revenue, including eCommerce, which grew "
            "23% globally with strength across segments."
        ),
    ),
    (
        "2026-05-21T10:59:53.000Z",
        (
            "| Walmart reports first quarter results |\n"
            "| •Revenue growth of 7.3%, up 5.9% in constant currency (cc)1 •Operating income "
            "growth of 5.0%, up 5.1% adjusted (cc)1 •eCommerce sales up 26% globally •GAAP EPS of "
            "$0.67; Adjusted EPS1 of $0.66 •Company issues guidance for Q2; reiterates outlook for "
            "FY27\n"
            "| BENTONVILLE, Ark., May 21, 2026 \u2013 Walmart Inc. (NASDAQ: WMT) announces "
            "first-quarter results with strong growth in revenue, including eCommerce, which grew "
            "26% globally with strength across segments."
        ),
    ),
    (
        "2026-02-19T11:59:55.000Z",
        (
            "| Walmart reports Q4 results |\n"
            "| •Revenue growth of 5.6%, up 4.9% in constant currency (cc)1 •Operating income "
            "growing faster at 10.8%, up 10.5% adjusted (cc)1 •eCommerce sales up 24% globally "
            "•GAAP EPS of $0.53; Adjusted EPS1 of $0.74 •Company announces new $30 billion share "
            "repurchase authorization •Company provides outlook for Q1 and FY27\n"
            "| BENTONVILLE, Ark., February 19, 2026 \u2013 Walmart Inc. (NASDAQ: WMT) announces "
            "fourth-quarter results with strong growth in revenue and adjusted operating income."
        ),
    ),
]
