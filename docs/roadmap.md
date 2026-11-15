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

The full plan is [international.md](international.md); the debug mode it's built with is
[track-testing.md](track-testing.md). Agreed with the owner on Nov 15:

- **Markets:** ASX, Hong Kong, Tokyo, Toronto and Europe's main markets. Any other exchange is
  learned when `/track` identifies one of its tickers and a second source confirms it.
- **Symbols:** `SYMBOL.SUFFIX` with Yahoo Finance's suffixes (`TEA.AX`, `0700.HK`, `7203.T`,
  `SHOP.TO`, `SAF.PA`); `EXCHANGE:SYMBOL` also accepted.
- **US tickers keep today's path.** The new logic runs only for symbols naming a non-US
  exchange, or plain symbols that aren't on SEC's list.
- **Decisions:** OpenFIGI for identification (it named all six test tickers correctly without
  a key); `exchange_calendars` for hours and holidays; Japanese and Chinese releases
  summarized in English by Muse Ember at low effort; new exchanges learned automatically once
  two sources agree.
- **Order:**
  1. Debug mode (`/track ... debug:true`).
  2. Foundations: symbols and the exchange table, a non-SEC company key, half-year periods,
     each exchange's hours, currencies and languages.
  3. Identification.
  4. A dedicated source per market: ASX, Toronto, Hong Kong, Tokyo, then Europe exchange by
     exchange.
  5. Live checks as each market reports.

  The company-key change waits until the streak ends and the multiple-servers PRs merge.

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

## Order of work

Agreed with the owner on Nov 15:

1. **`/track` debug mode** ([track-testing.md](track-testing.md)). Built in v0.21.28 for US
   tickers; it adds an option to `/track` and changes nothing else live.
2. **Verified summaries:** plan and test the summary checker that verifies each number's
   measure and period, aiming to remove the "Beta" label (plan item 6).
3. **After the streak (Nov 20):** merge and deploy multiple servers (#136 to #138), then
   measure Discord's rate limits with several servers.
4. **International tickers:** the foundations, identification, then a dedicated source
   per market ([international.md](international.md)), each going live once its answer
   keys pass one-shot.
5. **After 1.0:** `/transcript`.

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
