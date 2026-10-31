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

Results from 2026-10-31, with the open items last:

| Company | Release date | Wire | Time | Still to do |
|---|---|---|---|---|
| Bank of America | Oct 14, 2026 | None (not on PR Newswire either) | | |
| Goldman Sachs | Oct 13, 2026 | None. The Business Wire hit was Goldman Sachs BDC, a separate company | | |
| Broadcom | Sep 2, 2026 | None (not on PR Newswire either) | | |
| NVIDIA | Aug 26, 2026 | GlobeNewswire | | |
| McDonald's | Aug 4, 2026 | PR Newswire | 7:00 AM | |
| UnitedHealth Group | Oct 13, 2026 | Business Wire | 5:55 AM | |
| Salesforce | Aug 26, 2026 | Business Wire | 4:01 PM | |
| Walmart | Aug 20, 2026 | Business Wire | 7:01 AM | |
| JPMorgan Chase | Oct 13, 2026 | Business Wire | 7:08 AM | |
| Adobe | Sep 10, 2026 | Business Wire | 4:05 PM | |
| CrowdStrike | Aug 26, 2026 | Business Wire | 4:05 PM | |

## 2. Tesla's update deck

Done on 2026-10-31. The deck links follow one pattern
(`https://assets-ir.tesla.com/tesla-contents/IR/TSLA-Q3-2026-Update.pdf`), but that host
returns 403 to automated requests, so the bot can't use it. Tesla stays on the 8-K.

One optional check is left, at Tesla's next report (late January 2027). Note when the deck
appears in the Shareholder Deck column of <https://ir.tesla.com>'s quarterly table, using
<https://time.gov>. That shows how far the 8-K trails it.

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

Results from 2026-10-31:

| Company | PDF `last-modified` | Compared with | What it tells us |
|---|---|---|---|
| FICO | No PDF; the release page's stamp is the time it was viewed | Business Wire 4:15 PM | Nothing; the page URL contains the EPS figure, so it can't be guessed either |
| CrowdStrike | Aug 26, 4:09:12 PM (`/node/17376/pdf`) | Business Wire 4:05 PM, 8-K 4:07:02 PM | The site came after the wire and the 8-K |
| Lilly | Oct 29, 6:47:20 AM (`/node/55191/pdf`) | PR Newswire 6:45 AM | The site came after the wire |
| Home Depot | Aug 18, 6:05:18 AM (`q2-2026-earnings-release.pdf`) | PR Newswire 6:00 AM, 8-K 6:10:33 AM | The site came after the wire |
| Broadcom | Jun 6 (`/node/64371/pdf`), regenerated later | 8-K | Nothing |
| McDonald's | Aug 4, 6:46:00 AM (`MCD Q226 Earnings Release - Exhibit 99.1.pdf`) | PR Newswire 7:00 AM | Uploaded 14 minutes before the release, but the host blocks automated reads |

None of these PDF hosts answer automated requests (all return 403), so the bot can't use them.

Two sites that do answer us, checked the same way:

| Company | PDF `last-modified` | Compared with | What it tells us |
|---|---|---|---|
| JPMorgan Chase | Oct 13, 5:14:57 AM (named by a random ID) | 8-K 6:30:38 AM, Business Wire 7:08 AM | Uploaded 75 minutes before the 8-K. It can't be guessed, but the page that links it can be watched |
| Adobe | Sep 10, 4:05:37 PM | Business Wire 4:05 PM, 8-K 4:06:14 PM | Business Wire is first. Next: copy the PDF's link to see whether its name can be guessed |

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
