#!/usr/bin/env python3
"""
Captain's Log Collector
Monitors SH3 game memory for captain's log entries (the in-game "CAPTAIN'S
LOG" screen) and publishes them to the event bus.

Entries live in the same message-history array structure as radio messages.
Known entry formats (from format templates in the game's static data):
    Patrol %d\n%s, %s\nLeft at: %s\nFrom: %s   (+ \nMission Orders: Patrol grid %s)
    Ship sunk!\n%s\n%s, %d tons
    Aircraft destroyed!\n%s\n%s
    Patrol grid %s for %d hours
    Primary Objectives Completed
    Patrol started\n%s\nDate: %s\nLocation: %s

Example live entry:
    Patrol 9\nU-45, 2nd Flotilla\nLeft at: August 25, 1940, 01:30\n
    From: Wilhelmshaven\nMission Orders: Patrol grid BF15

Because some signatures match mid-string (e.g. "Left at:" appears on the
third line of the patrol entry), the collector uses sig_at_start=False so
the reader backtracks from each seed to the start of the containing string.
"""
import sys
import os

# Import shared utilities, event bus, and log collector
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import utils
import event_bus
from collectors.log_collector import LogCollectorConfig, run_collector

# --- CONFIGURATION ---
# Signatures for all known captain's log entry types. Format templates in
# static data also match these but are rejected by the validator (% placeholder).
SIG_TEXTS = [
    b'Left at:',                      # Patrol start entry (matches mid-string)
    b'Ship sunk!\n',                  # Ship sunk entry
    b'Aircraft destroyed!\n',         # Aircraft destroyed entry
    b'Patrol started\n',              # Patrol started entry
    b'Primary Objectives Completed',  # Objectives completed entry
    b'Patrol grid ',                  # "Patrol grid XX## for N hours" (also mid-string
                                      # in the patrol entry; content dedup handles it)
]
FILTER_TEXT = None  # validator is sufficient
# Static data block containing the format templates (0x3b4f26f1-0x3b4f2800).
# Templates with % placeholders are rejected by the validator, but
# 'Primary Objectives Completed' has no placeholders, so the whole block
# is excluded by address.
REJECT_REGIONS = [(0x3b4f2000, 0x3b4f3000)]
CACHE_FILE = '.sh3_captains_log_cache.json'
MONITOR_INTERVAL = 5  # seconds between checks


def validate_captains_log_format(text):
    """
    Validates that the text is a real captain's log entry (not a format
    template from static data, and not a radio message that merely mentions
    a signature word).

    Args:
        text: Log text to validate

    Returns:
        True if the text is a valid captain's log entry
    """
    # Format templates contain % placeholders
    if '%' in text:
        return False
    if len(text) < 10 or len(text) > 600:
        return False
    # Patrol start: "Patrol 9\nU-45, 2nd Flotilla\nLeft at: ..."
    if text.startswith('Patrol ') and '\nLeft at: ' in text:
        return True
    if text.startswith('Ship sunk!'):
        return True
    if text.startswith('Aircraft destroyed!'):
        return True
    if text.startswith('Patrol started'):
        return True
    if text.startswith('Primary Objectives Completed'):
        return True
    # "Patrol grid BF15 for 32 hours"
    if text.startswith('Patrol grid ') and ' hours' in text:
        return True
    return False


def run():
    """Main collector entry point."""
    config = LogCollectorConfig(
        log_type="captains_log",
        sig_text=SIG_TEXTS,
        filter_text=FILTER_TEXT,
        event_type=event_bus.EventTypes.CAPTAINS_LOG,
        cache_file=CACHE_FILE,
        monitor_interval=MONITOR_INTERVAL,
        sig_at_start=False,          # signatures may match mid-string
        publish_initial_logs=True,   # panel should show existing entries on startup
        reject_regions=REJECT_REGIONS,
        validator=validate_captains_log_format
    )

    # Run the collector
    run_collector(config)

if __name__ == "__main__":
    run()