from __future__ import annotations

import email.utils
import hashlib
import html
import http.client
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


USER_AGENT = "ai-radar-mvp/0.1 (+rules-first; contact owner before increasing rate)"

# Bump when the text produced by visible_text() changes: old snapshots would
# otherwise differ from every new fetch and flood the feed with false events.
SNAPSHOT_FORMAT = 2


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    url: str
    tier: str = "S"
    kind: str = "html"
    provider: str = ""
    category: str = "models"
    priority: str = "P2"
    interval_minutes: int = 60
    use_proxy: bool = True
    timeout_seconds: int = 8


@dataclass(frozen=True)
class Event:
    key: str
    source_id: str
    source_name: str
    title: str
    url: str
    event_type: str
    status: str
    confidence: float
    discovered_at: str
    evidence: str
    provider: str = ""
    priority: str = "P2"
    published_at: str = ""


# --------------------------------------------------------------------------
# Page text extraction
# --------------------------------------------------------------------------

_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "svg", "template", "iframe", "canvas", "object",
    "nav", "footer", "aside", "button", "select", "textarea", "dialog",
})
_ALWAYS_SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "template", "iframe", "canvas", "object"})
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
    "source", "track", "wbr",
})
_BLOCK_TAGS = frozenset({
    "address", "article", "blockquote", "body", "br", "caption", "center", "dd", "details",
    "div", "dl", "dt", "fieldset", "figcaption", "figure", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hgroup", "hr", "html", "legend", "li", "main", "menu", "ol", "p",
    "pre", "section", "summary", "table", "tbody", "tfoot", "thead", "title", "tr", "ul",
})
_TIME_TAGS = frozenset({"time", "relative-time", "local-time", "time-ago"})
_SPACE_TRANSLATION = {ord(char): " " for char in "               　"}
# Control, zero-width, bidi and private-use characters (icon fonts render as
# private-use glyphs and would otherwise pollute every line they sit in).
_INVISIBLE = re.compile(
    r"[\u0000-\u0008\u000e-\u001f\u007f-\u009f­​-‏‪-‮⁠-⁤"
    r"⁦-⁯﻿-\U000f0000-\U0010ffff]"
)
_ISO_PREFIX = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")


def _clean_text(value: str) -> str:
    value = value.translate(_SPACE_TRANSLATION)
    value = _INVISIBLE.sub("", value)
    return re.sub(r"\s+", " ", value).strip()


def _iso_date(value: str | None) -> str:
    match = _ISO_PREFIX.match(value or "")
    if not match:
        return ""
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _absolute_link(base: str, href: str | None) -> str:
    value = (href or "").strip()
    if not value or value.lower().startswith(("javascript:", "mailto:", "tel:", "data:", "blob:")):
        return ""
    try:
        joined = urllib.parse.urljoin(base, value) if base else value
    except ValueError:
        return ""
    return joined if joined.lower().startswith(("http://", "https://")) else ""


class PageText(str):
    """Extracted page text; ``links`` maps a line to the first link seen in it."""

    links: dict[str, str]

    def __new__(cls, value: str, links: dict[str, str] | None = None) -> "PageText":
        obj = super().__new__(cls, value)
        obj.links = links or {}
        return obj


class _VisibleText(HTMLParser):
    """Turn HTML into one text line per block-level element.

    Inline markup (links, code, emphasis) is merged into the surrounding line;
    navigation, footers and icon glyphs are dropped. ``<time datetime>`` is
    replaced by its ISO date so listing pages expose machine-readable dates.
    """

    def __init__(self, base_url: str = "", skip: frozenset[str] = _SKIP_TAGS) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hrefs: list[str] = []
        self._base = base_url
        self._skip_set = skip
        self._skip_tag = ""
        self._skip_depth = 0
        self._buffer: list[str] = []
        self._line_href = ""
        self._anchor = ""
        self._in_time = False

    @property
    def stuck_in_skip(self) -> bool:
        return bool(self._skip_tag)

    def flush(self) -> None:
        if self._buffer:
            text = _clean_text("".join(self._buffer))
            if text:
                self.parts.append(text)
                self.hrefs.append(self._line_href)
        self._buffer = []
        self._line_href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._skip_tag:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        attributes = dict(attrs)
        if tag in self._skip_set or ("hidden" in attributes and tag not in _VOID_TAGS):
            self.flush()
            if tag not in _VOID_TAGS:
                self._skip_tag = tag
                self._skip_depth = 1
            return
        if tag in _TIME_TAGS:
            stamp = _iso_date(attributes.get("datetime"))
            if stamp:
                self._buffer.append(f" {stamp} ")
                self._in_time = True
            return
        if tag == "a":
            self._anchor = _absolute_link(self._base, attributes.get("href"))
            if self._anchor and not self._line_href:
                self._line_href = self._anchor
            return
        if tag in ("td", "th"):
            if "".join(self._buffer).strip():
                self._buffer.append(" | ")
            return
        if tag in _BLOCK_TAGS:
            self.flush()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_tag:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth <= 0:
                    self._skip_tag = ""
                    self._skip_depth = 0
            return
        if tag in _TIME_TAGS:
            self._in_time = False
            return
        if tag == "a":
            self._anchor = ""
            return
        if tag in _BLOCK_TAGS:
            self.flush()

    def handle_data(self, data: str) -> None:
        if self._skip_tag or self._in_time:
            return
        if self._anchor and not self._line_href and data.strip():
            self._line_href = self._anchor
        self._buffer.append(data)


def _html_lines(text: str, base_url: str, skip: frozenset[str]) -> tuple[list[str], list[str], bool]:
    parser = _VisibleText(base_url, skip)
    try:
        parser.feed(text)
        parser.close()
    except Exception:  # malformed markup must not abort a whole collection cycle
        pass
    stuck = parser.stuck_in_skip
    parser.flush()
    return parser.parts, parser.hrefs, stuck


def _html_text(text: str, base_url: str = "") -> PageText:
    lines, hrefs, stuck = _html_lines(text, base_url, _SKIP_TAGS)
    if stuck and sum(len(line) for line in lines) < 600 and len(text) > 20000:
        # An unclosed <nav>/<footer> swallowed the page; read it again keeping them.
        lines, hrefs, _ = _html_lines(text, base_url, _ALWAYS_SKIP_TAGS)
    links: dict[str, str] = {}
    for line, href in zip(lines, hrefs):
        if href and line not in links:
            links[line] = href
    return PageText("\n".join(lines), links)


