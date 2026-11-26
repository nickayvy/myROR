-- U36: the unblurred original of a mirror photo may also be kept in the private bucket.
-- storage_key stays the AI-safe copy (face-blurred for mirror photos; the constraint from 0001 still
-- guards it). original_key is the untouched original and is never sent to fal or Claude.

ALTER TABLE photos ADD COLUMN IF NOT EXISTS original_key text UNIQUE;
