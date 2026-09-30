#!/usr/bin/env python3
"""
Aircraft Spotted Collector
Monitors SH3 game memory for "Aircraft spotted, sir!" log messages.
Publishes events to the event bus when new aircraft spotted messages are detected.
"""
import sys
import os

# Import shared utilities, event bus, and log collector
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import event_bus
from collectors.log_collector import LogCollectorConfig, run_collector

# --- CONFIGURATION ---
SIG_TEXT = b'Aircraft spotted, sir!'
SIG_HEADER = b'\x75\x80'  # Same as ship_spotted (backward compatibility, not used)
STRIDE = 0x40  # Same as ship_spotted (backward compatibility, not used)
FILTER_TEXT = "Aircraft spotted"
CACHE_FILE = '.sh3_aircraft_spotted_cache.json'
MONITOR_INTERVAL = 5  # seconds between checks

def run():
    """Main collector entry point."""
    # Create configuration for aircraft spotted collector
    config = LogCollectorConfig(
        log_type="aircraft_spotted",
        sig_text=SIG_TEXT,
        sig_header=SIG_HEADER,
        stride=STRIDE,
        filter_text=FILTER_TEXT,
        event_type=event_bus.EventTypes.AIRCRAFT_SPOTTED,
        cache_file=CACHE_FILE,
        monitor_interval=MONITOR_INTERVAL
    )
    
    # Run the collector
    run_collector(config)

if __name__ == "__main__":
    run()
