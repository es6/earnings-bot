# Contributing

Thanks for helping out! This project is in early development, so please open an issue to discuss
larger changes before you start on them.

## Setup

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/).
2. Fork and clone the repo, then run `uv sync`.
3. Copy `.env.example` to `.env` and add a token from your own test Discord application
   (see the README).

## Making a change

Read the [Python conventions and pre-1.0 quality review](docs/python-quality.md) for naming,
comments, module boundaries and task/resource ownership. It includes the current structure
diagrams, primary references and concrete review findings.

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
