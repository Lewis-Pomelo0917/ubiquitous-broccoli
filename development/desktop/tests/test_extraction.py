import time
import unittest

from ai_radar.core import _decode, _page_problem, visible_text

PAGE = """<!doctype html><html><head><title>Acorn changelog</title>
<style>.a{color:red}</style><script>var menu = "<nav>";</script></head>
<body><nav><a href="/">Home</a><a href="/pricing">Pricing</a></nav>
<main><h1>Changelog</h1>
<h2>October 22, 2026</h2>
<p>We&#39;ve launched <a href="/docs/acorn-3">Acorn 3</a>, reached through <code>api.acorn.test</code>.
 <span aria-hidden="true"></span></p>
<ul><li>Added <a href="/docs/tools">tool use</a>.</li></ul>
<table><tr><th>Model</th><th>Input</th></tr><tr><td>Acorn Mini</td><td>$1.00</td></tr></table>
<p><time datetime="2026-10-20T08:00:00Z">2 days ago</time> Posted by the team</p>
<div hidden>Secret template text</div>
</main><footer><a href="/terms">Terms</a> © 2026 Acorn</footer></body></html>"""


class HtmlExtractionTests(unittest.TestCase):
    def setUp(self):
        self.text = visible_text(PAGE.encode("utf-8"), "text/html; charset=utf-8", "https://example.com/changelog")
        self.lines = self.text.splitlines()

    def test_inline_markup_stays_in_one_readable_line(self):
        self.assertIn("We've launched Acorn 3, reached through api.acorn.test.", self.lines)
        self.assertIn("Added tool use.", self.lines)

    def test_icon_glyphs_and_hidden_or_navigation_text_are_dropped(self):
        joined = "\n".join(self.lines)
        self.assertNotIn("", joined)
        self.assertNotIn("Secret template", joined)
        for chrome in ("Home", "Pricing", "Terms", "© 2026"):
            self.assertNotIn(chrome, joined)

    def test_table_rows_become_single_lines(self):
        self.assertIn("Model | Input", self.lines)
        self.assertIn("Acorn Mini | $1.00", self.lines)

    def test_time_element_exposes_its_iso_date(self):
        self.assertIn("2026-10-20 Posted by the team", self.lines)

    def test_each_line_remembers_the_link_it_carries(self):
        line = "We've launched Acorn 3, reached through api.acorn.test."
        self.assertEqual(self.text.links[line], "https://example.com/docs/acorn-3")
        self.assertEqual(self.text.links["Added tool use."], "https://example.com/docs/tools")
        self.assertNotIn("Acorn Mini | $1.00", self.text.links)

    def test_unclosed_navigation_does_not_swallow_the_page(self):
        body = "<p>Real changelog content for readers.</p>" * 900
        page = f"<html><body><nav><a href='/'>Menu</a><main>{body}</main></body></html>"
        self.assertIn("Real changelog content for readers.", visible_text(page.encode(), "text/html", "https://example.com/"))

    def test_malformed_and_hostile_markup_never_raises(self):
        for raw in (b"", b"<", b"<a href=" * 500, b"<div>" * 5000 + b"x y z", bytes(range(256)) * 20):
            self.assertIsInstance(visible_text(raw, "text/html", "https://example.com/"), str)

    def test_declared_charset_is_honoured(self):
        payload = "<p>模型发布说明 已上线</p>".encode("gbk")
        self.assertIn("模型发布说明", visible_text(payload, "text/html; charset=gbk"))
        self.assertEqual(_decode("héllo".encode("utf-8")), "héllo")

    def test_button_text_is_kept_so_copyable_model_ids_are_not_lost(self):
        page = "<main><h2>Oct 22, 2026</h2><table><tr><td><button>claude-acorn-3-20261022</button></td><td>Active</td></tr></table></main>"
        self.assertIn("claude-acorn-3-20261022 | Active", visible_text(page.encode(), "text/html").splitlines())

    def test_a_row_with_tens_of_thousands_of_cells_is_read_in_linear_time(self):
        started = time.monotonic()
        text = visible_text(("<table><tr>" + "<td>x</td>" * 40000 + "</tr></table>").encode(), "text/html")
        self.assertLess(time.monotonic() - started, 4.0)  # the quadratic version needed ~9 s
        self.assertTrue(str(text).startswith("x | x | x"))

    def test_empty_table_cells_do_not_repeat_the_separator(self):
        raw = b"<table><tr><td>a</td><td></td><td></td><td>b</td></tr></table>"
        self.assertEqual(visible_text(raw, "text/html").splitlines(), ["a | b"])

    def test_unclosed_navigation_on_a_small_page_does_not_hide_the_text(self):
        page = "<p>Intro paragraph with words.</p><nav><a href='/'>Menu</a><main><p>Real changelog content for readers.</p></main>"
        self.assertIn("Real changelog content for readers.", visible_text(page.encode(), "text/html").splitlines())

    def test_item_containers_are_recorded_for_listings(self):
        page = "<ul><li><h3>Alpha item</h3><time datetime='2026-10-20'></time><p>First summary text.</p></li><li>Beta</li></ul>"
        text = visible_text(page.encode(), "text/html")
        self.assertEqual(text.splitlines(), ["Alpha item", "2026-10-20", "First summary text.", "Beta"])
        self.assertEqual(text.entries, [(0, 3)])  # a one-line item is not an item range

    def test_formatting_changes_do_not_change_the_text(self):
        before = b"<html><head><style>.x{color:red}</style></head><body><h1>2026-10-16</h1><p>New API endpoint</p></body></html>"
        after = b"<html><head><style>.x{color:blue}</style><script>ad()</script></head><body><div><h1>2026-10-16</h1><p>New   API  <b>endpoint</b></p></div></body></html>"
        self.assertEqual(str(visible_text(before, "text/html")), str(visible_text(after, "text/html")))


