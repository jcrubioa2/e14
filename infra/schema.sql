-- Postgres schema for the e14 vote store (Aurora Serverless v2, reached via the
-- RDS Data API). Faithful port of the community.sqlite DDL in
-- e14detector/community.py. field_state is preserved in full (VLM/appeal columns
-- included) by decision, even though the crowd-only swipe flow does not currently
-- drive the VLM adjudication path.
--
-- Type notes vs SQLite:
--   * INTEGER PRIMARY KEY AUTOINCREMENT -> BIGINT GENERATED ALWAYS AS IDENTITY
--   * created_at/updated_at held ISO-8601 strings -> timestamptz (the Data API
--     port writes ISO strings, which Postgres parses; new rows default to now()).
--   * rate_buckets.tokens / .updated_at stay double precision: allow() runs
--     token-bucket math on epoch seconds (time.time()), not wall-clock timestamps.

CREATE TABLE IF NOT EXISTS flags (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    field_key   text        NOT NULL,
    voter_token text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (field_key, voter_token)
);
CREATE INDEX IF NOT EXISTS idx_flags_field ON flags (field_key);

CREATE TABLE IF NOT EXISTS field_state (
    field_key              text        PRIMARY KEY,
    vlm_state              text        NOT NULL DEFAULT 'NONE',  -- NONE | PENDING | CLEAN | STRANGE
    last_adjudicated_votes integer     NOT NULL DEFAULT 0,
    published              integer     NOT NULL DEFAULT 0,
    image_hash             text,
    updated_at             timestamptz NOT NULL DEFAULT now(),
    -- Appeal path ("Se ve normal"): a separate tally of normal-votes that can
    -- challenge a crop shown as strange. appeal_cleared suppresses it once a
    -- neutral re-read comes back CLEAN; appeal_state (NONE|PENDING) de-dups
    -- concurrent appeal triggers without touching vlm_state.
    last_appealed_votes    integer     NOT NULL DEFAULT 0,
    appeal_state           text        NOT NULL DEFAULT 'NONE',
    appeal_cleared         integer     NOT NULL DEFAULT 0
);

-- "Se ve normal" votes. Own table (not mixed into flags) so the two directions
-- never share a tally and the suspicious flow is untouched.
CREATE TABLE IF NOT EXISTS appeals (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    field_key   text        NOT NULL,
    voter_token text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (field_key, voter_token)
);
CREATE INDEX IF NOT EXISTS idx_appeals_field ON appeals (field_key);

CREATE TABLE IF NOT EXISTS rate_buckets (
    voter_token text             PRIMARY KEY,
    tokens      double precision NOT NULL,
    updated_at  double precision NOT NULL  -- epoch seconds (token-bucket math)
);

-- Reverse map for anonymized crop ids. The swipe feed hands out opaque cids; to
-- serve the image and record a vote the server resolves a cid back to its field
-- key + crop path without the client ever seeing them. A row is registered the
-- moment a cid is surfaced, so it only holds crops actually shown and survives
-- restarts (shareable /c/{cid} links keep working).
CREATE TABLE IF NOT EXISTS cid_index (
    cid         text PRIMARY KEY,
    field_key   text NOT NULL,
    crop_rel    text NOT NULL,
    document_id text NOT NULL
);
