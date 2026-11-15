"""Off-window public HTTP profile A/B; optional curl_cffi, never a live poller setting."""

import argparse
import hashlib
import importlib
import ipaddress
import json
import re
import socket
import sys
import time
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from earnings_bot.browser import browser_host
from earnings_bot.fetch import USER_AGENT
from earnings_bot.http_policy import retry_after
from earnings_bot.text import html_to_text
from earnings_bot.trial_matching import candidates

PIN = "0.17.0"
HEADERS = {"date", "age", "cache-control", "etag", "retry-after", "server", "cf-ray", "x-cache"}
SECRET = re.compile(r"token|password|credential|signature|api.?key|authorization", re.IGNORECASE)


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: str = Field(pattern=r"^[a-z0-9-]{1,50}$")
    url: str
    listing: bool = True
    patterns: list[str] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def public(self) -> "Case":
        browser_host(self.url)
        if any(SECRET.search(key) for key, _ in parse_qsl(urlsplit(self.url).query)):
            raise ValueError("Credential-like parameters are not permitted")
        for pattern in self.patterns:
            if not pattern or len(pattern) > 200:
                raise ValueError("Three bounded identity/period/result patterns are required")
            re.compile(pattern)
        return self


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    off_window_confirmed: bool
    cases: list[Case] = Field(min_length=1, max_length=4)
    budget: int = Field(default=16, ge=1, le=16)

    @model_validator(mode="after")
    def confirmed(self) -> "Plan":
        if not self.off_window_confirmed:
            raise ValueError("Run only after checking the live schedule")
        if len({case.name for case in self.cases}) != len(self.cases):
            raise ValueError("Case names must be unique")
        return self


@dataclass
class Result:
    status: int
    headers: dict[str, str]
    body: bytes
    protocol: str = ""
    ip: str = ""


@cache
def public_ip(host: str) -> str:
    rows = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    ips = sorted({str(row[4][0]) for row in rows})
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError("DNS includes a nonpublic address")
    return next(ip for ip in ips if ":" not in ip)


def transfer(case: Case, profile: str | None) -> Result:
    """Fresh session, no cookies from another sample, proxies, redirects or retry loop.

    Pin validated public DNS with the original hostname retained for TLS/SNI and certificate
    verification. Application transfers are reserved by run(); this is not a TCP-packet count.
    """
    if version("curl_cffi") != PIN:
        raise ValueError("Use the documented pinned research dependency")
    client = importlib.import_module("curl_cffi.requests")
    options = importlib.import_module("curl_cffi").CurlOpt
    host = browser_host(case.url)
    ip = public_ip(host)
    with client.Session(
        curl_options={options.RESOLVE: [f"{host}:443:{ip}"]}, trust_env=False
    ) as session:
        response = session.get(
            case.url,
            impersonate=profile,
            default_headers=False,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
            allow_redirects=False,
            timeout=15,
            stream=True,
        )
        try:
            body = bytearray()
            for chunk in response.iter_content():
                body.extend(chunk)
                if len(body) > 4 * 1024 * 1024:
                    raise ValueError("Decoded response exceeds byte ceiling")
            return Result(
                response.status_code,
                dict(response.headers),
                bytes(body),
                str(response.http_version),
                ip,
            )
        finally:
            response.close()


def _save(path: Path, ledger: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(ledger, indent=1), encoding="utf-8")
    temporary.replace(path)


@contextmanager
def exclusive(folder: Path):
    lock = folder / "running.lock"
    with lock.open("x", encoding="utf-8"):
        pass
    try:
        yield
    finally:
        lock.unlink()


def run(
    plan: Plan,
    folder: Path,
    perform: Callable[[Case, str | None], Result] = transfer,
    *,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(plan.model_dump_json().encode()).hexdigest()
    path = folder / "ledger.json"
    with exclusive(folder):
        ledger = (
            json.loads(path.read_text())
            if path.exists()
            else {
                "plan_sha256": digest,
                "attempts": [],
                "until": {},
                "stopped": [],
                "blocks": {},
            }
        )
        if ledger["plan_sha256"] != digest:
            raise ValueError("This output belongs to a different plan; preserve its budget")
        deadline = now() + 900
        for case in plan.cases:
            host = browser_host(case.url)
            for index, profile in enumerate((None, "chrome", None, "chrome")):
                label = f"{case.name}:{index}"
                if any(row["label"] == label for row in ledger["attempts"]):
                    continue
                hosts = Counter(row["host"] for row in ledger["attempts"])
                if (
                    len(ledger["attempts"]) >= plan.budget
                    or hosts[host] >= 8
                    or host in ledger["stopped"]
                ):
                    break
                wait = max(0, ledger["until"].get(host, 0) - now())
                if now() + wait >= deadline:
                    break
                sleep(wait)
                row = {
                    "label": label,
                    "host": host,
                    "url": case.url,
                    "profile": profile,
                    "at": now(),
                    "curl_cffi": PIN,
                }
                ledger["attempts"].append(row)
                ledger["until"][host] = now() + 5
                _save(path, ledger)  # reserve even a transfer that fails or is interrupted
                started = now()
                try:
                    result = perform(case, profile)
                    headers = {key.lower(): value for key, value in result.headers.items()}
                    row.update(
                        status=result.status,
                        bytes=len(result.body),
                        protocol=result.protocol,
                        ip=result.ip,
                        sha256=hashlib.sha256(result.body).hexdigest(),
                        headers={key: value for key, value in headers.items() if key in HEADERS},
                    )
                    patterns = [re.compile(pattern, re.IGNORECASE) for pattern in case.patterns]
                    entries = candidates(result.body, case.url, patterns) if case.listing else []
                    text = html_to_text(result.body.decode(errors="replace"))
                    row["entry_matches"] = entries
                    row["document_patterns"] = not case.listing and all(
                        p.search(text) for p in patterns
                    )
                    # Status/pattern matches are observations, not accepted reports or speed proof.
                    (folder / f"{case.name}-{index}.body").write_bytes(result.body)
                    if result.status == 403:
                        blocks = ledger["blocks"][host] = ledger["blocks"].get(host, 0) + 1
                        ledger["until"][host] = now() + min(300, 30 * 2 ** (blocks - 1))
                    if result.status == 429 or result.status >= 500 or headers.get("retry-after"):
                        ledger["stopped"].append(host)
                        row["stop_reason"] = "server_backoff"
                        row["retry_after_seconds"] = retry_after(headers.get("retry-after", ""))
                except Exception as error:
                    row["error"] = type(error).__name__
                    code = getattr(error, "code", None)
                    if isinstance(code, int):
                        row["transport_error_code"] = code
                    response = getattr(error, "response", None)
                    status = getattr(response, "status_code", None)
                    if isinstance(status, int):
                        row["response_status"] = status
                    ledger["stopped"].append(host)
                row["seconds"] = round(now() - started, 3)
                _save(path, ledger)
                print(
                    json.dumps(
                        {
                            k: row[k]
                            for k in ("label", "status", "error", "document_patterns")
                            if k in row
                        }
                    ),
                    flush=True,
                )
        return ledger


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--dependency-dir", type=Path, help="Optional isolated pinned curl_cffi install"
    )
    args = parser.parse_args()
    if args.dependency_dir:
        sys.path.insert(0, str(args.dependency_dir.resolve()))
    run(Plan.model_validate_json(args.plan.read_text(encoding="utf-8")), args.out)


if __name__ == "__main__":
    main()
