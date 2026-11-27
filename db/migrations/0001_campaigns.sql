-- 0001: campaigns.
--
-- One row per campaign. The pinned terms live in `manifest` (the normalised
-- pin set the hash was computed over) and `manifest_hash`; the title, fee
-- history and reveal policy are operational and outside the hash, as in
-- Pareton. Database rules here back up the application validators so manual
-- SQL against the table can't break the guarantees miners rely on:
--   * a manifest is frozen once the campaign leaves draft
--   * status moves only along draft -> open <-> closed -> archived
--   * fee history is append-only, starts at block 0 and grows by block
--   * the open campaigns' emission start weights sum to at most 1

CREATE OR REPLACE FUNCTION radiant_valid_fee_history(history JSONB)
RETURNS BOOLEAN LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  entry JSONB;
  amount NUMERIC;
  block NUMERIC;
  previous_block NUMERIC := -1;
BEGIN
  IF history IS NULL OR jsonb_typeof(history) <> 'array' OR jsonb_array_length(history) = 0 THEN
    RETURN FALSE;
  END IF;
  FOR entry IN SELECT value FROM jsonb_array_elements(history) LOOP
    IF jsonb_typeof(entry) <> 'object'
       OR NOT (entry ?& ARRAY['amount_tao', 'recipient', 'effective_from_block'])
       OR (entry - ARRAY['amount_tao', 'recipient', 'effective_from_block']) <> '{}'::jsonb
       OR jsonb_typeof(entry->'amount_tao') <> 'string'
       OR (entry->>'amount_tao') !~ '^[0-9]+(\.[0-9]+)?$'
       OR jsonb_typeof(entry->'recipient') <> 'string'
       OR (entry->>'recipient') !~ '^[^[:space:]]+$'
       OR jsonb_typeof(entry->'effective_from_block') <> 'number'
       OR (entry->>'effective_from_block') !~ '^[0-9]+$' THEN
      RETURN FALSE;
    END IF;
    amount := (entry->>'amount_tao')::NUMERIC;
    block := (entry->>'effective_from_block')::NUMERIC;
    IF amount * 1000000000 <> trunc(amount * 1000000000)
       OR amount * 1000000000 > 18446744073709551615
       OR block <= previous_block OR block > 9223372036854775807
       OR (previous_block = -1 AND block <> 0) THEN
      RETURN FALSE;
    END IF;
    previous_block := block;
  END LOOP;
  RETURN TRUE;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
  RETURN FALSE;
END;
$$;

CREATE OR REPLACE FUNCTION radiant_valid_visibility(policy JSONB)
RETURNS BOOLEAN LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
  IF policy = '{"mode":"private"}'::jsonb THEN
    RETURN TRUE;
  END IF;
  RETURN COALESCE(
    policy->>'mode' = 'public_after_reveal'
    AND policy ?& ARRAY['mode', 'reveal_delay_s', 'fast_reveal_quality_threshold', 'fast_reveal_delay_s']
    AND (policy - ARRAY['mode', 'reveal_delay_s', 'fast_reveal_quality_threshold', 'fast_reveal_delay_s']) = '{}'::jsonb
    AND jsonb_typeof(policy->'reveal_delay_s') = 'number'
    AND (policy->>'reveal_delay_s')::NUMERIC BETWEEN 0 AND 2147483647
    AND (
      (jsonb_typeof(policy->'fast_reveal_quality_threshold') = 'null'
       AND jsonb_typeof(policy->'fast_reveal_delay_s') = 'null')
      OR (jsonb_typeof(policy->'fast_reveal_quality_threshold') = 'number'
          AND (policy->>'fast_reveal_quality_threshold')::NUMERIC > 0
          AND (policy->>'fast_reveal_quality_threshold')::NUMERIC <= 1
          AND jsonb_typeof(policy->'fast_reveal_delay_s') = 'number'
          AND (policy->>'fast_reveal_delay_s')::NUMERIC BETWEEN 0 AND (policy->>'reveal_delay_s')::NUMERIC)
    ),
    FALSE);
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
  RETURN FALSE;
END;
$$;

