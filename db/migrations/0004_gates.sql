-- 0004: the gate pipeline (R4).
--
-- A submission moves pending -> gating -> queued (ready for rounds), or ends
-- rejected (reason_code), rejected_duplicate, or infra_failed. infra_failed
-- gets one automatic requeue (back to pending); a second is terminal until an
-- operator requeues it; the retry waits RADIANT_INFRA_RETRY_AFTER_S so a
-- GPU shortage has time to clear. The worker claims rows with a lease, so a crashed
-- worker's claim expires and another picks the row up.
--
-- Only data-free logs are kept for miners: the image build log and the
-- qualification sandbox log (no evaluation data is ever mounted there).

ALTER TABLE submissions ALTER COLUMN state SET DEFAULT 'pending';
UPDATE submissions SET state = 'pending' WHERE state = 'queued';

ALTER TABLE submissions
  ADD COLUMN reason_code TEXT,
  ADD COLUMN reason_detail TEXT,
  ADD COLUMN infra_failures INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN lease_until TIMESTAMPTZ,
  ADD COLUMN next_attempt_at TIMESTAMPTZ,
  ADD COLUMN stripped_object_key TEXT,
  ADD COLUMN gpu TEXT,
  ADD COLUMN weights_repo TEXT,
  ADD COLUMN weights_revision TEXT,
  ADD COLUMN weights_sha256 TEXT,
  ADD COLUMN image_id TEXT,
  ADD COLUMN qualification JSONB,
  ADD COLUMN pin_repo TEXT,
  ADD COLUMN pin_revision TEXT,
  ADD COLUMN pin_files JSONB;

CREATE INDEX submissions_state_idx ON submissions (state, id);

CREATE TABLE submission_logs (
  submission_id BIGINT NOT NULL REFERENCES submissions(id),
  kind TEXT NOT NULL CHECK (kind IN ('build', 'qualification')),
  body TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (submission_id, kind)
);

-- g0: hotkeys an operator has disqualified from a campaign.
CREATE TABLE campaign_disqualifications (
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  hotkey TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (campaign_id, hotkey)
);
