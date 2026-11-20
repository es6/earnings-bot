"""Bounded public response evidence, persisted off the alert's event-loop path."""

import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from first_print.fetch import Response

_FILE = re.compile(r"[0-9a-f]{24}\.(?:negative|positive)\.(?:json|body|article)")
_PRIVATE = re.compile(r"key|token|auth|secret|signature|credential", re.IGNORECASE)
_HEADERS = frozenset(
    {
        "date",
        "age",
        "cache-control",
        "etag",
        "last-modified",
        "expires",
        "cf-cache-status",
        "x-cache",
        "x-cache-hits",
        "x-cache-status",
        "x-cache-remote",
        "x-varnish",
        "cache-status",
    }
)


def public_url(url: str) -> str:
    parts = urlsplit(url)
    query = "&".join(
        part.partition("=")[0] + "=[redacted]"
        if _PRIVATE.search(unquote_plus(part.partition("=")[0]))
        else part
        for part in parts.query.split("&")
    )
    # Credentials and fragments are never part of public route evidence.
    host = parts.hostname or ""
    if parts.port is not None:
        host += f":{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, query, ""))


@dataclass(frozen=True)
class Snapshot:
    key: str
    slot: str
    metadata: dict[str, Any]
    parts: tuple[tuple[dict[str, Any], bytes | None], ...]


class TransitionEvidence:
    def __init__(
        self,
        root: Path,
        *,
        max_routes: int = 16,
        max_body: int = 2 * 1024 * 1024,
        max_bytes: int = 128 * 1024 * 1024,
        checkpoint: float = 30,
    ) -> None:
        if min(max_routes, max_body, max_bytes) <= 0:
            raise ValueError("Evidence limits must be positive")
        self.root = root
        self.max_routes, self.max_body, self.max_bytes = max_routes, max_body, max_bytes
        self.checkpoint = checkpoint
        self.negative: OrderedDict[str, Snapshot] = OrderedDict()
        self.finished: OrderedDict[str, None] = OrderedDict()
        self._saved: dict[str, float] = {}
        self._queue: asyncio.Queue[Snapshot] = asyncio.Queue(maxsize=max_routes * 2)
        self._writer: asyncio.Task[None] | None = None
        self.dropped = 0
        self.evicted = 0
        self.omitted = 0
        self.errors: list[str] = []

    @classmethod
    def for_log(cls, log: Path) -> Self | None:
        for root in (log.resolve().parent, *log.resolve().parents):
            if (root / "pyproject.toml").is_file() and (root / "src/first_print").is_dir():
                return cls(root / ".context/research/source-transitions")
        return None

    def offer(
        self,
        identity: str,
        metadata: dict[str, Any],
        response: Response,
        *,
        positive: bool,
        article: Response | None = None,
    ) -> None:
        """No file I/O or body hashing here. Overflow drops evidence, never an alert."""
        if response.status != 200:
            return
        key = hashlib.sha256(identity.encode()).hexdigest()[:24]
        if key in self.finished:
            return
        parts = []
        budget = self.max_body
        pages = [("body", response)]
        if article is not None:
            pages.append(("article", article))
        for role, page in pages:
            raw = page.body if len(page.body) <= budget else None
            if raw is not None:
                budget -= len(raw)
            part = {
                "role": role,
                "url": public_url(page.url),
                "status": page.status,
                "bytes": len(page.body),
                "response_ms": round(page.elapsed * 1000),
                "headers": {k.lower(): v for k, v in page.headers.items() if k.lower() in _HEADERS},
            }
            if raw is None:
                self.omitted += 1
                part["omitted"] = "body exceeds the bounded capture budget"
            parts.append((part, raw))
        snapshot = Snapshot(key, "positive" if positive else "negative", metadata, tuple(parts))
        if positive:
            if last := self.negative.pop(key, None):
                self._enqueue(last)
            self._enqueue(snapshot)
            self._saved.pop(key, None)
            self.finished[key] = None
            if len(self.finished) > self.max_routes * 8:
                self.finished.popitem(last=False)
        else:
            self.negative[key] = snapshot
            self.negative.move_to_end(key)
            if len(self.negative) > self.max_routes:
                old, evicted = self.negative.popitem(last=False)
                self._saved.pop(old, None)
                self._enqueue(evicted)
            if time.monotonic() - self._saved.get(key, float("-inf")) >= self.checkpoint:
                self._enqueue(snapshot)
                self._saved[key] = time.monotonic()

    def _enqueue(self, snapshot: Snapshot) -> None:
        try:
            self._queue.put_nowait(snapshot)
        except asyncio.QueueFull:
            self.dropped += 1
            return
        if self._writer is None or self._writer.done():
            self._writer = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while not self._queue.empty():
            snapshot = self._queue.get_nowait()
            try:
                await asyncio.to_thread(self._write, snapshot)
            except OSError as error:
                self.errors = [*self.errors[-7:], type(error).__name__]
            finally:
                self._queue.task_done()

    async def flush(self) -> None:
        for snapshot in list(self.negative.values()):
            self._enqueue(snapshot)
        await self._queue.join()
        if self._writer is not None:
            await self._writer

    def _write(self, snapshot: Snapshot) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        # Once positive evidence is durable, later polls/restarts cannot overwrite the pair.
        if (self.root / f"{snapshot.key}.positive.json").exists():
            return
        base = f"{snapshot.key}.{snapshot.slot}"
        parts = []
        for part, raw in snapshot.parts:
            saved = part.copy()
            if raw is not None:
                filename = f"{base}.{part['role']}"
                temporary = self.root / f"{filename}.tmp"
                temporary.write_bytes(raw)
                temporary.replace(self.root / filename)
                saved.update(file=filename, sha256=hashlib.sha256(raw).hexdigest())
            parts.append(saved)
        data = {"version": "public-transition/1", **snapshot.metadata, "parts": parts}
        temporary = self.root / f"{base}.json.tmp"
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.root / f"{base}.json")
        files = [
            p
            for p in self.root.iterdir()
            if p.is_file() and _FILE.fullmatch(p.name.removesuffix(".tmp"))
        ]
        used = sum(p.stat().st_size for p in files)
        route_count = len({p.name.split(".")[0] for p in files})
        if used > self.max_bytes or route_count > self.max_routes * 8:
            routes: dict[str, list[Path]] = {}
            for path in files:
                routes.setdefault(path.name.split(".")[0], []).append(path)
            for group in sorted(routes.values(), key=lambda g: max(p.stat().st_mtime for p in g)):
                for path in group:
                    used -= path.stat().st_size
                    path.unlink()  # only controlled flat filenames inside the evidence root
                self.evicted += 1
                route_count -= 1
                if used <= self.max_bytes and route_count <= self.max_routes * 8:
                    break
