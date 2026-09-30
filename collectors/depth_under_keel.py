#!/usr/bin/env python3
"""
Depth Under Keel Collector
Monitors SH3 game memory for depth under keel.
Publishes events to the event bus with depth under keel updates.
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
DEPTH_PTR = 0x005543dc
DEPTH_OFFSET = 0xc4
UPDATE_INTERVAL = 0.5  # seconds between updates

def run():
    """Main collector loop."""
    # Get PID
    pid = utils.get_pid_by_name()
    if not pid:
        event_bus.publish_event(event_bus.EventTypes.ERROR,
                               "Depth under keel collector: Could not find SH3 process")
        return
    
    try:
        while True:
            try:
                # Read depth from memory
                depth = utils.get_value(DEPTH_PTR, DEPTH_OFFSET, pid)
                
                # Publish event
                event_bus.publish_event(
                    event_bus.EventTypes.DEPTH_UNDER_KEEL_UPDATE,
                    {
                        'depth': depth,
                        'timestamp': datetime.now().isoformat()
                    }
                )
                
            except Exception as e:
                event_bus.publish_event(event_bus.EventTypes.ERROR,
                                       f"Depth under keel collector error: {e}")
                time.sleep(5)  # Wait before retrying
                continue
            
            time.sleep(UPDATE_INTERVAL)
            
    except KeyboardInterrupt:
        event_bus.publish_event(event_bus.EventTypes.STATUS,
                               "Depth under keel collector stopped")

if __name__ == "__main__":
    run()
