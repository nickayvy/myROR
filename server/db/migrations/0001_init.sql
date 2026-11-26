-- MY-ror schema v1 (Postgres 17 + pgvector, hosted on Neon).
-- Image bytes never go in the database: columns ending in _key are object-storage keys.
-- Embeddings are FashionCLIP ViT-B/32, 512 dimensions. Changing the embedder means re-embedding.
-- The migration runner records each applied file in schema_migrations (version = file stem).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE devices (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name          text NOT NULL,
    kind          text NOT NULL DEFAULT 'phone' CHECK (kind IN ('phone', 'mirror', 'shortcut')),
    token_hash    text NOT NULL UNIQUE,
    paired_at     timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz,
    revoked_at    timestamptz
);

CREATE TABLE settings (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE calibrations (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at          timestamptz NOT NULL DEFAULT now(),
    gains               jsonb NOT NULL,
    background_ref_key  text,
    note                text
);

CREATE TABLE photos (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source          text NOT NULL CHECK (source IN ('phone_onboarding', 'phone_fullbody', 'mirror_checkin')),
    storage_key     text NOT NULL UNIQUE,
    sha256          text NOT NULL UNIQUE,
    width           integer,
    height          integer,
    face_blurred    boolean NOT NULL DEFAULT false,
    taken_at        timestamptz,
    uploaded_at     timestamptz NOT NULL DEFAULT now(),
    calibration_id  uuid REFERENCES calibrations (id) ON DELETE SET NULL,
    quality_flags   jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- A mirror photo may only be stored once its face blur has succeeded.
    CONSTRAINT mirror_photos_are_blurred CHECK (source <> 'mirror_checkin' OR face_blurred)
);

CREATE TABLE items (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name             text NOT NULL,
    category         text NOT NULL,
    subcategory      text,
    primary_color    text,
    primary_lab      real[],
    secondary_color  text,
    secondary_lab    real[],
    pattern          text,
    material_guess   text,
    formality        smallint CHECK (formality BETWEEN 1 AND 5),
    season           text[] NOT NULL DEFAULT '{}',
    fit              text,
    style_tags       text[] NOT NULL DEFAULT '{}',
    cutout_key       text,
    source_photo_id  uuid REFERENCES photos (id) ON DELETE SET NULL,
    embedding        vector(512),
    state            text NOT NULL DEFAULT 'clean' CHECK (state IN ('clean', 'dirty', 'retired')),
    last_worn_on     date,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX items_state_category_idx ON items (state, category);

-- Review inbox: nothing becomes an item without confirmation (P5).
CREATE TABLE detections (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    photo_id         uuid NOT NULL REFERENCES photos (id) ON DELETE CASCADE,
    box              jsonb,
    mask_key         text,
    cutout_key       text,
    raw_tags         jsonb NOT NULL DEFAULT '{}'::jsonb,
    embedding        vector(512),
    confidence       real,
    status           text NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'confirmed', 'merged', 'rejected')),
    matched_item_id  uuid REFERENCES items (id) ON DELETE SET NULL,
    match_score      real,
    created_at       timestamptz NOT NULL DEFAULT now(),
    resolved_at      timestamptz
);
CREATE INDEX detections_status_idx ON detections (status, created_at);
CREATE INDEX detections_photo_idx ON detections (photo_id);

CREATE TABLE outfits (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    for_date     date NOT NULL,
    rank         smallint NOT NULL DEFAULT 1,
    created_at   timestamptz NOT NULL DEFAULT now(),
    context      jsonb NOT NULL DEFAULT '{}'::jsonb,
    reasoning    text,
    source       text NOT NULL CHECK (source IN ('llm', 'rules')),
    status       text NOT NULL DEFAULT 'suggested'
                 CHECK (status IN ('suggested', 'shown', 'worn', 'skipped')),
    collage_key  text
);
CREATE INDEX outfits_for_date_idx ON outfits (for_date, rank);

CREATE TABLE outfit_items (
    outfit_id  uuid NOT NULL REFERENCES outfits (id) ON DELETE CASCADE,
    item_id    uuid NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    slot       text NOT NULL,
    PRIMARY KEY (outfit_id, item_id)
);

CREATE TABLE feedback (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    outfit_id  uuid NOT NULL REFERENCES outfits (id) ON DELETE CASCADE,
    value      smallint NOT NULL CHECK (value IN (-1, 1)),
    note       text,
    at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE wear_log (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id    uuid NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    worn_on    date NOT NULL,
    source     text NOT NULL CHECK (source IN ('manual', 'outfit', 'checkin')),
    outfit_id  uuid REFERENCES outfits (id) ON DELETE SET NULL,
    photo_id   uuid REFERENCES photos (id) ON DELETE SET NULL,
    UNIQUE (item_id, worn_on)
);
CREATE INDEX wear_log_worn_on_idx ON wear_log (worn_on);

-- Append-only: the current profile is the row with the highest version.
CREATE TABLE style_profile (
    version     integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    text        text NOT NULL,
    fit_notes   text,
    quiz        jsonb NOT NULL DEFAULT '{}'::jsonb,
    source      text NOT NULL DEFAULT 'user' CHECK (source IN ('user', 'quiz', 'weekly_update')),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE reminders (
    id         text PRIMARY KEY,
    title      text NOT NULL,
    due        timestamptz,
    list       text,
    completed  boolean NOT NULL DEFAULT false,
    synced_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE jobs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    type          text NOT NULL,
    payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
    status        text NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued', 'running', 'done', 'failed')),
    attempts      integer NOT NULL DEFAULT 0,
    max_attempts  integer NOT NULL DEFAULT 3,
    error         text,
    run_after     timestamptz NOT NULL DEFAULT now(),
    created_at    timestamptz NOT NULL DEFAULT now(),
    started_at    timestamptz,
    finished_at   timestamptz
);
CREATE INDEX jobs_queue_idx ON jobs (status, run_after);

-- Every paid call is logged here before its result is returned. 'live' and 'dev' are separate caps.
CREATE TABLE api_spend (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at        timestamptz NOT NULL DEFAULT now(),
    ledger    text NOT NULL CHECK (ledger IN ('live', 'dev')),
    provider  text NOT NULL,
    model     text NOT NULL,
    purpose   text NOT NULL,
    units     jsonb NOT NULL DEFAULT '{}'::jsonb,
    est_usd   numeric(10, 6) NOT NULL CHECK (est_usd >= 0),
    job_id    uuid REFERENCES jobs (id) ON DELETE SET NULL
);
CREATE INDEX api_spend_ledger_at_idx ON api_spend (ledger, at);
