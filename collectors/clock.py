#!/usr/bin/env python3
"""
In-Game Clock Collector
Polls the SH3 clock UI string in game memory and publishes the current
in-game date/time to the event bus as CLOCK_UPDATE events.

The clock string ("September 5, 1940\\nLocal time: 07:23") is the same one
the weather collector reads; the scanning logic lives in utils.find_ui_clock
(see specs/weather-reports.md for the memory-layout findings).

CLOCK_UPDATE events drive anything that needs to know the in-game time:
the patrol log writer uses them to detect the in-game day rollover and
timestamp daily recaps.

Publishes only when the in-game minute changes, so the event stream stays
quiet while the game is paused.
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import utils
import event_bus

# --- CONFIGURATION ---
# Seconds between clock polls. The in-game clock advances faster than real
# time when time compression is on, so keep this short enough to catch the
# day rollover promptly. Override with SH3_CLOCK_INTERVAL.
POLL_INTERVAL = float(os.environ.get('SH3_CLOCK_INTERVAL', '30'))


def read_clock(pid):
    """
    Read the in-game clock.

    Returns a dict {'date': 'YYYY-MM-DD', 'time': 'HH:MM',
                    'ymd': [year, month, day], 'minutes': minutes_of_day}
    or None if the clock string is not found.
    """
    ui = utils.find_ui_clock(pid)
    if not ui:
        return None
    (year, month, day), minutes = ui
    return {
        'date': f"{year:04d}-{month:02d}-{day:02d}",
        'time': f"{minutes // 60:02d}:{minutes % 60:02d}",
        'ymd': [year, month, day],
        'minutes': minutes,
    }


def run():
    """Main collector loop."""
    last_clock = None

    event_bus.publish_event(event_bus.EventTypes.STATUS,
                           f"Clock collector started (poll interval {POLL_INTERVAL}s)")

    while True:
        pid = utils.get_pid_by_name()
        if not pid:
            time.sleep(30)
            continue

        try:
            clock = read_clock(pid)
            if clock is None:
                event_bus.publish_event(event_bus.EventTypes.STATUS,
                                       "Clock collector: in-game clock UI string not found")
            elif clock != last_clock:
                last_clock = clock
                event_bus.publish_event(event_bus.EventTypes.CLOCK_UPDATE, clock)
        except Exception as e:
            event_bus.publish_event(event_bus.EventTypes.ERROR,
                                   f"Clock collector error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()