CREATE TABLE campaigns (
  id TEXT PRIMARY KEY
    CHECK (id ~ '^[a-z0-9]([a-z0-9]|-(?!-)){0,22}[a-z0-9]$'),
  title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft', 'open', 'closed', 'archived')),
  manifest_version INTEGER NOT NULL CHECK (manifest_version = 1),
  manifest JSONB NOT NULL CHECK (jsonb_typeof(manifest) = 'object' AND manifest->>'campaign_id' = id),
  manifest_hash TEXT NOT NULL UNIQUE CHECK (manifest_hash ~ '^sha256:[0-9a-f]{64}$'),
  submission_fee_history JSONB NOT NULL CHECK (radiant_valid_fee_history(submission_fee_history)),
  visibility JSONB NOT NULL CHECK (radiant_valid_visibility(visibility)),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX campaigns_status_idx ON campaigns (status);

CREATE OR REPLACE FUNCTION radiant_campaign_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status <> 'draft'
     AND (NEW.manifest IS DISTINCT FROM OLD.manifest OR NEW.manifest_hash IS DISTINCT FROM OLD.manifest_hash) THEN
    RAISE EXCEPTION 'campaign % is %: its manifest is frozen', OLD.id, OLD.status;
  END IF;
  IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
       (OLD.status = 'draft' AND NEW.status IN ('open', 'archived'))
    OR (OLD.status = 'open' AND NEW.status = 'closed')
    OR (OLD.status = 'closed' AND NEW.status IN ('open', 'archived'))) THEN
    RAISE EXCEPTION 'campaign % cannot move from % to %', OLD.id, OLD.status, NEW.status;
  END IF;
  IF jsonb_array_length(NEW.submission_fee_history) < jsonb_array_length(OLD.submission_fee_history)
     OR EXISTS (
       SELECT 1 FROM jsonb_array_elements(OLD.submission_fee_history) WITH ORDINALITY o(value, idx)
       WHERE NEW.submission_fee_history -> (idx::int - 1) IS DISTINCT FROM o.value) THEN
    RAISE EXCEPTION 'campaign % fee history is append-only', OLD.id;
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

CREATE TRIGGER campaigns_guard_trg
  BEFORE UPDATE ON campaigns
  FOR EACH ROW EXECUTE FUNCTION radiant_campaign_guard();

-- Ported from Pareton: open campaigns must not promise more than the subnet
-- has. A CHECK sees one row, so this is a trigger; start weights bound the
-- decayed weights at every block.
CREATE OR REPLACE FUNCTION radiant_emission_start_weight(manifest JSONB) RETURNS NUMERIC
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  raw JSONB := manifest -> 'emission_rule' -> 'start_weight';
  weight NUMERIC;
BEGIN
  IF raw IS NULL OR jsonb_typeof(raw) <> 'number' THEN
    RAISE EXCEPTION 'emission_rule.start_weight must be a number';
  END IF;
  weight := raw::TEXT::NUMERIC;
  IF weight < 0 OR weight > 1 THEN
    RAISE EXCEPTION 'emission_rule.start_weight must be in [0, 1], got %', weight;
  END IF;
  RETURN weight;
END;
$$;

CREATE OR REPLACE FUNCTION radiant_emission_sum_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  others NUMERIC;
  mine NUMERIC;
BEGIN
  IF NEW.status <> 'open' THEN
    RETURN NEW;
  END IF;
  -- Serialise concurrent openings so two can't each see the other as closed.
  PERFORM pg_advisory_xact_lock(hashtext('radiant_emission_sum'));
  mine := radiant_emission_start_weight(NEW.manifest);
  SELECT COALESCE(SUM(radiant_emission_start_weight(manifest)), 0) INTO others
    FROM campaigns WHERE status = 'open' AND id <> NEW.id;
  IF mine + others > 1 THEN
    RAISE EXCEPTION 'opening % would take open start weights to %, over 1.0', NEW.id, mine + others;
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER campaigns_emission_sum_trg
  BEFORE INSERT OR UPDATE ON campaigns
  FOR EACH ROW EXECUTE FUNCTION radiant_emission_sum_guard();
