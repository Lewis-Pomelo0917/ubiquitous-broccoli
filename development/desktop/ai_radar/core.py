from __future__ import annotations

import difflib
import hashlib
import html
import http.client
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


USER_AGENT = "ai-radar-mvp/0.1 (+rules-first; contact owner before increasing rate)"


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


class _VisibleText(HTMLParser):
    _ignored = {"script", "style", "noscript", "svg", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._ignored:
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._ignored and self.depth:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.depth:
            value = re.sub(r"\s+", " ", html.unescape(data)).strip()
            if value:
                self.parts.append(value)


def visible_text(payload: bytes, content_type: str = "") -> str:
    text = payload.decode("utf-8", errors="replace")
    if "xml" in content_type.lower() or text.lstrip().startswith("<rss") or "<feed" in text[:500]:
        return text
    parser = _VisibleText()
    parser.feed(text)
    return "\n".join(parser.parts)


def normalize(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


CLASSIFICATION_RULES: list[tuple[str, tuple[str, ...], float]] = [
        ("reset", ("rate limit reset", "usage limit reset", "reset usage", "banked reset", "quota reset", "重置额度", "额度重置", "使用限制重置"), 0.99),
        ("security", ("security incident", "vulnerability", "outage", "data breach", "jailbreak", "安全事件", "漏洞", "宕机", "事故"), 0.98),
        ("deprecation", ("deprecat", "retir", "sunset", "shut down", "弃用", "下线", "停止支持", "退役"), 0.96),
        ("pricing", ("pricing", "price", "cost", "per 1m", "价格", "计费", "费用"), 0.94),
    ("open_weights", ("open weight", "open-weight", "open source", "open-source", "apache 2", "开源", "开放权重"), 0.93),
    ("cloud_availability", ("amazon bedrock", "azure ai", "azure foundry", "vertex ai", "model catalog", "云平台上架"), 0.91),
    ("policy", ("regulation", "regulatory", "ai act", "executive order", "compliance", "export control", "policy", "法案", "监管", "合规", "政策"), 0.90),
    ("compute", ("gpu", "accelerator", "inference chip", "data center", "datacenter", "hbm", "cluster", "tpu", "npu", "算力", "芯片", "数据中心"), 0.88),
    ("tool", ("developer tool", "coding agent", "code assistant", "ide", "agent platform", "开发工具", "编程助手", "智能体平台"), 0.86),
    ("research", ("research paper", "preprint", "arxiv", "technical report", "论文", "预印本", "技术报告"), 0.78),
    ("release", ("released", "launch", "available", "introducing", "发布", "上线", "推出"), 0.92),
        ("benchmark", ("benchmark", "leaderboard", "evaluation", "state-of-the-art", "基准", "排行榜", "评测"), 0.84),
        ("industry", ("acquisition", "acquired", "funding", "partnership", "investment", "收购", "融资", "合作", "投资"), 0.82),
        ("api", ("api", "endpoint", "parameter", "structured output", "接口", "参数"), 0.86),
]


def classify_all(text: str) -> list[tuple[str, float]]:
    lower = text.lower()
    matches = []
    for event_type, keywords, confidence in CLASSIFICATION_RULES:
        if any(keyword in lower for keyword in keywords):
            matches.append((event_type, confidence))
    return matches or [("content_change", 0.55)]


def classify(text: str) -> tuple[str, float]:
    return classify_all(text)[0]


class State:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
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

    def recent_events(self, limit: int = 20) -> list[Event]:
        rows = self.db.execute(
            "SELECT * FROM events ORDER BY discovered_at DESC LIMIT ?", (limit,)
        ).fetchall()
        events = []
        for row in rows:
            data = dict(row)
            data["key"] = data.pop("event_key")
            events.append(Event(**data))
        return events

    def events_since(self, since: datetime, limit: int = 200) -> list[Event]:
        rows = self.db.execute(
            "SELECT * FROM events WHERE discovered_at >= ? ORDER BY discovered_at DESC LIMIT ?",
            (since.astimezone(timezone.utc).isoformat(), limit),
        ).fetchall()
        events = []
        for row in rows:
            data = dict(row)
            data["key"] = data.pop("event_key")
            events.append(Event(**data))
        return events

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


def export_feed(state: State, path: str | Path, source_ids: Iterable[str] | None = None, limit: int = 50, extra_failed: Iterable[str] = (), checked_since: datetime | None = None) -> dict:
    """Write the local intelligence feed atomically and return its JSON shape."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    ids = list(source_ids) if source_ids is not None else None
    pending_sources = [{"source_id": source_id, "message": "not checked: cycle deadline reached"} for source_id in dict.fromkeys(extra_failed)]
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
        "events": [asdict(event) for event in state.recent_events(max(0, min(50, limit)))],
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
    return status, visible_text(payload[:2_000_000], ""), {}


def _fetch(source: Source, previous: sqlite3.Row | None, timeout: int | None = None) -> tuple[int, str, dict[str, str]]:
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
            return response.status, visible_text(raw, response_headers.get("content-type", "")), response_headers
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, "", {}
        if exc.code == 403 and b"challenges.cloudflare.com" in exc.read(8192):
            raise urllib.error.URLError("blocked: HTTP 403 Cloudflare challenge; browser verification required") from exc
        raise
    except (OSError, urllib.error.URLError, http.client.IncompleteRead) as exc:
        if _permission_boundary(exc):
            raise
        # Change the TLS implementation, preserving the source's selected route.
        try:
            return _curl_fetch(source, headers, timeout)
        except (OSError, subprocess.TimeoutExpired) as fallback_error:
            raise OSError(f"urllib: {exc}; curl: {fallback_error}") from fallback_error


def _evidence(previous: str, current: str, max_chars: int = 900) -> str:
    # Changelogs usually put newest entries at the top. Compare both the leading
    # and trailing sections so navigation/footer updates do not hide new posts.
    old_lines = previous.splitlines() if previous else []
    new_lines = current.splitlines()
    old = old_lines[:300] + old_lines[-100:]
    new = new_lines[:300] + new_lines[-100:]
    diff = list(difflib.unified_diff(old, new, lineterm=""))
    excerpt = "\n".join(line for line in diff if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
    return (excerpt or current[:max_chars])[:max_chars]


def _meaningful(text: str) -> bool:
    words = re.findall(r"[A-Za-z]{3,}|[\u4e00-\u9fff]{2,}", text)
    return len(words) >= 2


def _stable_evidence(text: str) -> str:
    volatile = re.compile(
        r"^[+-]?\s*(?:\d[\d,.]*|\d+\s+(?:seconds?|minutes?|hours?|days?)\s*(?:ago)?)$",
        re.IGNORECASE,
    )
    lines = [line for line in text.splitlines() if not volatile.match(line.strip())]
    return "\n".join(lines).strip()


_MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"


_YEARLESS_DATE = re.compile(rf"({_MONTH})\s+(\d{{1,2}})\.?", re.IGNORECASE)


def _parse_yearless(candidate: str, default_year: int) -> datetime | None:
    """Build a yearless "Sep 25" date without strptime's ambiguous year default."""
    match = _YEARLESS_DATE.fullmatch(candidate.strip())
    if not match:
        return None
    try:
        month = datetime.strptime(match.group(1)[:3].title(), "%b").month
        return datetime(default_year, month, int(match.group(2)), tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_candidate(candidate: str, default_year: int | None = None) -> datetime | None:
    value = candidate.replace("Sept ", "Sep ")
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    if default_year is not None:
        return _parse_yearless(value, default_year)
    return None


def _parse_date(line: str, default_year: int | None = None) -> datetime | None:
    """Parse a date heading, never guessing the year without page context."""
    value = line.strip().rstrip(":")
    value = re.sub(r"^(?:last updated|updated|published)\s*:\s*", "", value, flags=re.IGNORECASE)
    explicit = re.match(
        rf"^(\d{{4}}-\d{{2}}-\d{{2}}|\d{{4}}/\d{{1,2}}/\d{{1,2}}|\d{{4}}年\d{{1,2}}月\d{{1,2}}日|{_MONTH} \d{{1,2}}(?:,? \d{{4}}))\b",
        value,
        re.IGNORECASE,
    )
    if explicit:
        return _parse_candidate(explicit.group(1))
    if default_year is not None:
        implicit = re.fullmatch(rf"({_MONTH} \d{{1,2}})\.?", value, re.IGNORECASE)
        if implicit:
            return _parse_candidate(implicit.group(1), default_year)
    return None


def _embedded_explicit_date(line: str) -> datetime | None:
    match = re.search(
        rf"\d{{4}}-\d{{2}}-\d{{2}}|\d{{4}}/\d{{1,2}}/\d{{1,2}}|\d{{4}}年\d{{1,2}}月\d{{1,2}}日|{_MONTH} \d{{1,2}}(?:,? \d{{4}})",
        line,
        re.IGNORECASE,
    )
    return _parse_candidate(match.group(0)) if match else None


def _year_context(line: str, source_id: str = "") -> int | None:
    value = line.strip().rstrip(":")
    ordinary = re.fullmatch(rf"{_MONTH},?\s+(20\d{{2}})", value, re.IGNORECASE)
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


def _changed_blocks(evidence: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    current_date = ""
    current_lines: list[str] = []
    for line in evidence.splitlines():
        clean = line.lstrip("+-").strip()
        parsed = _parse_date(clean)
        if parsed is not None:
            if _meaningful("\n".join(current_lines)):
                blocks.append((current_date, "\n".join(current_lines)))
            current_date = parsed.isoformat()
            current_lines = [line]
        else:
            current_lines.append(line)
    if _meaningful("\n".join(current_lines)):
        blocks.append((current_date, "\n".join(current_lines)))
    return blocks or [("", evidence)]


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


def collect_events(source: Source, state: State, backfill_days: int = 0, force: bool = False) -> list[Event]:
    if not force and not state.is_due(source):
        return []
    previous = state.snapshot(source.id)
    try:
        status, text, headers = _fetch(source, previous)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        # Collection failures belong to source health, not the news feed.
        state.record_error(source.id, f"{type(exc).__name__}: {exc}")
        return []
    state.record_success(source.id)
    if status == 304 or not text:
        if status == 304:
            state.touch_snapshot(source.id)
        return []
    current = normalize(text)
    content_hash = sha256(current)
    if previous and previous["content_hash"] == content_hash:
        state.touch_snapshot(source.id)
        return []
    state.save_snapshot(source, current, content_hash, headers)
    if not previous:
        if backfill_days <= 0:
            return []
        if "community_signal" in source.category:
            return []
        segments = recent_dated_blocks(current, backfill_days, source_id=source.id)
        segments.extend(recent_actionable_lines(current, backfill_days))
        # A dated notice may also sit inside a dated block; keep only one copy.
        segments = list(dict.fromkeys(segments))
    else:
        segments = _changed_blocks(_evidence(previous["content"], current))

    allowed_types = _allowed_event_types(source)
    created: list[Event] = []
    bucket_counts: dict[tuple[str, str], int] = {}
    for published_at, raw_evidence in segments:
        evidence = _stable_evidence(raw_evidence)
        if not _meaningful(evidence):
            continue
        matches = classify_all(evidence)
        if source.id == "huggingface-models":
            matches = [("content_change", 0.55)]
        matches = [match for match in matches if match[0] in allowed_types]
        if not matches and "content_change" in allowed_types:
            matches = [("content_change", 0.55)]
        uncertain = any(
            marker in evidence.lower()
            for marker in ("rumor", "reportedly", " may ", " could ", "considering", "teaser", "leak", "unconfirmed", "据传", "或将", "考虑", "爆料")
        )
        for event_type, base_confidence in matches:
            bucket = (event_type, published_at[:10])
            bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
            if bucket_counts[bucket] > 3:
                continue
            status_label = (
                "backfill"
                if not previous
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
                title=f"{source.name} 内容发生变化",
                url=source.url,
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


def collect(source: Source, state: State, backfill_days: int = 0, force: bool = False) -> Event | None:
    events = collect_events(source, state, backfill_days=backfill_days, force=force)
    return events[0] if events else None


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
