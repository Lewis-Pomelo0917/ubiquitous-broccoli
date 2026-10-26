import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ai_radar.core import (
    SNAPSHOT_FORMAT,
    Source,
    State,
    _backfill_segments,
    _clean_body,
    _diff_segments,
    _headline,
    _parse_date,
    _priority,
    classify_all,
    collect_events,
    visible_text,
)


def kinds(text):
    return {kind for kind, _ in classify_all(text)}


def page(*lines):
    return "\n".join(lines)


def day(offset, form="%B %d, %Y"):
    return (datetime.now(timezone.utc) - timedelta(days=offset)).strftime(form).replace(" 0", " ")


class ClassificationTests(unittest.TestCase):
    def test_keywords_match_whole_words_only(self):
        self.assertNotIn("compute", kinds("The API returns more output tokens"))
        self.assertNotIn("tool", kinds("We provide guidance and decide the side effects"))
        self.assertNotIn("api", kinds("A rapid capital increase"))
        self.assertNotIn("release", kinds("The service is unavailable"))

    def test_real_phrases_still_match(self):
        self.assertIn("compute", kinds("New GPU clusters and TPU pods"))
        self.assertIn("deprecation", kinds("The endpoint is deprecated and will be retired"))
        self.assertIn("pricing", kinds("Prices drop by 20%; see pricing"))
        self.assertIn("api", kinds("Added a new SDK parameter to the API"))
        self.assertIn("release", kinds("Introducing Acorn 3, now available"))
        self.assertIn("policy", kinds("EU AI Act compliance deadline"))
        self.assertIn("deprecation", kinds("该模型将下线并停止支持"))


class DateParsingTests(unittest.TestCase):
    def test_common_heading_formats(self):
        expected = datetime(2026, 10, 22, tzinfo=timezone.utc)
        for text in (
            "October 22, 2026", "Oct 22, 2026", "Oct 22 2026", "22 October 2026", "22nd Oct 2026", "2026-10-22",
            "2026/10/22", "2026.10.22", "2026年10月22日", "Updated: 2026-10-22", "Posted on October 22, 2026",
            "Thursday, October 22, 2026", "2026-10-22: Added a batch endpoint", "Oct 22, 2026 · Announcements",
        ):
            with self.subTest(text=text):
                self.assertEqual(_parse_date(text), expected)

    def test_prose_that_merely_contains_or_starts_with_a_date_is_not_a_heading(self):
        for text in (
            "The model will be retired on October 22, 2026.",
            "October 22, 2026 is the day on which every request to the old endpoint will start to fail for good.",
            "September 17", "Version 2026", "2026-13-45",
        ):
            with self.subTest(text=text):
                self.assertIsNone(_parse_date(text))

    def test_yearless_dates_need_a_year_context(self):
        self.assertIsNone(_parse_date("Oct 22"))
        self.assertEqual(_parse_date("Oct 22", 2026), datetime(2026, 10, 22, tzinfo=timezone.utc))


class SegmentTests(unittest.TestCase):
    def test_only_new_lines_are_news_and_moved_lines_are_not(self):
        before = page("Changelog", "Oct 20, 2026", "Added batch endpoint for bulk calls.", "Oct 18, 2026", "Fixed streaming parser bug.")
        after = page("Changelog", "Oct 22, 2026", "Launched Acorn 3 with a longer context.", "Oct 20, 2026",
                     "Added batch endpoint for bulk calls.", "Oct 18, 2026", "Fixed streaming parser bug.")
        segments = _diff_segments(before, after)
        self.assertEqual(segments, [("2026-10-22T00:00:00+00:00", ["Oct 22, 2026", "Launched Acorn 3 with a longer context."])])
        reordered = page("Changelog", "Oct 18, 2026", "Fixed streaming parser bug.", "Oct 20, 2026", "Added batch endpoint for bulk calls.")
        self.assertEqual(_diff_segments(before, reordered), [])

    def test_a_new_bullet_under_an_existing_heading_inherits_its_date(self):
        before = page("Oct 22, 2026", "Launched Acorn 3 with a longer context.", "Oct 20, 2026", "Old entry about streaming.")
        after = page("Oct 22, 2026", "Launched Acorn 3 with a longer context.", "Added a new tool-use parameter.",
                     "Oct 20, 2026", "Old entry about streaming.")
        self.assertEqual(_diff_segments(before, after), [("2026-10-22T00:00:00+00:00", ["Added a new tool-use parameter."])])

    def test_one_hunk_with_several_dates_becomes_several_segments(self):
        before = page("Oct 10, 2026", "Old entry about streaming.")
        after = page("Oct 22, 2026", "Newest entry about the model.", "Oct 21, 2026", "Second entry about tools.", "Oct 10, 2026", "Old entry about streaming.")
        self.assertEqual([date[:10] for date, _ in _diff_segments(before, after)], ["2026-10-22", "2026-10-21"])

    def test_a_year_header_gives_context_to_yearless_headings(self):
        before = page("October 2026", "Oct 20", "Old entry about streaming.")
        after = page("October 2026", "Oct 22", "Newest entry about the model.", "Oct 20", "Old entry about streaming.")
        self.assertEqual([date[:10] for date, _ in _diff_segments(before, after)], ["2026-10-22"])

    def test_a_restructured_page_is_not_news(self):
        before = page(*[f"old line number {n} about the product" for n in range(100)])
        after = page(*[f"completely new wording {n} for the redesign" for n in range(100)])
        self.assertEqual(_diff_segments(before, after), [])

    def test_backfill_keeps_only_recent_dated_entries(self):
        text = page("Changelog", day(2), "Launched Acorn 3 with a longer context.", day(40), "Ancient entry about streaming.")
        segments = _backfill_segments(text, 7, "x")
        self.assertEqual(len(segments), 1)
        self.assertIn("Launched Acorn 3", "\n".join(segments[0][1]))


