#!/usr/bin/env python3
"""
Location Collector
Monitors SH3 game memory for the uboat's TRUE naval grid reference and
live position, using the reverse-engineered grid scanner from issue #17
(memory-tools/grid_collector.py).

The game world is equirectangular (1 degree = 120 km exactly).  The boat's
true (map) position is read via a static pointer chain
([0x93f644] -> +0x88 -> +0x18/+0x20, doubles in 1/120000 degree units)
and converted to a Marinequadrat reference with the ported transform.

NOTE: the game's own grid string at 0x53cfec is computed from a WRONGLY
projected position (it can differ from the map by more than a quadrat,
e.g. 'BE 33' while the boat is in BE 32).  It is published for reference
as 'game_grid' only and is never used to validate the position.

Published LOCATION_UPDATE payload:
  {'grid': 'BE 32' | None,        # true grid (from the map position)
   'available': bool,             # a valid grid is known
   'lat': 50.319, 'lon_w': 14.27, # true live position (when resolved)
   'position': bool,              # position resolved
   'game_grid': 'BE 33' | None,   # the game's own (wrong) string
   'timestamp': iso8601}

The collector fails soft: if sh3.exe is not running it retries quietly so
the rest of the monitor keeps running.
"""
import os
import sys
import time
from datetime import datetime

# Import shared utilities and event bus
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Grid scanner lives in memory-tools/ (issue #17)
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'memory-tools'))
import event_bus
import grid_collector

UPDATE_INTERVAL = 1.0   # seconds between LOCATION_UPDATE events


def _acquire():
    """Find the game process and open its memory. Returns (pid, mem)."""
    pid = grid_collector.find_pid()
    return pid, open(f'/proc/{pid}/mem', 'rb', buffering=0)


def run():
    """Main collector loop."""
    mem = None
    try:
        while True:
            # (Re)acquire the game process after restarts
            if mem is None:
                try:
                    _, mem = _acquire()
                except (SystemExit, OSError):
                    event_bus.publish_event(event_bus.EventTypes.STATUS,
                                           "Location collector: sh3.exe not running")
                    time.sleep(5)
                    continue

            try:
                pos = grid_collector.read_boat(mem)
                game_grid = grid_collector.read_grid(mem)
            except OSError:
                mem.close()
                mem = None
                continue

            if pos is not None:
                lat, lon_w = pos
                grid = grid_collector.compute_grid(lat, lon_w)
            else:
                lat = lon_w = None
                grid = None

            event_bus.publish_event(
                event_bus.EventTypes.LOCATION_UPDATE,
                {
                    'grid': grid,
                    'available': grid is not None,
                    'lat': lat,
                    'lon_w': lon_w,
                    'position': pos is not None,
                    'game_grid': game_grid,
                    'timestamp': datetime.now().isoformat()
                }
            )

            time.sleep(UPDATE_INTERVAL)

    except KeyboardInterrupt:
        event_bus.publish_event(event_bus.EventTypes.STATUS,
                               "Location collector stopped")
    finally:
        if mem is not None:
            try:
                mem.close()
            except OSError:
                pass


if __name__ == "__main__":
    run()