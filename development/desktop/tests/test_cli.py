import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_radar.__main__ import main
from ai_radar.core import State


class CollectionCycleTests(unittest.TestCase):
    def run_cycle(self, directory, worker, clock=None):
        root = Path(directory)
        sources = root / "sources.json"
        sources.write_text(json.dumps([
            {"id": name, "name": name, "url": "https://example.com/" + name}
            for name in ("first", "second")
        ]), encoding="utf-8")
        argv = ["ai_radar", "--db", str(root / "state.db"), "--sources", str(sources),
                "--once", "--daily", "--run-timeout", "60", "--backfill-days", "7",
                "--export-json", str(root / "feed.json")]
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("sys.argv", argv))
            runner = stack.enter_context(patch("ai_radar.__main__.subprocess.run", side_effect=worker))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            if clock is not None:
                stack.enter_context(patch("ai_radar.__main__.time.monotonic", side_effect=clock))
            self.assertEqual(main(), 0)
        return json.loads((root / "feed.json").read_text(encoding="utf-8")), runner.call_count

    def test_source_timeout_does_not_skip_next_source(self):
        with tempfile.TemporaryDirectory() as directory:
            def worker(command, **kwargs):
                source_id = command[command.index("--source-id") + 1]
                self.assertEqual(command[command.index("--backfill-days") + 1], "7")
                self.assertEqual(kwargs["encoding"], "utf-8")
                if source_id == "first":
                    raise subprocess.TimeoutExpired(command, 1)
                state = State(Path(directory) / "state.db")
                state.record_success(source_id)
                state.close()
                return subprocess.CompletedProcess(command, 0)
            feed, count = self.run_cycle(directory, worker)
            self.assertEqual(count, 2)
            self.assertEqual(feed["health"], {"total": 2, "checked": 2, "ok": 1, "failed": 1, "pending": 0})
            self.assertEqual([item["source_id"] for item in feed["failed_sources"]], ["first"])
            self.assertEqual(feed["pending_sources"], [])

    def test_run_deadline_exports_partial_without_counting_old_success(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory) / "state.db")
            state.record_success("second")
            state.close()
            def worker(command, **kwargs):
                state = State(Path(directory) / "state.db")
                state.record_success("first")
                state.close()
                return subprocess.CompletedProcess(command, 0)
            feed, count = self.run_cycle(directory, worker, clock=[0, 0, 0, 61])
            self.assertEqual(count, 1)
            self.assertEqual(feed["health"], {"total": 2, "checked": 1, "ok": 1, "failed": 0, "pending": 1})
            self.assertEqual(feed["failed_sources"], [])
            self.assertEqual([item["source_id"] for item in feed["pending_sources"]], ["second"])
