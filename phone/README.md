# phone/ — the mirror's iPhone web app

React + Vite + TypeScript, two runtime dependencies (`react`, `react-dom`). mirror-api serves
the built `dist/` at `/` with a single-page-app fallback, so the phone opens
`http://<pi-address>:<port>/` on home Wi-Fi. Plain HTTP by design (HANDOFF section 14): no
service worker, no in-page camera, no push, no clipboard dependency.

## Commands

```bash
npm ci                       # once
npm run dev                  # dev server on :5173; proxies /api and /ws to API_TARGET
API_TARGET=http://127.0.0.1:8806 npm run dev
npm run typecheck            # tsc
npm run lint                 # eslint
npm run build                # -> dist/ (what mirror-api serves)
API_URL=http://127.0.0.1:8806 npm run gen   # regenerate API types + taxonomy (see below)
```

On the Pi: `cd phone && npm ci && npm run build`; mirror-api picks up `phone/dist`
(`PHONE_DIST_DIR` overrides the path). No Node process runs for the phone app at runtime.

`VITE_MOCK_CAPTURE=1 npm run build` includes a local simulation of the capture state machine,
used only when the server answers 404 on `/api/capture`. It was used for QA before the
capture routes existed; normal builds leave it out.

## Generated code

- `src/api/schema.d.ts`: `openapi-typescript` output from a running server's `/openapi.json`
  (`npm run gen:api`, `API_URL=...` or `OPENAPI_FILE=...`). Request bodies and every route
  with a declared response model use it. Routes that return a plain dict show up as
  `object`, so their response shapes are written by hand in `src/api/types.ts`, each marked
  `HAND` with the server function it mirrors.
- `src/api/taxonomy.gen.ts`: the fixed tag lists, parsed from
  `server/mirror_api/taxonomy.py` (`npm run gen:taxonomy`). The API does not expose the
  taxonomy, so the edit forms use this copy; regenerate it whenever the taxonomy changes.

## Layout

| Path | What |
| --- | --- |
| `src/api/client.ts` | fetch wrapper, token in `localStorage` (`myror.token`), 401 → Pair, one retry on 503 |
| `src/api/ws.ts` | `/ws` client: `{"type":"auth"}` first, backoff 1–30 s, reconnect on wake, `reconnected` event |
| `src/api/capture.ts` | capture routes, garment → inbox route; `mockCapture.ts` (QA builds only) |
| `src/upload/` | bulk upload queue (IndexedDB-backed) |
| `src/screens/` | Pair, Today, Capture, AddItems, Inbox, Closet, ItemDetail, Checkin, Style, Settings, More |
| `src/components/` | shared UI, tag editor |

## Bulk upload

One file per request (`POST /api/items/upload`), three in parallel, per-photo progress from
XHR upload events. Each picked file is copied into IndexedDB first, so a reload, a killed
tab or a phone that slept resumes where it stopped (the server dedupes identical bytes, so
re-sending a half-sent file is safe). Network errors, timeouts and 5xx retry automatically
with backoff (up to 8 tries; going offline or to the background does not use up tries);
4xx failures (too large, wrong type) stop with a reason. After upload, progress through the
pipeline comes from `pipeline.photo` WebSocket events, with a status check on reconnect and
a 5-second re-check only while something is still processing.

## iPhone notes

- Pair by scanning the mirror's QR with the Camera app: it opens `/pair?code=...` (a
  `#code=` fragment also works). A Home Screen web app has storage separate from Safari, so
  pair once more inside it by typing the code shown under the QR.
- The Reminders Shortcut token screen tries the clipboard and otherwise selects the text so
  iOS shows its Copy menu (the clipboard API is unavailable on plain HTTP).
