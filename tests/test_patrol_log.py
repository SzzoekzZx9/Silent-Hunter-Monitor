#!/usr/bin/env python3
"""
Unit tests for the patrol log writer (collectors/patrol_log.py).

Run with:  python3 -m unittest tests.test_patrol_log -v
No network access required — the wiki and LLM are faked.
"""
import os
import sys
import json
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import event_bus
import collectors.patrol_log as pl


def make_event(event_type, data):
    return event_bus.Event(event_type=event_type,
                           timestamp=datetime.now(), data=data)


class TestDateFormatting(unittest.TestCase):
    def test_ordinal(self):
        self.assertEqual(pl._ordinal(1), '1st')
        self.assertEqual(pl._ordinal(2), '2nd')
        self.assertEqual(pl._ordinal(3), '3rd')
        self.assertEqual(pl._ordinal(4), '4th')
        self.assertEqual(pl._ordinal(11), '11th')
        self.assertEqual(pl._ordinal(12), '12th')
        self.assertEqual(pl._ordinal(13), '13th')
        self.assertEqual(pl._ordinal(21), '21st')
        self.assertEqual(pl._ordinal(22), '22nd')
        self.assertEqual(pl._ordinal(23), '23rd')
        self.assertEqual(pl._ordinal(31), '31st')

    def test_format_date_heading(self):
        self.assertEqual(pl.format_date_heading('1940-09-06'), 'September 6th')
        self.assertEqual(pl.format_date_heading('1941-01-21'), 'January 21st')

    def test_format_date_long(self):
        self.assertEqual(pl.format_date_long('1940-09-06'),
                         'September 6th, 1940')


class TestInsertEntry(unittest.TestCase):
    def test_insert_after_patrol_heading(self):
        content = "## Patrol 8\n\n### Aug 25th, 6am\nSet sail"
        result = pl.insert_entry(content, "### September 6th\nA quiet day.")
        self.assertEqual(
            result,
            "## Patrol 8\n\n### September 6th\nA quiet day.\n\n"
            "### Aug 25th, 6am\nSet sail")

    def test_insert_no_heading(self):
        content = "### Aug 25th, 6am\nSet sail"
        result = pl.insert_entry(content, "### September 6th\nA quiet day.")
        self.assertTrue(result.startswith("### September 6th\nA quiet day."))
        self.assertIn("### Aug 25th, 6am", result)

    def test_insert_only_heading(self):
        content = "## Patrol 9"
        result = pl.insert_entry(content, "### September 6th\nCalm seas.")
        self.assertEqual(result, "## Patrol 9\n\n### September 6th\nCalm seas.")


class TestReplaceSection(unittest.TestCase):
    def test_replace_middle_section(self):
        content = ("## Patrol 8\n\n"
                   "### September 6th\nOld entry.\n\n"
                   "### Aug 25th, 6am\nSet sail")
        result = pl.replace_section(content, "### September 6th",
                                    "New entry.")
        self.assertEqual(
            result,
            "## Patrol 8\n\n### September 6th\nNew entry.\n\n"
            "### Aug 25th, 6am\nSet sail")

    def test_replace_last_section(self):
        content = "## Patrol 8\n\n### September 6th\nOld entry."
        result = pl.replace_section(content, "### September 6th", "New entry.")
        self.assertEqual(result,
                         "## Patrol 8\n\n### September 6th\nNew entry.")

    def test_replace_missing_heading(self):
        content = "## Patrol 8\n\n### Aug 25th, 6am\nSet sail"
        self.assertIsNone(
            pl.replace_section(content, "### September 6th", "New entry."))