def _decode(payload: bytes, content_type: str = "") -> str:
    charset = ""
    match = re.search(r"charset=([\w-]+)", content_type or "", re.IGNORECASE)
    if match:
        charset = match.group(1)
    else:
        meta = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", payload[:4096], re.IGNORECASE)
        if meta:
            charset = meta.group(1).decode("ascii", errors="ignore")
    if charset and charset.lower().replace("_", "-") not in ("utf-8", "utf8"):
        try:
            return payload.decode(charset)
        except (LookupError, UnicodeDecodeError):
            pass
    return payload.decode("utf-8", errors="replace")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _feed_date(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    stamp = _iso_date(text)
    if stamp:
        return stamp
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return ""
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%d")


def _markup_to_text(markup: str) -> str:
    lines, _, _ = _html_lines(html.unescape(markup) if "&lt;" in markup else markup, "", _ALWAYS_SKIP_TAGS)
    return _clean_text(" ".join(lines))


def _feed_text(text: str, base_url: str = "") -> PageText | None:
    """Render an Atom/RSS feed as dated, readable lines (newest entries first)."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    entries = [element for element in root.iter() if _local_name(element.tag) in ("entry", "item")]
    if not entries:
        return None
    lines: list[str] = []
    links: dict[str, str] = {}
    for entry in entries[:40]:
        fields: dict[str, str] = {}
        link = ""
        for child in entry:
            name = _local_name(child.tag)
            if name == "link":
                href = child.attrib.get("href") or (child.text or "").strip()
                if href and (not link or child.attrib.get("rel", "alternate") == "alternate"):
                    link = _absolute_link(base_url, href)
            elif name in ("title", "updated", "published", "pubdate", "date", "summary", "description", "content", "encoded"):
                fields.setdefault(name, "".join(child.itertext()))
        title = _clean_text(fields.get("title", ""))
        body = _markup_to_text(fields.get("content") or fields.get("encoded") or fields.get("summary") or fields.get("description") or "")
        stamp = _feed_date(fields.get("updated") or fields.get("published") or fields.get("pubdate") or fields.get("date"))
        if body.lower().startswith(title.lower()):
            body = body[len(title):].lstrip(" -–—:.")
        line = _clean_text(f"{title}. {body[:420]}" if title and body else title or body[:420])
        if not line:
            continue
        if stamp:
            lines.append(stamp)
        lines.append(line)
        if link:
            links.setdefault(line, link)
    return PageText("\n".join(lines), links) if lines else None


def _statuspage_text(data: dict) -> str | None:
    status = data.get("status")
    if not isinstance(status, dict) and not isinstance(data.get("incidents"), list):
        return None
    lines: list[str] = []
    for incident in (data.get("incidents") or [])[:10]:
        if not isinstance(incident, dict):
            continue
        stamp = _iso_date(str(incident.get("created_at") or ""))
        updates = incident.get("incident_updates") or []
        latest = updates[0].get("body") if updates and isinstance(updates[0], dict) else ""
        summary = _clean_text(f"Incident: {incident.get('name', '')} ({incident.get('status', '')}). {latest or ''}")
        if stamp:
            lines.append(stamp)
        lines.append(summary)
    for component in (data.get("components") or []):
        if isinstance(component, dict) and component.get("status") not in (None, "operational"):
            lines.append(_clean_text(f"Component {component.get('name', '')}: {component.get('status')}"))
    for maintenance in (data.get("scheduled_maintenances") or [])[:5]:
        if isinstance(maintenance, dict):
            stamp = _iso_date(str(maintenance.get("scheduled_for") or ""))
            if stamp:
                lines.append(stamp)
            lines.append(_clean_text(f"Maintenance: {maintenance.get('name', '')} ({maintenance.get('status', '')})"))
    if not lines and isinstance(status, dict) and status.get("description"):
        lines.append(f"Status: {_clean_text(str(status['description']))}")
    return "\n".join(lines) if lines else None


def _json_text(text: str) -> str | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return _statuspage_text(data) if isinstance(data, dict) else None


def visible_text(payload: bytes, content_type: str = "", base_url: str = "") -> PageText:
    text = _decode(payload, content_type)
    head = text[:600].lstrip().lower()
    lowered_type = (content_type or "").lower()
    if "xml" in lowered_type or head.startswith(("<?xml", "<rss", "<feed")) or "<feed" in head[:500]:
        rendered = _feed_text(text, base_url)
        if rendered is not None:
            return rendered
    if "json" in lowered_type or head.startswith(("{", "[")):
        rendered_json = _json_text(text)
        if rendered_json is not None:
            return PageText(rendered_json)
    return _html_text(text, base_url)


def normalize(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def _keyword_pattern(word: str) -> str:
    """Whole-word match for ASCII keywords; a trailing ``*`` allows a longer word."""
    if re.search(r"[一-鿿]", word):
        return re.escape(word)
    prefix = word.endswith("*")
    body = re.escape(word.rstrip("*")).replace(r"\ ", r"\s+").replace(r"\-", r"[-\s]?")
    return rf"(?<![a-z0-9]){body}" + ("" if prefix else r"(?![a-z0-9])")


_CLASSIFICATION_SOURCE: list[tuple[str, tuple[str, ...], float]] = [
    ("reset", ("rate limit reset", "usage limit reset", "reset usage", "banked reset", "quota reset", "重置额度", "额度重置", "使用限制重置"), 0.99),
    ("security", ("security incident", "vulnerabilit*", "outage*", "data breach", "jailbreak*", "安全事件", "漏洞", "宕机", "事故"), 0.98),
    ("deprecation", ("deprecat*", "retir*", "sunset*", "shut down", "shutting down", "end of life", "discontinu*", "弃用", "下线", "停止支持", "退役"), 0.96),
    ("pricing", ("pricing", "price*", "cost*", "per 1m", "per million", "价格", "计费", "费用"), 0.94),
    ("open_weights", ("open weight*", "open-weight*", "open source", "open-source", "apache 2*", "开源", "开放权重"), 0.93),
    ("cloud_availability", ("amazon bedrock", "azure ai", "azure foundry", "vertex ai", "model catalog", "云平台上架"), 0.91),
    ("policy", ("regulation*", "regulatory", "ai act", "executive order", "compliance", "export control*", "policy", "policies", "法案", "监管", "合规", "政策"), 0.90),
    ("compute", ("gpu*", "accelerator*", "inference chip*", "data center*", "datacenter*", "hbm", "cluster*", "tpu*", "npu*", "算力", "芯片", "数据中心"), 0.88),
    ("tool", ("developer tool*", "coding agent*", "code assistant*", "ide", "agent platform", "开发工具", "编程助手", "智能体平台"), 0.86),
    ("research", ("research paper*", "preprint*", "arxiv", "technical report*", "论文", "预印本", "技术报告"), 0.78),
    ("release", ("released", "releasing", "launch*", "available", "introducing", "now live", "rolling out", "rolls out", "unveil*", "发布", "上线", "推出"), 0.92),
    ("benchmark", ("benchmark*", "leaderboard*", "evaluation*", "state-of-the-art", "sota", "基准", "排行榜", "评测"), 0.84),
    ("industry", ("acquisition*", "acquired", "acquires", "funding", "partnership*", "investment*", "收购", "融资", "合作", "投资"), 0.82),
    ("api", ("api", "apis", "endpoint*", "parameter*", "structured output*", "sdk*", "webhook*", "接口", "参数"), 0.86),
]

CLASSIFICATION_RULES: list[tuple[str, tuple[str, ...], float]] = _CLASSIFICATION_SOURCE
_COMPILED_RULES: list[tuple[str, "re.Pattern[str]", float]] = [
    (event_type, re.compile("|".join(_keyword_pattern(word) for word in words), re.IGNORECASE), confidence)
    for event_type, words, confidence in _CLASSIFICATION_SOURCE
]


def classify_all(text: str) -> list[tuple[str, float]]:
    matches = [(event_type, confidence) for event_type, pattern, confidence in _COMPILED_RULES if pattern.search(text)]
    return matches or [("content_change", 0.55)]


def classify(text: str) -> tuple[str, float]:
    return classify_all(text)[0]


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

class State:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self._prepare()
        except Exception:
            self.db.close()  # a damaged file must be released before it can be set aside (Windows)
            raise

    def _prepare(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                source_id TEXT PRIMARY KEY,
                etag TEXT,
                last_modified TEXT,
                content_hash TEXT NOT NULL,
                content TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                event_key TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                source_name TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL,
                confidence REAL NOT NULL,
                discovered_at TEXT NOT NULL,
                evidence TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                priority TEXT NOT NULL DEFAULT 'P2',
                published_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS source_health (
                source_id TEXT PRIMARY KEY,
                last_success TEXT,
                last_error TEXT,
                error_message TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(events)")}
        if "provider" not in columns:
            self.db.execute("ALTER TABLE events ADD COLUMN provider TEXT NOT NULL DEFAULT ''")
        if "priority" not in columns:
            self.db.execute("ALTER TABLE events ADD COLUMN priority TEXT NOT NULL DEFAULT 'P2'")
        if "published_at" not in columns:
            self.db.execute("ALTER TABLE events ADD COLUMN published_at TEXT NOT NULL DEFAULT ''")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < SNAPSHOT_FORMAT:
            # Snapshots written by an older text extractor cannot be diffed against
            # new fetches; start every source from a fresh baseline instead.
            self.db.execute("DELETE FROM snapshots")
            self.db.execute(f"PRAGMA user_version = {SNAPSHOT_FORMAT}")
        self.db.commit()

    def snapshot(self, source_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM snapshots WHERE source_id = ?", (source_id,)).fetchone()

    def save_snapshot(self, source: Source, content: str, content_hash: str, headers: dict[str, str]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            """INSERT INTO snapshots(source_id, etag, last_modified, content_hash, content, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_id) DO UPDATE SET etag=excluded.etag,
               last_modified=excluded.last_modified, content_hash=excluded.content_hash,
               content=excluded.content, fetched_at=excluded.fetched_at""",
            (source.id, headers.get("etag"), headers.get("last-modified"), content_hash, content, now),
        )
        self.db.commit()

    def touch_snapshot(self, source_id: str) -> None:
        self.db.execute(
            "UPDATE snapshots SET fetched_at = ? WHERE source_id = ?",
            (datetime.now(timezone.utc).isoformat(), source_id),
        )
        self.db.commit()

    def record_success(self, source_id: str) -> None:
        self.db.execute(
            """INSERT INTO source_health(source_id, last_success, consecutive_failures)
               VALUES (?, ?, 0)
               ON CONFLICT(source_id) DO UPDATE SET last_success=excluded.last_success,
               error_message=NULL, consecutive_failures=0""",
            (source_id, datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()

    def record_error(self, source_id: str, message: str) -> None:
        self.db.execute(
            """INSERT INTO source_health(source_id, last_error, error_message, consecutive_failures)
               VALUES (?, ?, ?, 1)
               ON CONFLICT(source_id) DO UPDATE SET last_error=excluded.last_error,
               error_message=excluded.error_message,
               consecutive_failures=source_health.consecutive_failures + 1""",
            (source_id, datetime.now(timezone.utc).isoformat(), message[:300]),
        )
        self.db.commit()

    def health_summary(self, source_ids: Iterable[str] | None = None, since: datetime | None = None) -> dict[str, int]:
        ids = list(source_ids or [])
        if ids:
            placeholders = ",".join("?" for _ in ids)
            rows = self.db.execute(
                f"SELECT * FROM source_health WHERE source_id IN ({placeholders})", ids
            ).fetchall()
            total = len(ids)
        else:
            rows = self.db.execute("SELECT * FROM source_health").fetchall()
            total = len(rows)
        if since is not None:
            cutoff = since.astimezone(timezone.utc).isoformat()
            rows = [row for row in rows if max(row["last_success"] or "", row["last_error"] or "") >= cutoff]
        failed = sum(1 for row in rows if row["consecutive_failures"] > 0)
        checked = len(rows)
        return {"total": total, "checked": checked, "ok": checked - failed, "failed": failed, "pending": max(0, total - checked)}

    def failed_sources(self, limit: int = 10, since: datetime | None = None) -> list[tuple[str, str]]:
        rows = self.db.execute(
            """SELECT source_id, error_message FROM source_health
               WHERE consecutive_failures > 0 AND last_error >= ? ORDER BY last_error DESC LIMIT ?""",
            (since.astimezone(timezone.utc).isoformat() if since else "", limit),
        ).fetchall()
        return [(row["source_id"], row["error_message"] or "unknown error") for row in rows]

    def is_due(self, source: Source) -> bool:
        row = self.snapshot(source.id)
        if not row:
            return True
        try:
            fetched_at = datetime.fromisoformat(row["fetched_at"])
        except (TypeError, ValueError):
            return True
        return datetime.now(timezone.utc) - fetched_at >= timedelta(minutes=max(1, source.interval_minutes))

    def add_event(self, event: Event) -> bool:
        cur = self.db.execute(
            """INSERT OR IGNORE INTO events
               (event_key, source_id, source_name, title, url, event_type, status,
                confidence, discovered_at, evidence, provider, priority, published_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            tuple(asdict(event).values()),
        )
        self.db.commit()
        return cur.rowcount == 1

    def _events(self, rows: list[sqlite3.Row]) -> list[Event]:
        events = []
        for row in rows:
            data = dict(row)
            data["key"] = data.pop("event_key")
            events.append(Event(**data))
        return events

    def recent_events(self, limit: int = 20) -> list[Event]:
        return self._events(self.db.execute(
            "SELECT * FROM events ORDER BY discovered_at DESC LIMIT ?", (limit,)
        ).fetchall())

    def events_since(self, since: datetime, limit: int = 200) -> list[Event]:
        return self._events(self.db.execute(
            "SELECT * FROM events WHERE discovered_at >= ? ORDER BY discovered_at DESC LIMIT ?",
            (since.astimezone(timezone.utc).isoformat(), limit),
        ).fetchall())

    def prune_events(self, older_than: datetime) -> int:
        cursor = self.db.execute(
            "DELETE FROM events WHERE discovered_at < ?", (older_than.astimezone(timezone.utc).isoformat(),)
        )
        self.db.commit()
        return cursor.rowcount

    def has_recent_event(self, source_id: str, event_type: str, hours: int = 6) -> bool:
        row = self.db.execute(
            """SELECT 1 FROM events WHERE source_id = ? AND event_type = ?
               AND discovered_at >= datetime('now', ?) LIMIT 1""",
            (source_id, event_type, f"-{hours} hours"),
        ).fetchone()
        return row is not None

    def close(self) -> None:
        self.db.close()


def load_sources(path: str | Path) -> list[Source]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Source(**item) for item in data]


