# Roadmap

What's left before 1.0 and what comes after, as agreed with the owner. Status as of
November 15, 2026. Times are US Eastern.

## The 1.0 gate (agreed November 12)

| # | Item | Status |
|---|---|---|
| 1 | A route that dies mid-window is loud | Done: the watchdog alarms when a window's polling stops or a route is refused, and names it (#90, #91). `/status` and the window report show a degraded route (#100) |
| 2 | Polling is gentler far from the release time | Done: 15 s far from a release time, 5 s from 5 minutes before, full speed from 2 minutes before to 10 after (#92, #128). Not yet seen on a live release |
| 3 | A live streak | Next. HD, KLAR and NVDA on Tue Nov 17, ROAD on Thu Nov 19, WRD on Fri Nov 20. Every IR route stays alive (or recovers and says so), the alert posts, and the summary comes from the first available text. Each is written up from the journal the same day, and a failure restarts the streak |
| 4 | Failure-replay tests | Done. A refusal mid-window (`test_recorder`), repeated timeouts as a release goes live (#93, #101), a wire alert with no readable article, a restart after delivery not summarizing again, and Discord refusing a post or edit (#123) |
| 5 | Summaries labeled beta until plan item 6 ships | Done: the footer starts "Beta · Summary by ..." (#132) |

From the earlier 1.0 list:

| Item | Status |
|---|---|
| Crash cleanup | Done: Chrome ends with the bot, and the bot clears Chrome and profiles a previous run left (#133) |
| Load and failure replay | Done: a 20-release burst replay and the failure replays above |
| Soak | Open. Nothing yet runs the bot for hours to check memory, handles and processes over time |
| Chrome limits, telemetry | Partly there: Chrome reads have byte caps, and every window is journaled. What more is wanted isn't defined yet |
| Whether the first missed IR page had the same cause as Nebius's | With the source-research agent |

## Added before 1.0 (November 15)

### International tickers

`/track` should accept tickers listed outside the US, such as `SAF.PA` (Safran, Euronext
Paris) and `TEA.AX` (Tasmea, ASX). Neither is on SEC's ticker list. This replaces "reject
non-US tickers": supported exchanges are accepted, and others are refused with a clear
message.

The polling, alerts, Chrome reading, summaries and Discord parts don't depend on the
exchange. The company model does:

- **Identity.** Every company is keyed by its SEC CIK: 15 of 58 modules and the database's
  keys. A non-SEC issuer needs another key (such as `ASX:TEA`) and a data migration.
- **Ticker lookup.** Only SEC's list is read. The suffix (`.AX`, `.PA`, `.L`, ...) should
  pick the exchange, which supplies the time zone, trading hours, currency and announcement
  source.
- **Research.** The IR site and fiscal-year naming come from SEC filings. Instead: the ASX
  announcements, and for Euronext the company's own site. Safran's `/finance` page answered
  the bot 403 on Nov 15, so it would go through Chrome.
- **Periods.** Only `FYyyyyQn` exists. Half-year and full-year periods are needed. Safran
  gives full results twice a year and revenue only in Q1 and Q3. ASX companies file half-year
  and full-year reports (Appendix 4D and 4E).
- **Timing.** "Before the open" and "after the close" defaults and the polling sessions assume
  09:30 to 16:00 ET. They should come from the exchange's own hours.
- **Sources.** SEC's feeds are added to every window. For these companies, use the exchange's
  announcements instead. ASX's public announcements data answered the bot on Nov 15 with each
  item's time to the second, headline, type and price-sensitive flag, so one adapter would
  cover every ASX company. Euronext's regulated news isn't checked yet.
- **Detection.** Headlines like "Half Year Results", "1H FY26" and "Appendix 4D", and
  matching a half-year to its period.

Suggested order, after the streak ends on Nov 20, since the identity change touches the data
every window depends on:

1. A non-SEC company key and the migration.
2. Exchange-aware tickers.
3. Half-year and full-year periods.
4. ASX announcements as a source, company lookup and dates.
5. Euronext.
6. Each exchange's hours for polling.
7. Summary and headline tuning, measured on many international releases.

### Multiple Discord servers

Decided Nov 15: each server tracks its own companies; only the owner adds a server, in
config; each server sees scorecards for its own companies; a server tracking a company
another already researched may import that research after a quick check.

1. Several alert channels from config, own lists, alerts fanned out (in review, not deployed
   until the streak ends Nov 20). `DISCORD_SERVER_CHANNEL_IDS`; commands registered in each
   server; one window, one set of polls and one summary per release; the alert goes to every
   server tracking the company at once.
2. Research once per company across servers, written to each server that tracks it (in
   review, stacked on step 1). A server that starts tracking a company another already
   researched gets its findings at the next research pass, within about an hour, or at
   once with step 3.
3. Importing another server's research at `/track`, after a quick check that it's current
   (in review, stacked on step 2).

Open: Discord's rate limits with many servers posting the same release (one send per server,
then its edits), to be measured.

### CPU use of `/track` and live releases

Not measured yet. First, profile a `/track` run and a replayed release window (the burst
replay) to find where the time goes: Chrome rendering, HTML and PDF parsing, feed parsing on
one-second polls. Then set targets, and measure the change before claiming one.

## Other open work

- Mon Nov 16: review GlobeNewswire's two record-only batch runs (06:58 and 15:58) before NVDA's
  first live article guessing at 16:18 on Tue Nov 17. NVDA's SEC directory trial runs at
  16:19:30 that day.
- Summary quality, plan items 6 and 7. The reference-bound writer keeps 42% of correct values,
  and driver profiles showed no gain, so both stay off and summaries stay beta.
- Source problems (Business Wire's feed delay, Notified IR sites, Intel's and AMD's feeds,
  ROAD's release history and others) are with the source-research agent.
- Owner: pause Windows Update before Nov 17, and decide on Dependabot #119 (Docker
  Python 3.15).

## After 1.0

### Earnings call transcript summaries (`/transcript`)

The owner's idea (Nov 15). `/transcript TICKER` turns on listening for that company's call.
The bot posts a Discord message at once and keeps it updated with status: call found, live,
ended, transcript found, summarizing. Once the call is over, it gets the transcript and runs
the summarizer with the same aim as for releases: the most useful new information from the
call, not a repeat of the release. The summary then replaces the status.

To work out:

- Where transcripts come from and how soon after the call. Options include the company's IR
  site, the webcast provider and transcript services.
- How the bot knows the call has started and ended.
- Muse Ember at low effort only, with a spend cap per call.
