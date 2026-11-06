# Public IR browser trial

The trial uses stock desktop Chrome on a separate inactive Windows desktop with
a temporary profile. It leaves the user's browser session alone. Chrome and
ordinary HTTP requests share the existing per-host pacing. During a watch
window, the first browser poll warms the issuer tab. Later polls fetch from
that tab without reloading the page. The bot keeps the complete response in
memory for attachments or summaries.

Use the recorder first. These tests exercised an active Windows login session.
Check Chrome startup under the intended Task Scheduler account before counting
on this source after a reboot. A browser startup failure is reported for that
source. The other sources continue watching.

## Enable a source for one schedule

Add an `[target.trial.ir]` section after the matching `[[target]]` entry. The
following are examples using historical releases. Set the target's expected time
and the headline's fiscal period for the upcoming release.

```toml
[[target]]
ticker = "FICO"
cik = 814547
expected = 2026-11-04T16:15:00
newswire = ["FICO", "Fair Isaac"]

[target.trial.ir]
listing = "https://investors.fico.com/news-releases/"
headline = 'FICO Announces Earnings of .+ for Fourth Quarter Fiscal 2026'
```

The headline is a case-insensitive full-match regex. Include the issuer and
fiscal period. The listing must link to the result by that title. A webcast or
a date announcement won't count. The recorder waits until the linked article
contains the title and enough text to pass the release check.

Other listing starting points for the same trial:

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
[target.trial.ir]
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

Tesla has a separate HTTP trial with the actual project name in the User-Agent:

```toml
[target.trial.ir]
tesla_deck = "https://assets-ir.tesla.com/tesla-contents/IR/TSLA-Q3-2026-Update.pdf"
```

All trial sources are recorded. To let a trial source add to the Discord alert,
set `post = true` in that target's `[target.trial]` section after validating its timing.
The bot keeps the PDF link if the file exceeds the attachment limit. Use only
the current quarter's PDF candidates. A verified trial result on the first
poll counts as a catch-up, so a late start can still see that scheduled result.

```powershell
uv run earnings-bot-record schedules/<day>.toml
```
