"""Bind a trial candidate to one feed/JSON entry or HTML link, never a whole listing."""

import json
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.href = ""
        self.parts: list[str] = []
        self.entries: list[dict[str, str]] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag == "a":
            self.href = dict(attrs).get("href") or ""
            self.parts = []

    def handle_data(self, data: str) -> None:
        if self.href and not self.hidden:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag == "a" and self.href:
            self.entries.append({"id": self.href, "title": " ".join(self.parts)})
            self.href = ""
            self.parts = []


def _entries(body: bytes) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(body)
        entries = []
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1] not in {"item", "entry"}:
                continue
            fields = {child.tag.rsplit("}", 1)[-1]: child for child in node}
            link = fields.get("link")
            title = fields.get("title")
            entries.append(
                {
                    "id": (link.get("href", "") or link.text or "") if link is not None else "",
                    "title": "".join(title.itertext()) if title is not None else "",
                    "text": " ".join(node.itertext()),
                }
            )
        if entries:
            return entries
    except ET.ParseError:
        pass
    try:
        value = json.loads(body)
        if isinstance(value, dict):
            value = next(
                (
                    value[k]
                    for k in ("items", "results", "data", "GetPressReleaseListResult")
                    if isinstance(value.get(k), list)
                ),
                [],
            )
        if isinstance(value, list):
            return [
                {
                    "id": str(
                        next(
                            (
                                row[k]
                                for k in ("url", "link", "LinkToDetailPage", "id")
                                if row.get(k)
                            ),
                            "",
                        )
                    ),
                    "title": str(row.get("title", row.get("Headline", ""))),
                    "text": json.dumps(row, ensure_ascii=False),
                }
                for row in value
                if isinstance(row, dict)
            ]
    except (ValueError, UnicodeDecodeError):
        pass
    parser = _Links()
    parser.feed(body.decode("utf-8", errors="replace"))
    return parser.entries


def candidates(body: bytes, base: str, patterns: list[re.Pattern[str]]) -> list[dict[str, str]]:
    out = []
    seen = set()
    for entry in _entries(body):
        if not entry["id"] or not all(
            p.search(entry.get("text", entry["title"])) for p in patterns
        ):
            continue
        identity = urljoin(base, entry["id"])
        parts = urlsplit(identity)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username:
            continue
        if identity in seen:
            continue
        seen.add(identity)
        out.append({"id": identity, "title": entry["title"]})
    return out
