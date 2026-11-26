# mirror-api (server/)

FastAPI backend for the mirror. Python 3.11, managed with `uv`. Everything runs
against local containers in dev; no cloud account or API key is needed.

## Run the dev stack

```bash
# Postgres+pgvector, MinIO and the API, isolated by project name and ports
COMPOSE_PROJECT_NAME=myror-a PG_PORT=54321 MINIO_PORT=59001 API_PORT=8801 \
  docker compose -f ops/dev/docker-compose.yml up -d --build --wait
docker compose -p myror-a -f ops/dev/docker-compose.yml exec -T api python -m db.migrate
# API on http://127.0.0.1:8801 (code in server/ is mounted live; restart the api
# container to pick up changes). Requests from the host count as "LAN", not localhost.
docker compose -p myror-a -f ops/dev/docker-compose.yml down -v
```

## Tests

```bash
ops/dev/qa.sh                      # up, migrate, pytest, ruff, down
COMPOSE_PROJECT_NAME=myror-b PG_PORT=54322 MINIO_PORT=59002 API_PORT=8802 ops/dev/qa.sh -k jobs
KEEP=1 ops/dev/qa.sh               # leave the stack up afterwards
```

Tests use their own `mirror_test` database and `mirror-images-test` bucket, and
refuse to run if `DATABASE_URL` is not a local container.

## Layout

| Path | What |
| --- | --- |
| `mirror_api/app.py` | `create_app(settings, overrides)`; auto-includes routers, auto-imports job modules |
| `mirror_api/config.py` | Settings from the repo-root `.env` (names as `.env.example`) |
| `mirror_api/deps.py` | Dependency providers (`DbDep`, `StorageDep`, `LlmDep`, ...), `require_device`, `require_localhost` |
| `mirror_api/routers/` | One file per feature; any module with `router` is included |
| `mirror_api/jobs.py` | `@job`, `@daily`, durable queue, scheduler |
| `mirror_api/spend.py`, `pricing.py` | Spend ledger, caps, price table |
| `mirror_api/contracts.py` | Shared pydantic models (`WeatherSnapshot`, `TodayPayload`, ...) |
| `mirror_api/ws.py` | WebSocket hub |
| `db/` | Async pool, `python -m db.migrate`, `migrations/NNNN_*.sql` |
| `storage/` | `Storage` protocol, S3, local disk, read cache |
| `providers/llm/` | `LLM` protocol, `ClaudeLLM`, `FakeLLM` |
| `hardware/` | Protocols and mocks (`HARDWARE=mock`) |
| `pipeline/`, `recommender/`, `integrations/`, `capture/` | Feature packages; add `jobs.py` for handlers, `lifecycle.py` for startup/shutdown |
| `capture/` | Mirror capture state machine, quality check, calibration seam; routes in `routers/capture*.py` |
| `mirror_api/presence.py` | Presence watcher: debounced `presence` event, refresh when stale |
| `mirror_api/spa.py` | Serves `phone/dist` (`PHONE_DIST_DIR`) at `/` with a SPA fallback |
| `mirror_api/devtools.py` | `/api/dev/*` mock-hardware controls (dev + mock only) |
| `scripts/smoke_claude.py` | One real Claude call (needs a key; costs a fraction of a cent) |

## Run the end-to-end journey

```bash
COMPOSE_PROJECT_NAME=myror-a PG_PORT=54321 MINIO_PORT=59001 API_PORT=8801 LLM=scripted \
  docker compose -f ops/dev/docker-compose.yml up -d --build --wait
docker compose -p myror-a -f ops/dev/docker-compose.yml exec -T api python -m db.migrate
cd server && uv run python ../ops/dev/e2e.py http://127.0.0.1:8801 \
  --compose-project myror-a --minio http://127.0.0.1:59001
```

Needs a fresh (empty) stack. `LLM=scripted` (providers/llm/scripted.py) is a deterministic
fake that tags the generated garments, finds them in mirror photos and composes outfits, so
the LLM code paths run; the stock `LLM=fake` answers `{}` and only reaches the fallbacks.
`/api/dev/*` (mirror_api/devtools.py) drives the mock camera, cover and presence sensors;
it exists only when `APP_ENV=dev` and `HARDWARE=mock`.

## Extension conventions

- **Route**: add `mirror_api/routers/<name>.py` with `router = APIRouter(prefix="/api/<name>")`.
  It requires a device token (or localhost) by default; set `AUTH = "localhost"` or
  `AUTH = "none"` at module level to change that.
- **Job**: in `<package>/jobs.py`, `@job("<package>.<name>")` on `async def h(ctx, payload)`.
  Enqueue with `await jobs.enqueue("<package>.<name>", {...})`.
- **Daily job**: `@daily("06:00")` or `@daily(setting="precompute_time")` on `async def f(ctx)`.
- **Startup/shutdown**: `<package>/lifecycle.py` with `async def startup(services)` /
  `async def shutdown(services)` (packages listed in `app.FEATURE_PACKAGES`). startup runs
  only with `start_workers=True`; shutdown always runs and must cope with startup not having run.
- **Per-process feature state**: a named field on `Services` (`integrations`, `recommender`,
  `capture`, `presence`), created by the package's own accessor. Pipeline providers
  (`bg_remover`, `segmenter`, `embedder`, `face_blurrer`, `fal`) can be passed to
  `create_app(overrides=...)`.
- **Database outages**: every pool wait is bounded (`DB_CONNECT_TIMEOUT_S`, `DB_ACQUIRE_TIMEOUT_S`,
  `DB_RECONNECT_TIMEOUT_S`); an unreachable database answers 503 with `Retry-After`. Token checks
  fall back to the last known device list (`<LOCAL_DATA_DIR>/cache/auth/devices.json`, hashes only).
- **Enqueue inside a transaction**: `jobs.enqueue(..., conn=conn)` wakes the worker when that
  connection is released (after commit); no extra `wake()` is needed.
- **Schema change**: add the next `db/migrations/NNNN_description.sql`. Never edit an applied file.
- Never poll the database on a timer: Neon must be allowed to scale to zero.
