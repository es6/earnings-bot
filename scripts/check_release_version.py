"""Require the next major, minor, or patch version for a deployable change."""

import re
import subprocess
import sys
import tomllib

DEPLOYED = (
    "src/earnings_bot",
    "scripts/run-bot.ps1",
    "scripts/update-bot.ps1",
    "Dockerfile",
    "pyproject.toml",
    "uv.lock",
)


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, encoding="utf-8")


def version_at(commit: str) -> tuple[int, int, int]:
    version = tomllib.loads(git("show", f"{commit}:pyproject.toml"))["project"]["version"]
    part = r"(0|[1-9][0-9]*)"
    if not isinstance(version, str) or not (
        match := re.fullmatch(rf"{part}\.{part}\.{part}", version)
    ):
        sys.exit(f"Use a major.minor.patch project version. Got {version!r}.")
    return int(match[1]), int(match[2]), int(match[3])


def check(base: str) -> None:
    if not git("diff", "--name-only", base, "HEAD", "--", *DEPLOYED):
        return
    major, minor, patch = version_at(base)
    allowed = {(major + 1, 0, 0), (major, minor + 1, 0), (major, minor, patch + 1)}
    if version_at("HEAD") not in allowed:
        sys.exit(
            f"This change updates the deployed bot, currently {major}.{minor}.{patch}. "
            "Run uv version --bump patch, minor, or major, then commit pyproject.toml and uv.lock."
        )


if __name__ == "__main__":
    check(sys.argv[1])
