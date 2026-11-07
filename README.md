# earnings-bot

[![CI](https://github.com/es6/earnings-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/es6/earnings-bot/actions/workflows/ci.yml)
[![Docker](https://github.com/es6/earnings-bot/actions/workflows/docker.yml/badge.svg)](https://github.com/es6/earnings-bot/actions/workflows/docker.yml)

A Discord bot that posts company earnings the moment they're released, pulled straight from
investor relations pages and SEC filings, along with a summary of the numbers and KPIs that
matter for each company.

> **Status: early development.** The bot watches the releases listed in a schedule file and
> posts each one: an alert with links within seconds, then the release files, then a summary
> written by Claude. Adding companies with a slash command comes next. See
> [docs/architecture.md](docs/architecture.md) for the design.

## Features

- Watches investor relations sites, SEC EDGAR, Business Wire and guessed file URLs at once,
  and posts as soon as any of them shows the release
- Attaches the release files (press release, supplemental data) as they appear
- Edits in a summary of results, guidance and the KPIs that matter for that company, with
  consensus estimates from [Financial Modeling Prep](https://site.financialmodelingprep.com/)
- Planned: request and manage tracked companies with slash commands

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

### Run continuously on Windows

Run `uv run earnings-bot schedules` to watch every TOML in the directory. Edits and new files
are picked up within a few seconds. A malformed edit keeps the last valid version active.
Use `/watch_status` for the next watch window and the last successful schedule scan. Set
`DISCORD_STATUS_CHANNEL_ID` to a separate channel to receive window reports there. Reports
are also written to `schedules/watch.jsonl`.

Give the bot **Read Message History** in the releases channel. Directory mode keeps a local
`schedules/.earnings-state.sqlite3` file so a restart resumes the original Discord message.
Keep that file with the schedules. New entries should specify `period = "FY2027Q3"` (using
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

[Chrome IR trials](docs/browser-ir.md) are opt-in per release and record-only by default.
For the first Chrome trial, use an **At log on** trigger with **Run only when user is logged
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

## License

[MIT](LICENSE)