class EventTextTests(unittest.TestCase):
    def test_body_drops_date_heading_and_boilerplate_but_keeps_content(self):
        lines = ["Oct 22, 2026 · Announcements", "Copy page", "Introducing Acorn 3", "Read more", "Acorn 3 is our most capable model.", "Introducing Acorn 3"]
        self.assertEqual(_clean_body(lines), ["Announcements", "Introducing Acorn 3", "Acorn 3 is our most capable model."])
        self.assertEqual(_clean_body(["Oct 22", "Real content line"]), ["Real content line"])

    def test_headline_is_a_real_sentence_not_a_label(self):
        self.assertEqual(_headline(["Announcements", "Introducing Acorn 3", "Acorn 3 is here."]), "Introducing Acorn 3")
        long_line = "We've launched Acorn 3 with a longer context window, faster tools and lower prices for everyone. More text follows here."
        title = _headline([long_line])
        self.assertTrue(title.startswith("We've launched Acorn 3"))
        self.assertLessEqual(len(title), 111)
        self.assertNotIn("More text", title)

    def test_release_priority_depends_on_whether_a_model_is_involved(self):
        self.assertEqual(_priority("P1", "release"), "P0")
        self.assertEqual(_priority("P1", "release", "", "Launched the Acorn 3 model."), "P0")
        self.assertEqual(_priority("P1", "release", "", "Released SDK version 2.0.1 with fixes."), "P1")


def source(**changes):
    values = dict(id="s", name="Acorn Changelog", url="https://docs.example.com/changelog", provider="Acorn",
                  category="model_release,api_update,deprecation", priority="P1")
    values.update(changes)
    return Source(**values)


