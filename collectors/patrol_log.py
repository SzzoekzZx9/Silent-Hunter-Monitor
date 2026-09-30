#!/usr/bin/env python3
"""
Patrol Log Writer — LLM-generated daily Captain's Log recaps.

Subscribes to the tracker event bus and accumulates a digest for every
in-game day (sightings, radio traffic, weather reports, grid changes,
dive/speed stats), keyed by in-game date.

Generation is triggered by REAL time, not game time: once per real-life
day at midnight, every in-game day that has unwritten events is written
to the wiki as its own entry. If the game runs in sync with real time,
this is also the in-game day boundary. If not (time compression, paused
game, monitor restarted), multiple in-game days may be pending — each
gets its own entry — and an in-game day that spans several real days is
regenerated (replaced on the wiki) at each midnight so it stays a single
coherent entry for that day.

Entries are written to the Gollum wiki, newest-first, directly under the
"## Patrol N" heading, in the style of the hand-written logs. The target
page is chosen by the PATROL NUMBER from the captain's log scanner
(CAPTAINS_LOG "Patrol N ... Left at:" entries): each day digest is tagged
with the patrol that was active when its events were seen, and the entry
is written to "<wiki dir>/Patrol N.md". If that page does not exist yet
(e.g. a new patrol started), it is created automatically.

Resilience:
- A journal file (.sh3_patrol_log_journal.json) persists all day digests
  across monitor restarts, so nothing is lost mid-patrol.
- If the LLM or the wiki is unreachable, the due recaps are kept and
  retried periodically; the monitor itself never crashes.
- Entries are upserted by date heading, so a regenerated day replaces its
  old entry instead of duplicating it.

See specs/captains-log.md for the full design.

Configuration (environment variables, or a .env file in the repo root;
see .env.example). The writer is DISABLED unless SH3_PATROL_LOG_ENABLED
is true:
    SH3_PATROL_LOG_ENABLED  master switch; false/0/no/off disables the
                            writer entirely (default: off)
    SH3_LLM_URL         OpenAI-compatible API base (REQUIRED when enabled)
                        Requests are streamed and tagged with the llama-swap
                        priority header (X-llamaswap-Priority), so a recap
                        queued behind other work is kept alive by the
                        scheduler's SSE keep-alive frames instead of timing
                        out. The timeout is only a safety net.
    SH3_LLM_KEY         API key (optional; some local servers need none)
    SH3_LLM_MODEL       Model name (REQUIRED when enabled)
    SH3_WIKI_URL        Gollum wiki base URL (REQUIRED when enabled)
    SH3_WIKI_DIR        Wiki directory holding the patrol pages
                        (default "Games/SH3 Captain's Log")
    SH3_WIKI_PAGE       Fixed patrol page path (OVERRIDE — bypasses patrol-number
                        routing; unset by default)
    SH3_PATROL_JOURNAL  Journal file path (default .sh3_patrol_log_journal.json)
"""
import sys
import os
import json
import time
import re
from datetime import date as real_date
from queue import Empty as QueueEmpty
import urllib.request
import urllib.parse
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import env_config
import event_bus

env_config.load_env()

# --- CONFIGURATION ---
# Master switch: the patrol log writer (LLM recaps + wiki updates) is
# disabled unless explicitly turned on.
ENABLED = env_config.env_flag('SH3_PATROL_LOG_ENABLED', default=False)
LLM_URL = os.environ.get('SH3_LLM_URL')
LLM_KEY = os.environ.get('SH3_LLM_KEY', '')
LLM_MODEL = os.environ.get('SH3_LLM_MODEL')
WIKI_URL = os.environ.get('SH3_WIKI_URL')
WIKI_DIR = os.environ.get('SH3_WIKI_DIR', "Games/SH3 Captain's Log")
# Optional fixed page override; when unset, the page is derived from the
# patrol number seen in the captain's log scanner.
WIKI_PAGE = os.environ.get('SH3_WIKI_PAGE')
JOURNAL_FILE = os.environ.get('SH3_PATROL_JOURNAL', '.sh3_patrol_log_journal.json')


def page_for_patrol(patrol_number):
    """Patrol number (int) -> wiki page path, e.g. 9 -> .../Patrol 9.md"""
    return f"{WIKI_DIR}/Patrol {patrol_number}.md"

