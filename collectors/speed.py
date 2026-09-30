#!/usr/bin/env python3
"""
Speed Collector
Monitors SH3 game memory for uboat speed.
Publishes events to the event bus with speed updates.
"""
import sys
import os
import time
from datetime import datetime

# Import shared utilities and event bus
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import utils
import event_bus

# --- CONFIGURATION ---
SPEED_PTR = 0x00554698
SPEED_OFFSET = 0x68
UPDATE_INTERVAL = 0.5  # seconds between updates

def run():
    """Main collector loop."""
    # Get PID
    pid = utils.get_pid_by_name()
    if not pid:
        event_bus.publish_event(event_bus.EventTypes.ERROR,
                               "Speed collector: Could not find SH3 process")
        return
    
    try:
        while True:
            try:
                # Read speed from memory
                speed = utils.get_value(SPEED_PTR, SPEED_OFFSET, pid)
                
                # Publish event
                event_bus.publish_event(
                    event_bus.EventTypes.SPEED_UPDATE,
                    {
                        'speed': speed,
                        'timestamp': datetime.now().isoformat()
                    }
                )
                
            except Exception as e:
                event_bus.publish_event(event_bus.EventTypes.ERROR,
                                       f"Speed collector error: {e}")
                time.sleep(5)  # Wait before retrying
                continue
            
            time.sleep(UPDATE_INTERVAL)
            
    except KeyboardInterrupt:
        event_bus.publish_event(event_bus.EventTypes.STATUS,
                               "Speed collector stopped")

if __name__ == "__main__":
    run()
