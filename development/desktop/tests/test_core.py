import tempfile
import io
import subprocess
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from ai_radar.core import (
    Event,
    Source,
    State,
    _curl_command,
    _parse_date,
    _priority,
    _fetch,
    classify,
    classify_all,
    collect,
    collect_events,
    export_feed,
    load_sources,
    normalize,
    recent_actionable_lines,
    recent_dated_blocks,
    recent_dated_excerpt,
    render_digest,
)


class CoreTests(unittest.TestCase):
    def test_normalize_is_stable(self):
        self.assertEqual(normalize("  a   b\n\n c  "), "a b\nc")

    def test_classify_prioritizes_deprecation(self):
        self.assertEqual(classify("Pricing change: model deprecated")[0], "deprecation")

    def test_classify_detects_usage_reset(self):
        self.assertEqual(classify("We reset usage limits for all paid Codex users")[0], "reset")

    def test_model_release_is_immediate(self):
        self.assertEqual(_priority("P1", "release"), "P0")

    def test_sqlite_state_starts_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "state.db")
            self.assertEqual(state.recent_events(), [])
            state.close()

    def test_recent_events_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "state.db")
            event = Event("k", "s", "Source", "Title", "https://example.com", "reset", "observe", 0.99, datetime.now(timezone.utc).isoformat(), "evidence")
            self.assertTrue(state.add_event(event))
            self.assertEqual(state.recent_events()[0], event)
            state.close()

    def test_export_feed_has_versioned_atomic_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "state.db")
            event = Event("k", "s", "Source", "Title", "https://example.com", "release", "confirm", 0.99, datetime.now(timezone.utc).isoformat(), "evidence")
            state.add_event(event)
            destination = Path(directory) / "ai-intelligence.json"
            feed = export_feed(state, destination, ["s"])
            self.assertEqual(feed["version"], 1)
            self.assertEqual(feed["events"][0], event.__dict__)
            self.assertEqual(feed["health"]["total"], 1)
            self.assertEqual(feed, __import__("json").loads(destination.read_text(encoding="utf-8")))
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])
            state.close()

    def test_recent_event_window(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "state.db")
            event = Event("k", "s", "Source", "Title", "https://example.com", "reset", "observe", 0.99, datetime.now(timezone.utc).isoformat(), "evidence")
            state.add_event(event)
            self.assertTrue(state.has_recent_event("s", "reset"))
            self.assertFalse(state.has_recent_event("other", "reset"))
            state.close()

    def test_empty_digest_is_explicit(self):
        self.assertIn("没有发现", render_digest([]))

    def test_first_snapshot_is_baseline_then_top_change_emits(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "state.db")
            source = Source("s", "Source", "https://example.com", provider="Lab", priority="P1")
            with patch("ai_radar.core._fetch", side_effect=[(200, "Initial page", {}), (200, "Released NewModel today", {})]):
                self.assertIsNone(collect(source, state, force=True))
                event = collect(source, state, force=True)
            self.assertIsNotNone(event)
            self.assertEqual(event.event_type, "release")
            state.close()

    def test_uncertain_release_is_downgraded(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "state.db")
            source = Source("s", "Source", "https://example.com", provider="Lab", priority="P1")
            with patch("ai_radar.core._fetch", side_effect=[(200, "Initial page", {}), (200, "Reportedly may launch NewModel soon", {})]):
                collect(source, state, force=True)
                event = collect(source, state, force=True)
            self.assertEqual(event.priority, "P3")
            self.assertEqual(event.status, "observe")
            state.close()

    def test_source_failure_updates_health_not_events(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "state.db")
            source = Source("s", "Source", "https://example.com")
            with patch("ai_radar.core._fetch", side_effect=OSError("offline")):
                self.assertIsNone(collect(source, state, force=True))
            self.assertEqual(state.recent_events(), [])
            self.assertEqual(state.health_summary(["s"])["failed"], 1)
            state.close()

    def test_recent_dated_excerpt_excludes_old_entry(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        excerpt = recent_dated_excerpt(f"{today}\nReleased NewModel\n2020-01-01\nOld model", 14)
        self.assertIn("Released NewModel", excerpt)
        self.assertNotIn("Old model", excerpt)

    def test_registry_covers_broad_ecosystem(self):
        sources = load_sources(Path(__file__).parents[1] / "ai_radar" / "sources.json")
        providers = {source.provider for source in sources}
        self.assertGreaterEqual(len(sources), 90)
        self.assertTrue({"Meta", "Alibaba Qwen", "Moonshot Kimi", "Z.ai GLM", "AWS Bedrock", "NVIDIA", "European Commission", "Cursor", "ByteDance Seed", "MiniMax", "Xiaomi MiMo", "Meituan LongCat", "Ant Ling"}.issubset(providers))

    def test_full_industry_categories_are_classified(self):
        self.assertIn("policy", {kind for kind, _ in classify_all("AI Act compliance regulation")})
        self.assertIn("compute", {kind for kind, _ in classify_all("new GPU accelerator and HBM cluster")})

    def test_one_page_can_emit_release_and_deprecation(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "state.db")
            source = Source("s", "Source", "https://example.com", category="model_release,deprecation", priority="P1")
            with patch("ai_radar.core._fetch", side_effect=[(200, "Initial page", {}), (200, "Released Model X. Old Model Y is deprecated.", {})]):
                collect_events(source, state, force=True)
                events = collect_events(source, state, force=True)
            self.assertEqual({event.event_type for event in events}, {"release", "deprecation"})
            state.close()

    def test_two_distinct_resets_are_not_time_suppressed(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "state.db")
            source = Source("s", "Source", "https://example.com", category="pricing_quota", priority="P0")
            payloads = [(200, "Initial page", {}), (200, "We reset usage limits for Codex users", {}), (200, "A second banked reset is available for Codex users", {})]
            with patch("ai_radar.core._fetch", side_effect=payloads):
                collect_events(source, state, force=True)
                first = collect_events(source, state, force=True)
                second = collect_events(source, state, force=True)
            self.assertEqual(first[0].event_type, "reset")
            self.assertEqual(second[0].event_type, "reset")
            self.assertNotEqual(first[0].key, second[0].key)
            state.close()

    def test_old_reset_is_not_a_current_emergency(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        self.assertEqual(_priority("P0", "reset", old_date, "usage reset"), "P3")

    def test_imminent_real_price_increase_is_p0_but_cancelled_one_is_not(self):
        tomorrow = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        self.assertEqual(_priority("P1", "pricing", tomorrow, "Prices increase tomorrow"), "P0")
        self.assertEqual(_priority("P1", "pricing", tomorrow, "Price increase will not occur"), "P1")

    def test_far_future_deprecation_does_not_become_p0(self):
        future = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
        self.assertEqual(_priority("P1", "deprecation", future, "Model retires"), "P2")

    def test_yearless_date_is_never_guessed_without_context(self):
        self.assertIsNone(_parse_date("September 17"))
        reference = datetime(2026, 8, 31, tzinfo=timezone.utc)
        self.assertEqual(recent_dated_blocks("Sep 15\nReleased old model", reference_time=reference), [])

    def test_old_year_context_is_not_backfilled_as_current_year(self):
        reference = datetime(2026, 8, 31, tzinfo=timezone.utc)
        text = "September, 2025\nSep 26\nReleased old model"
        self.assertEqual(recent_dated_blocks(text, days=14, reference_time=reference), [])

    def test_mistral_compact_year_context_keeps_only_current_entries(self):
        reference = datetime(2026, 8, 31, tzinfo=timezone.utc)
        text = "Sep 25\nSeptember 17\nReleased old model\nAug 26\nAugust 27\nReleased current model"
        blocks = recent_dated_blocks(text, days=14, source_id="mistral-changelog", reference_time=reference)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0][0][:10], "2026-08-27")
        self.assertIn("current model", blocks[0][1])

    def test_embedded_explicit_action_date_is_notice_not_heading(self):
        reference = datetime(2026, 8, 31, tzinfo=timezone.utc)
        line = "This model will be retired on September 30, 2026."
        self.assertIsNone(_parse_date(line))
        notices = recent_actionable_lines(line, reference_time=reference)
        self.assertEqual(notices[0][0][:10], "2026-09-30")

    def test_yearless_price_increase_cannot_become_backfill_p0(self):
        reference = datetime(2026, 8, 31, tzinfo=timezone.utc)
        self.assertEqual(recent_actionable_lines("Prices increase on September 1", reference_time=reference), [])

    def test_chinese_explicit_date_is_parsed_without_guessing(self):
        self.assertEqual(_parse_date("2026年8月31日").date().isoformat(), "2026-08-31")
        self.assertIsNone(_parse_date("8月31日"))

    def test_expired_deprecation_is_downgraded(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        self.assertEqual(_priority("P1", "deprecation", old_date, "Model retired"), "P3")

    def test_curl_fallback_recovers_from_tls_urlopen_failure(self):
        source = Source("status", "Status", "https://example.com/status.json", kind="json")
        completed = subprocess.CompletedProcess(["curl"], 0, stdout=b'{"status":"operational"}\n__ai_radar_http_status__200', stderr=b"")
        with (
            patch("ai_radar.core.urllib.request.urlopen", side_effect=urllib.error.URLError("TLS")),
            patch("ai_radar.core.time.sleep"),
            patch("ai_radar.core.subprocess.run", return_value=completed),
        ):
            status, text, headers = _fetch(source, None)
        self.assertEqual(status, 200)
        self.assertIn("operational", text)
        self.assertEqual(headers, {})

    def test_curl_fallback_reuses_urllib_proxy_route(self):
        source = Source("status", "Status", "https://example.com/status.json", kind="json")
        headers = {"User-Agent": "ua", "Accept": "*/*"}
        with (
            patch("ai_radar.core.urllib.request.getproxies", return_value={"https": "http://proxy.test:8080"}),
            patch("ai_radar.core.urllib.request.proxy_bypass", return_value=False),
        ):
            command = _curl_command(source, headers, 8)
        self.assertIn("--proxy", command)
        self.assertEqual(command[command.index("--proxy") + 1], "http://proxy.test:8080")
        self.assertEqual(command[-1], "https://example.com/status.json")

    def test_curl_fallback_skips_proxy_for_bypassed_hosts(self):
        source = Source("status", "Status", "https://example.com/status.json", kind="json")
        headers = {"User-Agent": "ua", "Accept": "*/*"}
        with (
            patch("ai_radar.core.urllib.request.getproxies", return_value={"https": "http://proxy.test:8080"}),
            patch("ai_radar.core.urllib.request.proxy_bypass", return_value=True),
        ):
            command = _curl_command(source, headers, 8)
        self.assertNotIn("--proxy", command)

    def test_direct_source_uses_no_proxy_in_both_transports(self):
        source = Source("gov", "Gov", "https://example.gov/", use_proxy=False, timeout_seconds=25)
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.headers = {"Content-Type": "text/html"}
        response.read.return_value = b"<p>Research publication</p>"
        with patch("ai_radar.core.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = response
            self.assertIn("Research publication", _fetch(source, None)[1])
            self.assertEqual(opener.call_args.args[0].proxies, {})
            self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 25)
        with patch("ai_radar.core._curl_proxy") as proxy:
            command = _curl_command(source, {"User-Agent": "ua", "Accept": "*/*"}, 25)
            proxy.assert_not_called()
        self.assertEqual(command[command.index("--noproxy") + 1], "*")
        self.assertNotIn("--insecure", command)

    def test_blocked_challenge_is_not_a_success_or_retried(self):
        source = Source("s", "Status", "https://example.com")
        error = urllib.error.HTTPError(source.url, 403, "Forbidden", {}, io.BytesIO(b"https://challenges.cloudflare.com"))
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "state.db")
            with patch("ai_radar.core.urllib.request.urlopen", side_effect=error), patch("ai_radar.core._curl_fetch") as fallback:
                self.assertEqual(collect_events(source, state, force=True), [])
                fallback.assert_not_called()
            self.assertIn("blocked: HTTP 403 Cloudflare challenge", state.failed_sources()[0][1])
            self.assertIsNone(state.snapshot("s"))
            state.close()

    def test_both_transports_failing_records_error_without_event(self):
        source = Source("s", "Status", "https://example.com")
        completed = subprocess.CompletedProcess(["curl"], 35, stdout=b"", stderr=b"TLS failed")
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "state.db")
            with patch("ai_radar.core.urllib.request.urlopen", side_effect=urllib.error.URLError("TLS")), patch("ai_radar.core.subprocess.run", return_value=completed):
                self.assertEqual(collect_events(source, state, force=True), [])
            self.assertIn("curl exit 35", state.failed_sources()[0][1])
            self.assertEqual(state.health_summary(["s"])["failed"], 1)
            state.close()

    def test_permission_boundary_never_uses_fallback(self):
        with patch("ai_radar.core.urllib.request.urlopen", side_effect=urllib.error.URLError(PermissionError("denied"))), patch("ai_radar.core._curl_fetch") as fallback:
            with self.assertRaises(urllib.error.URLError):
                _fetch(Source("s", "S", "https://example.com"), None)
            fallback.assert_not_called()

    def test_curl_must_provide_valid_success_status(self):
        for payload in (b"challenge page", b"body\n__ai_radar_http_status__invalid", b"body\n__ai_radar_http_status__403", b"redirect\n__ai_radar_http_status__302"):
            with self.subTest(payload=payload), patch("ai_radar.core.urllib.request.urlopen", side_effect=TimeoutError("timeout")), patch("ai_radar.core.subprocess.run", return_value=subprocess.CompletedProcess(["curl"], 0, stdout=payload, stderr=b"")):
                with self.assertRaises(OSError):
                    _fetch(Source("s", "S", "https://example.com"), None)

    def test_stale_error_is_pending_in_cycle_export(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "state.db")
            state.record_error("old", "yesterday")
            cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)
            feed = export_feed(state, Path(directory) / "feed.json", ["old"], extra_failed=["old"], checked_since=cutoff)
            self.assertEqual(feed["health"], {"total": 1, "checked": 0, "ok": 0, "failed": 0, "pending": 1})
            self.assertEqual(feed["failed_sources"], [])
            self.assertEqual(feed["pending_sources"][0]["source_id"], "old")
            state.close()


if __name__ == "__main__":
    unittest.main()
