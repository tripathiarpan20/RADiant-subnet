-- 0005: intake, visibility, and reaper hardening.
--
-- * campaigns.opened_at_block: optional lower bound on valid payment blocks so
--   transfers predating a campaign cannot be replayed.
-- * intake_rejections.reaped_at: marks permanent rejections whose S3 upload has
--   already been deleted so the reaper does not re-delete them every hour.
-- * radiant_valid_visibility: enforce integer JSON numbers on reveal delays
--   (matching effective_from_block in radiant_valid_fee_history).

ALTER TABLE campaigns
  ADD COLUMN opened_at_block BIGINT CHECK (opened_at_block IS NULL OR opened_at_block >= 0);

ALTER TABLE intake_rejections
  ADD COLUMN reaped_at TIMESTAMPTZ;

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
    AND (policy->>'reveal_delay_s') ~ '^[0-9]+$'
    AND (policy->>'reveal_delay_s')::NUMERIC BETWEEN 0 AND 2147483647
    AND (
      (jsonb_typeof(policy->'fast_reveal_quality_threshold') = 'null'
       AND jsonb_typeof(policy->'fast_reveal_delay_s') = 'null')
      OR (jsonb_typeof(policy->'fast_reveal_quality_threshold') = 'number'
          AND (policy->>'fast_reveal_quality_threshold')::NUMERIC > 0
          AND (policy->>'fast_reveal_quality_threshold')::NUMERIC <= 1
          AND jsonb_typeof(policy->'fast_reveal_delay_s') = 'number'
          AND (policy->>'fast_reveal_delay_s') ~ '^[0-9]+$'
          AND (policy->>'fast_reveal_delay_s')::NUMERIC BETWEEN 0 AND (policy->>'reveal_delay_s')::NUMERIC)
    ),
    FALSE);
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
  RETURN FALSE;
END;
$$;
