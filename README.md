# Flavour Founders Instagram DM Bot

AI-powered Instagram DM chatbot built with Claude (Anthropic) + FastAPI.
Qualifies leads, closes through DM, and re-engages silent qualified leads
on a 24h / 72h / 7d / 14d follow-up sequence.

State lives in Supabase, so restarts don't lose live conversations and the
follow-up scheduler can do its job.

---

## What's New

**v2.1 (latest):**
- **Whop webhook** — auto-tracks programme purchases as `closed_won_total` (sales tracker, doesn't touch the public capacity counter)
- **High-value lead detection** — bot flags leads who say £100K+/month or 3+ shops; Command Centre shows them in a "🔥 High-value leads" section so you can step in personally
- **Smoke-test CLI** — `python test_bot.py "your message"` to dry-run the bot's reply without sending real DMs

**v2:**
- **Persistent state** — conversations stored in Supabase, not RAM
- **3-question qualification** (was 6) — faster, sharper, ends with a belief-builder
- **Value anchor** before the programme outline link (no more cold price reveals)
- **Hard binary CTA** — book a call or buy directly, no passive closes
- **Capacity-aware urgency** — bot pulls one of 3 nudges based on how full your month is
- **Follow-up sequence** — automatic re-engagement of silent qualified leads
- **Admin endpoints** — bump the client count from your phone when you close a deal

---

## Files

| File | Purpose |
|------|---------|
| `main.py` | The bot — webhook, Claude, persistence, scheduler, Whop endpoint |
| `schema.sql` | Initial Supabase tables |
| `schema_v2.sql` | v2.1 migration — high-value flag + closed_won counter |
| `case_studies.txt` | Real client wins, used for social proof + follow-ups |
| `founder-profile.txt` | Bio injected into every system prompt |
| `test_bot.py` | Smoke-test CLI — pipe a fake message, get the bot's reply |
| `CLAUDE.md` | Trigger keywords (edit this to change comment triggers) |
| `requirements.txt` | Python deps |
| `Procfile` | Railway run command |
| `.env.example` | Env var template |

---

## First-Time Deployment

### 1. Run the schema
Open the Supabase SQL editor for the **same project** the audit-generator uses
(so `calculator_sessions.ig_sender_id` joins to `instagram_conversations.ig_sender_id`).
Paste in `schema.sql` and run.

That gives you three tables:
- `instagram_conversations` — one row per IG user the bot has spoken to
- `processed_comments` — survives restarts
- `bot_config` — capacity counter (seeded with `monthly_capacity=10`, `current_clients_this_month=6`)

### 2. Fill in `case_studies.txt`
The placeholder file has three example wins. Replace them with **real, current** client
results — first name + city, real numbers. The bot drops these into qualification
conversations and uses them in 72h follow-ups. If they're vague or fake the
conversion impact disappears.

### 3. Push to GitHub
The repo is already wired up at `Johnshawes/Flavour-founders-bot`. Just commit + push.

### 4. Set Railway env vars
In Railway → your bot service → Variables, make sure these are set:

| Variable | Value |
|----------|-------|
| `ANTHROPIC_API_KEY` | Existing |
| `INSTAGRAM_ACCESS_TOKEN` | Existing |
| `APP_SECRET` | Existing |
| `VERIFY_TOKEN` | Existing |
| `INSTAGRAM_PAGE_ID` | Existing (`17841411627714313`) |
| `LEAD_MAGNET_URL` | Existing (`https://ff-margin-calculator.vercel.app`) |
| `AUDIT_PAYMENT_URL` | Existing |
| `SUPABASE_URL` | **NEW** — same as audit-generator |
| `SUPABASE_KEY` | **NEW** — same as audit-generator (service role) |
| `ADMIN_KEY` | **NEW** — pick any long random string, e.g. `openssl rand -hex 32` |

### 5. Redeploy
Railway will auto-deploy on git push. Hit `https://web-production-3aebb.up.railway.app/`
and you should see something like:

```json
{
  "status": "Flavour Founders Bot is running 🚀",
  "supabase": true,
  "capacity": {"capacity": 10, "current": 6, "spots_left": 4, "pct_full": 0.6}
}
```

If `supabase` is `false`, the env vars aren't loaded yet.

---

## Day-to-Day Operations

### When you close a new client → bump the counter
The bot uses the live count to pick its urgency line. After every paying client,
fire one of these from your phone (Shortcuts app, curl, anything):

```bash
curl -X POST https://web-production-3aebb.up.railway.app/admin/clients/increment \
  -H "X-Admin-Key: YOUR_ADMIN_KEY"
```

### On the 1st of each month → reset
```bash
curl -X POST https://web-production-3aebb.up.railway.app/admin/clients/reset-month \
  -H "X-Admin-Key: YOUR_ADMIN_KEY"
```

(Or set it to a specific number with `/admin/clients/set` and body `{"value": N}`.)

### Check what the bot is currently saying
```bash
curl https://web-production-3aebb.up.railway.app/admin/capacity \
  -H "X-Admin-Key: YOUR_ADMIN_KEY"
```

Returns the current state plus the urgency line the bot is using right now.

### Force a follow-up sweep (testing)
```bash
curl -X POST https://web-production-3aebb.up.railway.app/admin/follow-ups/run \
  -H "X-Admin-Key: YOUR_ADMIN_KEY"
```

The hourly scheduler does this automatically. This endpoint is for testing only.

---

## How the Follow-Up Sequence Works

When the bot sends a reply, `awaiting_user=true` and `next_follow_up_at = now + 24h`.

If the user replies, the timer resets and the count goes back to 0.
If they don't reply, the hourly scheduler fires:

| When | Count | Message |
|------|-------|---------|
| +24h silent | 0 → 1 | "Did you get a chance to read the outline?" |
| +72h after that | 1 → 2 | Case study + soft pull back to outline |
| +7d after that | 2 → 3 | Capacity-aware soft close (Whop link + booking link) |
| +7d after that | 3 → archived | Conversation marked archived, no more bot follow-ups |

Lead-magnet and startup-course funnels get a single gentler nudge then archive.

---

## How Capacity Messaging Works

The bot reads `monthly_capacity` and `current_clients_this_month` from `bot_config`
on every reply and picks one of these lines:

| % full | Line |
|--------|------|
| 0–60% | "I work with owners 1:1 so I cap intake at 10 a month — still got room this month if it's the right fit." |
| 60–90% | "I've got X spots left this month before I close intake." |
| 90–100% | "Quick heads up — only 1 spot left this month before I close intake." |
| 100% | "I'm full this month. Next opening's the start of next month. Happy to hold a spot." |

The bot also uses these in the 7-day follow-up automatically.

---

## Whop Webhook (Sales Tracking)

The bot exposes `POST /webhook/whop`. When Whop fires a successful purchase event for the
programme plan (`plan_PNt9PcJaESP6i`), the bot increments `closed_won_total` in
`bot_config`. Audit purchases (a different plan) are ignored.

**Set up:**

1. **Run the schema migration** in Supabase SQL editor (one-time, idempotent):
   ```
   ALTER TABLE instagram_conversations
     ADD COLUMN IF NOT EXISTS is_high_value BOOLEAN NOT NULL DEFAULT FALSE;
   ALTER TABLE instagram_conversations
     ADD COLUMN IF NOT EXISTS high_value_flagged_at TIMESTAMPTZ;
   INSERT INTO bot_config (key, value) VALUES ('closed_won_total', '0')
     ON CONFLICT (key) DO NOTHING;
   ```
   (Or paste `schema_v2.sql`.)

2. **In your Whop dashboard:**
   - Settings → Developer → Webhooks (path varies; look for "Webhooks")
   - Add a new webhook with URL: `https://web-production-3aebb.up.railway.app/webhook/whop`
   - Subscribe to: `payment.succeeded` (or `membership.went_valid` / `membership.created` —
     bot accepts any of them)
   - Copy the **signing secret** Whop generates

3. **Add to Railway:**
   ```
   WHOP_WEBHOOK_SECRET=<the secret from step 2>
   ```

4. **Test:** make a real purchase or use Whop's "Send test event". Check Railway logs for
   `closed_won_total: N -> N+1`. The Insta Bot dashboard `/instabot` will show the new
   "Closed sales" KPI.

**Note:** the webhook does NOT modify `current_clients_this_month`. That counter is under
manual control. The Whop event only bumps the all-time sales tracker.

---

## High-Value Lead Flag

When a lead's qualification answers indicate they do **£100K+/month** OR run **3+ shops/sites**,
Claude appends `[HIGH_VALUE_LEAD]` to its reply. The bot strips it before sending the DM
and sets `is_high_value=true` on the conversation row.

These leads show up in a "🔥 High-value leads" section at the top of the
`/instabot` dashboard so you can step in personally.

**To change the threshold:** edit `build_application_prompt` in `main.py`, look for the
`HIGH-VALUE LEAD FLAG` block.

---

## Smoke-Test the Bot

Test the bot's Claude responses without sending real DMs:

```
pip install httpx python-dotenv
export ADMIN_KEY=<your admin key>
python test_bot.py "I run a bakery in Bristol, doing £30K/month"
```

Multi-turn (alternating user → bot → user → … ending with user):
```
python test_bot.py --history \
  "I run a bakery" \
  "Nice — how long?" \
  "5 years, doing £150K/month across 4 shops"
```

Different funnel:
```
python test_bot.py --funnel lead_magnet "yes please send the calculator"
```

Output shows the bot's reply and flags `🔥 FLAGGED AS HIGH-VALUE LEAD` if appropriate.

---

## Updating Trigger Keywords

Edit `CLAUDE.md` (in this folder), commit, push. Bot reloads on restart.

---

## Customising Tone / Flow

All prompt logic lives in `build_application_prompt`, `build_lead_magnet_prompt`,
`build_startup_course_prompt` in `main.py`. Edit there.

The qualification questions are deliberately compressed to 3 — adding more is
the fastest way to **drop conversion**, not raise it. If you change them,
keep Q3 a belief-builder ("what's it costing you not to fix that").
