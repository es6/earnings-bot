# earnings-bot

[![CI](https://github.com/es6/earnings-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/es6/earnings-bot/actions/workflows/ci.yml)
[![Docker](https://github.com/es6/earnings-bot/actions/workflows/docker.yml/badge.svg)](https://github.com/es6/earnings-bot/actions/workflows/docker.yml)

A Discord bot that posts company earnings the moment they're released, pulled straight from
investor relations pages and SEC filings, along with a summary of the numbers and KPIs that
matter for each company.

> **Status: early development.** The bot connects to Discord and answers `/ping`. Earnings
> detection and summaries come next. See [docs/architecture.md](docs/architecture.md) for the
> design.

## Planned features

- Watch investor relations pages and SEC EDGAR for new earnings releases
- Post the release files (press release, slides, supplemental data) to a channel
- Post a formatted summary of results, guidance and company-specific KPIs
- Request and manage tracked companies with slash commands

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

## Development

```bash
uv sync                  # install dependencies into .venv
uv run ruff check .      # lint
uv run ruff format .     # format
uv run pyright           # type check
uv run pytest            # test
```

CI runs the same checks on every pull request. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
