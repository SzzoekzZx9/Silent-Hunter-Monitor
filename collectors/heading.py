#!/usr/bin/env python3
"""
Heading Collector
Monitors SH3 game memory for the uboat's heading (course).
Publishes events to the event bus with heading updates.

The heading is a float (degrees, 0-359.9...) in the same player-boat
structure as the speed (collectors/speed.py): pointer 0x00554698,
heading at +0x64, speed at +0x68. Located by diffing memory snapshots
across controlled course changes (north -> west -> east).
"""
import sys
import os
import time
from datetime import datetime

# Import shared utilities and event bus
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import utils
import event_bus

# --- CONFIGURATION ---
HEADING_PTR = 0x00554698
HEADING_OFFSET = 0x64
UPDATE_INTERVAL = 0.5  # seconds between updates

def run():
    """Main collector loop."""
    # Get PID
    pid = utils.get_pid_by_name()
    if not pid:
        event_bus.publish_event(event_bus.EventTypes.ERROR,
                               "Heading collector: Could not find SH3 process")
        return

    try:
        while True:
            try:
                # Read heading from memory and normalize to [0, 360)
                heading = utils.get_value(HEADING_PTR, HEADING_OFFSET, pid)
                heading = heading % 360.0

                # Publish event
                event_bus.publish_event(
                    event_bus.EventTypes.HEADING_UPDATE,
                    {
                        'heading': heading,
                        'timestamp': datetime.now().isoformat()
                    }
                )

            except Exception as e:
                event_bus.publish_event(event_bus.EventTypes.ERROR,
                                       f"Heading collector error: {e}")
                time.sleep(5)  # Wait before retrying
                continue

            time.sleep(UPDATE_INTERVAL)

    except KeyboardInterrupt:
        event_bus.publish_event(event_bus.EventTypes.STATUS,
                               "Heading collector stopped")

if __name__ == "__main__":
    run()