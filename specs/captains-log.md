# Captain's Log — LLM-Generated Daily Recaps

Status: Active
Story: #3 (daily summary entries; event-triggered entries and the ship-sunk
scanner remain future work)

Trigger note: recap generation fires at **real-time midnight**, not on the
in-game day rollover — see "Daily recap flow" below.

## Problem

The tracker collects rich game state (sightings, radio traffic, weather,
grid, speed, depth, in-game date/time) but the Captain's Log on the Gollum
wiki is maintained by hand. This spec covers the automatic generation of
**end-of-day recap entries** written in the captain's voice.

## Architecture

```
collectors (ship, aircraft, radio, weather, location, speed, depth, clock)
        │ publish_event()
        ▼
   event bus ──────────────► master coordinator (ncurses UI)
        │ subscribe()              ▲
        ▼                           │ STATUS/ERROR events
 collectors/patrol_log.py ─────────┘
        │
        ├─► LLM API (OpenAI-compatible chat completions)
        └─► Gollum wiki (web form API)
```

### New components

- **`collectors/clock.py`** — polls the in-game clock UI string
  (`utils.find_ui_clock`, shared with the weather collector) every
  `SH3_CLOCK_INTERVAL` seconds (default 30) and publishes `CLOCK_UPDATE`
  events `{'date': 'YYYY-MM-DD', 'time': 'HH:MM', 'ymd': [...],
  'minutes': N}`. Publishes only when the in-game minute changes. This
  resolves the "in-game date/time scanner" dependency listed in #3.
- **`collectors/patrol_log.py`** — the daily recap writer (see below).
- **`event_bus.subscribe()`** — fan-out: subscriber queues receive a copy
  of every published event in addition to the main queue, so the patrol
  log writer gets the full stream without stealing events from the master.

### Daily recap flow

