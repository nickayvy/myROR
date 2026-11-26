-- Recommender: which stored outfits are the current suggestions for a day, and when one was shown.
-- A rebuild or reroll replaces the day's current set without deleting it (feedback, wears and skips
-- keep pointing at the old rows): superseded_at marks a set that is no longer current.
-- shown_at records the first time the mirror or phone displayed the outfit; at the end of the day,
-- outfits that were shown and not worn become status 'skipped'.

ALTER TABLE outfits ADD COLUMN IF NOT EXISTS superseded_at timestamptz;
ALTER TABLE outfits ADD COLUMN IF NOT EXISTS shown_at timestamptz;

CREATE INDEX IF NOT EXISTS outfits_current_idx ON outfits (for_date, rank) WHERE superseded_at IS NULL;
