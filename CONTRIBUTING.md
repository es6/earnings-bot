# Contributing

Thanks for helping out! This project is in early development, so please open an issue to discuss
larger changes before you start on them.

## Setup

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/).
2. Fork and clone the repo, then run `uv sync`.
3. Copy `.env.example` to `.env` and add a token from your own test Discord application
   (see the README).

## Making a change

1. Create a branch off `main`.
2. Make your change and add or update tests in `tests/`.
3. Run the same checks CI runs:

   ```bash
   uv run ruff check .
   uv run ruff format .
   uv run pyright
   uv run pytest
   ```

4. Open a pull request. CI has to pass before it's merged.

## Secrets

Never commit a Discord token or any other credential. `.env` is gitignored for this reason.
If you leak a token by accident, reset it in the Discord Developer Portal right away.