class FeedExtractionTests(unittest.TestCase):
    ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Acorn releases</title>
<entry><title>v1.5.0</title><updated>2026-10-21T10:00:00Z</updated>
<link rel="alternate" href="https://example.com/rel/v1.5.0"/>
<content type="html">&lt;p&gt;Added streaming support. &lt;b&gt;Apache 2.0&lt;/b&gt; license.&lt;/p&gt;</content></entry>
<entry><title>v1.4.0</title><updated>2026-10-01T10:00:00Z</updated><link href="https://example.com/rel/v1.4.0"/></entry>
</feed>"""
    RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Acorn news</title>
<item><title>Acorn 3 is here</title><link>https://example.com/blog/acorn-3</link>
<pubDate>Tue, 20 Oct 2026 09:30:00 GMT</pubDate><description>&lt;p&gt;Our newest model.&lt;/p&gt;</description></item></channel></rss>"""

    def test_atom_entries_become_dated_readable_lines_with_links(self):
        text = visible_text(self.ATOM.encode("utf-8"), "application/atom+xml", "https://example.com/releases.atom")
        self.assertEqual(
            text.splitlines()[:2],
            ["2026-10-21", "v1.5.0. Added streaming support. Apache 2.0 license."],
        )
        self.assertEqual(text.links["v1.5.0. Added streaming support. Apache 2.0 license."], "https://example.com/rel/v1.5.0")
        self.assertIn("2026-10-01", text.splitlines())
        self.assertNotIn("<", str(text))

    def test_rss_dates_are_normalised(self):
        text = visible_text(self.RSS.encode("utf-8"), "application/rss+xml", "https://example.com/feed")
        self.assertEqual(text.splitlines(), ["2026-10-20", "Acorn 3 is here. Our newest model."])

    def test_a_byte_order_mark_or_blank_lines_before_the_xml_declaration_are_tolerated(self):
        for prefix in ("\ufeff", "\n\n", "\ufeff\n"):
            with self.subTest(prefix=repr(prefix)):
                text = visible_text((prefix + self.RSS).encode("utf-8"), "")
                self.assertEqual(text.splitlines(), ["2026-10-20", "Acorn 3 is here. Our newest model."])
                self.assertEqual(text.kind, "feed")

    def test_an_impossible_feed_date_is_ignored_not_fatal(self):
        rss = self.RSS.replace("Tue, 20 Oct 2026 09:30:00 GMT", "Fri, 31 Dec 9999 23:59:59 -0100")
        self.assertEqual(visible_text(rss.encode(), "application/rss+xml").splitlines(), ["Acorn 3 is here. Our newest model."])

    def test_an_entry_title_that_only_repeats_the_date_is_dropped(self):
        rss = self.RSS.replace("<title>Acorn 3 is here</title>", "<title>Acorn news - October 20, 2026</title>")
        self.assertEqual(visible_text(rss.encode(), "application/rss+xml").splitlines(), ["2026-10-20", "Our newest model."])

    def test_a_lone_surrogate_in_json_is_removed(self):
        raw = (b'{"status":{"description":"x"},"incidents":[{"name":"Outage \\ud800 in API","status":"investigating",'
               b'"created_at":"2026-10-26T08:00:00Z","incident_updates":[]}]}')
        text = str(visible_text(raw, "application/json"))
        text.encode("utf-8")  # a stored snapshot must be encodable
        self.assertIn("Incident: Outage in API (investigating).", text)

    def test_status_json_lists_incidents_and_stays_quiet_when_healthy(self):
        incident = (
            '{"status":{"description":"Partial System Outage"},"components":[{"name":"API","status":"degraded_performance"}],'
            '"incidents":[{"name":"Elevated errors","status":"investigating","created_at":"2026-10-26T08:00:00Z",'
            '"incident_updates":[{"body":"We are looking into it."}]}]}'
        )
        text = visible_text(incident.encode(), "application/json").splitlines()
        self.assertEqual(text[0], "2026-10-26")
        self.assertIn("Incident: Elevated errors (investigating). We are looking into it.", text)
        self.assertIn("Component API: degraded_performance", text)
        healthy = '{"status":{"description":"All Systems Operational"},"components":[],"incidents":[]}'
        self.assertEqual(str(visible_text(healthy.encode(), "application/json")), "Status: All Systems Operational")


