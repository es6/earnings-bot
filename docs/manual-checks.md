# Manual checks

Some IR sites block automated requests, and the bot never works around that. A person can
still look at them in a browser. These checks fill the gaps listed as `todo` in
`src/earnings_bot/companies.toml`. Write down what you find, then the file and the
[architecture doc](architecture.md#companies-off-q4) get updated.

Use your normal browser, Chrome or Edge. All times are US Eastern.

## 1. Which newswire carried the last release

About 2 minutes per company. The release date in each row is the day of the company's last
earnings 8-K.

1. Go to <https://www.businesswire.com>. Type the company name and the quarter into the search
   box at the top, for example `Bank of America third quarter 2026 results`, and press Enter.
2. If the release from that date shows up, open it. The date and time appear under the
   headline, for example "Oct 14, 2026 6:45 AM Eastern Daylight Time". Write down the time.
3. If it isn't on Business Wire, search <https://www.globenewswire.com> the same way. Its
   releases show a time like "October 14, 2026 06:45 ET".
4. If it's on neither, write "none".

| Company | Release date | Business Wire time | GlobeNewswire time |
|---|---|---|---|
| Bank of America | Oct 14, 2026 | | |
| UnitedHealth Group | Oct 13, 2026 | | |
| Goldman Sachs | Oct 13, 2026 | | |
| JPMorgan Chase | Oct 13, 2026 | | |
| Adobe | Sep 10, 2026 | | |
| Broadcom | Sep 2, 2026 | | |
| CrowdStrike | Aug 26, 2026 | | |
| NVIDIA | Aug 26, 2026 | | |
| Salesforce | Aug 26, 2026 | | |
| Walmart | Aug 20, 2026 | | |
| McDonald's | Aug 4, 2026 | | |

## 2. Tesla's update deck link

About 2 minutes. This is the most valuable check: Tesla's 8-K comes 30 to 35 minutes after the
deck, and Tesla uses no newswire.

1. Open <https://ir.tesla.com>.
2. Find the Q3 2026 Update, the quarterly results deck.
3. Right-click its link, choose **Copy link address**, and paste the link into the chat.

If the deck sits on a file host that answers automated requests, the bot can try next quarter's
file name from 4:00 PM on.

## 3. When a blocked IR site posted its release

About 5 minutes per company. Release pages often show only a date, so the release PDF's upload
time is the better clue.

1. Open the company's IR site (table below) and find its latest quarterly results release.
2. Press **F12** to open the developer tools, and click the **Network** tab.
3. Click the link to the release PDF. A row for the PDF appears in the Network list. If the PDF
   opens in a new tab instead, press F12 in that tab, click Network, and press F5 to reload.
4. Click the PDF's row, then **Headers**. Under **Response Headers**, copy the `last-modified`
   value.
5. Also write down the time on the release page, if it shows one.

`last-modified` is when the file was uploaded. A file can be uploaded before the release and
kept hidden, or re-uploaded later, so treat it as a clue, not proof.

| Company | IR site | Time on the release page | PDF `last-modified` | PDF link |
|---|---|---|---|---|
| FICO | <https://investors.fico.com> | | | |
| CrowdStrike | <https://ir.crowdstrike.com> | | | |
| Lilly | <https://investor.lilly.com> | | | |
| Home Depot | <https://ir.homedepot.com> | | | |
| Broadcom | <https://investors.broadcom.com> | | | |
| McDonald's | <https://corporate.mcdonalds.com/corpmcd/investors.html> | | | |

## 4. Live check: FICO on November 4

About 15 minutes, while the bot runs. The bot watches Business Wire and EDGAR for FICO. This
check shows whether FICO's own site posts sooner.

1. At 4:12 PM, open <https://time.gov> in one tab, and in another open
   <https://investors.fico.com> and go to its press releases list.
2. Refresh the press releases list about every 10 seconds.
3. When FICO's fourth-quarter fiscal 2026 results release appears, write down the time from
   time.gov, to the second.
4. Afterwards, compare it with when the bot saw Business Wire and EDGAR. Those times are in
   `schedules/2026-11-04.jsonl`, or ask in the chat.

Write down only the time you saw it. Don't use the site's own timestamp, which is the
scheduled minute.
