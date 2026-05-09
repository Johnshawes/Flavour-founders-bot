-- Insta-bot v3 migration: lead email capture + GHL handoff
-- Run this in the same Supabase project as schema.sql + schema_v2.sql.
--
-- Adds three columns to track email-first lead capture so we can hand off
-- to GHL for follow-ups 2 + 3 (Meta's 24h DM window kills IG follow-ups
-- past T+24h, so any silent qualified lead beyond that needs email reach).

ALTER TABLE instagram_conversations
  ADD COLUMN IF NOT EXISTS email TEXT;

ALTER TABLE instagram_conversations
  ADD COLUMN IF NOT EXISTS email_captured_at TIMESTAMPTZ;

ALTER TABLE instagram_conversations
  ADD COLUMN IF NOT EXISTS ghl_contact_id TEXT;

-- Quick lookup: does this conversation already have an email captured?
-- Used to skip GHL push on subsequent messages from the same lead.
CREATE INDEX IF NOT EXISTS idx_ig_conv_email_captured
  ON instagram_conversations (email_captured_at)
  WHERE email IS NOT NULL;
