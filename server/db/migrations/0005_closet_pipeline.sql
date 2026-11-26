-- Closet pipeline: per-photo progress, resumable job state, and which embedder made a vector.
--
-- photos.pipeline_status: queued -> processing -> done | failed (shown on the phone per photo).
-- photos.pipeline_state: results of paid steps that have no other home (e.g. the check-in vision
--   call), so a retried job does not pay for them twice.
-- detections.stage: 'processing' while the job is still filling the row; the inbox only shows
--   'ready' rows. detections.notes: pipeline metadata (tagging outcome, label, candidates).
-- *.embedding_model: name of the embedder that produced the vector; NULL vector or a different
--   model name means "re-embed" (pipeline.backfill_embeddings). Vectors from different models
--   are never compared.

ALTER TABLE photos ADD COLUMN IF NOT EXISTS pipeline_status text NOT NULL DEFAULT 'done'
    CHECK (pipeline_status IN ('queued', 'processing', 'done', 'failed'));
ALTER TABLE photos ADD COLUMN IF NOT EXISTS pipeline_step text;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS pipeline_error text;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS pipeline_state jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE detections ADD COLUMN IF NOT EXISTS stage text NOT NULL DEFAULT 'ready'
    CHECK (stage IN ('processing', 'ready'));
ALTER TABLE detections ADD COLUMN IF NOT EXISTS notes jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE detections ADD COLUMN IF NOT EXISTS embedding_model text;

ALTER TABLE items ADD COLUMN IF NOT EXISTS embedding_model text;

CREATE INDEX IF NOT EXISTS items_source_photo_idx ON items (source_photo_id);
