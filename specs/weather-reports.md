# Weather Reports (Story #7)

**Status:** Active
**Story:** #7 (see the issue tracker)
**Last verified:** 2026-09-05 (SH3 v1.4b under Proton/Wine, session PID 38821)

## Purpose

The monitor actively requests weather reports from the Navigation Officer
(NA) by sending **Ctrl-W** to the game window, reads the report from game
memory, and republishes it on the event bus timestamped with the **in-game**
date and time (for the LLM captain's log, story #3).

## Key sending

- `xdotool key --clearmodifiers ctrl+w` on the host X display.
- Requires `DISPLAY=:0` and `XAUTHORITY=~/.Xauthority` in the environment
  (dropping the environment causes "Authorization required" failures).
- The game window is named `SilentHunter3`; sending to the focused window
  works because the game runs fullscreen.

## Memory layout

### HUD message buffer (source of the report text)

Heap-resident (address shifts between game sessions; found by signature
scan). Messages are color/tag-marked strings:

```
`C0xCCCCCCFF`16:40 `T40`NA:  `T68`Heavy clouds, no rain, moderate visibility, Wind speed 14 m/s, direction  27.
```

- `` `T68` `` precedes the message body; `` `T40` `` precedes the role name.
- Roles seen: `NA` (Navigation Officer - weather), `WO`, `RA`, `CE`, etc.
- Each line carries its own in-game timestamp `HH:MM`.
- The buffer keeps history (messages from earlier in the session remain).

### Static format template (must be rejected)

A format string `%s, %s, %s Wind speed %d m/s, direction %3d` exists in
memory (e.g. at 0x3b4ef673 in one session). It matches a naive "Wind speed"
signature but contains `%` and no HUD markup.

### Captain's log array (not used)

Structured entries (`[Log Entry N]` / `EntryText=` / `Date=19400904` /
`Time=1249` / `Categ=1`) around 0x00936666 (session-specific). New weather
reports do **not** serialize into this array promptly, so it is not a
reliable source for fresh reports.

### In-game clock (source of the date)

The game renders a clock UI string (visible when hovering the HUD clock):

```
September 5, 1940\nLocal time: 07:23
```

- Heap-resident (address shifts between sessions; located by scanning for
  `\nLocal time: ` and parsing the date in front of it).
- A static format template `\nLocal time: %02d:%02d` also exists in memory;
  it is rejected by requiring actual digits.
- The weather collector uses the **date** from this string; the **time**
  comes from the HUD message line itself.

### Timezones

- HUD message timestamps and the captain's log `Time=` use base/Berlin time.
- The clock UI string's "Local time" runs ~1 hour behind (boat's local
  timezone); the date is used as-is.

### Rejected: (date, time) int pairs

The in-game clock also exists as two adjacent little-endian ints
(`YYYYMMDD` + `HHMM` decimal, e.g. `19400905` `0742`), heap-resident and
verified live. However, memory contains **thousands** of such pairs: a
campaign-event database (dates spanning 1939-1945, e.g. the 0x062f-0x0630
region), patrol log entry arrays, and object timestamps. Under time
compression, campaign events fire and their entries change too, so no
selection heuristic (latest advancing pair, closest to the fresh message
time) reliably picks the live clock - wrong dates (e.g. 1943, 1945) get
picked. The UI string approach above is authoritative; do not use the int
pairs for this.

## Collector design (`collectors/weather.py`)

1. Send Ctrl-W, wait ~3 s for the message to render.
2. Full-memory scan for `Wind speed`; validate with a regex requiring the
   HUD markup (`` `C0x...`HH:MM `T40`NA: `T68` ``), which rejects the
   static template and the captain's log entries.
3. Read the clock UI string for the in-game date; the time comes from the
   HUD line itself.
4. Deduplicate on `(date, HHMM, text)` and publish `WEATHER` events:
   `{'date': '1940-09-04', 'time': '16:40', 'text': '...'}`.

Query interval defaults to 6 hours; override with `SH3_WEATHER_INTERVAL`
(plain seconds like `900`, or suffixed like `90m`, `6h`, `1d`).