class TestGollumFormParsing(unittest.TestCase):
    EDIT_FORM = """
    <form id="gollum-editor-form" action="/gollum/edit/Patrol+8.md" method="post">
      <input type="hidden" name="page" id="gollum-editor-page-title"
             value="Patrol 8.md">
      <input type="hidden" name="etag"
             value="4139a6e517eece7da9bc9c9480b444e72d74fe7a">
      <input type="hidden" name="path"
             value="/Games/SH3 Captain&#39;s Log">
      <input type="hidden" name="format" value="markdown">
      <textarea id="gollum-editor-body" name="content"
        >## Patrol 8

### Aug 25th, 6am
Set sail</textarea>
    </form>
    """

    def test_input_values(self):
        self.assertEqual(pl.GollumWiki._input_value(self.EDIT_FORM, 'page'),
                         'Patrol 8.md')
        self.assertEqual(
            pl.GollumWiki._input_value(self.EDIT_FORM, 'etag'),
            '4139a6e517eece7da9bc9c9480b444e72d74fe7a')
        self.assertEqual(
            pl.GollumWiki._input_value(self.EDIT_FORM, 'path'),
            "/Games/SH3 Captain's Log")
        self.assertEqual(pl.GollumWiki._input_value(self.EDIT_FORM, 'format'),
                         'markdown')

    def test_textarea_content(self):
        content = pl.GollumWiki._textarea_content(
            self.EDIT_FORM, 'gollum-editor-body')
        self.assertEqual(content, "## Patrol 8\n\n### Aug 25th, 6am\nSet sail")


class FakeWiki:
    """In-memory stand-in for GollumWiki (content per page path)."""

    def __init__(self, content=None):
        self.pages = {}
        if content is not None:
            self.pages["Games/SH3 Captain's Log/Patrol 9.md"] = content
        self.page = "Games/SH3 Captain's Log/Patrol 9.md"
        self.writes = []

    def read_page(self):
        content = self.pages.get(self.page)
        if content is None:
            return None
        return {'page': self.page.rsplit('/', 1)[-1], 'etag': 'abc',
                'path': '/Games', 'format': 'markdown', 'content': content}

    def get_entry(self, date_str):
        form = self.read_page()
        if form is None:
            return None
        heading = f"### {pl.format_date_heading(date_str)}"
        lines = form['content'].split('\n')
        for i, line in enumerate(lines):
            if line.strip() == heading:
                j = i + 1
                while j < len(lines) and not lines[j].startswith('#'):
                    j += 1
                return '\n'.join(lines[i + 1:j]).strip('\n') or None
        return None

    def upsert_entry(self, date_str, entry_text, message):
        heading = f"### {pl.format_date_heading(date_str)}"
        entry = f"{heading}\n{entry_text.strip()}"
        form = self.read_page()
        if form is None:
            base = self.page.rsplit('/', 1)[-1].rsplit('.', 1)[0]
            self.pages[self.page] = f"## {base}\n\n{entry}\n"
        else:
            content = pl.replace_section(form['content'], heading,
                                         entry_text.strip())
            if content is None:
                content = pl.insert_entry(form['content'], entry)
            self.pages[self.page] = content
        self.writes.append((self.page, date_str, message))
        return True


