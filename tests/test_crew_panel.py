"""Tests for the crew screen (story #13).

Covers the pure parts of the crew display: cutaway geometry, glyph
placement, fatigue thresholds, roster formatting, and the CREW_UPDATE
event handler. No real terminal needed.
"""
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sh3_master_ncurses as ui
import event_bus


class CutawayTests(unittest.TestCase):
    def setUp(self):
        self.art = ui.build_cutaway()
        self.compartment_x = {cid: (x0, x1) for cid, _l1, _l2, x0, x1 in ui.COMPARTMENTS}

    def test_dimensions(self):
        self.assertEqual(len(self.art), ui.CUTAWAY_HEIGHT)
        for line in self.art:
            self.assertEqual(len(line), ui.CUTAWAY_WIDTH)

    def test_hull_borders_present(self):
        self.assertTrue(self.art[5].startswith(' \u2554'))
        self.assertTrue(self.art[5].endswith('\u2557 '))
        self.assertTrue(self.art[10].startswith(' \u255a'))
        self.assertTrue(self.art[10].endswith('\u255d '))

    def test_all_slots_on_blank_cells(self):
        """Every glyph slot must land on a blank cell of the static art."""
        for st, slots in ui.STATION_SLOTS.items():
            for y, x in slots:
                self.assertEqual(
                    self.art[y][x], ' ',
                    f'{st} slot ({y},{x}) collides with art char {self.art[y][x]!r}')

    def test_hull_slots_inside_their_compartment(self):
        """Hull station slots must stay between their compartment walls."""
        hull = {cid for cid, *_ in ui.COMPARTMENTS}
        for st, slots in ui.STATION_SLOTS.items():
            if st not in hull:
                continue
            x0, x1 = self.compartment_x[st]
            for y, x in slots:
                self.assertTrue(6 <= y <= 9, f'{st} slot row {y} outside hull')
                self.assertTrue(x0 <= x <= x1,
                                f'{st} slot col {x} outside {x0}..{x1}')

    def test_overflow_marker_cells_are_blank(self):
        """The overflow marker positions (row 9, compartment right edge)."""
        for cid, _l1, _l2, _x0, x1 in ui.COMPARTMENTS:
            self.assertEqual(self.art[9][x1], ' ',
                             f'{cid} overflow cell (9,{x1}) not blank')


class CrewGlyphTests(unittest.TestCase):
    def test_glyph_roles(self):
        self.assertEqual(ui.crew_glyph({'specialty': 'watch', 'on_duty': True}), 'W')
        self.assertEqual(ui.crew_glyph({'specialty': 'engineer', 'on_duty': True}), 'L')
        self.assertEqual(ui.crew_glyph({'specialty': None, 'on_duty': True}), 'o')
        self.assertEqual(ui.crew_glyph({'specialty': 'watch', 'on_duty': False}), 'z')

    def test_fatigue_colors(self):
        self.assertEqual(ui.fatigue_color_pair(0.0), 1)
        self.assertEqual(ui.fatigue_color_pair(0.49), 1)
        self.assertEqual(ui.fatigue_color_pair(0.5), 3)
        self.assertEqual(ui.fatigue_color_pair(0.74), 3)
        self.assertEqual(ui.fatigue_color_pair(0.75), 4)
        self.assertEqual(ui.fatigue_color_pair(1.0), 4)

    def test_off_duty_displayed_in_bunks(self):
        members = [
            {'name': 'A', 'station': 'engine_room', 'on_duty': False, 'fatigue': 0.1},
            {'name': 'B', 'station': 'engine_room', 'on_duty': False, 'fatigue': 0.1,
             'officer': True},
        ]
        pos = ui.crew_glyph_positions(members)
        crew_bunks = {tuple(s) for s in ui.STATION_SLOTS['crew_quarters']}
        officer_bunks = {tuple(s) for s in ui.STATION_SLOTS['officers_quarters']}
        # pos follows member order: enlisted first, officer second
        self.assertIn((pos[0][0], pos[0][1]), crew_bunks)
        self.assertIn((pos[1][0], pos[1][1]), officer_bunks)
        for p in pos:
            self.assertTrue(p[5], 'off-duty glyphs must be dim')

    def test_overflow_marker(self):
        # engine_room has 5 slots; add 7 crew there
        members = [{'name': f'm{i}', 'station': 'engine_room',
                    'on_duty': True, 'fatigue': 0.2} for i in range(7)]
        pos = ui.crew_glyph_positions(members)
        glyphs = [p[2] for p in pos]
        self.assertEqual(glyphs.count('o'), 5)
        self.assertEqual(glyphs.count('+'), 1)

    def test_no_collision_between_glyphs(self):
        pos = ui.crew_glyph_positions(ui.demo_crew())
        cells = [(y, x) for y, x, *_ in pos]
        self.assertEqual(len(cells), len(set(cells)),
                         'two glyphs drawn on the same cell')


