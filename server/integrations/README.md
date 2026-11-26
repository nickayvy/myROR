# integrations/

Weather, calendars, reminders, event formality, and the cached "today" payload.

| File | What |
| --- | --- |
| `weather.py` | Open-Meteo forecast -> `WeatherSnapshot` (Celsius stored, `TEMP_UNIT` for display) |
| `calendars.py` | Up to three iCal feeds -> merged `CalendarEvent`s; last good copy kept on disk |
| `formality.py` | Event title -> formality 1-5 via `LLM_TAG_MODEL`, cached by title hash |
| `reminders.py` | Parsing and storage of the iPhone Shortcut's push |
| `today.py` | In-memory + on-disk `TodayPayload`, timers, `refresh_if_stale`, `invalidate_outfits`, `stale` flags, collage links (`with_links`) |
| `context.py` | `get_day_context(services, for_date) -> DayContext` for the recommender |
| `jobs.py` | Job `integrations.refresh` (same as `refresh_if_stale`) |

Routes: `GET /api/today`, `POST /api/today/refresh`, `GET /api/today/status`,
`POST/GET /api/reminders`, `POST/GET /api/shortcut/tokens`,
`DELETE /api/shortcut/tokens/{id}`, `GET /api/calendar.ics` (Pi only).

## Database rules

Timers (weather every 30 min, calendars every 15 min, midnight rollover) make HTTP
calls only; they start from `integrations/lifecycle.py` when the app starts its workers.
The database is read on triggers: the presence sensor (via `mirror_api/presence.py`, only
when something is stale), the phone's
`POST /api/today/refresh`, a reminders push, `invalidate_outfits`, and
`get_day_context`; each is rate-limited (outfits at most every 10 min while missing,
spend mode hourly, an unrated title retried hourly). `GET /api/today` never queries
the database, except that the first request of a process with no cache at all starts
one background load.

Cache files live in `<LOCAL_DATA_DIR>/cache/integrations/` (mode 0600): `today.json`,
`formality.json` and `calendars/<source>.ics|.json`. The calendar URLs are never written
there (only a hash) and never logged.

## For the recommender

```python
from integrations.context import get_day_context
from integrations.today import invalidate_outfits

ctx = await get_day_context(services, for_date)     # never raises
...store outfits...
await invalidate_outfits(services)                  # rebuild + broadcast today.updated
```

`integrations.today` calls `recommender.today.get_outfits(services, for_date)` on the
triggers above. It should be a cheap read of stored outfits, not a generator: it may
be called several times a day.

## Reminders push contract

`POST /api/reminders` with `Authorization: Bearer <shortcut token>` and a JSON body:

```json
{
  "list": "Errands",
  "reminders": [
    {"id": "2026-11-20T09:15:02-05:00", "title": "Pick up dry cleaning",
     "due": "2026-11-25T17:00:00-05:00", "completed": false}
  ]
}
```

- `list` (required): the Reminders list name. Everything stored for that list is
  replaced by this push. A push with an empty or missing `reminders` clears the list.
- `reminders[].title` (required). Entries without a title are skipped.
- `reminders[].id` (optional): anything stable per reminder. The steps below use the
  reminder's creation date. Without it, title + due is used.
- `reminders[].due` (optional): ISO 8601 is best. Also understood: `Nov 25, 2026 at 5:00 PM`,
  `11/25/26, 5:00 PM`, `Today at 5:00 PM`, a date only (`2026-11-25`, meaning by the end of
  that day), or unix seconds. Times without an offset are read in `TIMEZONE`. An
  unreadable value keeps the reminder with no due date and is counted in
  `due_unparsed` in the response.
- `reminders[].completed` (optional): `true`/`false`, `Yes`/`No` or `1`/`0`.
- Also accepted: Shortcuts-style key names (`Title`, `Due Date`, `Is Completed`), a
  JSON array of such objects, `{"lists": [...]}`, and `reminders` sent as a JSON string.

Response: `{"lists": ["Errands"], "stored": 4, "open": 3, "due_unparsed": 0, "synced_at": "..."}`.
422 when `list` is missing or the body is not JSON.

The mirror shows open reminders only, soonest due first, up to 50.

## Building the Shortcut (iPhone)

These steps have not been tried on a real iPhone yet. If a step's wording differs on
your iOS version, the goal of each step is stated so you can find the equivalent.

**1. Get a token (once).** In the mirror's phone app (paired phone), open Settings and
create a Shortcut token, or, until that screen exists, from a computer on home Wi-Fi:

```bash
curl -X POST http://<pi-address>:<port>/api/shortcut/tokens \
  -H "Authorization: Bearer <your phone's token>" -H "Content-Type: application/json" \
  -d '{"name": "Reminders Shortcut"}'
```

Copy the `token` from the reply. It is shown once. Anyone who has it can push
reminders, so keep it only in the Shortcut. To revoke it: `DELETE /api/shortcut/tokens/<device_id>`.

**2. Create the Shortcut.** Shortcuts app -> **+** -> name it "Mirror reminders". Add these
actions in order (search for each by name):

1. **Find Reminders**. Tap *Add Filter*: `List` `is` *your list* (for example "Errands").
   Add a second filter: `Is Completed` `is not` on (shows "is false" on some versions).
   Leave *Limit* off.
2. **Repeat with Each** (input: the *Reminders* from step 1).
3. Inside the repeat, **Dictionary**. Add four items:
   - Text key `id` -> value: tap *Repeat Item*, choose **Creation Date**, then tap the
     variable again and set *Date Format* to **ISO 8601** (turn *Include ISO 8601 Time* on).
   - Text key `title` -> *Repeat Item* -> **Title**.
   - Text key `due` -> *Repeat Item* -> **Due Date**, *Date Format* **ISO 8601**, time on.
   - Boolean key `completed` -> *Repeat Item* -> **Is Completed** (or just set it to *False*).
4. **End Repeat** is added automatically. Its output is *Repeat Results* (a list of
   the dictionaries).
5. After the repeat, **Dictionary** with one Text item: key `list`, value your list name
   (exactly as in Reminders).
6. **Set Dictionary Value**: key `reminders`, value *Repeat Results*, dictionary: the
   *Dictionary* from step 5. Its output is *Updated Dictionary*.
7. **Get Contents of URL**: URL `http://<pi-address>:<port>/api/reminders`.
   Tap *Show More*: Method **POST**; Headers: `Authorization` = `Bearer <token>` and
   `Content-Type` = `application/json`; Request Body **File**, and choose the
   *Updated Dictionary* from step 6. (If your version offers Request Body **JSON** with a
   way to pass a whole dictionary, that works too.)
8. Optional: **Show Result** with *Contents of URL* while testing; you should see
   `"stored": N`. Remove it once it works.

For a second list, duplicate steps 1-7 in the same Shortcut (or make a copy of the
Shortcut) with the other list name. Each list is replaced independently.

**3. Run it automatically.** Shortcuts -> *Automation* -> **+** -> *Personal Automation*:

- *Wi-Fi*: when joining your home network -> Run "Mirror reminders".
- *Time of Day*: 06:30, 12:00 and 18:00 (one automation each) -> Run.
- *App*: Reminders *Is Closed* -> Run.

Set each to *Run Immediately* and turn off *Notify When Run*.

**Check it worked:** `GET /api/today` (or the mirror) lists the reminders and
`last_sync.reminders` is the time of the last push.
