"""Tests for the ncurses UI resize handling (story #18).

Covers the pure layout computation and the resize-repaint wiring
(apply_resize) with mocked curses windows - no real terminal needed.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sh3_master_ncurses as ui


class ComputeLayoutTests(unittest.TestCase):
    def test_valid_layout(self):
        layout = ui.compute_layout(50, 212)
        self.assertIsNotNone(layout)
        # three columns: status+log | radio | captain's log
        self.assertEqual(layout['radio_width'], 42)
        self.assertEqual(layout['captains_width'], 48)
        self.assertEqual(layout['left_width'], 212 - 42 - 48)
        self.assertEqual(layout['log_height'], 50 - ui.STATUS_HEIGHT - ui.FOOTER_HEIGHT)
        self.assertEqual(layout['side_height'], ui.STATUS_HEIGHT + layout['log_height'])
        self.assertEqual(layout['width'], 212)

    def test_minimum_size_is_valid(self):
        """Exactly the minimum must produce a layout, not None."""
        self.assertIsNotNone(ui.compute_layout(ui.MIN_HEIGHT, ui.MIN_WIDTH))

    def test_too_narrow(self):
        self.assertIsNone(ui.compute_layout(50, ui.MIN_WIDTH - 1))

    def test_too_short(self):
        self.assertIsNone(ui.compute_layout(ui.MIN_HEIGHT - 1, 212))


class ApplyResizeTests(unittest.TestCase):
    def _fake_newwin(self):
        """Patch curses.newwin with a recorder returning mock windows."""
        created = []

        def factory(rows, cols, y, x):
            win = mock.Mock()
            win.geometry = (rows, cols, y, x)
            created.append(win)
            return win

        return created, factory

    def test_recreates_windows_and_forces_repaint(self):
        created, factory = self._fake_newwin()
        stdscr = mock.Mock()
        stdscr.getmaxyx.return_value = (50, 212)
        prev = {'initialized': True, 'aircraft_line': 'stale'}

        with mock.patch.object(ui.curses, 'newwin', side_effect=factory), \
             mock.patch.object(ui.curses, 'endwin') as endwin:
            windows, layout = ui.apply_resize(stdscr, prev)

        self.assertIsNotNone(windows)
        self.assertEqual(len(created), 5)  # five fresh panel windows
        self.assertEqual(set(windows), {'status', 'log', 'radio', 'captains', 'footer'})
        # the endwin()/refresh() dance invalidates ncurses' screen model
        # (curscr) so the next doupdate() fully repaints the physical screen;
        # without it, static content identical to the stale curscr is never
        # re-emitted and stays blank after a resize
        endwin.assert_called_once_with()
        stdscr.refresh.assert_called_once_with()
        # the partial-refresh cache is cleared -> static rows are redrawn
        self.assertEqual(prev, {})

    def test_too_small_shows_message_and_keeps_state(self):
        created, factory = self._fake_newwin()
        stdscr = mock.Mock()
        stdscr.getmaxyx.return_value = (20, 80)
        prev = {'initialized': True}

        with mock.patch.object(ui.curses, 'newwin', side_effect=factory), \
             mock.patch.object(ui.curses, 'endwin'):
            windows, layout = ui.apply_resize(stdscr, prev)

        self.assertIsNone(windows)
        self.assertIsNone(layout)
        self.assertEqual(len(created), 1)  # only the message window
        msg = created[0]
        msg.noutrefresh.assert_called_once_with()
        # message text was drawn on it
        self.assertTrue(msg.addstr.called)
        # the refresh cache is NOT cleared while too small
        self.assertEqual(prev, {'initialized': True})


class FitTests(unittest.TestCase):
    def test_pads_short_line(self):
        self.assertEqual(ui.fit('abc', 6), 'abc   ')

    def test_truncates_long_line(self):
        self.assertEqual(ui.fit('abcdefg', 6), 'abcdef')

    def test_exact_length_unchanged(self):
        self.assertEqual(ui.fit('abcdef', 6), 'abcdef')


if __name__ == '__main__':
    unittest.main()