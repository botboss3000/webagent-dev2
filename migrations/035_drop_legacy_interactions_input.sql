-- `interactions.input` existed only in early remote Postgres schemas.  The
-- canonical schema and local SQLite backend have never used it; keeping it made
-- `SELECT *` hybrid mirrors incompatible with local SQLite.

ALTER TABLE interactions DROP COLUMN IF EXISTS input;
ANALYZE interactions;