def export_feed(state: State, path: str | Path, source_ids: Iterable[str] | None = None, limit: int = 50, extra_failed: Iterable[str] = (), checked_since: datetime | None = None, events_since: datetime | None = None) -> dict:
    """Write the local intelligence feed atomically and return its JSON shape."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    ids = list(source_ids) if source_ids is not None else None
    pending_sources = [{"source_id": source_id, "message": "not checked: cycle deadline reached"} for source_id in dict.fromkeys(extra_failed)]
    size = max(0, min(1000, limit))
    events = state.events_since(events_since, size) if events_since is not None else state.recent_events(size)
    feed = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "health": state.health_summary(ids, since=checked_since),
        "failed_sources": [
            {"source_id": source_id, "message": message}
            for source_id, message in state.failed_sources(limit=1000, since=checked_since)
            if ids is None or source_id in ids
        ],
        "pending_sources": pending_sources,
        "events": [asdict(event) for event in events],
    }
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=destination.parent,
            prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(feed, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(3):
            try:
                os.replace(temporary, destination)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.05 * (2 ** attempt))
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    return feed


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

CURL_STATUS_MARKER = "__ai_radar_http_status__"


def _permission_boundary(exc: BaseException) -> bool:
    """True when the runner itself forbade the connection, not the remote host."""
    if isinstance(exc, PermissionError):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, PermissionError)


def _curl_proxy(url: str) -> str:
    """Reuse urllib's proxy route so the fallback changes the TLS stack, not the path."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    try:
        if host and urllib.request.proxy_bypass(host):
            return ""
        proxies = urllib.request.getproxies()
    except OSError:
        return ""
    return proxies.get(parts.scheme) or proxies.get("all") or ""


