"""End-to-end update cycles against a local fixture website and a stand-in for the model binary."""
import json
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from ai_radar import watch
from ai_radar.core import State, collect_all, load_sources

PAGES = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        entry = PAGES.get(self.path)
        if entry is None:
            self.send_response(404)
            self.end_headers()
            return
        body, content_type, delay = entry() if callable(entry) else entry
        time.sleep(delay)
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def day(offset):
    return datetime.now(timezone.utc) - timedelta(days=offset)


def long_date(offset):
    return day(offset).strftime("%B %d, %Y").replace(" 0", " ")


def changelog(entries):
    html = "<html><head><title>Acorn changelog</title></head><body><nav><a href='/'>Home</a></nav><main><h1>Changelog</h1>"
    for date, items in entries:
        html += f"<h2>{date}</h2><ul>" + "".join(f"<li>{text}</li>" for text in items) + "</ul>"
    return (html + "</main><footer>Acorn</footer></body></html>", "text/html; charset=utf-8", 0)


def blog(posts):
    html = "<html><body><main><h1>Latest posts</h1>"
    for title, offset, summary in posts:
        html += (f"<article><time datetime='{day(offset):%Y-%m-%d}'>x</time><h3><a href='/blog/{offset}'>{title}</a></h3>"
                 f"<p>{summary}</p></article>")
    return (html + "</main></body></html>", "text/html", 0)


def status_page(description, incidents=()):
    data = {"status": {"description": description}, "components": [], "incidents": list(incidents)}
    return (json.dumps(data), "application/json", 0)


def model_runner(mode="good"):
    """Stand-in for llama-completion: reads the prompt file and answers with the number of an option."""
    def run(command, **kwargs):
        if mode == "crash":
            return Mock(returncode=1, stdout="", stderr="failed to load model")
        prompt = (Path(kwargs["cwd"]) / command[command.index("--file") + 1]).read_text(encoding="utf-8")
        options = [line.split(". ", 1)[1] for line in prompt.split("Options:\n", 1)[1].splitlines() if line[:1].isdigit()]
        if mode == "garbage":
            return Mock(returncode=0, stdout="I think the answer is 42.", stderr="")
        choice = 99 if mode == "range" else len(options)  # a model that prefers the last option
        return Mock(returncode=0, stdout=json.dumps({"choice": choice}) + " [end of text]", stderr="")
    return run


class CycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        PAGES.clear()
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.data = self.root / "appdata"
        self.data.mkdir()
        self.sources = []
        (self.root / "model.gguf").write_bytes(b"x")
        self.args = Namespace(
            data_dir=self.data, feed=self.data / "ai-intelligence.json", status=self.data / "status.json",
            db=self.data / "state.sqlite3", sources=self.root / "sources.json", runner=self.root / "llama.exe",
            model=self.root / "model.gguf", schema=self.root / "schema.json", run_timeout=60, source_timeout=5, workers=4,
        )
        self.model = patch("ai_radar.watch.subprocess.run", side_effect=model_runner())
        self.model.start()
        self.addCleanup(self.model.stop)
        self.addCleanup(self.directory.cleanup)

    def add_source(self, source_id, path, **fields):
        entry = dict(id=source_id, name=source_id.title(), url=self.base + path, tier="S", provider="Acorn",
                     category="model_release,api_update", priority="P1", use_proxy=False, timeout_seconds=5)
        entry.update(fields)
        self.sources.append(entry)
        (self.root / "sources.json").write_text(json.dumps(self.sources), encoding="utf-8")

    def cycle(self):
        return watch.run_cycle(self.args)

    def feed(self):
        return json.loads(self.args.feed.read_text(encoding="utf-8"))

    def test_first_run_shows_recent_news_at_once_and_explains_unreachable_sources(self):
        PAGES["/changelog"] = changelog([(long_date(1), ["Launched the Acorn 3 model with a 1M token context window.",
                                                         "Added a new tool-use parameter to the API for agents."]),
                                          (long_date(40), ["Ancient entry about the first model."])])
        PAGES["/blog"] = blog([("Acorn 2 benchmark results", 2, "New evaluation results show state-of-the-art accuracy.")])
        PAGES["/ok-status"] = status_page("All Systems Operational")
        PAGES["/blocked"] = ("<html><title>Just a moment...</title><body>Checking your browser before accessing</body></html>", "text/html", 0)
        PAGES["/spa"] = ("<html><body><div id='root'></div><script>boot()</script></body></html>", "text/html", 0)
        self.add_source("changelog", "/changelog")
        self.add_source("blog", "/blog", category="research_blog,industry")
        self.add_source("status", "/ok-status", kind="json", category="safety_status")
        self.add_source("blocked", "/blocked")
        self.add_source("spa", "/spa")
        self.add_source("missing", "/nowhere")

        status = self.cycle()

        self.assertEqual(status["state"], "partial")
        self.assertEqual((status["failed_sources"], status["sources_ok"], status["source_count"]), (3, 3, 6))
        self.assertEqual(status["failure_kinds"], {"被拦截": 1, "需要 JavaScript": 1, "页面不存在": 1})
        self.assertEqual({sample["source_id"] for sample in status["failed_samples"]}, {"blocked", "spa", "missing"})
        self.assertGreater(status["new_items"], 0)
        feed = self.feed()
        titles = [item["title"] for item in feed["events"]]
        self.assertIn("Launched the Acorn 3 model with a 1M token context window.", titles)
        self.assertNotIn("Ancient entry about the first model.", titles)
        self.assertFalse(any(title.endswith("内容发生变化") for title in titles))
        for item in feed["events"]:
            self.assertNotRegex(item["evidence"], r"(?m)^[+-]")
            self.assertIn(item["summary_mode"] if "summary" in item else "rule-v1", ("extractive-v1", "rule-v1"))
        chosen = watch.display_events(feed["events"])
        self.assertEqual(chosen[0]["title"], "Launched the Acorn 3 model with a 1M token context window.")
        self.assertGreater(status["next_due_unix"], time.time() + 6 * 3600 - 5)
        report = json.loads((self.data / "last-run.json").read_text(encoding="utf-8"))["sources"]
        self.assertEqual({row["id"] for row in report if not row["ok"]}, {"blocked", "spa", "missing"})
        self.assertEqual(sum(row["new_events"] for row in report), status["new_items"] + status["duplicate_items"])

    def test_model_trouble_never_blocks_the_news(self):
        PAGES["/changelog"] = changelog([(long_date(0), ["Launched the Acorn 3 model for everyone today", "It is available in the API for all customers. The context window grows to one million tokens."])])
        self.add_source("changelog", "/changelog")
        for mode in ("crash", "garbage", "range"):
            with self.subTest(mode=mode):
                self.args.feed.unlink(missing_ok=True)
                self.args.db.unlink(missing_ok=True)
                with patch("ai_radar.watch.subprocess.run", side_effect=model_runner(mode)):
                    status = self.cycle()
                self.assertEqual(status["state"], "updated")
                self.assertEqual((status["model_state"], status["summarized_items"]), ("unavailable", 0))
                self.assertIn("规则摘录", status["message"])
                event = self.feed()["events"][0]
                self.assertEqual(event["summary_mode"], "rule-v1")
                self.assertIn(event["summary"], event["evidence"])

    def test_the_model_picks_one_of_the_sentences_and_never_writes_its_own(self):
        PAGES["/changelog"] = changelog([(long_date(0), ["Introducing Acorn 3 for everyone today",
                                                         "Acorn 3 is our most capable model, now available in the API. It reads longer documents than any earlier model."])])
        self.add_source("changelog", "/changelog")
        status = self.cycle()
        event = self.feed()["events"][0]
        self.assertEqual((event["summary_mode"], status["summarized_items"], status["model_state"]), ("extractive-v1", 1, "ok"))
        self.assertEqual(event["summary"], "It reads longer documents than any earlier model.")
        self.assertIn(event["summary"], event["evidence"])

    def test_news_collected_by_a_cycle_that_never_finished_is_published_next_time(self):
        PAGES["/changelog"] = changelog([(long_date(5), ["Added the batch endpoint for bulk requests to the API."])])
        self.add_source("changelog", "/changelog")
        self.cycle()
        before = {item["key"] for item in self.feed()["events"]}
        PAGES["/changelog"] = changelog([(long_date(0), ["Launched the Acorn 3 model with a 1M token context window."]),
                                          (long_date(5), ["Added the batch endpoint for bulk requests to the API."])])
        # The app was closed after the pages were read but before the feed was written.
        watch.collect_candidate(self.args.db, self.args.sources, self.root / "unused.json", 60, 5,
                                events_since=datetime.now(timezone.utc) - timedelta(days=3))
        self.assertEqual({item["key"] for item in self.feed()["events"]}, before)
        status = self.cycle()
        titles = [item["title"] for item in self.feed()["events"]]
        self.assertIn("Launched the Acorn 3 model with a 1M token context window.", titles)
        self.assertEqual(status["new_items"], 1)

    def test_an_unchanged_site_adds_nothing_and_says_so(self):
        PAGES["/changelog"] = changelog([(long_date(1), ["Launched the Acorn 3 model with a 1M token context window."])])
        self.add_source("changelog", "/changelog")
        self.cycle()
        first = self.args.feed.read_bytes()
        status = self.cycle()
        self.assertEqual((status["state"], status["new_items"]), ("no_new_items", 0))
        self.assertEqual({item["key"] for item in json.loads(first)["events"]}, {item["key"] for item in self.feed()["events"]})

    def test_total_failure_keeps_the_last_feed_and_backs_off_further_each_time(self):
        PAGES["/changelog"] = changelog([(long_date(1), ["Launched the Acorn 3 model with a 1M token context window."])])
        self.add_source("changelog", "/changelog")
        self.cycle()
        kept = self.args.feed.read_bytes()
        PAGES.clear()
        delays = []
        for expected in (1, 2, 3):
            status = self.cycle()
            self.assertEqual((status["state"], status["consecutive_failures"]), ("failed", expected))
            delays.append(round(status["next_due_unix"] - time.time(), -2))
        self.assertEqual(delays, [1800, 3600, 7200])
        self.assertEqual(self.args.feed.read_bytes(), kept)

    def test_first_release_events_and_a_damaged_feed_are_replaced_not_fatal(self):
        PAGES["/changelog"] = changelog([(long_date(1), ["Launched the Acorn 3 model with a 1M token context window."])])
        self.add_source("changelog", "/changelog")
        legacy = {"key": "legacy", "source_id": "changelog", "source_name": "Changelog", "title": "Changelog 内容发生变化",
                  "url": "https://example.com", "event_type": "release", "status": "confirmed", "confidence": 0.9,
                  "discovered_at": datetime.now(timezone.utc).isoformat(), "evidence": "+raw diff line", "provider": "Acorn",
                  "priority": "P0", "published_at": day(0).isoformat()}
        watch.atomic_json(self.args.feed, {"version": 1, "events": [legacy]})
        self.cycle()
        self.assertNotIn("legacy", {item["key"] for item in self.feed()["events"]})
        self.args.feed.write_text("{ this is not json", encoding="utf-8")
        status = self.cycle()
        self.assertNotEqual(status["state"], "failed")
        self.assertEqual(self.feed()["version"], 1)

    def test_a_corrupt_state_database_is_set_aside_and_rebuilt(self):
        PAGES["/changelog"] = changelog([(long_date(1), ["Launched the Acorn 3 model with a 1M token context window."])])
        self.add_source("changelog", "/changelog")
        self.args.db.write_bytes(b"this is definitely not an sqlite database" * 50)
        status = self.cycle()
        self.assertEqual(status["state"], "updated")
        self.assertTrue(list(self.data.glob("state.sqlite3.corrupt-*")))

    def test_slow_sources_do_not_hold_up_the_rest(self):
        PAGES["/fast"] = changelog([(long_date(1), ["Launched the Acorn 3 model with a 1M token context window."])])
        PAGES["/slow"] = ("<html><body><p>Nothing new here at all.</p></body></html>", "text/html", 2.5)
        for name in ("fast", "slow"):
            self.add_source(name, "/" + name)
        sources = load_sources(self.args.sources)
        state = State(self.root / "concurrent.db")
        started = time.monotonic()
        events, pending = collect_all(sources, state, backfill_days=7, workers=4, deadline_seconds=1.0, source_timeout=5)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(pending, ["slow"])
        self.assertEqual(len(events), 1)
        state.close()


if __name__ == "__main__":
    unittest.main()
