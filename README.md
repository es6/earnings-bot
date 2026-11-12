# earnings-bot

[![CI](https://github.com/es6/earnings-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/es6/earnings-bot/actions/workflows/ci.yml)
[![Docker](https://github.com/es6/earnings-bot/actions/workflows/docker.yml/badge.svg)](https://github.com/es6/earnings-bot/actions/workflows/docker.yml)

A Discord bot that posts company earnings the moment they're released, pulled straight from
investor relations pages and SEC filings, along with a summary of the numbers and KPIs that
matter for each company.

In directory mode, `/track` adds any company with a ticker on SEC's list to the
earnings calendar. The bot finds the next release date, prepares the sources, and keeps an editable Discord
scorecard. See the [calendar guide](docs/unattended.md) and [architecture](docs/architecture.md).
The optional summary writer uses a compact, business-aware policy. See
[summary research](docs/summaries.md) for the feedback rules, offline evaluation commands
and the Sonnet/Ember comparison; these research tools make no API calls.

## Features

- Races configured IR, newswire and SEC sources in parallel and alerts on the first eligible
  release. Trial sources can record separate timing.
- Attaches the release files (press release, supplemental data) as they appear
- Edits in a summary of results, guidance and the KPIs that matter for that company, with
  consensus estimates from [Financial Modeling Prep](https://site.financialmodelingprep.com/)
- Tracks the latest release timing by source and compares Discord alerts with @earnings_guy

## Run your own copy

Every copy of the bot runs under its own Discord application. You'll need
[uv](https://docs.astral.sh/uv/getting-started/installation/), which installs the right Python
version for you.

1. **Create the Discord application.** Go to the
   [Developer Portal](https://discord.com/developers/applications) and click **New Application**.
   On the **Bot** tab, click **Reset Token** and copy the token. Leave the privileged intents off.
2. **Invite the bot to your server.** Under **OAuth2 → URL Generator**, pick the scopes `bot` and
   `applications.commands` and the permissions *View Channels*, *Send Messages*, *Embed Links* and
   *Attach Files*. Open the generated URL.
3. **Configure it.**

   ```bash
   git clone https://github.com/es6/earnings-bot.git
   cd earnings-bot
   cp .env.example .env
   ```

   Put your token in `.env`. Setting `DISCORD_GUILD_ID` to your test server's ID makes slash
   commands show up there instantly.
4. **Run it.**

   ```bash
   uv run earnings-bot
   ```

   Or with Docker:

   ```bash
   docker build -t earnings-bot .
   docker run --env-file .env earnings-bot
   ```
5. **Watch a day's releases.** Fill in `DISCORD_CHANNEL_ID`, `SEC_USER_AGENT`,
   `ANTHROPIC_API_KEY` and `FMP_API_KEY` in `.env` (see `.env.example`), then give the bot a
   schedule file:

   ```bash
   uv run earnings-bot schedules/2026-11-04.toml
   ```

   It waits for each release's window and posts to the channel. Without the Anthropic key, posts
   have no summary; without the FMP key, summaries have no estimates. Every step is logged to
   `schedules/*.jsonl`, the same log the latency recorder writes.

   To test the whole flow on a past release, replay it:

   ```bash
   uv run earnings-bot-replay schedules/2026-11-04.toml UBER --date 2026-08-05 --q4-release 1585 --edgar 0001543151-26-000027
   ```

### Earnings calendar commands

Use `/track ticker:NVDA` in directory mode. `/upcoming` is a compact calendar of the next
seven days: one line per release by day and market session, with its scorecard's readiness,
and what needs attention (problems, issuers awaiting a date) first. Members with Manage Server can set a release time or remove a subscription
with `/untrack`. See the [command reference](docs/unattended.md#commands) for exact syntax.
The `/track` scorecard updates in place and marks research complete once source checks and
date discovery finish. It shows whether a watch is scheduled or still awaiting a date.

### Run continuously on Windows

Run `uv run earnings-bot schedules` to watch every TOML in the directory. Edits and new files
are picked up within a few seconds. A malformed edit keeps the last valid version active.
Use `/status` (formerly `/watch_status`, which still works) for health: the running release
version and Git build, the last schedule scan, the next watch start, how many scorecards are
ready, whether live summaries are on, and problems. The release version also appears on scorecards, at startup, and in
`uv run earnings-bot --version`. Set `DISCORD_STATUS_CHANNEL_ID` to receive window reports in
a channel, which can be the alerts' own. Reports are also written to `schedules/watch.jsonl`.

Window reports use **@earnings_guy** as the main external speed benchmark. The runner
checks a shared public RSS timeline during release windows and verifies candidate results
posts through X's public embed service. It compares the X post's creation time with the
actual Discord alert time. The most recent comparison is in `/status`. The journal
records the X link, first sighting and when usable release text arrived. The status distinguishes
an unmatched post from an unavailable lookup or an unverified candidate. Benchmark requests
run alongside the release watchers.

Give the bot **Read Message History** in the earnings and status channels. Keep
`schedules/.earnings-state.sqlite3` and `schedules/.tracking-progress.sqlite3` with the
schedules so a restart resumes alerts and scorecards. New TOML entries should specify
`period = "FY2027Q3"` (using
the issuer's fiscal year and quarter) so moving a window to another day keeps the same
identity. Older entries use the issuer's CIK and Eastern release date.

For Windows Task Scheduler, create a task that starts at system startup and launches
`powershell.exe` with arguments like
`-NoProfile -File "C:\earnings-bot\scripts\run-bot.ps1"`. Use the bot owner's Windows
account, with access to the checkout, its `.env`, and `uv`. Select **Run whether user is
logged on or not** for startup monitoring, and **Do not start a new instance** for
overlapping task launches. Disable the task's execution time limit. The script restarts
the bot after it exits and
writes its console output to `recordings/`. Run the script manually first to check its
configuration, then stop that copy before starting the scheduled task.

To put the latest `main` on the running bot, run `.\scripts\update-bot.ps1`. It refuses if
the checkout isn't a clean `main`, or if a release window is open, or opens within 5 minutes,
with its alert not posted yet. Otherwise it stops the bot, fast-forwards to `main`, starts the
scheduled task again and waits until the bot says monitoring has started. Add `-Wait` to
restart as soon as the window allows, `-Restart` to restart with nothing new, or `-Force` to
restart during a window.

[Chrome IR sources](docs/browser-ir.md) can be prepared by the planner or configured per release.
They record timing by default.
For Chrome, use an **At log on** trigger with **Run only when user is logged
on**, unless you've already verified Chrome under the unattended startup account. Use a single
task. Other sources keep running if Chrome cannot start.

## Development

```bash
uv sync                  # install dependencies into .venv
uv run ruff check .      # lint
uv run ruff format .     # format
uv run pyright           # type check
uv run pytest            # test
```

CI runs the same checks on every pull request. See [CONTRIBUTING.md](CONTRIBUTING.md).

### Latency recorder

To measure which source shows a release first, the recorder polls every source around a
scheduled release and logs when each one first lists it:

```bash
uv run earnings-bot-record schedules/2026-11-02.toml
```

The plan file lists each release's expected time (US Eastern) and where to look. The recorder
waits for each window, prints new items as they appear, and prints a summary table when the window
closes. Every event is appended to a JSON Lines log next to the plan (`schedules/*.jsonl`, not
committed). It needs `SEC_USER_AGENT` in `.env`.

Times don't come from your computer's clock, which can be seconds off. The recorder asks NTP time
servers (Cloudflare, Google and NIST) when it starts, when each window opens and every five
minutes during a window, and counts forward from their answer in between.

## Release versions

Run one version bump for each PR that changes what gets deployed. `uv version` updates both
`pyproject.toml` and `uv.lock` so the package, Discord and CLI use the same version.

| Change | Command | Example |
| --- | --- | --- |
| Breaking change | `uv version --bump major` | `0.2.1` → `1.0.0` |
| New feature or major bug fix | `uv version --bump minor` | `0.2.1` → `0.3.0` |
| Routine fix, maintenance, or dependency update | `uv version --bump patch` | `0.2.1` → `0.2.2` |

CI checks the bump against the PR's base. If another release lands first, update from `main` and
pick the next version. Docs and tests can change on their own without a version bump.

## License

[MIT](LICENSE)
