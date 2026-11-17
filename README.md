<img src="assets/first-print.svg" alt="First Print" width="96" align="right">

# First Print

Formerly earnings-bot (renamed November 16, 2026; old GitHub links redirect here).

[![CI](https://github.com/es6/first-print/actions/workflows/ci.yml/badge.svg)](https://github.com/es6/first-print/actions/workflows/ci.yml)
[![Docker](https://github.com/es6/first-print/actions/workflows/docker.yml/badge.svg)](https://github.com/es6/first-print/actions/workflows/docker.yml)

A Discord bot that races public earnings sources, posts a release as soon as it detects it,
then edits in a short summary of the numbers that move the stock.

Type `/track NVDA`. The bot works out where the company publishes (its investor relations
site, its newswire, its SEC filings), when its next results come out and at what time of day,
and watches every one of those routes at once around that time. The first route to show the
release wins. Speed is the whole point: first the time from release to alert, then the time
to a summary.

## What it does

**Finds the sources and the date by itself.**
- `/track` takes any ticker on SEC's list. For a company it hasn't seen, it reads the last
  results release from EDGAR, follows the IR site it names, and checks for a Q4 press release
  feed, an RSS feed or a press release page. Sites that block automated requests are read in
  a hidden Chrome.
- It names the company's wire (Business Wire, PR Newswire or GlobeNewswire) from its past
  releases, or watches all three.
- It finds the next release date from the company's own notices first, then an earnings
  calendar. The time of day comes from when its recent releases actually came out: IR
  timestamps, wire times and SEC acceptance times.
- A scorecard in the channel follows the research and turns green only when every watched
  route has been checked fresh.

**Watches the release window closely, and only then.**
- The window covers the expected time and any other time of day the company has released
  at. Polling is every 15 s far from those times, every 5 s from 5 minutes before, and at full
  speed from 2 minutes before to 10 minutes after.
- All routes race. Before an alert goes out, the bot checks that the headline or text
  actually announces results, so a date notice or an unrelated filing doesn't set it off.
- It gets past slow caches where it can: CDN-cached feeds are requested under fresh path
  spellings, and GlobeNewswire article numbers are guessed around the release time. A wire
  report it can't read directly is recovered from mirrors that are checked to be the same
  report.

**Posts and summarizes.**
- The alert links the release and attaches its files (press release, PDF, supplementary
  data) as they appear.
- When summaries are on, a summary written by Muse Ember (low effort) is edited into the
  alert from the first readable text: headline results, guidance and the KPIs that matter for
  that company. Every number must appear in a quoted passage of the release, and with
  `VERIFIED_SUMMARIES=true` each fact's measure, period and basis must also be stated there,
  in the release's text or tables, or the fact is left out. Summaries stay labeled **Beta**
  until live releases have confirmed the check ([summaries.md](docs/summaries.md)).
- Several Discord servers can each keep their own tracked list. A company that several of
  them track is watched once and posted to each.

**Keeps score and stays honest.**
- Every window is journaled: which route saw the release first, when, and how long the alert
  and summary took. Window reports compare the alert with the
  [@earnings_guy](https://x.com/earnings_guy) post on X.
- A separate watchdog alarms when a window's polling stops or a route is refused.
- `/track NVDA debug:true` reruns all of `/track`'s research from scratch, saving nothing,
  and reports every fact found, unsure or missing. It's how research gets fixed
  ([track-testing.md](docs/track-testing.md)).

## Commands

Replies are private to the member who ran the command.

| Command | Who | What it does |
|---|---|---|
| `/track ticker:NVDA` | Everyone | Adds a company and starts research and its scorecard |
| `/track ticker:NVDA when:2026-11-17T16:20 period:FY2027Q3` | Manage Server | Pins a release time for a fiscal period |
| `/track ticker:NVDA debug:true` | Manage Server, or the bot's owner anywhere | Researches from scratch and reports what it found, saving nothing |
| `/upcoming` | Everyone | The next seven days of releases, with each scorecard's readiness |
| `/untrack ticker:NVDA` | Manage Server | Removes a company |
| `/status` | Everyone | Watcher health: what's live, what's next, problems, the latest benchmark |
| `/ping` | Everyone | Discord connection latency |

Details: [command reference](docs/unattended.md#commands).

## Status

Before 1.0. The [roadmap](docs/roadmap.md) says what 1.0 means.

## Run your own copy

Every copy runs under its own Discord application. You need
[uv](https://docs.astral.sh/uv/getting-started/installation/), which installs the right Python
for you.

1. **Create the application.** In the
   [Developer Portal](https://discord.com/developers/applications), click **New Application**.
   On the **Bot** tab, click **Reset Token** and copy the token. Leave the privileged intents
   off.
2. **Invite it.** Under **OAuth2 → URL Generator**, pick the scopes `bot` and
   `applications.commands` and the permissions *View Channels*, *Send Messages*, *Embed
   Links*, *Attach Files* and *Read Message History*. Open the generated URL.
3. **Configure it.**

   ```bash
   git clone https://github.com/es6/first-print.git
   cd first-print
   cp .env.example .env
   ```

   In `.env`, set `DISCORD_TOKEN`, `DISCORD_CHANNEL_ID` (where alerts go) and
   `SEC_USER_AGENT` (your name and email, which SEC requires). Optional:
   - `DISCORD_GUILD_ID`, so slash commands show up in your test server at once;
   - `DISCORD_STATUS_CHANNEL_ID` for window reports;
   - `DISCORD_SERVER_CHANNEL_IDS` for additional servers' alert channels;
   - `LIVE_SUMMARIES=true` with `META_MODEL_API_KEY` for summaries (a key alone never starts
     paid calls);
   - `FMP_API_KEY` for earnings-calendar dates;
   - `WATCHDOG_WEBHOOK_URL` for the watchdog's alarms.

   `.env.example` explains each one.
4. **Run it.**

   ```bash
   uv run first-print schedules
   ```

   It watches the companies tracked with `/track`, plus any schedule files in `schedules/`.
   Keep the `schedules/*.sqlite3` files: they let a restart resume alerts and scorecards
   without posting anything twice. Docker works too:

   ```bash
   docker build -t first-print .
   docker run --env-file .env -v "${PWD}/schedules:/data" first-print first-print /data
   ```

   The mounted directory must be writable by container user 1000. The Chrome routes for sites that block
   automated requests run only on Windows, in a signed-in session, so a Docker copy works
   without them.

### Running it all the time on Windows

`.\scripts\install-tasks.ps1` registers the bot and its watchdog as Task Scheduler tasks.
They run hidden, start at logon and again every 5 minutes, and won't start a second copy.
`.\scripts\update-bot.ps1` deploys the latest `main`, refusing while an unreleased window is
open or starts within five minutes. Details: [running continuously on Windows](docs/unattended.md#run-continuously-on-windows).

## How it works

[Architecture](docs/architecture.md) covers release detection: what was measured for each
source, the IR platforms, how fresh each feed is, the live runs, scheduling, and the path
from detection to Discord. The [IR access playbook](docs/ir-access-playbook.md) records how
each kind of IR site is reached, [browser IR](docs/browser-ir.md) the Chrome routes, and
[wire recovery](docs/wire-recovery.md) the verified mirrors.

Two command-line tools come with it:
- `uv run first-print-record schedules/<day>.toml` polls every source around a release and
  logs when each one first listed it, to measure which is fastest
  ([latency recorder](docs/unattended.md#latency-recorder)).
- `uv run first-print-replay` replays a past release through the whole flow.

## Development

The [Python quality guide](docs/python-quality.md) explains the project's conventions,
structure and resource ownership, with diagrams and references.

```bash
uv sync               # install
uv run ruff check .   # lint
uv run ruff format .  # format
uv run pyright        # type check
uv run pytest         # test
```

For every pull request, CI lints, type checks and tests on Linux, and tests on Windows. Each PR that changes what gets
deployed bumps the version once (`uv version --bump patch|minor|major`), and CI checks the
bump against the PR's base. See [CONTRIBUTING.md](CONTRIBUTING.md#release-versions).

## License

[MIT](LICENSE)