1. `PatrolLogWriter` subscribes to the event bus and feeds every event
   into a `DayBook`, which accumulates a digest **per in-game date**:
   - notable events (ship/aircraft spotted, radio messages, weather
     reports, grid changes, notable captain's log entries) as
     `HH:MM — event` lines
   - grids visited, latest weather, max depth, max speed
   - **big depth/speed changes** (see below)
   Events are attributed to the last known in-game date (from
     `CLOCK_UPDATE`/`WEATHER` events); events seen before any date is
     known are held in a limbo bucket and folded into the first real date.
2. **Generation is triggered by real time, not game time**: once per
   real-life day at midnight, every in-game day with unwritten events is
   written to the wiki, oldest first, one entry per in-game day.
   - If the game runs in sync with real time, real midnight is also the
     in-game day boundary — the day that just ended gets its entry.
   - If not (time compression, paused game, monitor restarted), several
     in-game days may be pending; each gets its own entry. An in-game day
     that spans several real days is **regenerated** at each midnight: the
     existing wiki section for that date is replaced by a fresh entry
     (the LLM is shown the earlier entry so it can fold it in), keeping
     one coherent entry per in-game day.
3. Each recap is a 2–5 sentence first-person entry (style-matched to the
   existing hand-written logs; explicitly instructed not to invent
   engagements), written under a `### <Month> <Day><st/nd/rd/th>` heading.
   New entries are inserted directly under the `## Patrol N` heading,
   matching the newest-first ordering used in Patrols 5–8.
4. On failure (LLM or wiki unreachable) the due recaps stay due and are
   retried every `SH3_PATROL_RETRY_DELAY` seconds (default 300). The
   monitor never crashes on account of the log writer.

### Maneuver tracking (depth/speed/heading)

Depth, speed and heading updates arrive continuously; only **big
changes** become digest events, so the log is not spammed and the LLM can
correlate them with sightings (aircraft spotted + dove to 80m = evasive
dive; came about to 270\u00b0 after a contact = shadowing or running):

- **Depth**: dive/surface transitions (across 7.5m) are always recorded
  ("Dove to 25m", "Surfaced"); other changes only when they differ by
  ≥ 20m from the last *recorded* depth ("Dove deeper to 80m",
  "Rose to 40m"). Hysteresis is against the last recorded value, so a
  slow deliberate dive produces a handful of events, not one per poll.
  The boundary is 7.5m, not lower, because the depth gauge reads 0-7m
  while the boat is on the surface (wave motion) — the system prompt
  also tells the LLM that 0-7m readings mean "surfaced", so neither
  the event stream nor the recap mistakes surface bobbing for a dive.
- **Speed**: changes only when they differ by ≥ 5 knots from the last
  recorded speed ("Increased speed to 18 knots", "Slowed to 7 knots",
  "All stop" below 1 kt).
- **Heading** (from `collectors/heading.py`, float at pointer `0x00554698`
  + `0x64` — same player-boat structure as speed at `+0x68`): recorded
  only when the course changes by \u2265 30\u00b0 from the last recorded
  heading, measured the short way around the compass (350\u00b0 \u2192 20\u00b0 is
  +30\u00b0, not -330\u00b0). \u2265 90\u00b0 reads as "Came about to 270\u00b0", smaller
  changes as "Altered course to 045\u00b0".
- The hysteresis state (`last_depth`, `last_speed`, `last_heading`)
  persists in the journal, so a monitor restart does not re-record the
  current depth/speed/course.
- Ship sinkings from the captain's log are recorded with the ship type
  and an **estimated** tonnage (rounded to 500t above 1000t, else 100t) —
  never the exact game figure.

### Flush policy

| Situation | What is written |
|---|---|
| Real-time midnight | Every in-game day with unwritten events (including the active day) |
| Monitor startup | Only *sealed* days (in-game date already rolled past) that missed their midnight while the monitor was off |
| Retry after failure | Everything still due, every `SH3_PATROL_RETRY_DELAY` seconds |

The currently active in-game day is never written outside of midnight, so
a running day is never cut in half.

### Journal

`.sh3_patrol_log_journal.json` persists the full day book (all digests,
the current in-game date, per-day dirty flags) and the list of dates due
for writing. On restart the writer resumes accumulation, catches up
sealed days, and retries anything still due. Entries are upserted by date
heading on the wiki, so a regenerated day replaces its old entry instead
of duplicating it — even if the journal is lost.

## Gollum wiki API (confirmed against the live instance)

Gollum is git-backed but exposes editing through its web forms; no git
credentials are needed:

- `GET /gollum/edit/<page>` → HTML form containing hidden inputs `page`,
  `etag`, `path`, `format` and the current content in a textarea
  (`gollum-editor-body`). 404 if the page does not exist.
- `POST /gollum/edit/<page>` with form fields `page`, `etag`, `path`,
  `format`, `content`, `message` → saves the page (etag provides
  optimistic concurrency).
- `GET /gollum/create/<page>` → create form (yields the default `path`).
- `POST /gollum/create` with `page` (base name), `path`, `format`,
  `content`, `message` → creates the page.
- Raw content can also be read via `GET /<page>?raw=true`.

Reference implementation: the BotWatcher Gollum stone
(`money.ai/botwatcher-source-gollum_wiki`), which this client follows.

## Patrol Page Selection

The target wiki page is chosen by the **patrol number from the captain's
log scanner** (`collectors/captains_log.py`, story #6). The captain's log
array contains a `Patrol N\n<boat>\nLeft at: ...` entry at the start of
every patrol, and the collector republishes cached entries on startup, so
the number is always re-learned when the monitor restarts.

- Each day digest is tagged with the patrol that was active when its
  events were observed (`DayBook.patrol_number`).
- When the number is first learned, existing untagged days are backfilled
  (they belong to the current patrol).
- A day's recap is written to `<SH3_WIKI_DIR>/Patrol N.md`. If that page
does not exist (e.g. Patrol 10 just started), it is created automatically
with a `## Patrol N` heading.
- Days observed before any patrol number is known are held in
  `flush_due` and flush immediately once a `CAPTAINS_LOG` patrol entry
  arrives (or on the next retry tick).
- Setting `SH3_WIKI_PAGE` pins all writes to one fixed page and bypasses
  the routing entirely.

Notable captain's log entries (ship sunk, aircraft destroyed, patrol
start, objectives completed) are also recorded as events in the day's
digest so they appear in the recap.

## LLM API

Any OpenAI-compatible endpoint, configured via environment variables or
a `.env` file in the repo root (see `.env.example`):

- Base URL: `SH3_LLM_URL` (required when the writer is enabled)
- Key: `SH3_LLM_KEY` (optional; some local servers need none)
- Model: `SH3_LLM_MODEL` (required when the writer is enabled)
- Note: reasoning models emit `reasoning_content` before the final
  `content`, so `max_tokens` must leave headroom (default 2048).
- Requests are **streamed** and tagged with the llama-swap priority
  header (`X-llamaswap-Priority`, default `20` via `SH3_LLM_PRIORITY` —
  recaps run at most once a day at real midnight, so they are served
  ahead of routine traffic). While
  the request waits in the priority scheduler's queue, llama-swap emits
  SSE `: keep-alive` comment frames every 15s, so the connection never
  goes idle no matter how deep the queue is. `SH3_LLM_TIMEOUT`
  (default 3600s) is therefore a safety net, not a queue deadline.

The system prompt embeds real entries from previous patrols as style
examples and forbids inventing engagements or sinkings.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `SH3_PATROL_LOG_ENABLED` | `false` | Master switch for the writer (LLM recaps + wiki updates) |
| `SH3_LLM_URL` | *(unset — required)* | LLM API base |
| `SH3_LLM_KEY` | *(unset — optional)* | LLM API key |
| `SH3_LLM_MODEL` | *(unset — required)* | Model |
| `SH3_LLM_MAX_TOKENS` | `2048` | Completion budget (reasoning included) |
| `SH3_LLM_TIMEOUT` | `3600` | LLM request timeout (s) — safety net only (stream + keep-alive) |
| `SH3_LLM_PRIORITY` | `20` | llama-swap priority header value |
| `SH3_LLM_PRIORITY_HEADER` | `X-llamaswap-Priority` | Header name carrying the priority |
| `SH3_WIKI_URL` | *(unset — required)* | Gollum base URL |
| `SH3_WIKI_DIR` | `Games/SH3 Captain's Log` | Wiki directory holding patrol pages |
| `SH3_WIKI_PAGE` | *(unset)* | Fixed page override — bypasses patrol routing |
| `SH3_PATROL_JOURNAL` | `.sh3_patrol_log_journal.json` | Journal file |
| `SH3_PATROL_JOURNAL` | `.sh3_patrol_log_journal.json` | Journal file |
| `SH3_PATROL_RETRY_DELAY` | `300` | Recap retry interval (s) |
| `SH3_CLOCK_INTERVAL` | `30` | Clock poll interval (s) |

## Out of scope (future stories)

- Event-triggered entries (ship spotted etc. written immediately)
- Batching closely-spaced events into a single entry