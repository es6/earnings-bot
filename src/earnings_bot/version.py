"""The package version and the revision loaded by this bot process."""

import os
import re
import subprocess
from contextlib import suppress
from functools import cache
from importlib.metadata import version
from pathlib import Path


@cache
def build_version() -> str:
    revision = os.getenv("EARNINGS_BOT_REVISION", "")
    checkout = Path(__file__).resolve().parents[2]
    if not revision and (checkout / ".git").exists():
        with suppress(OSError, subprocess.SubprocessError):
            revision = subprocess.check_output(
                ["git", "describe", "--always", "--dirty", "--abbrev=10", "--exclude=*"],
                cwd=checkout,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            ).strip()
    package = f"v{version('earnings-bot')}"
    if match := re.fullmatch(r"([0-9a-f]{10,64})(-dirty)?", revision):
        suffix = ".dirty" if match[2] else ""
        return f"{package}+g{match[1][:10]}{suffix}"
    return package