class DemoCrewTests(unittest.TestCase):
    def test_demo_crew_valid(self):
        crew = ui.demo_crew()
        self.assertGreater(len(crew), 0)
        known_stations = set(ui.STATION_SLOTS)
        for m in crew:
            self.assertIn('name', m)
            self.assertIn('station', m)
            self.assertIn(m['station'], known_stations)
            self.assertTrue(0.0 <= m['fatigue'] <= 1.0)
            self.assertIsInstance(m['on_duty'], bool)

    def test_demo_crew_fits_slots(self):
        """The demo roster must not overflow any station's slots."""
        pos = ui.crew_glyph_positions(ui.demo_crew())
        self.assertFalse(any(p[2] == '+' for p in pos),
                         'demo crew overflows a station - add slots or trim crew')


class RosterFormatTests(unittest.TestCase):
    def test_fatigue_bar(self):
        self.assertEqual(ui.fatigue_bar(0.0, 6), '\u2591' * 6)
        self.assertEqual(ui.fatigue_bar(1.0, 6), '\u2588' * 6)
        self.assertEqual(ui.fatigue_bar(0.5, 6), '\u2588' * 3 + '\u2591' * 3)

    def test_roster_row_truncates_name(self):
        m = {'name': 'Verylongname-That-Does-Not-Fit', 'rank': 'Matrose',
             'specialty': None, 'station': 'engine_room', 'on_duty': True,
             'fatigue': 0.5}
        row = ui.format_roster_row(1, m, 8, 0, 6)
        self.assertLessEqual(len(row), 2 + 1 + 8 + 1 + 6 + 1 + 4)
        self.assertIn('50%', row)

    def test_roster_row_bunk_station(self):
        m = {'name': 'A', 'station': 'engine_room', 'on_duty': False,
             'fatigue': 0.2}
        row = ui.format_roster_row(2, m, 8, 10, 6)
        self.assertIn('Bunk', row)

    def test_summary(self):
        s = ui.crew_summary(ui.demo_crew())
        self.assertIn('aboard', s)
        self.assertIn('on duty', s)
        self.assertIn('worst:', s)


class CrewEventTests(unittest.TestCase):
    def tearDown(self):
        ui.crew_state = None
        ui.crew_demo = False

    def test_handle_crew_update_stores_state(self):
        crew = ui.demo_crew()
        event = event_bus.Event(event_bus.EventTypes.CREW_UPDATE, datetime.now(),
                                {'crew': crew})
        ui.handle_crew_update(event)
        self.assertEqual(ui.crew_state, crew)
        self.assertFalse(ui.crew_demo)

    def test_handle_crew_update_replaces_demo(self):
        ui.crew_state = ui.demo_crew()
        ui.crew_demo = True
        crew = [{'name': 'X', 'station': 'bridge', 'on_duty': True,
                 'fatigue': 0.1}]
        event = event_bus.Event(event_bus.EventTypes.CREW_UPDATE, datetime.now(),
                                {'crew': crew})
        ui.handle_crew_update(event)
        self.assertEqual(ui.crew_state, crew)
        self.assertFalse(ui.crew_demo)

    def test_handle_crew_update_ignores_bad_payload(self):
        ui.crew_state = ui.demo_crew()
        event = event_bus.Event(event_bus.EventTypes.CREW_UPDATE, datetime.now(),
                                {'crew': None})
        ui.handle_crew_update(event)
        self.assertIsNotNone(ui.crew_state)  # unchanged

    def test_crew_update_event_type_exists(self):
        self.assertEqual(event_bus.EventTypes.CREW_UPDATE, 'crew_update')


if __name__ == '__main__':
    unittest.main()