class TestDayBook(unittest.TestCase):
    def test_events_attributed_to_clock_date(self):
        book = pl.DayBook()
        book.observe(make_event(event_bus.EventTypes.CLOCK_UPDATE,
                                {'date': '1940-09-06', 'time': '07:00'}))
        book.observe(make_event(event_bus.EventTypes.SHIP_SPOTTED,
                                "Ship spotted, sir! Bearing 45!"))
        self.assertEqual(book.dirty_dates(), ['1940-09-06'])
        day = book.days['1940-09-06']
        self.assertEqual(day['events'],
                         ["07:00 Ship spotted: Ship spotted, sir! Bearing 45!"])

    def test_limbo_events_folded_into_first_date(self):
        book = pl.DayBook()
        # Sighting before any clock/weather event arrived.
        book.observe(make_event(event_bus.EventTypes.SHIP_SPOTTED,
                                "Ship spotted, sir!"))
        self.assertEqual(book.dirty_dates(), [])
        book.observe(make_event(event_bus.EventTypes.CLOCK_UPDATE,
                                {'date': '1940-09-06', 'time': '08:00'}))
        self.assertEqual(book.dirty_dates(), ['1940-09-06'])
        self.assertIn("Ship spotted", book.days['1940-09-06']['events'][0])

    def test_multiple_days_tracked(self):
        book = pl.DayBook()
        book.observe(make_event(event_bus.EventTypes.CLOCK_UPDATE,
                                {'date': '1940-09-05', 'time': '20:00'}))
        book.observe(make_event(event_bus.EventTypes.MESSAGE_RECEIVED,
                                "From: BdU\nTo: U-45\nOrders."))
        book.observe(make_event(event_bus.EventTypes.CLOCK_UPDATE,
                                {'date': '1940-09-06', 'time': '06:00'}))
        book.observe(make_event(event_bus.EventTypes.AIRCRAFT_SPOTTED,
                                "Aircraft spotted, sir!"))
        self.assertEqual(book.dirty_dates(), ['1940-09-05', '1940-09-06'])
        self.assertEqual(book.sealed_dirty_dates(), ['1940-09-05'])
        self.assertIn('Radio message',
                      book.days['1940-09-05']['events'][0])
        self.assertIn('Aircraft spotted',
                      book.days['1940-09-06']['events'][0])

    def test_grid_tracking(self):
        book = pl.DayBook()
        book.observe(make_event(event_bus.EventTypes.CLOCK_UPDATE,
                                {'date': '1940-09-06', 'time': '07:00'}))
        for grid in ('AM98', 'AM98', 'AN63'):
            book.observe(make_event(event_bus.EventTypes.LOCATION_UPDATE,
                                    {'grid': grid, 'available': True}))
        self.assertEqual(book.days['1940-09-06']['grids'], ['AM98', 'AN63'])

    def test_weather_placeholder_date_ignored(self):
        book = pl.DayBook()
        book.observe(make_event(event_bus.EventTypes.WEATHER,
                                {'date': '????-??-??', 'time': '07:23',
                                 'text': 'Heavy clouds.'}))
        self.assertIsNone(book.current_date)
        self.assertEqual(book.dirty_dates(), [])

    def test_depth_speed_stats(self):
        book = pl.DayBook()
        book.observe(make_event(event_bus.EventTypes.CLOCK_UPDATE,
                                {'date': '1940-09-06', 'time': '07:00'}))
        for depth in (10.0, 95.5, 40.0):
            book.observe(make_event(event_bus.EventTypes.DEPTH_UPDATE,
                                    {'depth': depth}))
        book.observe(make_event(event_bus.EventTypes.SPEED_UPDATE,
                                {'speed': 17.25}))
        day = book.days['1940-09-06']
        self.assertEqual(day['max_depth'], 95.5)
        self.assertEqual(day['max_speed'], 17.25)

    def test_depth_change_events(self):
        book = pl.DayBook()
        book.observe(make_event(event_bus.EventTypes.CLOCK_UPDATE,
                                {'date': '1940-09-06', 'time': '07:00'}))
        # Start submerged (e.g. monitor restart mid-dive).
        for depth in (10.0, 12.0, 14.0):
            book.observe(make_event(event_bus.EventTypes.DEPTH_UPDATE,
                                    {'depth': depth}))
        # Sudden deep dive (e.g. evading an aircraft).
        book.observe(make_event(event_bus.EventTypes.DEPTH_UPDATE,
                                {'depth': 80.0}))
        # Small adjustments ignored.
        for depth in (78.0, 82.0, 80.0):
            book.observe(make_event(event_bus.EventTypes.DEPTH_UPDATE,
                                    {'depth': depth}))
        # Back up.
        book.observe(make_event(event_bus.EventTypes.DEPTH_UPDATE,
                                {'depth': 12.0}))
        book.observe(make_event(event_bus.EventTypes.DEPTH_UPDATE,
                                {'depth': 3.0}))
        events = book.days['1940-09-06']['events']
        self.assertEqual(events, ['07:00 At 10m depth',
                                  '07:00 Dove deeper to 80m',
                                  '07:00 Rose to 12m',
                                  '07:00 Surfaced'])

    def test_depth_dive_from_surface(self):
        book = pl.DayBook()
        book.observe(make_event(event_bus.EventTypes.CLOCK_UPDATE,
                                {'date': '1940-09-06', 'time': '07:00'}))
        book.observe(make_event(event_bus.EventTypes.DEPTH_UPDATE,
                                {'depth': 0.0}))
        book.observe(make_event(event_bus.EventTypes.DEPTH_UPDATE,
                                {'depth': 25.0}))
        events = book.days['1940-09-06']['events']
        self.assertEqual(events, ['07:00 Dove to 25m'])

    def test_depth_surfaced_bobbing_is_not_a_dive(self):
        """The gauge reads 0-7m while surfaced (wave motion); bobbing
        around the old 5m boundary must not produce dive/surface events."""
        book = pl.DayBook()
        book.observe(make_event(event_bus.EventTypes.CLOCK_UPDATE,
                                {'date': '1940-09-06', 'time': '07:00'}))
        for depth in (4.2, 5.1, 6.8, 5.9, 4.0, 6.5, 7.0, 5.2):
            book.observe(make_event(event_bus.EventTypes.DEPTH_UPDATE,
                                    {'depth': depth}))
        events = book.days['1940-09-06']['events']
        self.assertEqual(events, [])

    def test_speed_change_events(self):
        book = pl.DayBook()
        book.observe(make_event(event_bus.EventTypes.CLOCK_UPDATE,
                                {'date': '1940-09-06', 'time': '07:00'}))
        book.observe(make_event(event_bus.EventTypes.SPEED_UPDATE,
                                {'speed': 12.0}))
        # Small changes ignored.
        for speed in (13.0, 14.0, 12.5):
            book.observe(make_event(event_bus.EventTypes.SPEED_UPDATE,
                                    {'speed': speed}))
        book.observe(make_event(event_bus.EventTypes.SPEED_UPDATE,
                                {'speed': 18.0}))
        book.observe(make_event(event_bus.EventTypes.SPEED_UPDATE,
                                {'speed': 0.0}))
        events = book.days['1940-09-06']['events']
        self.assertEqual(events, ['07:00 Increased speed to 18 knots',
                                  '07:00 All stop'])

    def test_heading_change_events(self):
        book = pl.DayBook()
        book.observe(make_event(event_bus.EventTypes.CLOCK_UPDATE,
                                {'date': '1940-09-06', 'time': '07:00'}))
        # Initial reading sets the baseline, no event.
        book.observe(make_event(event_bus.EventTypes.HEADING_UPDATE,
                                {'heading': 355.0}))
        # Small changes ignored.
        for h in (357.0, 353.0, 356.0):
            book.observe(make_event(event_bus.EventTypes.HEADING_UPDATE,
                                    {'heading': h}))
        # 30-degree change records.
        book.observe(make_event(event_bus.EventTypes.HEADING_UPDATE,
                                {'heading': 25.0}))
        # Wraparound: 25 -> 50 is +25, ignored.
        book.observe(make_event(event_bus.EventTypes.HEADING_UPDATE,
                                {'heading': 50.0}))
        # Big turn (>= 90 degrees) is 'came about'.
        book.observe(make_event(event_bus.EventTypes.HEADING_UPDATE,
                                {'heading': 270.0}))
        events = book.days['1940-09-06']['events']
        self.assertEqual(events, ['07:00 Altered course to 025\u00b0',
                                  '07:00 Came about to 270\u00b0'])

    def test_heading_wraparound_short_way(self):
        book = pl.DayBook()
        book.observe(make_event(event_bus.EventTypes.CLOCK_UPDATE,
                                {'date': '1940-09-06', 'time': '07:00'}))
        # 350 -> 10 is a 20-degree turn across north, not 340.
        book.observe(make_event(event_bus.EventTypes.HEADING_UPDATE,
                                {'heading': 350.0}))
        book.observe(make_event(event_bus.EventTypes.HEADING_UPDATE,
                                {'heading': 10.0}))
        self.assertNotIn('1940-09-06', book.days)  # no event -> no day
        # 350 -> 20 is 30 degrees across north: recorded.
        book.observe(make_event(event_bus.EventTypes.HEADING_UPDATE,
                                {'heading': 20.0}))
        self.assertEqual(book.days['1940-09-06']['events'],
                         ['07:00 Altered course to 020\u00b0'])

    def test_heading_state_survives_journal_round_trip(self):
        book = pl.DayBook()
        book.observe(make_event(event_bus.EventTypes.HEADING_UPDATE,
                                {'heading': 270.0}))
        state = json.loads(json.dumps(book.dump()))
        restored = pl.DayBook()
        restored.load(state)
        self.assertEqual(restored.last_heading, 270.0)
        # A small change after restore is ignored.
        restored.observe(make_event(event_bus.EventTypes.CLOCK_UPDATE,
                                    {'date': '1940-09-06', 'time': '07:00'}))
        restored.observe(make_event(event_bus.EventTypes.HEADING_UPDATE,
                                    {'heading': 280.0}))
        self.assertNotIn('1940-09-06', restored.days)  # no event -> no day

    def test_hysteresis_state_survives_journal_round_trip(self):
        book = pl.DayBook()
        book.observe(make_event(event_bus.EventTypes.DEPTH_UPDATE,
                                {'depth': 80.0}))
        book.observe(make_event(event_bus.EventTypes.SPEED_UPDATE,
                                {'speed': 12.0}))
        state = json.loads(json.dumps(book.dump()))
        restored = pl.DayBook()
        restored.load(state)
        self.assertEqual(restored.last_depth, 80.0)
        self.assertEqual(restored.last_speed, 12.0)
        # A small change after restore is still ignored (no duplicate
        # 'At 80m depth' events after a monitor restart).
        restored.observe(make_event(event_bus.EventTypes.CLOCK_UPDATE,
                                    {'date': '1940-09-06', 'time': '07:00'}))
        restored.observe(make_event(event_bus.EventTypes.DEPTH_UPDATE,
                                    {'depth': 85.0}))
        # Only the original limbo event (folded into the day) is present.
        self.assertEqual(restored.days['1940-09-06']['events'],
                         ['??:?? At 80m depth'])

    def test_journal_round_trip(self):
        book = pl.DayBook()
        book.observe(make_event(event_bus.EventTypes.CLOCK_UPDATE,
                                {'date': '1940-09-06', 'time': '07:00'}))
        book.observe(make_event(event_bus.EventTypes.SHIP_SPOTTED,
                                "Ship spotted, sir!"))
        # Limbo bucket must survive the JSON round trip too.
        book.observe(make_event(event_bus.EventTypes.SHIP_SPOTTED,
                                "Another ship, sir!"))  # same day, no limbo
        state = json.loads(json.dumps(book.dump()))

        restored = pl.DayBook()
        restored.load(state)
        self.assertEqual(restored.current_date, '1940-09-06')
        self.assertEqual(restored.dirty_dates(), ['1940-09-06'])
        self.assertEqual(len(restored.days['1940-09-06']['events']), 2)

    def test_limbo_journal_round_trip(self):
        book = pl.DayBook()
        book.observe(make_event(event_bus.EventTypes.SHIP_SPOTTED,
                                "Ship spotted, sir!"))
        state = json.loads(json.dumps(book.dump()))
        self.assertIn(pl.DayBook._LIMBO_KEY, state['days'])

        restored = pl.DayBook()
        restored.load(state)
        self.assertIn(None, restored.days)
        # Folding still works after restore.
        restored.observe(make_event(event_bus.EventTypes.CLOCK_UPDATE,
                                    {'date': '1940-09-06', 'time': '07:00'}))
        self.assertEqual(restored.dirty_dates(), ['1940-09-06'])


