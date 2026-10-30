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
