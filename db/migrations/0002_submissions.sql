-- 0002: submissions from on-chain commitments (R3).
--
-- One row per (campaign, zip sha256), first seen in chain order wins. The
-- short numeric id names the HF pin (rad-<campaign>-<hotkey>-s<id>). A fee
-- proof (block, extrinsic index) can back at most one submission ever.
-- Commitments the watcher refuses go to intake_rejections instead, so a bad
-- proof never claims the dedupe slot and miners can see why.

CREATE TABLE submissions (
  id BIGSERIAL PRIMARY KEY,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  hotkey TEXT NOT NULL,
  coldkey TEXT NOT NULL DEFAULT '',
  uid INTEGER,
  sha256 TEXT NOT NULL CHECK (sha256 ~ '^sha256:[0-9a-f]{64}$'),
  code_sha256 TEXT NOT NULL CHECK (code_sha256 ~ '^sha256:[0-9a-f]{64}$'),
  object_key TEXT NOT NULL,
  size_bytes BIGINT NOT NULL CHECK (size_bytes > 0),
  commit_block BIGINT NOT NULL CHECK (commit_block > 0),
  payment_block BIGINT,
  payment_tx INTEGER,
  fee_exempt BOOLEAN NOT NULL DEFAULT false,
  state TEXT NOT NULL DEFAULT 'queued' CHECK (state ~ '^[a-z0-9_]+$'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (campaign_id, sha256),
  CHECK ((payment_block IS NULL) = (payment_tx IS NULL))
);

CREATE UNIQUE INDEX submissions_payment_ref_uq ON submissions (payment_block, payment_tx)
  WHERE payment_block IS NOT NULL;
CREATE INDEX submissions_campaign_state_idx ON submissions (campaign_id, state);
CREATE INDEX submissions_hotkey_idx ON submissions (hotkey);

CREATE TABLE submission_events (
  id BIGSERIAL PRIMARY KEY,
  submission_id BIGINT NOT NULL REFERENCES submissions(id),
  state TEXT NOT NULL,
  detail JSONB NOT NULL DEFAULT '{}'::jsonb,
  at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX submission_events_submission_idx ON submission_events (submission_id, at);

CREATE TABLE intake_rejections (
  hotkey TEXT NOT NULL,
  commit_block BIGINT NOT NULL,
  raw TEXT NOT NULL,
  campaign_id TEXT,
  sha256 TEXT,
  reason TEXT NOT NULL CHECK (reason ~ '^[a-z0-9_]+$'),
  first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (hotkey, commit_block, raw)
);
CREATE INDEX intake_rejections_sha_idx ON intake_rejections (sha256);
