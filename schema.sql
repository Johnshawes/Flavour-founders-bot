CREATE TABLE IF NOT EXISTS instagram_conversations (
  ig_sender_id TEXT PRIMARY KEY,
  funnel TEXT NOT NULL,
  stage TEXT NOT NULL DEFAULT 'qualifying',
  path TEXT,
  message_history JSONB NOT NULL DEFAULT '[]'::jsonb,
  last_user_message_at TIMESTAMPTZ,
  last_assistant_message_at TIMESTAMPTZ,
  outline_sent_at TIMESTAMPTZ,
  awaiting_user BOOLEAN NOT NULL DEFAULT FALSE,
  follow_up_count INTEGER NOT NULL DEFAULT 0,
  next_follow_up_at TIMESTAMPTZ,
  archived BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ig_conv_followup
  ON instagram_conversations (next_follow_up_at)
  WHERE archived = FALSE AND awaiting_user = TRUE;

CREATE INDEX IF NOT EXISTS idx_ig_conv_stage
  ON instagram_conversations (stage);

CREATE TABLE IF NOT EXISTS processed_comments (
  comment_id TEXT PRIMARY KEY,
  processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bot_config (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO bot_config (key, value) VALUES
  ('monthly_capacity', '10'),
  ('current_clients_this_month', '6')
ON CONFLICT (key) DO NOTHING;