LLM_MAX_TOKENS = int(os.environ.get('SH3_LLM_MAX_TOKENS', '2048'))
# Safety net only: the request is streamed, so llama-swap's priority
# scheduler emits SSE keep-alive frames while it sits in the queue and the
# connection never goes idle. One hour covers any realistic queue depth.
LLM_TIMEOUT = int(os.environ.get('SH3_LLM_TIMEOUT', '3600'))
# Priority sent to llama-swap (higher = served first, ties keep arrival
# order). Recaps run at most once a day at real midnight, so they jump
# ahead of routine traffic.
LLM_PRIORITY = int(os.environ.get('SH3_LLM_PRIORITY', '20'))
LLM_PRIORITY_HEADER = os.environ.get('SH3_LLM_PRIORITY_HEADER',
                                     'X-llamaswap-Priority')
# Seconds to wait before retrying failed recaps (LLM or wiki failure).
RETRY_DELAY = int(os.environ.get('SH3_PATROL_RETRY_DELAY', '300'))
# How long to wait for events before checking the clock / pending recaps.
EVENT_TIMEOUT = 5

MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June',
               'July', 'August', 'September', 'October', 'November', 'December']


def _ordinal(day):
    if 10 <= day % 100 <= 20:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
    return f"{day}{suffix}"


def format_date_heading(date_str):
    """'1940-09-06' -> 'September 6th' (matches existing log headings)."""
    year, month, day = (int(x) for x in date_str.split('-'))
    return f"{MONTH_NAMES[month - 1]} {_ordinal(day)}"


def format_date_long(date_str):
    """'1940-09-06' -> 'September 6th, 1940'."""
    year, month, day = (int(x) for x in date_str.split('-'))
    return f"{MONTH_NAMES[month - 1]} {_ordinal(day)}, {year}"


# ----------------------------------------------------------------------------
# Page content manipulation (pure functions, unit-tested)
# ----------------------------------------------------------------------------

def insert_entry(content, entry):
    """
    Insert a log entry into the patrol page, newest-first.

    Existing patrol pages list entries with the most recent directly under
    the "## Patrol N" heading, so the entry is inserted right after that
    heading. If no level-2 heading exists, the entry is prepended at the
    top of the page.
    """
    entry_lines = entry.strip('\n').split('\n')
    lines = content.split('\n')
    for i, line in enumerate(lines):
        if line.startswith('## '):
            rest = lines[i + 1:]
            while rest and rest[0].strip() == '':
                rest.pop(0)
            new_lines = lines[:i + 1] + [''] + entry_lines
            if rest:
                new_lines += [''] + rest
            return '\n'.join(new_lines)
    while lines and lines[0].strip() == '':
        lines.pop(0)
    return '\n'.join(entry_lines + [''] + lines)


def replace_section(content, heading, new_body):
    """
    Replace the body of an existing '### heading' section with new_body.
    The section extends to the next heading (any line starting with '#')
    or the end of the page. Returns the new content, or None if the
    heading is not present.
    """
    lines = content.split('\n')
    for i, line in enumerate(lines):
        if line.strip() == heading:
            j = i + 1
            while j < len(lines) and not lines[j].startswith('#'):
                j += 1
            new_lines = lines[:i] + [heading] + new_body.strip('\n').split('\n')
            if j < len(lines):
                new_lines += [''] + lines[j:]
            return '\n'.join(new_lines)
    return None


# ----------------------------------------------------------------------------
# Gollum wiki client
# ----------------------------------------------------------------------------

