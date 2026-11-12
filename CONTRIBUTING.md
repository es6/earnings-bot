# Contributing

Thanks for helping out! This project is in early development, so please open an issue to discuss
larger changes before you start on them.

## Setup

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/).
2. Fork and clone the repo, then run `uv sync`.
3. Copy `.env.example` to `.env` and add a token from your own test Discord application
   (see the README).

## Making a change

For IR discovery, blocked sites, or stale `/track` sources, read the
[IR access experiment playbook](docs/ir-access-playbook.md) before investigating.
Record evidence and unresolved freshness questions using its case template.

1. Create a branch off `main`.
2. Make your change and add or update tests in `tests/`. Check that each new test fails
   without your change (undo the fix, run the test, put the fix back): a test that passes
   either way doesn't protect anything. For changes that could affect how fast a release is
   seen, compare `uv run python -m tests.burst_replay` before and after.
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
