-- U37: mirror photos may be stored and sent to fal and Claude without a face blur.
-- Face blur becomes an optional setting (FACE_BLUR, off by default). When it is on, storage_key is the
-- blurred copy and original_key the untouched original; when it is off, storage_key is the original,
-- face_blurred is false and original_key stays null.

ALTER TABLE photos DROP CONSTRAINT IF EXISTS mirror_photos_are_blurred;
