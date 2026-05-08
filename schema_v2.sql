-- Insta-bot v2.1 migration: high-value lead flag + closed_won counter
-- Run this in the same Supabase project as schema.sql.

ALTER TABLE instagram_conversations
  ADD COLUMN IF NOT EXISTS is_high_value BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE instagram_conversations
  ADD COLUMN IF NOT EXISTS high_value_flagged_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_ig_conv_high_value
  ON instagram_conversations (high_value_flagged_at DESC)
  WHERE is_high_value = TRUE;

INSERT INTO bot_config (key, value) VALUES ('closed_won_total', '0')
ON CONFLICT (key) DO NOTHING;