def _curl_command(source: Source, headers: dict[str, str], timeout: int) -> list[str]:
    command = [
        "curl",
        "--silent",
        "--show-error",
        "--location",
        "--max-time",
        str(max(5, timeout)),
        "--user-agent",
        headers["User-Agent"],
        "--header",
        f"Accept: {headers['Accept']}",
        "--header",
        "Accept-Encoding: identity",
        "--write-out",
        f"\n{CURL_STATUS_MARKER}%{{http_code}}",
    ]
    proxy = _curl_proxy(source.url) if source.use_proxy else ""
    if proxy:
        command.extend(["--proxy", proxy])
    else:
        command.extend(["--noproxy", "*"])
    for name in ("If-None-Match", "If-Modified-Since"):
        if headers.get(name):
            command.extend(["--header", f"{name}: {headers[name]}"])
    command.append(source.url)
    return command


def _curl_fetch(source: Source, headers: dict[str, str], timeout: int) -> tuple[int, str, dict[str, str]]:
    """Fetch through the system curl, which trusts the OS certificate store.

    urllib validates against its own CA bundle, so a TLS-inspecting proxy can
    break urllib while the very same URL still loads in curl.
    """
    completed = subprocess.run(
        _curl_command(source, headers, timeout),
        capture_output=True,
        timeout=max(5, timeout) + 5,
        check=False,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        raise OSError(f"curl exit {completed.returncode}: {detail[:200] or 'no stderr'}")
    payload = completed.stdout or b""
    marker = CURL_STATUS_MARKER.encode("ascii")
    if marker not in payload:
        raise OSError("curl response missing HTTP status")
    payload, _, raw_status = payload.rpartition(marker)
    payload = payload.rstrip(b"\r\n")
    try:
        status = int(raw_status.strip())
    except ValueError as exc:
        raise OSError("curl response has invalid HTTP status") from exc
    if status == 304:
        return 304, "", {}
    if not 200 <= status < 300:
        raise OSError(f"curl HTTP {status}")
    return status, visible_text(payload[:2_000_000], "", source.url), {}


def _fetch(source: Source, previous: sqlite3.Row | dict | None, timeout: int | None = None) -> tuple[int, str, dict[str, str]]:
    timeout = max(1, timeout if timeout is not None else source.timeout_seconds)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.1",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    if previous:
        if previous["etag"]:
            headers["If-None-Match"] = previous["etag"]
        if previous["last_modified"]:
            headers["If-Modified-Since"] = previous["last_modified"]
    request = urllib.request.Request(source.url, headers=headers)
    opener = urllib.request.urlopen if source.use_proxy else urllib.request.build_opener(urllib.request.ProxyHandler({})).open
    try:
        with opener(request, timeout=timeout) as response:
            try:
                response.fp.raw._sock.settimeout(timeout)
            except (AttributeError, OSError):
                pass
            raw = response.read(2_000_000)
            response_headers = {k.lower(): v for k, v in response.headers.items()}
            return response.status, visible_text(raw, response_headers.get("content-type", ""), source.url), response_headers
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, "", {}
        if exc.code == 403 and b"challenges.cloudflare.com" in exc.read(8192):
            raise urllib.error.URLError("blocked: HTTP 403 Cloudflare challenge; browser verification required") from exc
        raise
    except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
        if _permission_boundary(exc):
            raise
        # Change the TLS implementation, preserving the source's selected route.
        try:
            return _curl_fetch(source, headers, timeout)
        except (OSError, subprocess.TimeoutExpired) as fallback_error:
            raise OSError(f"urllib: {exc}; curl: {fallback_error}") from fallback_error


_INTERSTITIAL = re.compile(
    r"just a moment|checking (?:if the site connection is secure|your browser)|enable javascript and cookies|"
    r"you need to enable javascript|please enable javascript|javascript is (?:required|disabled)|"
    r"attention required|access denied|verify you are (?:a )?human|are you a robot|captcha|"
    r"请完成安全验证|安全验证|访问被拒绝|请开启\s*javascript|请启用\s*javascript",
    re.IGNORECASE,
)


def _meaningful(text: str) -> bool:
    words = re.findall(r"[A-Za-z]{3,}|[一-鿿]{2,}", text)
    return len(words) >= 2


def _page_problem(text: str) -> str:
    """Why a fetched page cannot be monitored (empty string when it can)."""
    if len(text) < 2500 and _INTERSTITIAL.search(text):
        return "blocked: browser verification or JavaScript required"
    if not _meaningful(text):
        return "empty: no readable text (the page may need JavaScript)"
    return ""


@dataclass
class FetchResult:
    source: Source
    ok: bool
    status: int = 0
    text: str = ""
    headers: dict = field(default_factory=dict)
    error: str = ""


def fetch_source(source: Source, previous: sqlite3.Row | dict | None, timeout: int | None = None) -> FetchResult:
    """Fetch one source without touching the database; never raises."""
    try:
        status, text, headers = _fetch(source, previous, timeout) if timeout is not None else _fetch(source, previous)
    except Exception as exc:  # one unreachable or odd source must not stop the cycle
        return FetchResult(source, False, error=f"{type(exc).__name__}: {exc}")
    return FetchResult(source, True, status, text, headers)


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------

_MONTH_NUMBERS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
_DATE_FORMS = (
    re.compile(r"(?<!\d)(?P<y>20\d{2})[-/](?P<m>\d{1,2})[-/](?P<d>\d{1,2})(?!\d)"),
    re.compile(r"(?<!\d)(?P<y>20\d{2})\.(?P<m>\d{2})\.(?P<d>\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(?P<y>20\d{2})年\s*(?P<m>\d{1,2})月\s*(?P<d>\d{1,2})日?"),
    re.compile(rf"(?<![A-Za-z])(?P<mon>{_MONTH})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s*(?P<y>20\d{{2}})(?!\d)", re.IGNORECASE),
    re.compile(rf"(?<!\d)(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<mon>{_MONTH})\.?,?\s+(?P<y>20\d{{2}})(?!\d)", re.IGNORECASE),
)
_YEARLESS_DATE = re.compile(rf"(?P<mon>{_MONTH})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\.?", re.IGNORECASE)
_DATE_LABEL_PREFIX = re.compile(r"^(?:last\s+updated|updated|published|posted|date)\s*(?:on\b|:)?\s*:?\s*", re.IGNORECASE)
_WEEKDAY_PREFIX = re.compile(r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?,?\s+", re.IGNORECASE)


def _date_from_match(match: "re.Match[str]") -> datetime | None:
    groups = match.groupdict()
    try:
        month = _MONTH_NUMBERS[groups["mon"][:3].lower()] if groups.get("mon") else int(groups["m"])
        return datetime(int(groups["y"]), month, int(groups["d"]), tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return None


def _date_at(value: str, position: int = 0) -> tuple[datetime, int] | None:
    for form in _DATE_FORMS:
        match = form.match(value, position)
        if match:
            parsed = _date_from_match(match)
            if parsed is not None:
                return parsed, match.end()
    return None


def _date_heading(line: str, default_year: int | None = None) -> tuple[datetime, str] | None:
    """Parse a line that opens with a date; also returns the text after the date."""
    value = line.strip().rstrip(":").strip()
    value = _DATE_LABEL_PREFIX.sub("", value, count=1)
    value = _WEEKDAY_PREFIX.sub("", value, count=1)
    hit = _date_at(value)
    if hit is not None:
        parsed, end = hit
        rest = value[end:].strip()
        core = rest.strip(" ,.;:–—-|·•")
        iso_style = bool(re.match(r"20\d{2}-\d{2}-\d{2}", value))
        if not core or rest[0] in ":：-–—|·•,;" or iso_style or len(rest) <= 40:
            return parsed, core
        return None
    if default_year is not None:
        implicit = _YEARLESS_DATE.fullmatch(value)
        if implicit:
            try:
                month = _MONTH_NUMBERS[implicit.group("mon")[:3].lower()]
                return datetime(default_year, month, int(implicit.group("d")), tzinfo=timezone.utc), ""
            except ValueError:
                return None
    return None


def _parse_date(line: str, default_year: int | None = None) -> datetime | None:
    """Parse a date heading, never guessing the year without page context."""
    heading = _date_heading(line, default_year)
    return heading[0] if heading else None


def _embedded_explicit_date(line: str) -> datetime | None:
    best: tuple[int, datetime] | None = None
    for form in _DATE_FORMS:
        for match in form.finditer(line):
            parsed = _date_from_match(match)
            if parsed is not None:
                if best is None or match.start() < best[0]:
                    best = (match.start(), parsed)
                break
    return best[1] if best else None


def _year_context(line: str, source_id: str = "") -> int | None:
    value = line.strip().rstrip(":")
    ordinary = re.fullmatch(rf"{_MONTH}\.?,?\s+(20\d{{2}})", value, re.IGNORECASE)
    if ordinary:
        return int(ordinary.group(1))
    year_only = re.fullmatch(r"(20\d{2})年?", value)
    if year_only:
        return int(year_only.group(1))
    # Mistral uses compact section labels such as "Sep 25" for September 2025.
    if source_id == "mistral-changelog":
        compact = re.fullmatch(r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{2})", value, re.IGNORECASE)
        if compact:
            return 2000 + int(compact.group(1))
    return None


def recent_dated_blocks(
    text: str,
    days: int = 14,
    max_lines: int = 30,
    future_days: int = 30,
    source_id: str = "",
    reference_time: datetime | None = None,
) -> list[tuple[str, str]]:
    now = reference_time or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(days=max(1, days))
    future_cutoff = now + timedelta(days=max(1, future_days))
    lines = text.splitlines()
    blocks: list[tuple[str, str]] = []
    current_date: datetime | None = None
    current_lines: list[str] = []
    context_year: int | None = None

    def flush() -> None:
        nonlocal current_date, current_lines
        if current_date is not None and cutoff <= current_date <= future_cutoff:
            body = "\n".join(current_lines).strip()
            if _meaningful(body):
                blocks.append((current_date.isoformat(), body))
        current_date = None
        current_lines = []

    for line in lines:
        new_context = _year_context(line, source_id)
        if new_context is not None:
            flush()
            context_year = new_context
            continue
        parsed = _parse_date(line, context_year)
        if parsed is not None:
            flush()
            current_date = parsed
            current_lines = [line]
        elif current_date is not None and len(current_lines) < max_lines:
            current_lines.append(line)
    flush()
    return blocks


def recent_actionable_lines(
    text: str,
    days: int = 14,
    future_days: int = 30,
    reference_time: datetime | None = None,
) -> list[tuple[str, str]]:
    """Keep dated pricing/retirement notices without treating prose dates as headings."""
    now = reference_time or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(days=max(1, days))
    future_cutoff = now + timedelta(days=max(1, future_days))
    action = re.compile(
        r"deprecat|retir|sunset|shut down|pricing|price|cost|increase|discount|promotion ends|"
        r"reset|security incident|outage|effective|comes into force|compliance deadline|"
        r"弃用|退役|下线|价格|涨价|重置|安全事件|宕机|生效|合规期限",
        re.IGNORECASE,
    )
    found: list[tuple[str, str]] = []
    for line in text.splitlines():
        parsed = _embedded_explicit_date(line)
        if parsed is not None and cutoff <= parsed <= future_cutoff and action.search(line):
            found.append((parsed.isoformat(), line.strip()))
    return found


def recent_dated_excerpt(text: str, days: int = 14, max_chars: int = 1800) -> str:
    return "\n".join(body for _, body in recent_dated_blocks(text, days))[:max_chars]


# --------------------------------------------------------------------------
# Turning page changes into events
# --------------------------------------------------------------------------

Segment = tuple[str, list[str]]  # (ISO date or "", lines)

_BOILERPLATE_LINE = re.compile(
    r"^(?:copy(?: page)?|subscribe|share|read more|learn more|view (?:more|all)|see (?:more|all)|"
    r"skip to (?:main )?content|menu|search|close|log ?in|sign ?(?:in|up)|get started|contact sales|"
    r"download|accept(?: all)?|reject(?: all)?|previous|next|back to top|on this page|"
    r"table of contents|was this page helpful\??|status: all systems operational)$",
    re.IGNORECASE,
)
_VOLATILE_LINE = re.compile(
    r"^[+-]?\s*(?:\d[\d,.]*[kKmM]?|\d+\s+(?:seconds?|minutes?|hours?|days?|weeks?|months?)\s*(?:ago)?|"
    r"(?:just now|yesterday|today)|(?:last )?updated\s+.*\bago)$",
    re.IGNORECASE,
)
_STRONG_UNCERTAIN = re.compile(
    r"rumou?r|reportedly|unconfirmed|alleged|\bleak(?:s|ed|ing)?\b|teaser|据传|传闻|爆料|未经证实",
    re.IGNORECASE,
)
_WEAK_UNCERTAIN = re.compile(r"\b(?:may|might|could)\b(?!\s+\d)|considering|或将|考虑|可能", re.IGNORECASE)
_MODEL_WORDS = re.compile(
    r"\bmodels?\b|\b(?:claude[-\s])?(?:opus|sonnet|haiku|fable)[-\s]?\d|"
    r"\b(?:gpt|gemini|qwen|llama|deepseek|mistral|kimi|grok|glm|ernie|hunyuan|gemma|phi)[-\s]?\d",
    re.IGNORECASE,
)


def _stable_evidence(text: str) -> str:
    lines = [line for line in text.splitlines() if not _VOLATILE_LINE.match(line.strip())]
    return "\n".join(lines).strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space >= limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:-–—.") + "…"


def _first_sentence(text: str, limit: int = 110) -> str:
    parts = re.split(r"(?<=[.!?。！？])\s+(?=[\"'“‘(\[A-Z0-9一-鿿])", text.strip(), maxsplit=2)
    sentence = parts[0]
    if len(sentence) < 25 and len(parts) > 1:
        sentence = f"{sentence} {parts[1]}"
    return _truncate(sentence, limit)


def _headline(lines: list[str]) -> str:
    """A readable title taken verbatim from the first informative line."""
    pick = next((line for line in lines[:4] if len(line.split()) >= 3 or len(line) >= 16), lines[0] if lines else "")
    if len(pick) <= 110 and not re.search(r"[.!?。！？]\s+\S", pick):
        return pick.rstrip(" :") or pick
    return _first_sentence(pick)


def _clean_body(lines: list[str], source_id: str = "") -> list[str]:
    body: list[str] = []
    for index, line in enumerate(lines):
        text = line.strip()
        if index == 0:
            heading = _date_heading(text)
            if heading is not None:
                text = heading[1]
            elif _YEARLESS_DATE.fullmatch(text):
                text = ""
        text = re.sub(r"^[•·▪‣\-–]\s+", "", text)
        if len(re.findall(r"[A-Za-z0-9\u4e00-\u9fff]", text)) < 2 or _BOILERPLATE_LINE.match(text):
            continue
        if text in body:
            continue
        body.append(text)
    return body


def _cut_words(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space >= limit * 0.6 else cut).rstrip()


def _limit_evidence(lines: list[str], max_chars: int = 900) -> str:
    kept: list[str] = []
    total = 0
    for line in lines:
        room = max_chars - total
        if room <= 0:
            break
        if len(line) > room:
            if not kept:
                kept.append(_cut_words(line, room))
            break
        kept.append(line)
        total += len(line) + 1
    return "\n".join(kept)


def _site(url: str) -> str:
    """Registrable-looking domain, so docs.example.com and www.example.com count as one site."""
    labels = (urllib.parse.urlsplit(url).hostname or "").lower().split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in ("co", "com", "org", "net", "gov", "ac", "edu"):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _pick_link(source: Source, links: dict[str, str], lines: list[str]) -> str:
    own = _site(source.url)
    for line in lines[:8]:
        href = links.get(line.strip())
        if href and _site(href) == own:
            return href
    return source.url


def _year_marks(lines: list[str], source_id: str) -> list[int | None]:
    """marks[i] is the year context in force after line i (None before any year heading)."""
    marks: list[int | None] = []
    year: int | None = None
    for line in lines:
        context = _year_context(line, source_id)
        if context is not None:
            year = context
        marks.append(year)
    return marks


def _heading_before(lines: list[str], index: int, year: int | None) -> str:
    for position in range(index - 1, max(-1, index - 13), -1):
        parsed = _parse_date(lines[position], year)
        if parsed is not None:
            return parsed.isoformat()
    return ""


def _split_by_dates(lines: list[str], base_date: str = "", year: int | None = None, source_id: str = "") -> list[Segment]:
    segments: list[Segment] = []
    current_date = base_date
    current: list[str] = []
    for line in lines:
        context = _year_context(line, source_id)
        if context is not None:
            year = context
            continue
        parsed = _parse_date(line, year)
        if parsed is not None:
            if current:
                segments.append((current_date, current))
            current_date = parsed.isoformat()
            current = [line]
        else:
            current.append(line)
    if current:
        segments.append((current_date, current))
    return segments


def _backfill_segments(current: str, days: int, source_id: str) -> list[Segment]:
    segments: list[Segment] = [
        (published, body.splitlines()) for published, body in recent_dated_blocks(current, days, source_id=source_id)
    ]
    segments.extend((published, [line]) for published, line in recent_actionable_lines(current, days))
    seen: set[tuple[str, str]] = set()
    unique: list[Segment] = []
    for published, lines in segments:
        marker = (published, "\n".join(lines))
        if marker not in seen:
            seen.add(marker)
            unique.append((published, lines))
    return unique


def _diff_segments(previous: str, current: str, source_id: str = "") -> list[Segment]:
    """Lines that are new on the page, grouped under their date headings.

    A line already present anywhere in the previous snapshot is not news, so
    reordered or moved content never counts as added.
    """
    old_lines = set(previous.splitlines())
    new = current.splitlines()
    if len(new) > 4000:
        new = new[:2000] + new[-2000:]
    added = [index for index, line in enumerate(new) if line not in old_lines]
    # Re-skinned or restructured pages replace most lines at once; that is not news.
    if len(added) > max(60, 0.6 * len(new)):
        return []
    hunks: list[tuple[int, list[str]]] = []
    previous_index = -2
    for index in added:
        if index == previous_index + 1 and hunks:
            hunks[-1][1].append(new[index])
        else:
            hunks.append((index, [new[index]]))
        previous_index = index
    marks = _year_marks(new, source_id)
    segments: list[Segment] = []
    for start, lines in hunks:
        year = marks[start - 1] if start > 0 else None
        segments.extend(_split_by_dates(lines, _heading_before(new, start, year), year, source_id))
    return segments


def _allowed_event_types(source: Source) -> set[str]:
    mapping = {
        "model_release": {"release"},
        "pricing": {"pricing"},
        "pricing_quota": {"pricing", "reset"},
        "api_update": {"api"},
        "deprecation": {"deprecation"},
        "safety_status": {"security"},
        "open_weights": {"open_weights"},
        "cloud_availability": {"cloud_availability"},
        "research_blog": {"release", "open_weights", "benchmark", "security"},
        "industry": {"industry"},
        "product_agent": {"release", "api"},
        "devtool_agent": {"tool", "release", "api", "pricing", "deprecation"},
        "compute_infrastructure": {"compute", "release", "industry"},
        "policy_regulation": {"policy", "security"},
        "research_benchmark": {"research", "benchmark"},
        "business": {"industry"},
        "community_signal": {"content_change"},
    }
    allowed: set[str] = set()
    if source.category == "models":
        return {event_type for event_type, _, _ in CLASSIFICATION_RULES} | {"content_change"}
    for category in source.category.split(","):
        allowed.update(mapping.get(category.strip(), set()))
    return allowed or {"content_change"}


def _priority(_source_priority: str, event_type: str, related_date: str = "", evidence: str = "") -> str:
    """Event severity; source priority controls polling cadence, not news severity."""
    type_priority = {
        "reset": "P0",
        "security": "P0",
        "deprecation": "P1",
        "pricing": "P1",
        "release": "P0",
        "open_weights": "P1",
        "cloud_availability": "P2",
        "policy": "P1",
        "compute": "P2",
        "tool": "P2",
        "research": "P3",
        "api": "P2",
        "benchmark": "P3",
        "industry": "P2",
        "source_error": "P3",
        "content_change": "P3",
    }.get(event_type, "P3")
    priority = type_priority
    # A release that never mentions a model (SDK, CLI, docs) is routine, not urgent.
    if event_type == "release" and evidence and not _MODEL_WORDS.search(evidence):
        priority = "P1"
    if related_date:
        try:
            related = datetime.fromisoformat(related_date)
            if related.tzinfo is None:
                related = related.replace(tzinfo=timezone.utc)
            days_until = (related - datetime.now(timezone.utc)).total_seconds() / 86400
            if event_type == "deprecation":
                if days_until < -1:
                    priority = "P3"
                elif days_until <= 1:
                    priority = "P0"
                elif days_until > 30:
                    priority = "P2"
            elif event_type == "reset" and days_until < -1:
                priority = "P3"
            elif (
                event_type == "pricing"
                and -1 <= days_until <= 1
                and any(word in evidence.lower() for word in ("increase", "increases", "涨价"))
                and not any(word in evidence.lower() for word in ("will not occur", "cancel", "取消"))
            ):
                priority = "P0"
            elif (
                event_type == "policy"
                and -1 <= days_until <= 1
                and any(word in evidence.lower() for word in ("effective", "comes into force", "compliance deadline", "生效", "合规期限"))
            ):
                priority = "P0"
        except (TypeError, ValueError):
            pass
    return priority


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


MAX_SEGMENTS_PER_SOURCE = 10
# Types that deserve their own event; the rest only count when nothing stronger matched.
_HEADLINE_TYPES = frozenset({"deprecation", "pricing", "reset", "security", "release", "open_weights", "policy"})


def _events_from_segments(
    source: Source, state: State, segments: list[Segment], links: dict[str, str], first_snapshot: bool
) -> list[Event]:
    allowed_types = _allowed_event_types(source)
    created: list[Event] = []
    bucket_counts: dict[tuple[str, str], int] = {}
    for published_at, raw_lines in segments[:MAX_SEGMENTS_PER_SOURCE]:
        body = _clean_body(raw_lines, source.id)
        evidence = _limit_evidence(_stable_evidence("\n".join(body)).splitlines())
        if not _meaningful(evidence):
            continue
        if published_at and _parse_iso(published_at) > datetime.now(timezone.utc) + timedelta(days=120):
            published_at = ""  # a mis-read year must not keep an item "current" forever
        if not published_at:
            notices = recent_actionable_lines(evidence, days=14, future_days=90)
            if notices:
                published_at = notices[0][0]
        matches = classify_all(evidence)
        if source.id == "huggingface-models":
            matches = [("content_change", 0.55)]
        matches = [match for match in matches if match[0] in allowed_types]
        if not matches and "safety_status" in source.category:
            matches = [("security", 0.9)]
        if not matches and "content_change" in allowed_types:
            matches = [("content_change", 0.55)]
        uncertain = bool(_STRONG_UNCERTAIN.search(evidence)) or (source.tier == "C" and bool(_WEAK_UNCERTAIN.search(evidence)))
        title = _headline(evidence.splitlines()) or source.name
        url = _pick_link(source, links, raw_lines)
        headline_matches = [match for match in matches if match[0] in _HEADLINE_TYPES]
        for event_type, base_confidence in (headline_matches or matches)[:2]:
            bucket = (event_type, published_at[:10])
            bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
            if bucket_counts[bucket] > 3:
                continue
            status_label = (
                "backfill"
                if first_snapshot
                else "confirmed"
                if source.tier == "S"
                and event_type in {"release", "pricing", "deprecation", "reset", "security", "open_weights"}
                and not uncertain
                else "observe"
            )
            event = Event(
                key=sha256(f"{source.id}:{event_type}:{published_at}:{sha256(evidence)}"),
                source_id=source.id,
                source_name=source.name,
                title=title,
                url=url,
                event_type=event_type,
                status=status_label,
                confidence=(
                    min(base_confidence, 0.55)
                    if uncertain
                    else base_confidence
                    if source.tier == "S"
                    else min(base_confidence, 0.75)
                ),
                discovered_at=datetime.now(timezone.utc).isoformat(),
                evidence=evidence,
                provider=source.provider,
                priority="P3" if uncertain else _priority(source.priority, event_type, published_at, evidence),
                published_at=published_at,
            )
            if state.add_event(event):
                created.append(event)
    return created


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------

def _apply_fetch(
    source: Source, state: State, previous: sqlite3.Row | None, result: FetchResult, backfill_days: int
) -> list[Event]:
    if not result.ok:
        # Collection failures belong to source health, not the news feed.
        state.record_error(source.id, result.error)
        return []
    if result.status == 304:
        state.record_success(source.id)
        state.touch_snapshot(source.id)
        return []
    text = result.text or ""
    problem = _page_problem(text)
    if problem:
        state.record_error(source.id, problem)
        return []
    state.record_success(source.id)
    links = dict(getattr(text, "links", {}) or {})
    current = normalize(text)
    content_hash = sha256(current)
    if previous and previous["content_hash"] == content_hash:
        state.touch_snapshot(source.id)
        return []
    first_snapshot = previous is None
    if first_snapshot:
        if backfill_days <= 0 or "community_signal" in source.category:
            segments: list[Segment] = []
        else:
            segments = _backfill_segments(current, backfill_days, source.id)
    else:
        segments = _diff_segments(previous["content"], current, source.id)
    events = _events_from_segments(source, state, segments, links, first_snapshot) if segments else []
    # Saved last: if event creation fails the same change is retried next cycle.
    state.save_snapshot(source, current, content_hash, result.headers)
    return events


def apply_fetch(
    source: Source, state: State, previous: sqlite3.Row | None, result: FetchResult, backfill_days: int = 0
) -> list[Event]:
    try:
        return _apply_fetch(source, state, previous, result, backfill_days)
    except Exception as exc:  # a page we cannot interpret must not fail the whole cycle
        state.record_error(source.id, f"internal: {type(exc).__name__}: {exc}")
        return []


def collect_events(source: Source, state: State, backfill_days: int = 0, force: bool = False) -> list[Event]:
    if not force and not state.is_due(source):
        return []
    previous = state.snapshot(source.id)
    result = fetch_source(source, previous)
    return apply_fetch(source, state, previous, result, backfill_days)


def collect(source: Source, state: State, backfill_days: int = 0, force: bool = False) -> Event | None:
    events = collect_events(source, state, backfill_days=backfill_days, force=force)
    return events[0] if events else None


def collect_all(
    sources: list[Source],
    state: State,
    backfill_days: int = 0,
    workers: int = 8,
    per_host: int = 2,
    deadline_seconds: float = 1200,
    source_timeout: int = 30,
    min_timeout: int = 20,
    report: list[dict] | None = None,
) -> tuple[list[Event], list[str]]:
    """Fetch every source concurrently; database work stays on the calling thread.

    Returns the new events and the ids of sources not reached before the deadline.
    When ``report`` is given it receives one row per source (outcome, reason, timing).
    """
    deadline = time.monotonic() + max(1.0, deadline_seconds)
    gates: dict[str, threading.BoundedSemaphore] = {}
    gates_lock = threading.Lock()

    def gate_for(url: str) -> threading.BoundedSemaphore:
        host = urllib.parse.urlsplit(url).hostname or ""
        with gates_lock:
            return gates.setdefault(host, threading.BoundedSemaphore(max(1, per_host)))

    def task(source: Source, previous: dict | None) -> tuple[FetchResult, float]:
        timeout = max(1, min(max(source.timeout_seconds, min_timeout), source_timeout))
        with gate_for(source.url):
            started = time.monotonic()
            return fetch_source(source, previous, timeout), time.monotonic() - started

    created: list[Event] = []
    pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="ai-fetch")
    try:
        meta = {}
        for source in sources:
            row = state.snapshot(source.id)
            future = pool.submit(task, source, dict(row) if row is not None else None)
            meta[future] = (source, row)
        remaining = set(meta)
        while remaining:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            done, remaining = wait(remaining, timeout=min(left, 5.0), return_when=FIRST_COMPLETED)
            for future in done:
                source, row = meta[future]
                try:
                    result, seconds = future.result()
                except Exception as exc:  # fetch_source never raises; this is a last resort
                    result, seconds = FetchResult(source, False, error=f"{type(exc).__name__}: {exc}"), 0.0
                new = apply_fetch(source, state, row, result, backfill_days)
                created.extend(new)
                if report is not None:
                    health = state.db.execute(
                        "SELECT consecutive_failures, error_message FROM source_health WHERE source_id = ?", (source.id,)
                    ).fetchone()
                    failed = bool(health and health["consecutive_failures"] > 0)
                    report.append({
                        "id": source.id, "name": source.name, "url": source.url, "ok": not failed,
                        "error": (health["error_message"] if failed else "") or "",
                        "new_events": len(new), "seconds": round(seconds, 1),
                    })
        pending = [meta[future][0].id for future in remaining]
        if report is not None:
            report.extend({"id": meta[future][0].id, "name": meta[future][0].name, "url": meta[future][0].url,
                           "ok": False, "error": "not reached before the cycle deadline", "new_events": 0, "seconds": 0}
                          for future in remaining)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return created, pending


def render_digest(events: Iterable[Event], max_items: int = 5) -> str:
    all_events = sorted(events, key=lambda event: (int(event.priority[1:]), -event.confidence))
    items = [event for event in all_events if event.priority == "P0"]
    selected = {event.key for event in items}
    groups = [
        ({"deprecation"}, 4),
        ({"pricing", "reset"}, 4),
        ({"release", "open_weights"}, 5),
        ({"api", "cloud_availability", "tool"}, 4),
        ({"compute", "policy", "industry"}, 4),
        ({"security", "benchmark", "research"}, 3),
        ({"content_change"}, 2),
    ]
    for event_types, cap in groups:
        for event in [candidate for candidate in all_events if candidate.event_type in event_types and candidate.key not in selected][:cap]:
            if len(items) >= max_items and event.priority != "P0":
                break
            items.append(event)
            selected.add(event.key)
    if not items:
        return "AI 雷达：本轮没有发现新的可核验变化。"
    lines = ["模型观察｜AI 行业情报", ""]
    for event in items:
        label = {"deprecation": "弃用", "pricing": "价格", "release": "发布", "api": "API", "reset": "额度重置", "security": "安全", "open_weights": "开放权重", "cloud_availability": "云平台", "benchmark": "评测", "industry": "产业", "policy": "政策", "compute": "算力", "tool": "工具", "research": "研究"}.get(event.event_type, "观察")
        lines.extend(
            [
                f"[{event.priority}｜{label}] {event.title}",
                f"机构：{event.provider or event.source_name}",
                f"状态：{event.status}｜置信度：{event.confidence:.0%}",
                *([f"相关日期：{event.published_at[:10]}"] if event.published_at else []),
                f"证据：{event.evidence[:220]}",
                f"来源：{event.url}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()
