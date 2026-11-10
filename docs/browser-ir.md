# Investor relations in desktop Chrome

The bot uses headful Chrome on a separate inactive Windows desktop with
a temporary profile. During a watch window, Chrome and ordinary HTTP
requests share per-host pacing. The first browser poll warms the issuer tab.
Later polls fetch from that tab without reloading the page. The bot keeps
the complete response in memory for attachments or summaries.

The worker has been exercised under an active Windows login session. Chrome
startup under the Task Scheduler account depends on that account's desktop
access. A browser startup failure is reported for that source. The other
sources continue watching.

## Automatic sources for tracked issuers

For a listing researched by hand, or one the bot found when it investigated a newly
tracked company, `/track` can use Chrome to read an issuer's date notice. Chrome
opens any public https page, never plain http, another port, a private or
loopback address, or a local-only name.
It can also observe same-origin JSON requested by the public IR page. The
scorecard distinguishes responses that matched the last release from endpoint
leads. A new route can be added as a source through a dedicated adapter.

The planner can prepare a listing source after retrieving the previous quarter's
full report. It checks the upcoming fiscal period and requires usable financial
results text before recording a sighting. Chrome races the other sources for the
initial alert and can add the IR link to an alert already posted.

## Configure a source for one schedule

Add a `[target.ir]` section after the matching `[[target]]` entry. The
following are examples using historical releases. Set the target's expected time
and the headline's fiscal period for the upcoming release.

```toml
[[target]]
ticker = "FICO"
cik = 814547
expected = 2026-11-04T16:15:00
newswire = ["FICO", "Fair Isaac"]

[target.ir]
listing = "https://investors.fico.com/news-releases/"
headline = 'FICO Announces Earnings of .+ for Fourth Quarter Fiscal 2026'
```

The headline is a case-insensitive full-match regex. Include the issuer and
fiscal period. The listing must link to the result by that title. A webcast or
a date announcement won't count. The recorder waits until the linked article
contains the title and enough text to pass the release check. Set `verify_text = true`
to require financial results text, as automatic sources do. When the article
links to a same-host **PDF Version**, the bot verifies and attaches the PDF
after adding the article link.

Other listing starting points:

| Issuer | Listing |
| --- | --- |
| CrowdStrike | `https://ir.crowdstrike.com/press-releases` |
| Lilly | `https://investor.lilly.com/` |
| HubSpot | `https://ir.hubspot.com/news` |
| Broadcom | `https://investors.broadcom.com/financial-information/financial-news-releases` |

For a PDF, provide a public warmup page on the same host and the exact expected
file URL or URLs. McDonald's has used different filenames across quarters.
Verify the candidates before arming the schedule.

```toml
[target.ir]
warmup = "https://corporate.mcdonalds.com/corpmcd/investors/financial-information.html"
pdfs = [
  "https://corporate.mcdonalds.com/content/dam/sites/corp/nfl/pdf/MCD%20Q326%20-%20Exhibit%2099.1.pdf",
]
```

Home Depot uses the same fields. For example, set `warmup` to
`https://ir.homedepot.com/financial-reports/quarterly-earnings/2026` and use
`https://ir.homedepot.com/~/media/Files/H/HomeDepot-IR/press-release/q2-2026-earnings-release.pdf`
for its Q2 2026 file. A cache-busted GET validates the PDF signature and keeps
the bytes for the attachment.

Tesla has a separate HTTP source with the project name in the User-Agent.

```toml
[target.ir]
tesla_deck = "https://assets-ir.tesla.com/tesla-contents/IR/TSLA-Q3-2026-Update.pdf"
```

Existing `[target.trial.ir]` entries load as live sources. The `post` switch
under `[target.trial]` controls the other experimental sources.
The bot keeps the PDF link if the file exceeds the attachment limit. Use only
the current quarter's PDF candidates. A verified IR result on the first
poll counts as a catch-up, so a late start can still see that scheduled result.

The journal records each listing's response time, queue time, cache headers
and headline match separately from the article fetch.

## Read an issuer feed in Chrome

Some IR feeds return HTTP 403 to the bot's HTTP client and readable RSS to
Chrome. A dated, same-host feed item whose headline matches the scheduled
quarter can start the alert. Chrome reads the article and downloads its
verified PDF afterward. This removes the article request from the alert's
critical path.

```toml
[target.ir]
rss = "https://ir.lucidmotors.com/rss/news-releases.xml"
warmup = "https://ir.lucidmotors.com/press-releases"
headline = 'Lucid Announces Third Quarter 2026 Results.*'
```

Set both `rss` and `listing` to compare the feed and page at the same release.
On the same host, each polls every two seconds so they share the existing
one-request-per-second budget. The journal records `browser_feed` and
`browser_listing` separately. The planner can add a known issuer feed
alongside the listing after reading the previous quarter's full report through it.

An exact PDF URL can redirect to a generic page before publication. Chrome
stops at the redirect and keeps the tab warm for the next check.

```powershell
uv run earnings-bot-record schedules/<day>.toml
```
