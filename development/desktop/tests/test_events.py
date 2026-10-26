import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ai_radar.core import (
    SNAPSHOT_FORMAT,
    Source,
    State,
    _STRONG_UNCERTAIN,
    _backfill_segments,
    _clean_body,
    _diff_segments,
    _first_sentence,
    _headline,
    _parse_date,
    _priority,
    _site,
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


    def test_leak_is_a_rumour_marker_only_in_its_news_sense(self):
        for text in (
            "Launched the Acorn 3 model; the release also fixes a memory leak in the API server.",
            "API keys leaked through the logging endpoint were rotated.",
            "Fixed memory leak",
        ):
            self.assertIsNone(_STRONG_UNCERTAIN.search(text), text)
        for text in (
            "Leaked benchmark screenshots suggest Acorn 4 is close.",
            "A leak suggests Acorn 4 ships next week.",
            "according to a leak, Acorn 4 arrives soon",
            "Acorn leaker claims a new model",
        ):
            self.assertIsNotNone(_STRONG_UNCERTAIN.search(text), text)


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


    def test_a_price_that_changed_alone_borrows_the_label_above_it(self):
        before = page("Acorn Pro", "$4 / MTok", "Acorn Mini", "$1 / MTok")
        after = page("Acorn Pro", "$5 / MTok", "Acorn Mini", "$1 / MTok")
        self.assertEqual(_diff_segments(before, after), [("", ["Acorn Pro", "$5 / MTok"])])


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

    def test_sites_on_shared_hosting_are_told_apart(self):
        self.assertEqual(_site("https://docs.example.com/a"), _site("https://www.example.com/b"))
        self.assertEqual(_site("https://news.example.co.uk/a"), "example.co.uk")
        self.assertNotEqual(_site("https://acorn.github.io/blog"), _site("https://other.github.io/blog"))
        self.assertEqual(_site("https://acorn.github.io/blog"), _site("https://acorn.github.io/news"))

    def test_a_label_and_its_number_read_as_one_headline(self):
        self.assertEqual(_headline(["Acorn Pro", "$5 / MTok"]), "Acorn Pro — $5 / MTok")
        self.assertEqual(_headline(["Introducing Acorn 3", "Acorn 3 is here."]), "Introducing Acorn 3")

    def test_chinese_text_is_cut_at_full_stops_and_counts_double_for_length(self):
        text = "今天发布了新一代模型，支持更长上下文。价格同步下调百分之二十。"
        self.assertEqual(_first_sentence(text), "今天发布了新一代模型，支持更长上下文。")
        self.assertEqual(_headline([text]), "今天发布了新一代模型，支持更长上下文。")
        self.assertEqual(_first_sentence("已发布。支持更长上下文和工具调用。再次下调价格。"), "已发布。支持更长上下文和工具调用。")

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


    def test_a_memory_leak_fix_in_a_release_is_still_a_confirmed_release(self):
        text = "Launched the Acorn 3 model; the release also fixes a memory leak in the API server."
        with fetch_returning("Initial page about acorn", text):
            collect_events(source(), self.state, force=True)
            events = collect_events(source(), self.state, force=True)
        self.assertEqual((events[0].priority, events[0].status), ("P0", "confirmed"))

    def test_a_pricing_page_reports_a_changed_price_without_any_keyword(self):
        pricing = source(id="pricing", category="pricing", provider="Acorn")
        with fetch_returning(page("Acorn Pro", "$4 / MTok", "Acorn Mini", "$1 / MTok"), page("Acorn Pro", "$5 / MTok", "Acorn Mini", "$1 / MTok")):
            collect_events(pricing, self.state, force=True)
            events = collect_events(pricing, self.state, force=True)
        self.assertEqual([(event.event_type, event.title) for event in events], [("pricing", "Acorn Pro — $5 / MTok")])


def html_pages(*pages):
    return patch("ai_radar.core._fetch", side_effect=[
        (200, visible_text(body.encode("utf-8"), "text/html", "https://news.example.com/"), {}) for body in pages
    ])


def entry(title, when, summary):
    return f'<article><h3><a href="/p/{title.split()[-1].lower()}">{title}</a></h3><div class="meta">{when}</div><p>{summary}</p></article>'


class ListingLayoutTests(unittest.TestCase):
    ITEMS = (
        ("Introducing Acorn 3", day(1), "Acorn 3 is our most capable model, now available in the API with a 1M token context."),
        ("Acorn pricing update", day(2), "We have lowered the price of the Acorn Mini model to $0.50 per million tokens."),
        ("Evaluation results", day(3), "New evaluation results show Acorn 3 leads on agentic coding benchmarks."),
    )

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = State(Path(self.directory.name) / "state.db")
        self.source = source(id="listing", url="https://news.example.com/", category="models")

    def tearDown(self):
        self.state.close()
        self.directory.cleanup()

    def collect(self, *pages):
        events = []
        with html_pages(*pages):
            for _ in pages:
                events = collect_events(self.source, self.state, backfill_days=7, force=True)
        return events

    def test_a_headline_above_its_date_stays_with_its_own_entry(self):
        events = self.collect("<main>" + "".join(entry(*item) for item in self.ITEMS) + "</main>")
        by_title = {event.title: event for event in events}
        self.assertEqual(set(by_title), {item[0] for item in self.ITEMS})
        for title, when, _ in self.ITEMS:
            self.assertEqual(by_title[title].published_at[:10], _parse_date(when).strftime("%Y-%m-%d"))
            self.assertEqual(by_title[title].evidence.splitlines()[0], title)

    def test_a_new_entry_with_its_headline_above_the_date_is_one_event(self):
        old = "<main>" + "".join(entry(*item) for item in self.ITEMS) + "</main>"
        new = entry("Acorn 4 preview", day(0), "We are launching a preview of the Acorn 4 model for all API customers today.")
        events = self.collect(old, "<main>" + new + old[len("<main>"):])
        self.assertEqual([event.title for event in events], ["Acorn 4 preview"])
        self.assertEqual(events[0].published_at[:10], _parse_date(day(0)).strftime("%Y-%m-%d"))

    def test_listings_that_start_with_the_date_read_as_before(self):
        dated_first = "".join(
            f"<article><time>{when}</time><h3>{title}</h3><p>{summary}</p></article>" for title, when, summary in self.ITEMS
        )
        events = self.collect("<main>" + dated_first + "</main>")
        self.assertEqual({event.title for event in events}, {item[0] for item in self.ITEMS})

    def test_a_single_dated_section_in_a_page_wide_article_is_not_a_listing(self):
        body = f"<article><h1>Release notes</h1><h2>{day(1)}</h2><ul><li>Launched the Acorn 3 model with a 1M token context.</li></ul></article>"
        events = self.collect(body)
        self.assertEqual([event.title for event in events], ["Launched the Acorn 3 model with a 1M token context."])

    def test_a_category_label_above_the_date_belongs_to_the_entry_below_it(self):
        def card(label, when, title, body):
            return (f'<article><header><p><a href="/n/{label.lower()}">{label}</a></p><time>{when}</time></header>'
                    f'<a href="/n/{title.split()[-1].lower()}"><h2>{title}</h2><p>{body}</p></a></article>')
        cards = (
            card("Announcements", day(1), "Introducing Acorn 3", "Acorn 3 is our most capable model, now available in the API."),
            card("Pricing", day(2), "Lower prices for Acorn Mini", "We have lowered the price of the Acorn Mini model to $0.50 per million tokens."),
            card("Features", day(3), "Acorn gets tool search", "Tool search lets Acorn find the right tool in very large tool catalogs."),
        )
        events = {event.title: event for event in self.collect("<main>" + "".join(cards) + "</main>")}
        self.assertEqual(events["Introducing Acorn 3"].evidence.splitlines()[0], "Announcements")
        self.assertEqual(events["Introducing Acorn 3"].evidence.splitlines()[-1], "Acorn 3 is our most capable model, now available in the API.")
        self.assertEqual(events["Lower prices for Acorn Mini"].evidence.splitlines()[0], "Pricing")
        self.assertEqual(events["Lower prices for Acorn Mini"].event_type, "pricing")


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
