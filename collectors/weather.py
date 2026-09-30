#!/usr/bin/env python3
"""
Weather Report Collector
Actively queries the Navigation Officer for weather reports by sending Ctrl-W
to the game window (via xdotool), then reads the resulting HUD message from
game memory and publishes it to the event bus with the in-game date/time.

How it works:
1. Every QUERY_INTERVAL seconds, send Ctrl-W to the SilentHunter3 window.
   The Navigation Officer (NA) responds with a weather report that appears in
   the HUD message buffer with markup like:
       `C0xCCCCCCFF`16:40 `T40`NA:  `T68`Heavy clouds, no rain, moderate
       visibility, Wind speed 14 m/s, direction  27.
2. Scan memory for "Wind speed" and validate the surrounding HUD markup.
   The static format-string template ("%s, %s, %s Wind speed %d m/s, ...")
   and the captain's log array entries are rejected by the validator.
3. Read the in-game clock UI string ("September 5, 1940\\nLocal time: 07:23")
   for the date. The HUD line itself carries the in-game time (HH:MM).

See specs/weather-reports.md for the memory-layout findings behind this.
The shared memory-scan helpers (iter_memory_chunks, find_ui_clock) live in
utils.py and are also used by the clock collector.
"""
import sys
import os
import re
import time
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import utils
import event_bus

# --- CONFIGURATION ---
# Time between weather queries (Ctrl-W). Default: every 6 hours.
# Override with SH3_WEATHER_INTERVAL - plain seconds ("900") or with a
# unit suffix ("90m", "6h", "1d").
def _parse_interval(value):
    value = value.strip().lower()
    for suffix, mult in (('s', 1), ('m', 60), ('h', 3600), ('d', 86400)):
        if value.endswith(suffix):
            return int(float(value[:-1]) * mult)
    return int(value)


QUERY_INTERVAL = _parse_interval(os.environ.get('SH3_WEATHER_INTERVAL', '6h'))
# Seconds to wait after Ctrl-W before scanning (message must render first).
POST_TRIGGER_DELAY = 3
# X server for xdotool (the game runs on the host display).
DISPLAY = os.environ.get('DISPLAY', ':0')
XAUTHORITY = os.environ.get('XAUTHORITY', os.path.expanduser('~/.Xauthority'))
# Signature shared by all weather report strings (HUD messages, log entries
# and the static format template all contain it; the validator sorts them out).
SIG_TEXT = b'Wind speed'

# HUD weather message: `C0xCCCCCCFF`16:40 `T40`NA:  `T68`Heavy clouds, ...
# The timestamp and NA role prefix only exist on live HUD messages, which
# excludes the captain's log entries and the '%' format template.
HUD_WEATHER_RE = re.compile(
    r'`C0x[0-9A-Fa-f]+`(\d{1,2}):(\d{2}) `T40`NA:\s*`T68`'
    r'(.{10,200}?Wind speed \d+ m/s, direction\s*\d+\.)',
    re.DOTALL
)

# In-game clock reading is shared with the clock collector (see utils.py).
from utils import find_ui_clock, iter_memory_chunks


def trigger_weather_query():
    """Send Ctrl-W to the game window to request a weather report."""
    env = dict(os.environ, DISPLAY=DISPLAY, XAUTHORITY=XAUTHORITY)
    result = subprocess.run(
        ['xdotool', 'key', '--clearmodifiers', 'ctrl+w'],
        env=env, capture_output=True, timeout=10
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"xdotool failed (rc={result.returncode}): "
            f"{result.stderr.decode(errors='replace').strip()}")


def parse_weather_messages(pid):
    """
    Scan memory for HUD weather messages.

    Returns a list of (hh, mm, text) tuples, sorted by time.
    """
    messages = []
    seen = set()
    for off, buf in iter_memory_chunks(pid, 1 << 22, 512):
        idx = 0
        while True:
            i = buf.find(SIG_TEXT, idx)
            if i == -1:
                break
            # The signature sits mid-string; take a context window around it
            # so the regex can see the HUD timestamp and role prefix.
            lo = max(0, i - 128)
            ctx = buf[lo:i + 384]
            for m in HUD_WEATHER_RE.finditer(ctx.decode('utf-8', errors='replace')):
                hh, mm, text = int(m.group(1)), int(m.group(2)), m.group(3)
                text = re.sub(r'\s+', ' ', text).strip()
                key = (hh, mm, text)
                if key not in seen:
                    seen.add(key)
                    messages.append(key)
            idx = i + 1
    messages.sort(key=lambda x: (x[0], x[1]))
    return messages


def run():
    """Main collector loop."""
    seen_reports = set()   # (date, hhmm, text) tuples already published

    event_bus.publish_event(event_bus.EventTypes.STATUS,
                           f"Weather collector started (query interval {QUERY_INTERVAL}s)")

    while True:
        pid = utils.get_pid_by_name()
        if not pid:
            time.sleep(30)
            continue

        try:
            # 1. Ask the Navigation Officer for a weather report
            trigger_weather_query()
            time.sleep(POST_TRIGGER_DELAY)

            # 2. Read the HUD messages (newest = the one we just triggered)
            messages = parse_weather_messages(pid)
            if not messages:
                event_bus.publish_event(event_bus.EventTypes.STATUS,
                                       "Weather collector: no weather message found after Ctrl-W")
                time.sleep(QUERY_INTERVAL)
                continue

            # 3. Timestamp with the in-game date from the clock UI string;
            #    the time comes from the HUD message itself.
            date_str = "????-??-??"
            ui = find_ui_clock(pid)
            if ui:
                local_ymd, _local_min = ui
                date_str = f"{local_ymd[0]:04d}-{local_ymd[1]:02d}-{local_ymd[2]:02d}"
            else:
                event_bus.publish_event(event_bus.EventTypes.STATUS,
                                       "Weather collector: in-game clock UI string not found")

            # 4. Publish any reports we haven't seen before
            new_count = 0
            for hh, mm, text in messages:
                key = (date_str, f"{hh:02d}{mm:02d}", text)
                if key in seen_reports:
                    continue
                seen_reports.add(key)
                event_bus.publish_event(event_bus.EventTypes.WEATHER, {
                    'date': date_str,
                    'time': f"{hh:02d}:{mm:02d}",
                    'text': text,
                })
                new_count += 1

            if new_count:
                event_bus.publish_event(event_bus.EventTypes.STATUS,
                                       f"Weather collector: published {new_count} weather report(s)")

        except RuntimeError as e:
            event_bus.publish_event(event_bus.EventTypes.ERROR,
                                   f"Weather collector: {e}")
        except Exception as e:
            event_bus.publish_event(event_bus.EventTypes.ERROR,
                                   f"Weather collector error: {e}")

        time.sleep(QUERY_INTERVAL)


if __name__ == "__main__":
    run()