class PageProblemTests(unittest.TestCase):
    def test_interstitials_and_javascript_stubs_are_reported_not_monitored(self):
        self.assertIn("blocked", _page_problem("Just a moment... Checking your browser before accessing the site"))
        self.assertIn("blocked", _page_problem("You need to enable JavaScript to run this app."))
        self.assertIn("empty", _page_problem("Loading"))
        self.assertIn("empty", _page_problem(""))

    def test_status_json_and_feeds_are_never_mistaken_for_a_verification_page(self):
        incident = (
            '{"status":{"description":"x"},"incidents":[{"name":"Access denied errors for some API keys","status":"investigating",'
            '"created_at":"2026-10-26T08:00:00Z","incident_updates":[]}]}'
        )
        self.assertEqual(_page_problem(visible_text(incident.encode(), "application/json")), "")
        atom = (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>v1.2.3</title><updated>2026-10-20T00:00:00Z</updated>'
            '<content type="html">Fixed a captcha bug</content></entry></feed>'
        )
        self.assertEqual(_page_problem(visible_text(atom.encode(), "application/atom+xml")), "")

    def test_ordinary_pages_pass_even_when_they_mention_javascript(self):
        article = "Release notes for the JavaScript SDK. " + "The SDK now supports streaming responses. " * 80
        self.assertEqual(_page_problem(article), "")
        self.assertEqual(_page_problem("Initial page"), "")


if __name__ == "__main__":
    unittest.main()