class GollumWiki:
    """
    Minimal Gollum wiki client over the web interface (no git access needed).

    Gollum exposes editing through HTML forms:
      - GET  /gollum/edit/<page>   -> form with page, etag, path, format and
                                      the current content in a textarea
      - POST /gollum/edit/<page>   -> save (requires the etag for conflict
                                      detection)
      - GET  /gollum/create/<page> -> form with default path for a new page
      - POST /gollum/create        -> create the page
    """

    def __init__(self, base_url, page):
        self.base_url = base_url.rstrip('/')
        self.page = page

    # -- HTTP helpers --

    def _url(self, path):
        return f"{self.base_url}/{path}"

    def _encode_page(self):
        return '/'.join(urllib.parse.quote(seg) for seg in self.page.split('/'))

    def _get(self, url):
        req = urllib.request.Request(url, headers={'Accept': 'text/html'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.getcode(), resp.read().decode('utf-8', errors='replace')

    def _post(self, url, fields):
        data = urllib.parse.urlencode(fields).encode('utf-8')
        req = urllib.request.Request(
            url, data=data,
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.getcode()

    # -- HTML form parsing (Gollum's markup is stable enough for regexes) --

    @staticmethod
    def _decode_entities(text):
        return (text.replace('&amp;', '&').replace('&lt;', '<')
                    .replace('&gt;', '>').replace('&quot;', '"')
                    .replace('&#39;', "'").replace('&#x27;', "'")
                    .replace('&#x2F;', '/').replace('&nbsp;', ' '))

    @classmethod
    def _input_value(cls, html, name):
        m = re.search(rf'<input[^>]+name=["\']?{name}["\']?[^>]*>', html, re.I)
        if not m:
            return None
        v = re.search(r'value=["\']?([^"\'>]*)["\']?', m.group(0), re.I)
        return cls._decode_entities(v.group(1)) if v else ''

    @classmethod
    def _textarea_content(cls, html, textarea_id):
        m = re.search(
            rf'<textarea[^>]+id=["\']?{textarea_id}["\']?[^>]*>([\s\S]*?)</textarea>',
            html, re.I)
        return cls._decode_entities(m.group(1)) if m else None

    # -- Page operations --

    def read_page(self):
        """
        Read the current page. Returns a dict with page/etag/path/format/
        content, or None if the page does not exist (404).
        """
        code, html = self._get(self._url(f'/gollum/edit/{self._encode_page()}'))
        if code == 404:
            return None
        page = self._input_value(html, 'page')
        content = self._textarea_content(html, 'gollum-editor-body')
        if page is None or content is None:
            raise RuntimeError(f"Could not parse edit form for {self.page}")
        return {
            'page': page,
            'etag': self._input_value(html, 'etag'),
            'path': self._input_value(html, 'path') or '/',
            'format': self._input_value(html, 'format') or 'markdown',
            'content': content,
        }

    def write_page(self, content, message):
        """Overwrite an existing page. Returns True on success."""
        form = self.read_page()
        if form is None:
            return False
        if not form['etag']:
            raise RuntimeError(f"No etag for {self.page}; editing may be disabled")
        self._post(self._url(f'/gollum/edit/{self._encode_page()}'), {
            'page': form['page'],
            'etag': form['etag'],
            'path': form['path'],
            'format': form['format'],
            'content': content,
            'message': message,
        })
        return True

    def create_page(self, content, message, fmt='markdown'):
        """Create a new page. Returns True on success."""
        code, html = self._get(self._url(f'/gollum/create/{self._encode_page()}'))
        path = self._input_value(html, 'path') or '/'
        base = self.page.rsplit('/', 1)[-1]
        if '.' in base:
            base = base.rsplit('.', 1)[0]
        self._post(self._url('/gollum/create'), {
            'page': base,
            'path': path,
            'format': fmt,
            'content': content,
            'message': message,
        })
        return True

    def get_entry(self, date_str):
        """
        Return the existing entry body for the date (text between the date
        heading and the next heading), or None if there is no entry yet.
        """
        form = self.read_page()
        if form is None:
            return None
        heading = f"### {format_date_heading(date_str)}"
        lines = form['content'].split('\n')
        for i, line in enumerate(lines):
            if line.strip() == heading:
                j = i + 1
                while j < len(lines) and not lines[j].startswith('#'):
                    j += 1
                body = '\n'.join(lines[i + 1:j]).strip('\n')
                return body or None
        return None

    def upsert_entry(self, date_str, entry_text, message):
        """
        Write the entry for the given in-game date to the patrol page:
        replace the existing section if one exists (regenerated day),
        otherwise insert a new section under the patrol heading. Creates
        the page if it does not exist yet. Returns True on success.
        """
        heading = f"### {format_date_heading(date_str)}"
        entry = f"{heading}\n{entry_text.strip()}"
        form = self.read_page()
        if form is None:
            patrol = self.page.rsplit('/', 1)[-1].rsplit('.', 1)[0]
            return self.create_page(f"## {patrol}\n\n{entry}\n", message)
        content = replace_section(form['content'], heading, entry_text.strip())
        if content is None:
            content = insert_entry(form['content'], entry)
        return self.write_page(content, message)


# ----------------------------------------------------------------------------
# LLM client
# ----------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are the captain of a German U-boat in Silent Hunter 3, writing your \
patrol log (Kriegstagebuch) at the end of the day. Write in first person, \
past tense, concise (2-5 sentences). Match the tone of these real entries \
from previous patrols:

### Sept 10th, 2pm
We left port in the afternoon in route to our patrol area, AM98. We are \
taking the northern route around the Shetland islands and patrolling the \
Cardif port. Morale is high.

### Sept 11th, 9am
In AN63, still navigating north west. Weather is calm and cloudy.

### April 4th, 6pm
Sunk cargo ship, 1 hit. Trailed until confirmed sinking. 5000 tn

### April 5th, 4am
Small ship spotted in the dark. Seas too rough to launch torps.

Rules:
- Refer to naval grid references (e.g. AM98, AN63) naturally, as in the examples.
- Mention the day's weather and notable events (sightings, radio traffic, dives).
- The boat is ON THE SURFACE at any depth reading from 0 to 7 meters: the \
depth gauge shows a few meters even when surfaced (seas and swell). Never \
describe a 0-7m reading as a dive, or claim the boat submerged and \
surfaced because of one; only treat the boat as submerged when the log \
shows a deeper dive.
- NEVER invent engagements, sinkings, damage or contacts that are not in the \
log data. If the day was uneventful, say so plainly.
- When a sinking is reported, name the ship type (tanker, cargo, destroyer, \
etc.) and give the tonnage as your own rough estimate (e.g. "a cargo ship \
of some 5,000 tons"), never as an exact official figure.
- Output only the log paragraph itself: no heading, no date, no quotation \
marks, no markdown formatting."""


def build_recap_prompt(digest, previous_entry=None):
    """Build the user message describing the day's events."""
    lines = [f"End-of-day report for {format_date_long(digest['date'])}."]
    if digest.get('grids'):
        lines.append(f"Grids visited: {', '.join(digest['grids'])}.")
    if digest.get('weather'):
        lines.append(f"Latest weather: {digest['weather'][-1]}")
    if digest.get('max_depth'):
        lines.append(f"Maximum depth reached: {digest['max_depth']:.0f} meters.")
    if digest.get('max_speed'):
        lines.append(f"Maximum speed: {digest['max_speed']:.0f} knots.")
    lines.append("Events of the day (in-game time — event):")
    lines.extend(f"- {e}" for e in digest['events'])
    if previous_entry:
        lines.append(
            "An entry for this day was already written earlier. Here it is; "
            "write a single updated entry covering the whole day:")
        lines.append(previous_entry)
    lines.append("Write the log entry for this day.")
    return '\n'.join(lines)


def iter_sse_content(resp):
    """
    Iterate an SSE chat-completions stream, yielding content deltas.

    Comment frames (lines starting with ':', e.g. llama-swap's
    ': keep-alive' emitted while the request waits in the priority
    scheduler queue) are ignored, as are reasoning deltas.
    """
    for raw_line in resp:
        line = raw_line.decode('utf-8', errors='replace').strip()
        if not line or line.startswith(':'):
            continue
        if not line.startswith('data:'):
            continue
        payload = line[len('data:'):].strip()
        if payload == '[DONE]':
            return
        try:
            chunk = json.loads(payload)
        except ValueError:
            continue
        choices = chunk.get('choices') or []
        if not choices:
            continue
        delta = choices[0].get('delta') or {}
        content = delta.get('content')
        if content:
            yield content


def generate_recap(digest, previous_entry=None):
    """
    Ask the LLM to write the day's recap. Returns the entry text
    (without heading). Raises on API or response errors.

    The request is streamed and carries the llama-swap priority header, so
    the scheduler keeps the connection alive with SSE comment frames while
    the recap waits in the queue — the timeout is a safety net, not a queue
    deadline.
    """
    body = {
        'model': LLM_MODEL,
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': build_recap_prompt(digest, previous_entry)},
        ],
        'max_tokens': LLM_MAX_TOKENS,
        'temperature': 0.7,
        'stream': True,
    }
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream',
        LLM_PRIORITY_HEADER: str(LLM_PRIORITY),
    }
    if LLM_KEY:
        headers['Authorization'] = f"Bearer {LLM_KEY}"
    req = urllib.request.Request(
        f"{LLM_URL.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode('utf-8'),
        headers=headers)
    parts = []
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
        for content in iter_sse_content(resp):
            parts.append(content)

    text = ''.join(parts).strip().strip('"').strip()
    if not text:
        raise RuntimeError("LLM returned an empty entry")
    # Collapse to a single paragraph, matching the existing log style.
    text = re.sub(r'\s*\n\s*', ' ', text)
    return text


# ----------------------------------------------------------------------------
# Day book: per-in-game-day event digests
# ----------------------------------------------------------------------------

class DayBook:
    """
    Accumulates event digests keyed by in-game date.

    Events are attributed to the last known in-game date (from CLOCK_UPDATE
    or WEATHER events). Events that arrive before any date is known are held
    in a limbo bucket and folded into the first real date. Each day digest
    carries a 'dirty' flag: True while it has events not yet on the wiki.
    """

    LIMBO = None       # in-memory key for events with unknown in-game date
    _LIMBO_KEY = '__limbo__'  # journal key for the same bucket

    _DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

    def __init__(self):
        self.days = {}          # date_str -> digest dict (with 'dirty')
        self.current_date = None
        self.last_time = None
        self.patrol_number = None  # from captain's log "Patrol N" entries
        # Last depth/speed/heading an event was recorded for (hysteresis
        # state, carried across in-game days and persisted in the journal).
        self.last_depth = None
        self.last_speed = None
        self.last_heading = None

    # -- digest access --

    def _day(self, date_str):
        if date_str not in self.days:
            self.days[date_str] = {
                'date': date_str, 'events': [], 'grids': [], 'weather': [],
                'max_depth': 0.0, 'max_speed': 0.0, 'dirty': False,
                'patrol': self.patrol_number,
            }
        return self.days[date_str]

    def _now(self):
        return self.last_time or '??:??'

    def _add(self, date_str, text):
        day = self._day(date_str)
        # Cap the digest so a very eventful day can't blow up the prompt.
        if len(day['events']) < 100:
            day['events'].append(f"{self._now()} {text}")
            day['dirty'] = True

    # -- event handling --

    def observe(self, event):
        """Feed one event into the book."""
        et = event.event_type
        data = event.data

        if et == event_bus.EventTypes.CLOCK_UPDATE:
            self.last_time = data.get('time')
            if self._valid(data.get('date')):
                self.current_date = data['date']
                self._fold_limbo()
            return
        if et == event_bus.EventTypes.WEATHER and isinstance(data, dict):
            self.last_time = data.get('time') or self.last_time
            if self._valid(data.get('date')):
                self.current_date = data['date']
                self._fold_limbo()

        date = self.current_date

        if et == event_bus.EventTypes.CAPTAINS_LOG:
            self._observe_captains_log(str(data))
            return

        if et == event_bus.EventTypes.SHIP_SPOTTED:
            self._add(date, f"Ship spotted: {data}")
        elif et == event_bus.EventTypes.AIRCRAFT_SPOTTED:
            self._add(date, f"Aircraft spotted: {data}")
        elif et == event_bus.EventTypes.MESSAGE_RECEIVED:
            text = ' | '.join(l.strip() for l in str(data).split('\n') if l.strip())
            self._add(date, f"Radio message: {text[:300]}")
        elif et == event_bus.EventTypes.WEATHER:
            if isinstance(data, dict):
                day = self._day(date)
                if data.get('text'):
                    day['weather'].append(data['text'])
                    self._add(date, f"Weather report: {data['text']}")
        elif et == event_bus.EventTypes.LOCATION_UPDATE:
            if isinstance(data, dict) and data.get('available') and data.get('grid'):
                day = self._day(date)
                grid = data['grid']
                if not day['grids'] or day['grids'][-1] != grid:
                    if grid not in day['grids']:
                        day['grids'].append(grid)
                    self._add(date, f"Entered grid {grid}")
        elif et == event_bus.EventTypes.SPEED_UPDATE:
            if isinstance(data, dict):
                speed = data.get('speed', 0.0)
                day = self._day(date)
                day['max_speed'] = max(day['max_speed'], speed)
                self._note_speed(date, speed)
        elif et == event_bus.EventTypes.DEPTH_UPDATE:
            if isinstance(data, dict):
                depth = data.get('depth', 0.0)
                day = self._day(date)
                day['max_depth'] = max(day['max_depth'], depth)
                self._note_depth(date, depth)
        elif et == event_bus.EventTypes.HEADING_UPDATE:
            if isinstance(data, dict):
                self._note_heading(date, data.get('heading', 0.0))

    _PATROL_RE = re.compile(r'^Patrol (\d+)\r?\n')

    def _observe_captains_log(self, text):
        """
        Handle a captain's log entry: learn the patrol number from
        "Patrol N\n...\nLeft at:" entries and record notable happenings
        (ship sunk, aircraft destroyed, patrol start, objectives) as
        events for the day's recap.
        """
        m = self._PATROL_RE.match(text)
        if m:
            n = int(m.group(1))
            if self.patrol_number is None:
                # First time we learn the patrol number: backfill any days
                # already tracked (they are from this patrol).
                self.patrol_number = n
                for day in self.days.values():
                    if day.get('patrol') is None:
                        day['patrol'] = n
            else:
                self.patrol_number = n
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        first = lines[0] if lines else ''
        notable = (first.startswith('Ship sunk!') or
                   first.startswith('Aircraft destroyed!') or
                   first.startswith('Patrol started') or
                   first.startswith('Primary Objectives Completed') or
                   m is not None)
        if notable:
            summary = first
            if len(lines) > 1:
                summary += f" — {lines[1]}"
            if len(lines) > 2 and m is not None:
                summary += f" ({lines[2]})"
            if first.startswith('Ship sunk!') and len(lines) >= 3:
                # "Ship sunk!\n<name>\n<type>, <tons> tons" — record the
                # ship type and an ESTIMATED tonnage (rounded, as a captain
                # eyeballing the target would give), not the exact figure.
                tm = self._TONNAGE_RE.match(lines[2])
                if tm:
                    ship_type = tm.group(1).strip().lower()
                    tons = int(tm.group(2).replace(',', ''))
                    step = 500 if tons >= 1000 else 100
                    est = int(round(tons / float(step)) * step)
                    summary += f" ({ship_type}, estimated {est:,} tons)"
            self._add(self.current_date, f"Captain's log: {summary}")

    _TONNAGE_RE = re.compile(r'^(.+?),\s*([\d,]+)\s*tons$', re.IGNORECASE)

    # Depth/speed change tracking. Only BIG changes become events (the
    # LLM correlates them with sightings — e.g. aircraft spotted + dove
    # to 80m = evasive dive); small adjustments are ignored via
    # hysteresis against the last RECORDED value.
    # The depth gauge reads anywhere from 0 to 7 meters while the boat
    # is on the surface (wave motion / buoyancy), so the surfaced/submerged
    # boundary must sit ABOVE that range; otherwise a surfaced boat bobbing
    # across it generates spurious "Dove to 5m" / "Surfaced" events.
    SURFACE_DEPTH = 7.5       # meters; at/above this we count as submerged
    DEPTH_CHANGE_STEP = 20.0  # meters; minimum change to record
    SPEED_CHANGE_STEP = 5.0   # knots; minimum change to record
    HEADING_CHANGE_STEP = 30.0  # degrees; minimum course change to record

    def _note_depth(self, date, depth):
        """Record a depth event for big changes / dive-surface transitions."""
        prev = self.last_depth
        if prev is None:
            # Initial reading: only worth noting if we start submerged.
            if depth >= self.SURFACE_DEPTH:
                self._add(date, f"At {depth:.0f}m depth")
            self.last_depth = depth
            return
        was_sub = prev >= self.SURFACE_DEPTH
        now_sub = depth >= self.SURFACE_DEPTH
        # Dive/surface transitions are always notable, even from shallow
        # depth (e.g. surfacing from periscope depth is only a few meters).
        if was_sub == now_sub and abs(depth - prev) < self.DEPTH_CHANGE_STEP:
            return
        if now_sub and not was_sub:
            self._add(date, f"Dove to {depth:.0f}m")
        elif not now_sub and was_sub:
            self._add(date, "Surfaced")
        elif now_sub and depth > prev:
            self._add(date, f"Dove deeper to {depth:.0f}m")
        elif now_sub:
            self._add(date, f"Rose to {depth:.0f}m")
        else:
            # Both readings at surface level: not notable.
            self.last_depth = depth
            return
        self.last_depth = depth

    def _note_speed(self, date, speed):
        """Record a speed event for big changes only."""
        prev = self.last_speed
        if prev is None:
            self.last_speed = speed
            return
        if abs(speed - prev) < self.SPEED_CHANGE_STEP:
            return
        if speed < 1.0:
            self._add(date, "All stop")
        elif speed > prev:
            self._add(date, f"Increased speed to {speed:.0f} knots")
        else:
            self._add(date, f"Slowed to {speed:.0f} knots")
        self.last_speed = speed

    def _note_heading(self, date, heading):
        """Record a course change event for big changes only (circular)."""
        prev = self.last_heading
        if prev is None:
            self.last_heading = heading
            return
        # Circular difference: -180..180, so 350 -> 10 is +20, not -340.
        diff = (heading - prev + 180.0) % 360.0 - 180.0
        if abs(diff) < self.HEADING_CHANGE_STEP:
            return
        if abs(diff) >= 90.0:
            self._add(date, f"Came about to {heading:03.0f}\u00b0")
        else:
            self._add(date, f"Altered course to {heading:03.0f}\u00b0")
        self.last_heading = heading

    @classmethod
    def _valid(cls, date_str):
        """True for real in-game dates ('YYYY-MM-DD'), not placeholders."""
        return bool(date_str) and bool(cls._DATE_RE.match(date_str))

    def _fold_limbo(self):
        """Move events gathered before any date was known into the current day."""
        limbo = self.days.pop(self.LIMBO, None)
        if limbo and limbo['events'] and self.current_date:
            day = self._day(self.current_date)
            day['events'] = limbo['events'] + day['events']
            day['grids'] = limbo['grids'] or day['grids']
            day['weather'] = limbo['weather'] or day['weather']
            day['max_depth'] = max(limbo['max_depth'], day['max_depth'])
            day['max_speed'] = max(limbo['max_speed'], day['max_speed'])
            day['dirty'] = True

    # -- queries --

    def dirty_dates(self):
        """In-game dates that have events not yet written to the wiki."""
        return sorted(d for d, day in self.days.items()
                      if d is not None and day['dirty'] and day['events'])

    def sealed_dirty_dates(self):
        """Dirty dates that are fully in the past (the game has moved on)."""
        return [d for d in self.dirty_dates() if d != self.current_date]

    def mark_written(self, date_str):
        if date_str in self.days:
            self.days[date_str]['dirty'] = False

    # -- persistence --

    def dump(self):
        days = {k if k is not None else self._LIMBO_KEY: v
                for k, v in self.days.items()}
        return {
            'days': days,
            'current_date': self.current_date,
            'last_time': self.last_time,
            'patrol_number': self.patrol_number,
            'last_depth': self.last_depth,
            'last_speed': self.last_speed,
            'last_heading': self.last_heading,
        }

    def load(self, state):
        days = state.get('days', {})
        self.days = {None if k == self._LIMBO_KEY else k: v
                     for k, v in days.items()}
        self.current_date = state.get('current_date')
        self.last_time = state.get('last_time')
        self.patrol_number = state.get('patrol_number')
        self.last_depth = state.get('last_depth')
        self.last_speed = state.get('last_speed')
        self.last_heading = state.get('last_heading')


# ----------------------------------------------------------------------------
# Journal persistence
# ----------------------------------------------------------------------------

def load_journal(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_journal(path, journal):
    tmp = f"{path}.tmp"
    with open(tmp, 'w') as f:
        json.dump(journal, f, indent=2)
    os.replace(tmp, path)


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------

class PatrolLogWriter:
    """
    Generates and writes daily recaps.

    Flush policy (what gets written when):
    - At real-time midnight: every dirty day (including the currently
      active in-game day).
    - On startup: dirty days that are already sealed (in-game date has
      rolled past) — they missed their midnight while the monitor was off.
    - The active day is never written outside of midnight, so a running
      day is not cut in half.
    - Failures keep the day dirty; the flush is retried every RETRY_DELAY.
    """

    def __init__(self, wiki, journal_path=JOURNAL_FILE):
        self.wiki = wiki
        self.journal_path = journal_path
        self.book = DayBook()
        self.flush_due = []      # dates waiting to be (re)written
        self.last_attempt = 0.0

    # -- journal --

    def load(self):
        journal = load_journal(self.journal_path)
        self.book.load(journal.get('book', {}))
        self.flush_due = journal.get('flush_due', [])
        # Catch-up: sealed days that never got their midnight flush.
        for date_str in self.book.sealed_dirty_dates():
            if date_str not in self.flush_due:
                self.flush_due.append(date_str)
        self.save()

    def save(self):
        save_journal(self.journal_path, {
            'book': self.book.dump(),
            'flush_due': self.flush_due,
        })

    # -- flushing --

    def midnight_flush(self):
        """Real-time midnight: everything dirty is due."""
        for date_str in self.book.dirty_dates():
            if date_str not in self.flush_due:
                self.flush_due.append(date_str)
        self.attempt_flush()

    def attempt_flush(self):
        """
        Try to write every due recap, oldest day first. Days that fail
        stay in flush_due for a later retry. Each day is written to the
        patrol page derived from its patrol tag (captain's log scanner);
        days without a patrol number yet stay due until one is learned.
        """
        if not self.flush_due:
            return
        self.last_attempt = time.time()
        for date_str in sorted(self.flush_due):
            digest = self.book.days.get(date_str)
            if not digest or not digest['events']:
                self.flush_due.remove(date_str)
                continue
            patrol = digest.get('patrol')
            if WIKI_PAGE:
                self.wiki.page = WIKI_PAGE
            elif patrol:
                self.wiki.page = page_for_patrol(patrol)
            else:
                # No patrol number learned yet (no captain's log entry
                # seen). Keep the day due; it flushes once the number
                # arrives or SH3_WIKI_PAGE is set.
                event_bus.publish_event(
                    event_bus.EventTypes.STATUS,
                    f"Patrol log: holding recap for {date_str} — no patrol "
                    f"number known yet")
                continue
            try:
                # If this day was already written (regeneration), let the
                # LLM see the earlier entry so it can fold it in.
                previous = self.wiki.get_entry(date_str)
                entry_text = generate_recap(digest, previous)
                message = (f"Daily recap for {format_date_long(date_str)} "
                           f"(auto-generated)")
                if self.wiki.upsert_entry(date_str, entry_text, message):
                    self.book.mark_written(date_str)
                    self.flush_due.remove(date_str)
                    self.save()
                    event_bus.publish_event(event_bus.EventTypes.STATUS,
                           f"Patrol log: wrote daily recap for "
                           f"{format_date_long(date_str)}")
                else:
                    raise RuntimeError("wiki write failed")
            except Exception as e:
                event_bus.publish_event(event_bus.EventTypes.ERROR,
                       f"Patrol log: recap for {date_str} failed ({e}); "
                       f"will retry in {RETRY_DELAY}s")
        # Persist the (possibly reduced) due list even on failure, so a
        # restart does not lose or duplicate pending recaps.
        self.save()

    # -- event handling --

    def handle_event(self, event):
        had_patrol = self.book.patrol_number
        self.book.observe(event)
        if event.event_type in (event_bus.EventTypes.SHIP_SPOTTED,
                                event_bus.EventTypes.AIRCRAFT_SPOTTED,
                                event_bus.EventTypes.MESSAGE_RECEIVED,
                                event_bus.EventTypes.WEATHER,
                                event_bus.EventTypes.LOCATION_UPDATE,
                                event_bus.EventTypes.CAPTAINS_LOG,
                                event_bus.EventTypes.HEADING_UPDATE):
            self.save()
        # Learning the patrol number unblocks held recaps: flush now.
        if (event.event_type == event_bus.EventTypes.CAPTAINS_LOG and
                self.flush_due and not had_patrol and
                self.book.patrol_number):
            self.attempt_flush()

    def run(self):
        if not ENABLED:
            event_bus.publish_event(
                event_bus.EventTypes.STATUS,
                "Patrol log writer disabled (SH3_PATROL_LOG_ENABLED is not true)")
            return
        missing = [name for name, value in (
            ('SH3_LLM_URL', LLM_URL),
            ('SH3_LLM_MODEL', LLM_MODEL),
            ('SH3_WIKI_URL', WIKI_URL),
        ) if not value]
        if missing:
            event_bus.publish_event(
                event_bus.EventTypes.STATUS,
                "Patrol log writer disabled: missing " + ", ".join(missing)
                + " (set them in the environment or .env)")
            return
        target = (f"page {WIKI_PAGE}" if WIKI_PAGE else
                  f"patrol pages under {WIKI_DIR}/ (number from captain's log)")
        event_bus.publish_event(
            event_bus.EventTypes.STATUS,
            f"Patrol log writer started (model {LLM_MODEL}, {target})")
        self.load()
        # Retry recaps that were due before a restart.
        self.attempt_flush()

        events = event_bus.subscribe()
        last_real_day = real_date.today()
        while True:
            try:
                event = events.get(timeout=EVENT_TIMEOUT)
                self.handle_event(event)
            except QueueEmpty:
                pass

            today = real_date.today()
            if today != last_real_day:
                # Real-time midnight: generate the day's recaps.
                last_real_day = today
                event_bus.publish_event(event_bus.EventTypes.STATUS,
                       "Patrol log: midnight — generating daily recaps")
                self.midnight_flush()
            elif self.flush_due and \
                    time.time() - self.last_attempt >= RETRY_DELAY:
                self.attempt_flush()


def run():
    """Main collector entry point."""
    PatrolLogWriter(GollumWiki(WIKI_URL, WIKI_PAGE)).run()


if __name__ == "__main__":
    run()