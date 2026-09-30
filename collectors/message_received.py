#!/usr/bin/env python3
"""
Message Received Collector
Monitors SH3 game memory for radio message bodies and publishes events to the
event bus when new messages are received.

Radio messages are stored in the game's message history as strings beginning
with "From:", e.g.:
    From: U-56\\nTo: BdU\\n27/VIII \\nAM 0273 \\n Harms
    From: Lehmann Radio News\\nTo: on air\\n26/VIII \\nThe Royal Air Force bombed Berlin yesterday.

We scan for the "From:" signature and use validate_radio_message_format() to
filter out format-string templates and mission-order strings. New messages
appear at new addresses, so periodic rediscovery (every 60s) is used to catch
them; a full scan takes ~5s.
"""
import sys
import os

# Import shared utilities, event bus, and log collector
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import utils
import event_bus
from collectors.log_collector import LogCollectorConfig, run_collector

# --- CONFIGURATION ---
# Radio message bodies in the history array start with "From:".
SIG_TEXT = b'From:'
FILTER_TEXT = None  # signature + validator are sufficient
CACHE_FILE = '.sh3_message_received_cache.json'
MONITOR_INTERVAL = 5  # seconds between checks (heartbeats + re-reads)
# New messages appear at new addresses; rediscover frequently to catch them.
# A full scan takes ~5s, so 60s gives prompt detection at low cost.
REDISCOVERY_INTERVAL = 60
# History entries are immutable and don't move, so a small search window keeps
# periodic re-reads cheap. Discovery still does a full-memory seed scan.
SEARCH_WINDOW = 0x1000

def run():
    """Main collector entry point."""
    config = LogCollectorConfig(
        log_type="message_received",
        sig_text=SIG_TEXT,
        filter_text=FILTER_TEXT,
        event_type=event_bus.EventTypes.MESSAGE_RECEIVED,
        cache_file=CACHE_FILE,
        monitor_interval=MONITOR_INTERVAL,
        rediscovery_interval=REDISCOVERY_INTERVAL,
        search_window=SEARCH_WINDOW,
        validator=utils.validate_radio_message_format,
        publish_initial_logs=True,
    )
    run_collector(config)

if __name__ == "__main__":
    run()