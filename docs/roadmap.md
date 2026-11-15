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

Today the bot posts to one channel (`DISCORD_CHANNEL_ID`) and registers its commands on one
test server (`DISCORD_GUILD_ID`). The tables are already kept per channel. Needed:

- Each server sets its own alert channel with a command, and commands are registered
  globally.
- One detection, one text read and one summary per release, whatever the number of servers.
  Only the posts multiply. Polling and model calls mustn't grow with servers.
- The posts go out to every server at once, so the first server's alert isn't held up by the
  others, within Discord's rate limits (to be measured).
- Each server keeps its own tracked list and scorecards. Two servers tracking one company share
  its window and polls.

### Load on the PC from `/track` and live releases

The aim: a heavy release (four or more companies at once) or a `/track` run never slows the
owner's PC down, whether through CPU, memory or leftover processes. Measured on Nov 15 on this
PC (i9-13900K, 32 threads, 64 GB):

| Load | Chrome | The bot itself | Whole PC |
|---|---|---|---|
| Five release windows at once, full speed, real Chrome and sites, 3 min | 1.4 GB median, 1.6 GB peak working set (1.0 GB committed); 1.4 cores peak, about 0.1 on average | 76 MB; 0.1 core peak | CPU 14% median, 27% peak; free RAM unchanged at 35 GB |
| `/track` research for 3 new companies, one at a time as the bot does | 760 MB median, 1.2 GB peak; 2.1 cores peak | 129 MB; 0.9 core peak | CPU 40% peak |
| 40 real release files read at once (PDFs and pages) | — | 106 MB; 1 core (Python runs one thread at a time) | 4.5 s in all |

Done:
- Chrome ends with the bot, and leftovers are cleared at start (#133). A Chrome from Nov 13 was
  still running two days later, beside 1.8 GB of old profiles on disk.
- Chrome runs at below-normal priority, so the owner's programs come first when the CPU is
  busy, under an 8 GB ceiling on the memory its processes may commit together.
- Every minute of a window, the journal records the bot's memory and CPU, Chrome's processes,
  CPU and peak memory, and the PC's free memory (`resources` events).
- Tried and dropped: turning off images in Chrome saved about 3% of its memory and no CPU, and
  BN's rendered results table failed without them.

Open:
- Read the `resources` records from the live streak (Nov 17 to 20).
- A soak test: hours of windows, to catch slow growth in memory, handles or processes.
- When many releases land at once, one release's text can wait seconds behind the others'
  parsing (a PDF read took 0.3 s alone and up to 4 s among 40). That costs summary speed, not
  responsiveness. A separate process for parsing would fix it at some memory cost.

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
