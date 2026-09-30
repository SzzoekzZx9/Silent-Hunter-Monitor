#!/usr/bin/env python3
"""
Ship Spotted Collector
Monitors SH3 game memory for "Ship spotted, sir!" log messages.
Publishes events to the event bus when new ship spotted messages are detected.
"""
import sys
import os

# Import shared utilities, event bus, and log collector
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import event_bus
from collectors.log_collector import LogCollectorConfig, run_collector

# --- CONFIGURATION ---
SIG_TEXT = b'Ship spotted, sir!'
SIG_HEADER = b'\x75\x80'
STRIDE = 0x40
FILTER_TEXT = "Ship spotted"
CACHE_FILE = '.sh3_ship_spotted_cache.json'
MONITOR_INTERVAL = 5  # seconds between checks

def run():
    """Main collector entry point."""
    # Create configuration for ship spotted collector
    config = LogCollectorConfig(
        log_type="ship_spotted",
        sig_text=SIG_TEXT,
        sig_header=SIG_HEADER,
        stride=STRIDE,
        filter_text=FILTER_TEXT,
        event_type=event_bus.EventTypes.SHIP_SPOTTED,
        cache_file=CACHE_FILE,
        monitor_interval=MONITOR_INTERVAL
    )
    
    # Run the collector
    run_collector(config)

if __name__ == "__main__":
    run()