def fetch_returning(*texts, links=None):
    pages = []
    for text in texts:
        value = visible_text(("<p>" + "</p><p>".join(text.splitlines()) + "</p>").encode()) if links is None else text
        pages.append((200, value, {}))
    return patch("ai_radar.core._fetch", side_effect=pages)


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = State(Path(self.directory.name) / "state.db")

    def tearDown(self):
        self.state.close()
        self.directory.cleanup()

    def test_first_snapshot_with_backfill_publishes_recent_entries_with_readable_titles(self):
        text = page(day(1), "Launched the Acorn 3 model with a 1M token context.", day(30), "Ancient entry about the old model.")
        with fetch_returning(text):
            events = collect_events(source(), self.state, backfill_days=7, force=True)
        self.assertEqual([event.event_type for event in events], ["release"])
        event = events[0]
        self.assertEqual(event.title, "Launched the Acorn 3 model with a 1M token context.")
        self.assertEqual(event.evidence, "Launched the Acorn 3 model with a 1M token context.")
        self.assertEqual(event.status, "backfill")
        self.assertEqual(event.priority, "P0")
        self.assertEqual(event.url, "https://docs.example.com/changelog")

    def test_without_backfill_the_first_snapshot_stays_a_silent_baseline(self):
        with fetch_returning(page(day(1), "Launched the Acorn 3 model.")):
            self.assertEqual(collect_events(source(), self.state, force=True), [])
        self.assertIsNotNone(self.state.snapshot("s"))

    def test_later_changes_emit_only_what_was_added(self):
        before = page(day(5), "Added the batch endpoint for bulk requests.")
        after = page(day(0), "Launched the Acorn 3 model with a 1M token context.", day(5), "Added the batch endpoint for bulk requests.")
        with fetch_returning(before, after):
            collect_events(source(), self.state, backfill_days=7, force=True)
            events = collect_events(source(), self.state, backfill_days=7, force=True)
        self.assertEqual([event.title for event in events], ["Launched the Acorn 3 model with a 1M token context."])
        self.assertEqual(events[0].status, "confirmed")
        self.assertNotIn("+", events[0].evidence[:1])

    def test_one_event_per_passage_unless_it_matches_distinct_headline_types(self):
        text = page(day(0), "Launched the Acorn 3 model; the endpoint is available through the API.")
        with fetch_returning("Initial page about acorn", text):
            collect_events(source(), self.state, force=True)
            events = collect_events(source(), self.state, force=True)
        self.assertEqual([event.event_type for event in events], ["release"])

    def test_an_undated_notice_with_a_coming_date_is_dated_by_it(self):
        later = (datetime.now(timezone.utc) + timedelta(days=20)).strftime("%B %d, %Y").replace(" 0", " ")
        text = f"The Acorn 1 model is deprecated and will be retired on {later}."
        with fetch_returning("Initial page about acorn", text):
            collect_events(source(), self.state, force=True)
            events = collect_events(source(), self.state, force=True)
        self.assertEqual(events[0].event_type, "deprecation")
        self.assertEqual(events[0].published_at[:10], (datetime.now(timezone.utc) + timedelta(days=20)).strftime("%Y-%m-%d"))
        self.assertEqual(events[0].priority, "P1")

    def test_a_wildly_future_heading_is_treated_as_undated(self):
        text = page("December 31, 2099", "Launched the Acorn 3 model with a 1M token context.")
        with fetch_returning("Initial page about acorn", text):
            collect_events(source(), self.state, force=True)
            events = collect_events(source(), self.state, force=True)
        self.assertEqual(events[0].published_at, "")

    def test_documentation_prose_is_not_treated_as_a_rumour_but_forum_speculation_is(self):
        docs = "The Acorn 3 model is now available. You may need to update the SDK; requests could fail on old versions."
        with fetch_returning("Initial page about acorn", docs):
            collect_events(source(), self.state, force=True)
            official = collect_events(source(), self.state, force=True)
        self.assertEqual(official[0].priority, "P0")
        community = source(id="c", tier="C", category="model_release")
        with fetch_returning("Initial page about acorn", docs):
            collect_events(community, self.state, force=True)
            forum = collect_events(community, self.state, force=True)
        self.assertEqual((forum[0].priority, forum[0].status), ("P3", "observe"))

    def test_a_month_named_may_is_not_speculation(self):
        text = page("May 12, 2026", "Launched the Acorn 3 model with a 1M token context.")
        with fetch_returning("Initial page about acorn", text):
            collect_events(source(), self.state, force=True)
            events = collect_events(source(), self.state, force=True)
        self.assertNotEqual(events[0].priority, "P3")

    def test_unreadable_pages_are_failures_with_a_reason_not_silent_successes(self):
        for body, reason in (("Just a moment... Checking your browser", "blocked"), ("Loading", "empty")):
            with self.subTest(reason=reason), fetch_returning(body):
                self.assertEqual(collect_events(source(id=reason), self.state, force=True), [])
                self.assertIn(reason, self.state.failed_sources()[0][1])
                self.assertIsNone(self.state.snapshot(reason))

    def test_a_crash_while_reading_one_page_is_contained_and_reported(self):
        with fetch_returning("Initial page about acorn"), patch("ai_radar.core._diff_segments", side_effect=RuntimeError("boom")):
            collect_events(source(), self.state, force=True)
        with fetch_returning("Changed page about acorn models"), patch("ai_radar.core._diff_segments", side_effect=RuntimeError("boom")):
            self.assertEqual(collect_events(source(), self.state, force=True), [])
        self.assertIn("internal: RuntimeError: boom", self.state.failed_sources()[0][1])

    def test_status_sources_report_every_new_incident_even_without_keywords(self):
        status = source(id="status", category="safety_status", priority="P0")
        with fetch_returning("Status: All Systems Operational", "Elevated error rates on the Acorn service"):
            collect_events(status, self.state, force=True)
            events = collect_events(status, self.state, force=True)
        self.assertEqual([event.event_type for event in events], ["security"])


class SnapshotMigrationTests(unittest.TestCase):
    def test_snapshots_from_an_older_text_format_are_dropped_but_events_are_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            state = State(path)
            state.db.execute("INSERT INTO snapshots VALUES ('s', NULL, NULL, 'h', 'old +diff format text', '2026-10-01T00:00:00+00:00')")
            state.db.execute(
                "INSERT INTO events (event_key, source_id, source_name, title, url, event_type, status, confidence, discovered_at, evidence)"
                " VALUES ('k', 's', 'S', 'Old', 'https://e.test', 'release', 'confirmed', .9, '2026-10-01T00:00:00+00:00', 'evidence')"
            )
            state.db.execute("PRAGMA user_version = 0")
            state.db.commit()
            state.close()
            reopened = State(path)
            self.assertIsNone(reopened.snapshot("s"))
            self.assertEqual(len(reopened.recent_events()), 1)
            self.assertEqual(reopened.db.execute("PRAGMA user_version").fetchone()[0], SNAPSHOT_FORMAT)
            reopened.close()
            again = State(path)  # a current database is left alone
            again.db.execute("INSERT INTO snapshots VALUES ('t', NULL, NULL, 'h', 'text', '2026-10-01T00:00:00+00:00')")
            again.db.commit()
            again.close()
            final = State(path)
            self.assertIsNotNone(final.snapshot("t"))
            final.close()


if __name__ == "__main__":
    unittest.main()
