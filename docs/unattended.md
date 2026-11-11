# Unattended earnings calendar

## Commands

Start the bot in directory mode with `uv run earnings-bot schedules`. Calendar
commands are available in the server that contains the configured earnings channel.
Replies to the commands are private to the person who ran them.

| Command | Access | Result |
|---|---|---|
| `/track ticker:NVDA` | Server members | Adds any company on SEC's ticker list and starts date discovery and the source audit. Repeating it returns the existing subscription. |
| `/track ticker:NVDA when:2026-11-17T16:20 period:FY2027Q3` | Manage Server | Sets a future release time for a fiscal period. Supply both fields. An ISO time without an offset uses US Eastern. |
| `/upcoming` | Server members | Shows scheduled releases in the next seven days and issuers awaiting dates. |
| `/untrack ticker:NVDA` | Manage Server | Removes the calendar subscription. Any matching TOML schedule remains active. |
| `/watch_status` | Server members | Shows watcher health, including the next window, last scan and most recent benchmark result in directory mode. |
| `/ping` | Server members | Checks the bot's Discord connection and reports Gateway latency. |

The calendar holds up to 50 subscriptions per earnings channel.

Companies in the [ticker catalog](../src/earnings_bot/companies.toml) start
from researched sources: the issuer's feeds, its wire and URL patterns. Any
other ticker on SEC's list is looked up when it's added, and the bot then
investigates it outside release windows:

1. It reads the company's last results release from EDGAR and takes the IR
   site the release names. A 6-K requires a results announcement and financial
   figures in its exhibit before it can count as an earnings release.
2. It checks that site for Q4's press release feed, then for an RSS feed. Either
   one is polled directly in release windows.
3. Desktop Chrome always opens the site, finds its press release list and reads
   the latest results there. Once a date is known and last quarter's headline
   matches the upcoming quarter's pattern, Chrome reads that list live in the
   window too.
4. The dateline of its latest results, such as "/PRNewswire/", names its wire.
   Until one is found, both Business Wire and PR Newswire are watched, matching
   the exchange and ticker that open every wire release, such as
   "(NASDAQ: LCID)".

The scorecard is edited as each step runs. When the research finishes it shows
which source had the last results first, every source it will watch for the next
release, and each source it checked and rejected, with the reason. The investigation runs again a month later. The date comes
from the issuer's own notice, including a date announcement furnished on 6-K or
published through its newswire. The planner also reads dated earnings events
on IR calendars and keeps tentative dates pending. FMP's earnings calendar can supply a date when `FMP_API_KEY` is set. The bot
estimates the time from recent results filings and uses a wider window. The
scorecard explains when work is paused for release monitoring and when a
Manage Server member can supply `when` and `period`.

## From ticker to alert

1. **Add the issuer.** The bot saves the subscription by issuer CIK. It creates
   one scorecard and edits that message as the date and source findings change.
   Routine date, source-link and schedule changes update that card and stay in
   the journal. Startup, errors and window-close reports remain separate messages.
2. **Find the next date.** The planner looks for an issuer results notice through
   supported feeds or public IR listings, or for a company outside the catalog,
   uses FMP's calendar date. An exact release time uses a 10-minute lead
   for 8-K issuers and a 30-minute lead for 6-K issuers, with 30 minutes of
   follow-up. A confirmed day with an approximate time uses a wider window.
   Publisher timings from a prior quarter can move the window earlier. Pending
   issuers are rechecked roughly hourly outside release windows.
   Before the speed audit finishes, a verified results-article timestamp can
   already replace the SEC acceptance-time estimate for the first plan.
3. **Audit the last release.** The scorecard separates measured report-text and
   headline timings from archive timestamp estimates. It includes the
   `@earnings_guy` comparison when X verifies a matching post. Audits run one at
   a time outside release windows and refresh after a monitored release.
4. **Prepare and watch.** The watcher picks up a confirmed window within a few
   seconds. A matching TOML entry supplies its source settings and release key.
   The configured sources race in parallel. The first verified release sighting
   starts the Discord post, which is edited as links, files and any summary arrive.
   For delivered alerts, late sources are checked until the follow-up cutoff, including
   after a restart.
5. **Keep tracking.** After the window, the watcher reports source timing and
   the benchmark result. The subscription remains in place for the next quarter.

Windows Chrome research can inspect public issuer JSON and use selected listings
to find notices. The planner can prepare a live browser source for the upcoming
period after retrieving the previous quarter's report. See [browser IR](browser-ir.md)
for per-release schedule settings.

## Run and recover

- Set `DISCORD_CHANNEL_ID` and `SEC_USER_AGENT`. Set
  `DISCORD_STATUS_CHANNEL_ID` to a channel in the same server for scorecards and
  window reports; it can be the alerts' own channel. With only the earnings channel
  configured, scorecards appear there and window reports remain in the journal.
- Changing `DISCORD_CHANNEL_ID` moves the calendar with it at the next start: the
  tracked companies, their research, the alerts and windows already done, and the
  scorecards' records, from the channel that tracked the most companies.
- Give the bot **Read Message History** in both channels it uses. On restart it
  can recover an alert or scorecard that Discord accepted before saving its ID.
- Keep `.earnings-state.sqlite3`, `.tracking-progress.sqlite3`, and `watch.jsonl`
  in the schedule directory. These hold calendar and delivery state, scorecard
  delivery state, and timing history respectively. Keep any optional TOML
  schedules there as well.
- On Windows, [configure Task Scheduler](../README.md#run-continuously-on-windows)
  to run the supervisor. `scripts/run-bot.ps1` restarts the bot after an exit
  and writes console logs to `recordings/`. `scripts/update-bot.ps1` moves the
  checkout to the latest `main` and restarts the bot on it, unless that would
  interrupt a release window whose alert isn't posted yet.
- After an outage, a release already out can still be caught during its open
  window. Windows that expired during the gap are reported in the status log.