class TestPatrolLogWriter(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.journal = os.path.join(self.tmpdir.name, 'journal.json')
        self.wiki = FakeWiki(content="## Patrol 9\n\n### Aug 25th, 6am\nSet sail")
        self.writer = pl.PatrolLogWriter(self.wiki, journal_path=self.journal)
        # Most tests run on patrol 9 (matching the seeded wiki page).
        self.writer.book.patrol_number = 9

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_midnight_flush_writes_all_dirty_days(self):
        self.writer.book.observe(make_event(
            event_bus.EventTypes.CLOCK_UPDATE,
            {'date': '1940-09-05', 'time': '20:00'}))
        self.writer.book.observe(make_event(
            event_bus.EventTypes.MESSAGE_RECEIVED, "From: BdU\nTo: U-45"))
        self.writer.book.observe(make_event(
            event_bus.EventTypes.CLOCK_UPDATE,
            {'date': '1940-09-06', 'time': '06:00'}))
        self.writer.book.observe(make_event(
            event_bus.EventTypes.SHIP_SPOTTED, "Ship spotted, sir!"))

        generated = []
        original_generate = pl.generate_recap

        def fake_generate(digest, previous_entry=None):
            generated.append(digest['date'])
            return f"Recap for {digest['date']}."

        pl.generate_recap = fake_generate
        try:
            self.writer.midnight_flush()
        finally:
            pl.generate_recap = original_generate

        # Both the sealed day and the active day are written at midnight.
        self.assertEqual(generated, ['1940-09-05', '1940-09-06'])
        self.assertEqual(self.writer.flush_due, [])
        self.assertEqual(self.writer.book.dirty_dates(), [])
        # Newest entry sits directly under the patrol heading.
        self.assertTrue(self.wiki.pages[self.wiki.page].startswith(
            "## Patrol 9\n\n### September 6th\nRecap for 1940-09-06.\n\n"
            "### September 5th\nRecap for 1940-09-05."))

    def test_regeneration_replaces_existing_entry(self):
        self.writer.book.observe(make_event(
            event_bus.EventTypes.CLOCK_UPDATE,
            {'date': '1940-09-06', 'time': '07:00'}))
        self.writer.book.observe(make_event(
            event_bus.EventTypes.SHIP_SPOTTED, "Ship spotted, sir!"))

        pl.generate_recap = lambda digest, previous_entry=None: "First entry."
        self.writer.midnight_flush()
        first = self.wiki.pages[self.wiki.page]

        # More events for the same in-game day (game out of sync with
        # real time) -> day is dirty again.
        self.writer.book.observe(make_event(
            event_bus.EventTypes.AIRCRAFT_SPOTTED, "Aircraft spotted, sir!"))
        self.assertEqual(self.writer.book.dirty_dates(), ['1940-09-06'])

        seen_previous = []
        def fake_generate(digest, previous_entry=None):
            seen_previous.append(previous_entry)
            return "Updated entry."

        pl.generate_recap = fake_generate
        try:
            self.writer.midnight_flush()
        finally:
            pl.generate_recap = lambda d, p=None: "x"

        # The LLM saw the earlier entry and the section was replaced,
        # not duplicated.
        self.assertEqual(seen_previous, ["First entry."])
        self.assertEqual(self.wiki.pages[self.wiki.page].count("### September 6th"), 1)
        self.assertIn("Updated entry.", self.wiki.pages[self.wiki.page])
        self.assertNotIn("First entry.", self.wiki.pages[self.wiki.page])
        self.assertNotEqual(first, self.wiki.pages[self.wiki.page])

    def test_failure_keeps_day_due_for_retry(self):
        self.writer.book.observe(make_event(
            event_bus.EventTypes.CLOCK_UPDATE,
            {'date': '1940-09-06', 'time': '07:00'}))
        self.writer.book.observe(make_event(
            event_bus.EventTypes.SHIP_SPOTTED, "Ship spotted, sir!"))

        def failing_generate(digest, previous_entry=None):
            raise RuntimeError("LLM down")

        original = pl.generate_recap
        pl.generate_recap = failing_generate
        try:
            self.writer.midnight_flush()
        finally:
            pl.generate_recap = original

        self.assertEqual(self.writer.flush_due, ['1940-09-06'])
        self.assertEqual(self.writer.book.dirty_dates(), ['1940-09-06'])
        # Journal persists the due date across restarts.
        self.assertEqual(pl.load_journal(self.journal)['flush_due'],
                         ['1940-09-06'])

    def test_startup_catches_up_sealed_days_only(self):
        # A previous session left a sealed day and an active day, both dirty.
        self.writer.book.observe(make_event(
            event_bus.EventTypes.CLOCK_UPDATE,
            {'date': '1940-09-05', 'time': '20:00'}))
        self.writer.book.observe(make_event(
            event_bus.EventTypes.MESSAGE_RECEIVED, "From: BdU"))
        self.writer.book.observe(make_event(
            event_bus.EventTypes.CLOCK_UPDATE,
            {'date': '1940-09-06', 'time': '06:00'}))
        self.writer.book.observe(make_event(
            event_bus.EventTypes.SHIP_SPOTTED, "Ship spotted, sir!"))
        self.writer.save()

        # Simulate a restart: fresh writer, same journal + wiki.
        writer2 = pl.PatrolLogWriter(self.wiki, journal_path=self.journal)
        generated = []
        pl.generate_recap = lambda digest, previous_entry=None: (
            generated.append(digest['date']) or "Entry.")
        try:
            writer2.load()
            writer2.attempt_flush()
        finally:
            pl.generate_recap = lambda d, p=None: "x"

        # Only the sealed day is written at startup; the active day
        # waits for real midnight.
        self.assertEqual(generated, ['1940-09-05'])
        self.assertEqual(writer2.flush_due, [])
        self.assertEqual(writer2.book.dirty_dates(), ['1940-09-06'])

    def test_empty_days_are_skipped(self):
        self.writer.book.observe(make_event(
            event_bus.EventTypes.CLOCK_UPDATE,
            {'date': '1940-09-06', 'time': '07:00'}))
        # No notable events -> nothing to write at midnight.
        self.writer.midnight_flush()
        self.assertEqual(self.writer.flush_due, [])
        self.assertNotIn("### September 6th", self.wiki.pages[self.wiki.page])


class TestPatrolRouting(unittest.TestCase):
    """Patrol number from the captain's log scanner routes wiki writes."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.journal = os.path.join(self.tmpdir.name, 'journal.json')
        self.wiki = FakeWiki(content="## Patrol 9\n\n### Aug 25th, 6am\nSet sail")
        self.writer = pl.PatrolLogWriter(self.wiki, journal_path=self.journal)
        pl.generate_recap = lambda digest, previous_entry=None: "Entry."

    def tearDown(self):
        self.tmpdir.cleanup()
        pl.generate_recap = lambda d, p=None: "x"

    def _clock(self, date, time_):
        self.writer.book.observe(make_event(event_bus.EventTypes.CLOCK_UPDATE,
                                            {'date': date, 'time': time_}))

    def test_patrol_number_learned_from_captains_log(self):
        self.assertIsNone(self.writer.book.patrol_number)
        self._clock('1940-09-06', '07:00')
        self.writer.book.observe(make_event(
            event_bus.EventTypes.CAPTAINS_LOG,
            "Patrol 9\nU-45, 2nd Flotilla\nLeft at: August 25, 1940, 01:30\n"
            "From: Wilhelmshaven\nMission Orders: Patrol grid BF15"))
        self.assertEqual(self.writer.book.patrol_number, 9)
        # The patrol entry itself is recorded as a notable event.
        day = self.writer.book.days['1940-09-06']
        self.assertTrue(any('Patrol 9' in e for e in day['events']))
        self.assertEqual(day['patrol'], 9)

    def test_patrol_number_backfills_existing_days(self):
        self._clock('1940-09-06', '07:00')
        self.writer.book.observe(make_event(
            event_bus.EventTypes.SHIP_SPOTTED, "Ship spotted, sir!"))
        # Day exists before the patrol number is known.
        self.assertIsNone(self.writer.book.days['1940-09-06']['patrol'])
        self.writer.book.observe(make_event(
            event_bus.EventTypes.CAPTAINS_LOG,
            "Patrol 9\nU-45, 2nd Flotilla\nLeft at: August 25, 1940, 01:30"))
        self.assertEqual(self.writer.book.days['1940-09-06']['patrol'], 9)

    def test_flush_holds_recaps_until_patrol_known(self):
        self._clock('1940-09-06', '07:00')
        self.writer.book.observe(make_event(
            event_bus.EventTypes.SHIP_SPOTTED, "Ship spotted, sir!"))
        self.writer.midnight_flush()
        # No patrol number -> nothing written, day stays due.
        self.assertEqual(self.wiki.writes, [])
        self.assertEqual(self.writer.flush_due, ['1940-09-06'])

        # Captain's log event arrives -> held recap flushes immediately.
        self.writer.handle_event(make_event(
            event_bus.EventTypes.CAPTAINS_LOG,
            "Patrol 9\nU-45, 2nd Flotilla\nLeft at: August 25, 1940, 01:30"))
        self.assertEqual(self.writer.flush_due, [])
        self.assertEqual(self.wiki.writes[0][0],
                         "Games/SH3 Captain's Log/Patrol 9.md")

    def test_new_patrol_gets_its_own_page_created(self):
        self._clock('1940-09-06', '07:00')
        self.writer.book.observe(make_event(
            event_bus.EventTypes.CAPTAINS_LOG,
            "Patrol 9\nU-45, 2nd Flotilla\nLeft at: August 25, 1940, 01:30"))
        self.writer.midnight_flush()
        self.assertIn("Games/SH3 Captain's Log/Patrol 9.md", self.wiki.pages)

        # Patrol 10 starts (new in-game day, new captain's log entry).
        self._clock('1940-09-20', '06:00')
        self.writer.book.observe(make_event(
            event_bus.EventTypes.CAPTAINS_LOG,
            "Patrol 10\nU-45, 2nd Flotilla\nLeft at: September 19, 1940, 22:00"))
        self.writer.book.observe(make_event(
            event_bus.EventTypes.SHIP_SPOTTED, "Ship spotted, sir!"))
        self.writer.midnight_flush()

        # Patrol 10's page was created automatically with its heading.
        page10 = self.wiki.pages["Games/SH3 Captain's Log/Patrol 10.md"]
        self.assertTrue(page10.startswith("## Patrol 10\n\n### September 20th\n"))
        # Patrol 9's page untouched by the new patrol's day.
        self.assertNotIn("### September 20th",
                         self.wiki.pages["Games/SH3 Captain's Log/Patrol 9.md"])

    def test_ship_sunk_recorded_as_event(self):
        self._clock('1940-09-06', '07:00')
        self.writer.book.observe(make_event(
            event_bus.EventTypes.CAPTAINS_LOG,
            "Ship sunk!\nSS Foo\nCargo, 5000 tons"))
        day = self.writer.book.days['1940-09-06']
        self.assertIn(
            "07:00 Captain's log: Ship sunk! — SS Foo (cargo, estimated 5,000 tons)",
            day['events'])

    def test_ship_sunk_tonnage_is_rounded_estimate(self):
        self._clock('1940-09-06', '07:00')
        self.writer.book.observe(make_event(
            event_bus.EventTypes.CAPTAINS_LOG,
            "Ship sunk!\nHMS Barham\nDestroyer, 4873 tons"))
        day = self.writer.book.days['1940-09-06']
        # Exact game tonnage must not appear; rounded estimate does.
        self.assertIn("(destroyer, estimated 5,000 tons)", day['events'][0])
        self.assertNotIn('4,873', day['events'][0])
        self.assertNotIn('4873', day['events'][0])

        # Small warships round to the nearest 100 tons.
        self._clock('1940-09-07', '07:00')
        self.writer.book.observe(make_event(
            event_bus.EventTypes.CAPTAINS_LOG,
            "Ship sunk!\nArmed trawler\nTrawler, 275 tons"))
        day = self.writer.book.days['1940-09-07']
        self.assertIn("(trawler, estimated 300 tons)", day['events'][0])



class TestSSEStream(unittest.TestCase):
    """The recap request is streamed; parse SSE chunks incl. keep-alives."""

    def test_iter_sse_content(self):
        lines = [
            b': keep-alive\n',                      # llama-swap queue frame
            b'\n',
            b'data: {"choices":[{"delta":{"reasoning_content":"thinking..."}}]}\n',
            b'data: {"choices":[{"delta":{"content":"We sailed"}}]}\n',
            b': keep-alive\n',
            b'data: {"choices":[{"delta":{"content":" on."}}]}\n',
            b'data: not-json\n',
            b'data: {"choices":[{"delta":{}}]}\n',
            b'data: [DONE]\n',
            b'data: {"choices":[{"delta":{"content":"ignored"}}]}\n',
        ]
        self.assertEqual(list(pl.iter_sse_content(lines)),
                         ['We sailed', ' on.'])


class TestRecapPrompt(unittest.TestCase):
    def test_prompt_contains_digest(self):
        digest = {
            'date': '1940-09-06',
            'events': ['07:00 Ship spotted: Ship spotted, sir!'],
            'grids': ['AM98', 'AN63'],
            'weather': ['Heavy clouds, no rain.'],
            'max_depth': 95.0,
            'max_speed': 17.0,
        }
        prompt = pl.build_recap_prompt(digest)
        self.assertIn('September 6th, 1940', prompt)
        self.assertIn('AM98, AN63', prompt)
        self.assertIn('Heavy clouds', prompt)
        self.assertIn('95 meters', prompt)
        self.assertIn('17 knots', prompt)
        self.assertIn('Ship spotted', prompt)

    def test_prompt_includes_previous_entry(self):
        digest = {'date': '1940-09-06', 'events': ['07:00 calm'], 'grids': [],
                  'weather': [], 'max_depth': 0, 'max_speed': 0}
        prompt = pl.build_recap_prompt(digest, previous_entry="Old text.")
        self.assertIn('Old text.', prompt)
        self.assertIn('already written', prompt)


if __name__ == '__main__':
    unittest.main()