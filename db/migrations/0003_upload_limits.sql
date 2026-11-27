-- 0003: who may upload, and how often (R3 hardening).
--
-- registered_hotkeys mirrors the subnet's metagraph; the watcher replaces it
-- on every scan. The API presigns uploads only for hotkeys in it, so keys
-- that aren't on the subnet can't fill the bucket the validator pays for.
-- upload_authorizations records every presign, for a per-hotkey hourly cap
-- and for finding uploads that were never committed (orphans).

CREATE TABLE registered_hotkeys (
  hotkey TEXT PRIMARY KEY,
  uid INTEGER NOT NULL,
  coldkey TEXT NOT NULL DEFAULT '',
  synced_block BIGINT NOT NULL,
  synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE upload_authorizations (
  id BIGSERIAL PRIMARY KEY,
  hotkey TEXT NOT NULL,
  campaign_id TEXT NOT NULL REFERENCES campaigns(id),
  sha256 TEXT NOT NULL,
  object_key TEXT NOT NULL,
  size_bytes BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX upload_authorizations_hotkey_time_idx ON upload_authorizations (hotkey, created_at);
