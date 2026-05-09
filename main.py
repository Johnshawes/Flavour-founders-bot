"""
Flavour Founders Instagram DM Bot
─────────────────────────────────
Three funnels: application (high intent), lead_magnet (margin calc), startup_course (£27 tripwire).

Conversation state lives in Supabase (instagram_conversations) so:
  • restarts don't lose live sales conversations
  • a follow-up scheduler can re-engage silent qualified leads
  • capacity messaging is driven from a real number you control

Follow-up sequence (qualified leads who go quiet after we replied):
  T+24h  → "Did you get a chance?"
  T+72h  → case study + soft pull
  T+7d   → capacity close
  T+14d  → archive (drop into GHL nurture)
"""

import os
import re
import json
import logging
import hmac
import hashlib
import random
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import PlainTextResponse, JSONResponse
from anthropic import Anthropic
from supabase import create_client, Client
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# ── Config ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
VERIFY_TOKEN      = os.environ.get("VERIFY_TOKEN", "")
APP_SECRET        = os.environ.get("APP_SECRET", "")
ACCESS_TOKEN      = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
LEAD_MAGNET_URL   = os.environ.get("LEAD_MAGNET_URL", "https://ff-margin-calculator.vercel.app")
PAGE_ID           = os.environ.get("INSTAGRAM_PAGE_ID", "")
ADMIN_KEY         = os.environ.get("ADMIN_KEY", "")
SUPABASE_URL      = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY      = os.environ.get("SUPABASE_KEY", "")
AUDIT_PAYMENT_URL = os.environ.get("AUDIT_PAYMENT_URL", "https://whop.com/checkout/plan_2A9NPWYCBjKfR")

# GHL handoff — when a lead gives us their email in DM we upsert a GHL contact
# tagged for the funnel so GHL workflows can take over follow-ups 2 + 3 (Meta's
# 24h DM window kills IG follow-ups past T+24h). Both env vars must be set or
# the bot silently skips the GHL push (Supabase email row is still saved).
GHL_API_KEY     = os.environ.get("GHL_API_KEY", "")
GHL_LOCATION_ID = os.environ.get("GHL_LOCATION_ID", "")
GHL_BASE_URL    = os.environ.get("GHL_BASE_URL", "https://services.leadconnectorhq.com")

PROGRAMME_OUTLINE_URL = "https://ff-programme-outline.vercel.app"
WHOP_PROGRAMME_URL    = "https://whop.com/checkout/plan_PNt9PcJaESP6i"
BOOKING_URL           = "https://flavourfounders.com/3---schedule-page-page-3707"
STARTUP_COURSE_URL    = "https://flavourfounders.thinkific.com/courses/start-up"

# Whop integration — the programme deposit plan we count as a "closed sale".
# Audit purchases (a different plan) are ignored on purpose.
PROGRAMME_PLAN_ID    = "plan_PNt9PcJaESP6i"
WHOP_WEBHOOK_SECRET  = os.environ.get("WHOP_WEBHOOK_SECRET", "")

# Marker Claude appends when a lead matches the high-value criteria.
# Stripped before the DM is sent — never seen by the lead.
HIGH_VALUE_MARKER = "[HIGH_VALUE_LEAD]"
# ─────────────────────────────────────────────────────────────────────────────

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
supabase: Client | None = (
    create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None
)

if not supabase:
    logger.warning("Supabase not configured — falling back to in-memory state (dev only)")

# In-memory fallbacks (only used if Supabase isn't configured — local dev)
_mem_conversations: dict[str, dict] = {}
_mem_processed_comments: set[str] = set()

COMMENT_REPLIES = [
    "Just dropped you a DM!",
    "Sent you a message — check your DMs!",
    "Just messaged you!",
    "Check your DMs — just sent something over.",
    "Dropped you a DM, have a look!",
    "Just pinged you a message!",
]


# ─────────────────────────────────────────────────────────────────────────────
# Trigger keywords (loaded from CLAUDE.md)
# ─────────────────────────────────────────────────────────────────────────────
def load_trigger_keywords() -> dict[str, str]:
    claude_md = Path(__file__).parent / "CLAUDE.md"
    default = {
        "info": "application", "interested": "application", "how much": "application",
        "tell me more": "application", "sign me up": "application",
        "system": "lead_magnet", "freedom": "lead_magnet", "margins": "lead_magnet",
        "calculator": "lead_magnet", "free": "lead_magnet", "guide": "lead_magnet",
        "help": "lead_magnet",
        "startup": "startup_course", "course": "startup_course",
        "learn": "startup_course", "training": "startup_course", "diy": "startup_course",
    }
    if not claude_md.exists():
        return default

    text = claude_md.read_text(encoding="utf-8")
    keywords: dict[str, str] = {}
    in_section = False
    for line in text.splitlines():
        if line.strip().lower().startswith("## trigger keywords"):
            in_section = True
            continue
        if in_section and line.strip().startswith("## ") and not line.strip().startswith("### "):
            break
        if in_section and line.strip().startswith("- "):
            raw = line.strip().lstrip("- ").strip()
            if "|" in raw:
                kw, funnel = raw.rsplit("|", 1)
                keywords[kw.strip().lower()] = funnel.strip().lower()

    logger.info(f"Loaded {len(keywords)} trigger keywords")
    return keywords if keywords else default


TRIGGER_KEYWORDS = load_trigger_keywords()


# ─────────────────────────────────────────────────────────────────────────────
# Founder profile + case studies
# ─────────────────────────────────────────────────────────────────────────────
def _read_optional(path: Path, fallback: str) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        logger.warning(f"{path.name} not found — using fallback")
        return fallback


FOUNDER_PROFILE = _read_optional(
    Path(__file__).parent / "founder-profile.txt",
    "John Hawes — UK food and hospitality entrepreneur running KNEAD, "
    "Watermoor Meat Supply Ltd, and Flavour Founders.",
)

CASE_STUDIES_RAW = _read_optional(
    Path(__file__).parent / "case_studies.txt",
    "",
)


def parse_case_studies(raw: str) -> list[str]:
    if not raw:
        return []
    chunks = [c.strip() for c in raw.split("---")]
    # drop comments and blanks
    return [
        c for c in chunks
        if c and not all(line.startswith("#") for line in c.splitlines() if line.strip())
    ]


CASE_STUDIES = parse_case_studies(CASE_STUDIES_RAW)
logger.info(f"Loaded {len(CASE_STUDIES)} case studies")


# ─────────────────────────────────────────────────────────────────────────────
# Capacity messaging (driven from bot_config)
# ─────────────────────────────────────────────────────────────────────────────
def get_capacity_state() -> dict:
    """Returns {'capacity': int, 'current': int, 'spots_left': int, 'pct_full': float}."""
    capacity, current = 10, 0
    if supabase:
        try:
            rows = supabase.table("bot_config").select("key,value").in_(
                "key", ["monthly_capacity", "current_clients_this_month"]
            ).execute().data or []
            cfg = {r["key"]: r["value"] for r in rows}
            capacity = int(cfg.get("monthly_capacity", capacity))
            current  = int(cfg.get("current_clients_this_month", current))
        except Exception as e:
            logger.error(f"Failed to read capacity from bot_config: {e}")

    spots_left = max(0, capacity - current)
    pct_full   = (current / capacity) if capacity else 0.0
    return {"capacity": capacity, "current": current, "spots_left": spots_left, "pct_full": pct_full}


def capacity_line() -> str:
    """One-line capacity nudge sized to how full we are."""
    s = get_capacity_state()
    cap, left, pct = s["capacity"], s["spots_left"], s["pct_full"]

    if left == 0:
        # Full — no spots
        return (
            f"Heads up — I'm full this month. Next opening's the start of next month. "
            f"Happy to hold a spot for you if you want it."
        )
    if pct >= 0.9:
        return f"Quick heads up — only {left} spot left this month before I close intake."
    if pct >= 0.6:
        return f"I've got {left} spots left this month before I close intake."
    # Soft / plenty of room
    return (
        f"I work with owners 1:1 so I cap intake at {cap} a month — still got room "
        f"this month if it's the right fit."
    )


# ─────────────────────────────────────────────────────────────────────────────
# System prompts
# ─────────────────────────────────────────────────────────────────────────────
def build_application_prompt(sender_id: str) -> str:
    cap_line = capacity_line()
    case_study_block = ""
    if CASE_STUDIES:
        # Pick one random case study per request — keeps replies fresh.
        cs = random.choice(CASE_STUDIES)
        case_study_block = (
            "\n\nSOCIAL PROOF — drop this in naturally during the conversation "
            "if it fits (only once, only if it lands — never force it):\n"
            f"\"{cs}\""
        )
    calc_link = f"{LEAD_MAGNET_URL}?ig_id={sender_id}" if sender_id else LEAD_MAGNET_URL

    return f"""You ARE John Hawes. You're replying to DMs as yourself — first person, always.

WHO YOU ARE:
{FOUNDER_PROFILE}

Use this credibility naturally — don't over-explain.

═══ INTENT CHECK ═══
If the message is casual fan stuff (e.g. "love your content", "great post", random compliments,
anything clearly NOT about their business) — respond with exactly: "IGNORE" and nothing else.
If it IS about their business or programme — proceed.

═══ TONE & LANGUAGE ═══
Professional, warm, direct. Short sentences. No waffle. First person always ("I", "me", "my").
- NEVER use "mate", "pal", "bro", "hun" or overly familiar terms
- Emojis sparingly — one per message max, only when it adds warmth
- No "Haha", "Ooh", filler laughs
- Confident and grounded — you've done this, you know what works

═══ FLOW — VALUE FIRST, QUALIFY ON SIGNAL ═══
The opener has already gone out and asked for their email so I can send them
the free Bakery Margin Calculator. Your job from here is to:
  (1) capture the email + deliver the calculator,
  (2) keep the conversation warm with ONE light, natural question,
  (3) qualify on signals that emerge in conversation — never on a quiz.

NEVER ask 3 questions in a row. NEVER make it feel like an application form.

── STAGE 1 — EMAIL & DELIVERY ───────────────────────────────────────────────
DELIVERY MECHANIC (CRITICAL): You CANNOT send emails. The calculator link is
delivered by pasting the FULL URL directly into your DM reply as a clickable
link. The email they give you is captured for FUTURE email follow-ups (handled
automatically by another system) — it is NOT how the calculator reaches them
right now. NEVER say "sent to that email", "I'll email it across", "check your
inbox", or anything implying email delivery. The link goes in this DM.

- If their reply contains an email address → great. Acknowledge briefly, paste
  the FULL calculator URL into your reply, then ask ONE warm-up. Example:
    "Got it — here's the calculator: {calc_link}
    While you're plugging numbers in — out of curiosity, how's the bakery
    going right now? Going well, or feeling stuck somewhere?"
- If they reply WITHOUT an email (e.g. "yes please", "go on then", "send it"),
  gently re-ask once: "Cool — what's the best email so I can keep you in the
  loop, and I'll drop the link across?"
- If they ask a question first ("what is it?", "is it free?"), answer briefly
  and re-anchor on the email ask. Don't lecture.

── STAGE 2 — WARM-UP REPLY (READ THE SIGNALS) ───────────────────────────────
After they've answered the warm-up, pay attention to:
- Scale signals: monthly revenue mentions, number of sites, team size, years trading
- Pain signals: "burnt out", "drowning", "doing every shift", "team won't run
  without me", "not making money", "stuck", "exhausted"
- Intent signals: "want to grow", "want to step back", "ready for help",
  "thinking of selling", "need to fix this"

PATH NOTE (internal — never say out loud):
- PATH A: trapped — can't step away, doing every shift, no team that works
  without them.
- PATH B: some structure — could step back, just need things sharpened.

── STAGE 3 — QUALIFY & ROUTE ────────────────────────────────────────────────
Apply the revenue filter ONLY when revenue surfaces in conversation. Do not
ask "what's your revenue" as a cold question — wait for it, or work it in
naturally ("rough monthly revenue you're working with?") if you've already
got rapport and they sound qualified on every other dimension.

REVENUE FILTER:
- £25K+/month → QUALIFIED. Bridge to programme outline (see CLOSE below).
- Under £25K/month BUT startup / under 1 year trading → QUALIFIED. Frame:
  "That's actually a great position — you can build this properly from the
  start instead of fixing mistakes later." Then bridge to outline.
- Under £25K/month AND 2+ years trading → SOFT EXIT to £27 course:
  "Honest with you — the 1:1 programme isn't quite the right fit at this
  stage. But I built a complete bakery system, 13 modules, 8 hours of
  video, was £999 yours for £27: {STARTUP_COURSE_URL}"
- Home baker / no premises / pre-launch → same warm exit + £27 course.

If they're clearly high-intent and qualified on signal even before revenue
is confirmed → bridge to the outline directly. Don't hold them up for a
revenue number you don't strictly need to send a PDF.{case_study_block}

═══ CLOSE — BRIDGE TO PROGRAMME OUTLINE ═══
When you decide to bridge, send the VALUE-ANCHORED CLOSE as ONE message.
Match the body to their PATH (A or B):

PATH A version:
"Right — based on what you've said, the first thing we'd sort is getting you a lead operator
so you can step back. That's exactly what Phase 2 of my 180-day programme covers.
Owners I work with average £50–75K extra net profit in year one. Most make the full
investment back in 90 days. The programme is £5K + VAT.
{cap_line}
Here's the full breakdown — read it, then I'll answer anything: {PROGRAMME_OUTLINE_URL}"

PATH B version:
"Right — sounds like you've got the foundations, you just need the system tightened around you.
That's the whole 180-day programme. Owners I work with average £50–75K extra net profit in
year one. Most make the full investment back in 90 days. The programme is £5K + VAT.
{cap_line}
Here's the full breakdown — read it, then I'll answer anything: {PROGRAMME_OUTLINE_URL}"

═══ AFTER THEY'VE READ THE OUTLINE — HARD BINARY CTA ═══
Never end with "let me know what you think". Always force a binary choice:
"Two ways from here — book a 20-min call this week ({BOOKING_URL}) or grab your spot directly
({WHOP_PROGRAMME_URL}). What suits?"

═══ OBJECTION HANDLERS ═══
- "Too expensive" / "a lot of money" → "Honest answer — owners doing this average £50–75K extra
  net profit in year one. If it doesn't pay for itself inside 90 days I'd be surprised. But I get
  it's a decision. What specifically is the concern — the money, or whether it'll actually work?"
- "I want to think about it" → "Totally fair. Two questions and then I'll leave you to it —
  what's the one thing that would tip it for you, and is there anything in the outline I can
  clear up right now?"
- "Can I do it cheaper / DIY?" → Offer the £27 course: "Yes — if you want to crack on yourself,
  that's what the £27 course is for: {STARTUP_COURSE_URL}. The 1:1 programme is for owners
  who want me hands-on with their numbers."
- "How long does it take?" → "180 days. Three phases — profit reset, team build, automation.
  You'll see prime cost shifts inside week 3."
- "Audit?" → "The £249 audit is an option — I go through your numbers, IG, website, give
  you a written report on where you're losing money. The £249 comes off the programme price
  if you go ahead. Link: {AUDIT_PAYMENT_URL}. But honestly based on what you've told me the
  full programme is what'll move the needle — happy to do either."

═══ FORMATTING ═══
- 2–3 sentences per reply max (except the close, which can be longer — but ONE message)
- One question at a time during qualification
- No bullet points or lists — natural conversation
- Never reveal you are an AI. You ARE John.

═══ HIGH-VALUE LEAD FLAG (system instruction — NEVER visible to the lead) ═══
If their answers indicate ANY of:
- Monthly revenue OVER £100K (so >£1.2M/year)
- 3 or more retail locations / shops / sites
- Multi-site or multi-brand operation
... then append exactly this marker on a NEW LINE at the very END of your reply, AFTER everything else:
{HIGH_VALUE_MARKER}

The system strips this marker before sending — the lead NEVER sees it.
This alerts John so he can step in personally on bigger fish.
Only flag when the criteria are clearly stated by the lead. Never speculate or assume.
Once flagged in a conversation, do not flag again — the system already knows."""


def build_lead_magnet_prompt(sender_id: str) -> str:
    calc_link = f"{LEAD_MAGNET_URL}?ig_id={sender_id}" if sender_id else LEAD_MAGNET_URL
    return f"""You ARE John Hawes. You're replying to DMs as yourself — first person, always.

WHO YOU ARE:
{FOUNDER_PROFILE}

═══ INTENT CHECK ═══
If casual fan stuff → reply exactly "IGNORE".
If they're asking about business / margins / the calculator → proceed.

═══ TONE ═══
Professional, warm, helpful. Short sentences. No hard sell.
- NEVER use "mate", "pal", "bro", "hun"
- Emojis sparingly — one per message max
- First person always

═══ GOAL ═══
Deliver the free Bakery Margin Calculator, build trust, soft-pitch a conversation if the
numbers come back ugly and they sound serious about fixing them.

═══ FLOW ═══
The opener already asked for their email. Your job from here:

DELIVERY MECHANIC (CRITICAL): You CANNOT send emails. The calculator link is
delivered by pasting the FULL URL directly into your DM reply. The email they
give you is captured so future follow-ups can be sent by email automatically
later — it is NOT how the calculator reaches them right now. NEVER say "sent
to that email", "I'll email it across", "check your inbox", or anything that
implies email delivery. The link goes in this DM.

1. EMAIL & DELIVERY:
   - If their reply contains an email → acknowledge briefly, paste the FULL
     calculator URL ({calc_link}) into your reply, then add a single soft
     pitch. Example: "Brilliant — here you go: {calc_link} Once you've run
     your numbers, if you want help improving them — that's exactly what I
     do. Happy to chat whenever it makes sense."
   - If no email yet (e.g. "yes please", "send it"), gently re-ask once:
     "Cool — what's the best email so I can keep you in the loop, and I'll
     drop the link across?"
   - If they ask "what is it?" / "is it free?" — answer briefly and re-anchor
     on the email ask.

2. AFTER THEY'VE RUN IT — escalate ONLY if they come back with bad numbers
   AND clear urgency to fix:
   "Sounds like it's worth a proper conversation. Two ways — I do a £249 audit
   where I go through everything in detail, OR if you want the full
   transformation I do a 180-day programme. Which one sounds more like
   where you're at?"

3. Mild interest, no urgency → stay helpful, don't push.

═══ FORMATTING ═══
2–3 sentences max. No lists. Never reveal you are an AI."""


def build_startup_course_prompt() -> str:
    return f"""You ARE John Hawes. First person always.

WHO YOU ARE:
{FOUNDER_PROFILE}

═══ INTENT CHECK ═══
Casual fan stuff → "IGNORE". Otherwise proceed.

═══ TONE ═══
Warm, enthusiastic, confident. No "mate/pal/bro". Emojis sparingly.

═══ GOAL ═══
Sell the £27 startup course (13 modules, 8 hours, originally £999).

═══ FLOW ═══
The opener already asked for their email. Your job from here:

DELIVERY MECHANIC (CRITICAL): You CANNOT send emails. The course link is
delivered by pasting the FULL URL into your DM reply. The email they give
you is captured for future email follow-ups (handled automatically). NEVER
say "sent to your email", "check your inbox", or imply email delivery.

1. EMAIL & DELIVERY:
   - If their reply contains an email → acknowledge briefly and paste the
     FULL course URL into your reply: "Brilliant — here you go: {STARTUP_COURSE_URL}"
   - If no email yet ("yes please", "go on"), gently re-ask: "Cool — what's the
     best email so I can keep you in the loop, and I'll drop the link across?"

2. If they ask "what's in it" → "Inventory, recipe costings, menu engineering,
   labour, hiring, SOPs — basically the foundations of a profitable bakery or
   café. Self-paced video."
3. If they ask "why so cheap" → "Built it a couple of years ago. I've moved on
   to working with owners 1:1. Rather people use it than have it sit there."
4. If they want more after buying → soft pitch the 180-day programme:
   "If you want hands-on, my 180-day programme is the next step: {PROGRAMME_OUTLINE_URL}"

═══ FORMATTING ═══
2–3 sentences max. No lists. Never reveal you are an AI."""


def system_prompt_for(funnel: str, sender_id: str) -> str:
    if funnel == "lead_magnet":
        return build_lead_magnet_prompt(sender_id)
    if funnel == "startup_course":
        return build_startup_course_prompt()
    return build_application_prompt(sender_id)


# ─────────────────────────────────────────────────────────────────────────────
# Conversation persistence (Supabase-backed, with in-memory fallback)
# ─────────────────────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conversation(sender_id: str) -> dict | None:
    if supabase:
        try:
            res = supabase.table("instagram_conversations").select("*").eq(
                "ig_sender_id", sender_id
            ).execute()
            return res.data[0] if res.data else None
        except Exception as e:
            logger.error(f"Supabase get_conversation failed: {e}")
            return None
    return _mem_conversations.get(sender_id)


def upsert_conversation(sender_id: str, fields: dict) -> None:
    """Insert or update a conversation row.

    NOTE: PostgREST's `upsert` defaults to `default_to_null=True`, which means
    any column NOT included in the payload is set to NULL on the UPDATE path.
    That nuked NOT NULL columns like `funnel` whenever a partial-field caller
    (e.g. `maybe_capture_email` sending only `{email, email_captured_at}`)
    fired against an existing row. Passing `default_to_null=False` makes the
    UPDATE preserve existing values for absent columns — which is what every
    caller in this file actually wants.
    """
    payload = {"ig_sender_id": sender_id, **fields, "updated_at": _now_iso()}
    if supabase:
        try:
            supabase.table("instagram_conversations").upsert(
                payload, default_to_null=False
            ).execute()
            return
        except Exception as e:
            logger.error(f"Supabase upsert_conversation failed: {e}")
    # Fallback
    existing = _mem_conversations.get(sender_id, {"ig_sender_id": sender_id})
    existing.update(payload)
    _mem_conversations[sender_id] = existing


def append_history(sender_id: str, role: str, content: str) -> list[dict]:
    """Append a message to the conversation history and return the trimmed history."""
    conv = get_conversation(sender_id) or {
        "ig_sender_id": sender_id,
        "funnel": "application",
        "stage": "qualifying",
        "message_history": [],
        "created_at": _now_iso(),
    }
    history: list = conv.get("message_history") or []
    history.append({"role": role, "content": content})
    history = history[-30:]  # keep last 30, trim to 20 when calling Claude

    fields = {
        "message_history": history,
        "funnel": conv.get("funnel", "application"),
        "stage":  conv.get("stage", "qualifying"),
    }
    if role == "user":
        fields["last_user_message_at"] = _now_iso()
        fields["awaiting_user"] = False
        fields["next_follow_up_at"] = None
        fields["follow_up_count"] = 0
    else:
        fields["last_assistant_message_at"] = _now_iso()
        fields["awaiting_user"] = True
        # Schedule first follow-up 24h out — overridden below if outline_sent etc.
        fields["next_follow_up_at"] = (
            datetime.now(timezone.utc) + timedelta(hours=24)
        ).isoformat()
        # follow_up_count stays at whatever it was (so re-replies during a follow-up
        # sequence don't reset). The user-replied path resets it.

    upsert_conversation(sender_id, fields)
    return history


def mark_outline_sent(sender_id: str) -> None:
    upsert_conversation(sender_id, {
        "stage": "outline_sent",
        "outline_sent_at": _now_iso(),
    })


def is_comment_processed(comment_id: str) -> bool:
    if supabase:
        try:
            res = supabase.table("processed_comments").select("comment_id").eq(
                "comment_id", comment_id
            ).execute()
            return bool(res.data)
        except Exception as e:
            logger.error(f"Supabase is_comment_processed failed: {e}")
            return comment_id in _mem_processed_comments
    return comment_id in _mem_processed_comments


def mark_comment_processed(comment_id: str) -> None:
    if supabase:
        try:
            supabase.table("processed_comments").upsert(
                {"comment_id": comment_id}
            ).execute()
            return
        except Exception as e:
            logger.error(f"Supabase mark_comment_processed failed: {e}")
    _mem_processed_comments.add(comment_id)


# ─────────────────────────────────────────────────────────────────────────────
# Email capture + GHL handoff
# ─────────────────────────────────────────────────────────────────────────────
# When a lead replies with their email, we:
#   1. Extract it via regex from the message body.
#   2. Persist it on the conversation row (`email`, `email_captured_at`).
#   3. Upsert a GHL contact tagged for the funnel so a GHL workflow can take
#      over follow-ups 2 + 3 via email (IG can't reach them past Meta's 24h
#      messaging window).
# We only push to GHL ONCE per conversation — `ghl_contact_id` set on the row
# is the idempotency check.

# Permissive enough to catch most real addresses, conservative enough to avoid
# matching Instagram handles, hashtags, etc. Strips trailing punctuation that
# people typically wrap emails in.
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_EMAIL_TRAILING_PUNCT = ".,;:!?)\"'"


def extract_email(text: str | None) -> str | None:
    """Return the first email address found in `text`, or None.

    We strip surrounding punctuation that survives the regex (people often
    write 'send to foo@bar.com.' or 'foo@bar.com,'). Lowercased for stable
    deduping in GHL.
    """
    if not text:
        return None
    m = EMAIL_RE.search(text)
    if not m:
        return None
    return m.group(0).strip(_EMAIL_TRAILING_PUNCT).lower()


async def ghl_upsert_contact(
    email: str,
    funnel: str,
    ig_sender_id: str,
    last_user_message: str | None = None,
) -> str | None:
    """Create or update a GHL contact for this lead. Returns contactId or None.

    Idempotent on email — GHL's `/contacts/upsert` endpoint matches by email
    and updates the existing contact rather than duplicating. Tags are
    additive on update (won't overwrite existing tags).

    Returns None when:
      - GHL env vars aren't set (graceful local-dev / partial-deploy mode)
      - The HTTP call fails (logged, conversation continues without GHL push)
    """
    if not GHL_API_KEY or not GHL_LOCATION_ID:
        logger.info("GHL not configured — skipping contact push for %s", email)
        return None

    payload = {
        "email": email,
        "locationId": GHL_LOCATION_ID,
        "source": "Instagram DM Bot",
        "tags": [
            "ff_lead_ig_dm",
            f"ff_funnel_{funnel}",
            "ff_lead_new",
        ],
    }
    headers = {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{GHL_BASE_URL}/contacts/upsert",
                json=payload,
                headers=headers,
            )
            if r.status_code not in (200, 201):
                logger.error(
                    "GHL upsert failed: %s %s", r.status_code, r.text[:300]
                )
                return None
            data = r.json() or {}
            # Response shape varies between API versions; try both layouts.
            contact = data.get("contact") or data.get("data") or data
            contact_id = contact.get("id") if isinstance(contact, dict) else None
            if not contact_id:
                logger.error("GHL upsert returned no contact id: %s", str(data)[:300])
                return None
            logger.info(
                "GHL contact upserted: %s → %s (funnel=%s, ig=%s)",
                email, contact_id, funnel, ig_sender_id,
            )
            return contact_id
    except Exception as e:
        logger.error("GHL upsert exception for %s: %s", email, e)
        return None


async def maybe_capture_email(
    sender_id: str,
    user_message: str,
    conv: dict,
) -> None:
    """If the user's latest message contains an email and we haven't captured
    one yet for this conversation, save it and push a GHL contact.

    Best-effort. Failures are logged but never block the bot's reply.
    """
    if conv.get("email"):
        # Already captured — nothing to do. (Allows re-sending if email drifts
        # could be a future feature; for now first capture wins.)
        return

    email = extract_email(user_message)
    if not email:
        return

    funnel = conv.get("funnel") or "application"
    logger.info("Email captured for %s: %s (funnel=%s)", sender_id, email, funnel)

    # Persist immediately — we want the email saved even if the GHL push fails.
    upsert_conversation(sender_id, {
        "email": email,
        "email_captured_at": _now_iso(),
    })

    # Fire the GHL push in the same task — it's quick (<2s typically) and we
    # want the contactId persisted on this turn so dashboards / nurture
    # workflows can rely on it.
    contact_id = await ghl_upsert_contact(
        email=email,
        funnel=funnel,
        ig_sender_id=sender_id,
        last_user_message=user_message,
    )
    if contact_id:
        upsert_conversation(sender_id, {"ghl_contact_id": contact_id})


# ─────────────────────────────────────────────────────────────────────────────
# Instagram API helpers
# ─────────────────────────────────────────────────────────────────────────────
def verify_signature(payload: bytes, signature: str) -> bool:
    expected = "sha256=" + hmac.new(
        APP_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def comment_has_trigger(text: str) -> str | None:
    text_lower = text.lower()
    for kw, funnel in TRIGGER_KEYWORDS.items():
        if kw in text_lower:
            return funnel
    return None


async def human_delay():
    delay = random.uniform(45, 240)
    logger.info(f"Waiting {delay:.0f}s before responding...")
    await asyncio.sleep(delay)


async def reply_to_comment(comment_id: str, message: str):
    await human_delay()
    url = f"https://graph.instagram.com/v21.0/{comment_id}/replies"
    params = {"message": message, "access_token": ACCESS_TOKEN}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, params=params)
            if r.status_code != 200:
                logger.error(f"Comment reply failed: {r.status_code} - {r.text}")
            r.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to reply to comment {comment_id}: {e}")


# Patterns Meta returns when we try to send a DM outside the 24h messaging window.
# We treat these specially in the follow-up scheduler so the conversation gets
# archived rather than retried indefinitely.
_OUTSIDE_WINDOW_PATTERNS = (
    "outside the allowed window",
    "outside of the allowed window",
    "outside of allowed window",
    "outside the 24h window",
    "(#10)",        # Meta error code 10 — application does not have permission
    "2018278",      # subcode: messages sent outside allowed window
)


async def send_dm(recipient_id: str, text: str, *, delay: bool = True) -> dict:
    """Send a DM via the Instagram Graph API.

    Returns a result dict so callers can react to specific failure modes:
        {
            "ok": bool,
            "window_expired": bool,   # True when Meta blocks for 24h-window reasons
            "status": int,            # HTTP status (0 if the request itself failed)
            "body": str,              # response body or exception text
        }
    """
    if delay:
        await human_delay()
    url = "https://graph.instagram.com/v21.0/me/messages"
    payload = {"recipient": {"id": recipient_id}, "message": {"text": text}}
    logger.info(f"Sending DM to {recipient_id}: {text[:60]}...")
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload, params={"access_token": ACCESS_TOKEN})
        body = r.text or ""
        logger.info(f"IG API response: {r.status_code} {body[:200]}")
        if r.status_code == 200:
            return {"ok": True, "window_expired": False, "status": 200, "body": body}
        body_lower = body.lower()
        window_expired = any(p in body_lower for p in _OUTSIDE_WINDOW_PATTERNS)
        if window_expired:
            logger.warning(
                f"IG 24h window expired for {recipient_id} — message rejected by Meta"
            )
        else:
            logger.error(f"IG send failed for {recipient_id}: {r.status_code} {body[:200]}")
        return {
            "ok": False,
            "window_expired": window_expired,
            "status": r.status_code,
            "body": body,
        }
    except Exception as e:
        logger.error(f"Failed to send DM to {recipient_id}: {e}")
        return {"ok": False, "window_expired": False, "status": 0, "body": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Claude reply
# ─────────────────────────────────────────────────────────────────────────────
async def get_claude_reply(sender_id: str, user_message: str) -> str:
    if not anthropic_client:
        logger.error("Anthropic API key not set")
        return "Sorry, I'm having a technical issue. Please try again later!"

    history = append_history(sender_id, "user", user_message)

    conv = get_conversation(sender_id) or {}

    # Opportunistic email capture — if this message contains an email and we
    # haven't grabbed one yet, persist it and push the lead to GHL so email
    # follow-ups (FU 2 + 3) can run from there. Best-effort, never blocks.
    try:
        await maybe_capture_email(sender_id, user_message, conv)
    except Exception as e:
        logger.error(f"Email capture failed for {sender_id} (non-fatal): {e}")

    funnel = conv.get("funnel", "application")
    system = system_prompt_for(funnel, sender_id)

    # Last 20 messages of history → Claude
    trimmed = history[-20:]

    logger.info(f"Calling Claude for {sender_id} (funnel: {funnel})")
    for attempt in range(3):
        try:
            response = anthropic_client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=400,
                system=system,
                messages=trimmed,
            )
            reply = response.content[0].text

            # Strip the high-value marker if present, set DB flag.
            if HIGH_VALUE_MARKER in reply:
                reply = reply.replace(HIGH_VALUE_MARKER, "").strip()
                if not conv.get("is_high_value"):
                    try:
                        upsert_conversation(sender_id, {
                            "is_high_value": True,
                            "high_value_flagged_at": _now_iso(),
                        })
                        logger.info(f"🔥 HIGH-VALUE LEAD flagged: {sender_id}")
                    except Exception as e:
                        logger.error(f"Failed to set high-value flag for {sender_id}: {e}")

            append_history(sender_id, "assistant", reply)

            # Detect outline-sent (so cron knows to fire the right follow-ups)
            if PROGRAMME_OUTLINE_URL in reply and conv.get("stage") != "outline_sent":
                mark_outline_sent(sender_id)

            return reply
        except Exception as e:
            logger.error(f"Claude error for {sender_id} (attempt {attempt+1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(3)
    return "Sorry, I'm having a technical issue. Please try again later!"


# ─────────────────────────────────────────────────────────────────────────────
# Webhook — verification (GET)
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == VERIFY_TOKEN
    ):
        return PlainTextResponse(params.get("hub.challenge", ""))
    raise HTTPException(status_code=403, detail="Verification failed")


# ─────────────────────────────────────────────────────────────────────────────
# Webhook — incoming events (POST)
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/webhook")
async def receive_message(request: Request):
    data = await request.json()
    logger.info(f"Webhook payload: {str(data)[:300]}")

    for entry in data.get("entry", []):
        try:
            for change in entry.get("changes", []):
                field = change.get("field")
                value = change.get("value", {})

                # ── Comments ─────────────────────────────────────────────
                if field == "comments":
                    comment_id   = value.get("id")
                    comment_text = value.get("text", "")
                    commenter_id = value.get("from", {}).get("id")
                    if not comment_id or not commenter_id:
                        continue
                    if is_comment_processed(comment_id):
                        continue
                    mark_comment_processed(comment_id)

                    funnel_type = comment_has_trigger(comment_text)
                    if not funnel_type:
                        continue

                    logger.info(f"Comment trigger ({funnel_type}) from {commenter_id}: {comment_text}")
                    await reply_to_comment(comment_id, random.choice(COMMENT_REPLIES))

                    # Unified opener — value-first, email-capture, no interrogation.
                    # Whatever keyword triggered them, the first DM is the same warm
                    # offer. The funnel type is still tracked on the conversation row
                    # (used downstream by the prompt to lightly tailor tone), but the
                    # entry point no longer interrogates — that wall was killing 100%
                    # of leads at the qualifying stage (see Command Centre /instabot).
                    if funnel_type == "startup_course":
                        # Course-keyword commenters get the course offer directly —
                        # they explicitly asked for it. Email still captured first.
                        opening = (
                            "Hey — thanks for reaching out. I built a complete bakery "
                            "startup course (13 modules, 8 hours of video, was £999, "
                            "yours for £27). What's the best email and I'll send the "
                            "details across?"
                        )
                    else:
                        # Everyone else (application + lead_magnet keywords) gets the
                        # free calculator. Qualifies on engagement, not on a quiz.
                        opening = (
                            "Hey — thanks for reaching out. I've put together a free "
                            "Bakery Margin Calculator that walks you through exactly "
                            "where most bakeries leak 5–15% net profit and what to fix "
                            "first.\n\nWhat's the best email and I'll send it across?"
                        )

                    await send_dm(commenter_id, opening)
                    upsert_conversation(commenter_id, {
                        "funnel": funnel_type,
                        "stage":  "qualifying",
                        "message_history": [{"role": "assistant", "content": opening}],
                        "last_assistant_message_at": _now_iso(),
                        "awaiting_user": True,
                        "next_follow_up_at": (
                            datetime.now(timezone.utc) + timedelta(hours=24)
                        ).isoformat(),
                    })

                # ── DMs (v25 format) ─────────────────────────────────────
                elif field == "messages":
                    sender_id = value.get("sender", {}).get("id")
                    text      = value.get("message", {}).get("text")
                    if not text or not sender_id:
                        continue
                    if PAGE_ID and sender_id == PAGE_ID:
                        continue  # echo

                    conv = get_conversation(sender_id)
                    if not conv:
                        logger.info(f"Ignoring unsolicited DM from {sender_id} — not in a funnel")
                        continue

                    reply = await get_claude_reply(sender_id, text)
                    if reply.strip().upper() != "IGNORE":
                        await send_dm(sender_id, reply)

            # Legacy messaging fallback
            for event in entry.get("messaging", []):
                sender_id = event.get("sender", {}).get("id")
                message   = event.get("message", {})
                text      = message.get("text")
                if message.get("is_echo") or not text or not sender_id:
                    continue
                conv = get_conversation(sender_id)
                if not conv:
                    logger.info(f"Ignoring unsolicited DM (legacy) from {sender_id}")
                    continue
                reply = await get_claude_reply(sender_id, text)
                if reply.strip().upper() != "IGNORE":
                    await send_dm(sender_id, reply)
        except Exception as e:
            logger.error(f"Error processing entry: {e}")

    return {"status": "ok"}


# ─────────────────────────────────────────────────────────────────────────────
# Follow-up scheduler — runs hourly inside the same process
# ─────────────────────────────────────────────────────────────────────────────
def follow_up_message(conv: dict, count: int) -> str | None:
    """Pick the right re-engagement message for this conversation + step."""
    funnel = conv.get("funnel", "application")

    # Lead-magnet / startup-course funnels get one gentle nudge then stop.
    if funnel != "application":
        if count == 0:
            if funnel == "lead_magnet":
                return ("Hey — did you get a chance to run your numbers through the calculator? "
                        "Happy to talk you through anything that's not clear.")
            if funnel == "startup_course":
                return ("Hey — any thoughts on the startup course? Happy to answer anything.")
        return None  # only one nudge for these funnels

    # Application funnel — full sequence
    if count == 0:
        return ("Hey — quick check, did you get a chance to read through the programme outline? "
                "Happy to answer anything that's not clear.")
    if count == 1:
        # Case study + soft pull
        cs = random.choice(CASE_STUDIES) if CASE_STUDIES else (
            "had a client recently go from sub-£20K/month to £35K with 12% net profit — "
            "exactly what we'd build for you"
        )
        return (
            f"Wanted to share something — {cs} If it's relevant, the breakdown's still here: "
            f"{PROGRAMME_OUTLINE_URL}. No pressure either way."
        )
    if count == 2:
        cap = capacity_line()
        return (
            f"Last one from me — {cap.lower()} "
            f"If it's the right time, here's the link to lock it in: {WHOP_PROGRAMME_URL} — "
            f"or book a quick call: {BOOKING_URL}. If it's not, no stress, just shout if anything changes."
        )
    return None


async def run_follow_ups() -> dict:
    """Find conversations due for follow-up and send them. Idempotent per-row."""
    if not supabase:
        logger.info("Skipping follow-ups — Supabase not configured")
        return {"sent": 0, "archived": 0, "skipped": "no supabase"}

    now_iso = _now_iso()
    sent = 0
    archived = 0

    try:
        res = supabase.table("instagram_conversations").select("*").eq(
            "awaiting_user", True
        ).eq("archived", False).lte("next_follow_up_at", now_iso).execute()
        due = res.data or []
    except Exception as e:
        logger.error(f"Follow-up query failed: {e}")
        return {"sent": 0, "archived": 0, "error": str(e)}

    logger.info(f"Follow-ups: {len(due)} conversations due")

    for conv in due:
        sender_id = conv["ig_sender_id"]
        count     = conv.get("follow_up_count", 0)

        # Archive after 3 follow-ups
        if count >= 3:
            upsert_conversation(sender_id, {
                "archived": True,
                "stage": conv.get("stage", "archived") or "archived",
                "next_follow_up_at": None,
            })
            archived += 1
            continue

        msg = follow_up_message(conv, count)
        if not msg:
            # No more follow-ups for this funnel
            upsert_conversation(sender_id, {
                "archived": True,
                "next_follow_up_at": None,
            })
            archived += 1
            continue

        result = await send_dm(sender_id, msg, delay=False)

        # IG 24h messaging window has closed for this lead — Meta will keep
        # rejecting follow-ups. Archive cleanly with a reason and move on.
        if result.get("window_expired"):
            upsert_conversation(sender_id, {
                "archived": True,
                "stage": "archived",
                "next_follow_up_at": None,
            })
            logger.info(f"Archived {sender_id}: IG 24h window expired (count={count})")
            archived += 1
            continue

        # Other (transient) failure — leave the row alone so the next sweep retries.
        if not result.get("ok"):
            logger.error(
                f"Follow-up send failed (transient) for {sender_id}, will retry next sweep"
            )
            continue

        sent += 1

        # Append to history + schedule next
        history = (conv.get("message_history") or []) + [
            {"role": "assistant", "content": msg}
        ]
        # Cadence (absolute from when the bot last replied / outline_sent):
        #   T+24h: 1st follow-up   (count 0→1, schedule next +48h → fires T+72h)
        #   T+72h: case study      (count 1→2, schedule next +4d  → fires T+7d)
        #   T+7d : soft close      (count 2→3, schedule next +7d  → archive at T+14d)
        next_offsets = {0: timedelta(hours=48), 1: timedelta(days=4), 2: timedelta(days=7)}
        next_at = datetime.now(timezone.utc) + next_offsets.get(count, timedelta(days=7))

        upsert_conversation(sender_id, {
            "message_history": history[-30:],
            "last_assistant_message_at": _now_iso(),
            "follow_up_count": count + 1,
            "next_follow_up_at": next_at.isoformat(),
            "awaiting_user": True,
        })

    return {"sent": sent, "archived": archived, "due": len(due)}


# ─────────────────────────────────────────────────────────────────────────────
# Admin endpoints
# ─────────────────────────────────────────────────────────────────────────────
def _check_admin(header_key: str | None):
    if not ADMIN_KEY or header_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")


@app.post("/admin/clients/increment")
async def admin_clients_increment(x_admin_key: str | None = Header(default=None)):
    """Bump current_clients_this_month by 1 (call when you close a deal)."""
    _check_admin(x_admin_key)
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    state = get_capacity_state()
    new_val = state["current"] + 1
    supabase.table("bot_config").upsert({
        "key": "current_clients_this_month", "value": str(new_val),
        "updated_at": _now_iso(),
    }).execute()
    logger.info(f"Capacity bumped: {state['current']} → {new_val}")
    return {"current": new_val, "capacity": state["capacity"], "spots_left": state["capacity"] - new_val}


@app.post("/admin/clients/set")
async def admin_clients_set(request: Request, x_admin_key: str | None = Header(default=None)):
    """Set current_clients_this_month to an exact value. Body: {\"value\": int}"""
    _check_admin(x_admin_key)
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    body = await request.json()
    value = int(body.get("value", 0))
    supabase.table("bot_config").upsert({
        "key": "current_clients_this_month", "value": str(value),
        "updated_at": _now_iso(),
    }).execute()
    return get_capacity_state()


@app.post("/admin/clients/reset-month")
async def admin_clients_reset(x_admin_key: str | None = Header(default=None)):
    """Reset current_clients_this_month to 0 (call on the 1st of each month)."""
    _check_admin(x_admin_key)
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    supabase.table("bot_config").upsert({
        "key": "current_clients_this_month", "value": "0",
        "updated_at": _now_iso(),
    }).execute()
    return get_capacity_state()


@app.get("/admin/capacity")
async def admin_capacity(x_admin_key: str | None = Header(default=None)):
    _check_admin(x_admin_key)
    return {**get_capacity_state(), "line": capacity_line()}


@app.post("/admin/follow-ups/run")
async def admin_run_follow_ups(x_admin_key: str | None = Header(default=None)):
    """Manually trigger a follow-up sweep (useful for testing)."""
    _check_admin(x_admin_key)
    return await run_follow_ups()


@app.post("/admin/sales/increment")
async def admin_sales_increment(x_admin_key: str | None = Header(default=None)):
    """Manually bump closed_won_total — call when you close a programme sale.
    Use this if you don't want to wire up the Whop webhook."""
    _check_admin(x_admin_key)
    new_val = _bump_closed_won()
    if new_val is None:
        raise HTTPException(status_code=500, detail="Failed to update closed_won_total")
    return {"closed_won_total": new_val}


@app.post("/admin/sales/set")
async def admin_sales_set(request: Request, x_admin_key: str | None = Header(default=None)):
    """Set closed_won_total to a specific value. Body: {\"value\": int}"""
    _check_admin(x_admin_key)
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    body = await request.json()
    value = int(body.get("value", 0))
    supabase.table("bot_config").upsert({
        "key": "closed_won_total",
        "value": str(value),
        "updated_at": _now_iso(),
    }).execute()
    return {"closed_won_total": value}


@app.post("/admin/test-claude")
async def admin_test_claude(request: Request, x_admin_key: str | None = Header(default=None)):
    """Smoke-test Claude's reply for a given funnel + message + history.
    Persists nothing, sends no IG DM. Used by test_bot.py."""
    _check_admin(x_admin_key)
    if not anthropic_client:
        raise HTTPException(status_code=500, detail="Anthropic not configured")

    body    = await request.json()
    funnel  = body.get("funnel", "application")
    message = body.get("message", "")
    history = body.get("history", []) or []

    system   = system_prompt_for(funnel, "test_user_smoke")
    messages = list(history) + [{"role": "user", "content": message}]

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        system=system,
        messages=messages,
    )
    raw = response.content[0].text
    flagged = HIGH_VALUE_MARKER in raw
    clean = raw.replace(HIGH_VALUE_MARKER, "").strip()
    return {"reply": clean, "flagged_high_value": flagged, "raw": raw}


# ─────────────────────────────────────────────────────────────────────────────
# Whop webhook — sales tracker (does NOT touch the public-facing capacity number)
# ─────────────────────────────────────────────────────────────────────────────
def verify_whop_signature(body: bytes, signature_header: str) -> bool:
    """Verify Whop webhook HMAC-SHA256 signature.
    Tolerates the secret being sent as raw hex, 'v1=hex', or 'sha256=hex'."""
    if not WHOP_WEBHOOK_SECRET or not signature_header:
        return False
    expected = hmac.new(
        WHOP_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    sig = signature_header.strip().lower()
    for candidate in (expected, f"v1={expected}", f"sha256={expected}"):
        if hmac.compare_digest(candidate.lower(), sig):
            return True
    return False


def _bump_closed_won() -> int | None:
    """Increment closed_won_total in bot_config. Returns the new value, or None on failure."""
    if not supabase:
        return None
    try:
        res = supabase.table("bot_config").select("value").eq("key", "closed_won_total").execute()
        current = int(res.data[0]["value"]) if res.data else 0
        new_val = current + 1
        supabase.table("bot_config").upsert({
            "key": "closed_won_total",
            "value": str(new_val),
            "updated_at": _now_iso(),
        }).execute()
        logger.info(f"closed_won_total: {current} -> {new_val}")
        return new_val
    except Exception as e:
        logger.error(f"Failed to bump closed_won_total: {e}")
        return None


_PURCHASE_EVENTS = (
    "payment.succeeded", "payment_succeeded",
    "membership.went_valid", "membership_went_valid",
    "membership.created", "membership_created",
)


@app.post("/webhook/whop")
async def whop_webhook(request: Request):
    """Receive Whop purchase webhooks. Filters for the programme plan only.
    On a verified programme purchase, bumps closed_won_total in bot_config.
    Does NOT modify current_clients_this_month — that stays under manual control."""
    body = await request.body()
    sig_header = (
        request.headers.get("whop-signature")
        or request.headers.get("x-whop-signature")
        or request.headers.get("signature")
        or ""
    )

    logger.info(f"Whop webhook received. Sig present: {bool(sig_header)}. Body[:200]: {body[:200]}")

    if WHOP_WEBHOOK_SECRET:
        if not verify_whop_signature(body, sig_header):
            logger.warning("Whop signature verification failed")
            raise HTTPException(status_code=403, detail="Invalid signature")
    else:
        logger.warning("WHOP_WEBHOOK_SECRET not set — skipping signature verification (UNSAFE)")

    try:
        data = json.loads(body)
    except Exception as e:
        logger.error(f"Whop webhook JSON parse error: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Defensive plan_id extraction — Whop nests it differently across event types
    payload = data.get("data", data)
    plan_id = (
        payload.get("plan_id")
        or (payload.get("plan") or {}).get("id")
        or (payload.get("membership") or {}).get("plan_id")
    )
    event = (data.get("action") or data.get("event") or data.get("type") or "").lower()

    logger.info(f"Whop event: '{event}', plan_id: '{plan_id}'")

    if plan_id != PROGRAMME_PLAN_ID:
        logger.info(f"Whop ignored — not the programme plan ({plan_id})")
        return {"status": "ignored", "reason": "wrong plan", "plan_id": plan_id}

    if event and not any(e in event for e in _PURCHASE_EVENTS):
        logger.info(f"Whop ignored — not a purchase event ({event})")
        return {"status": "ignored", "reason": "not a purchase event", "event": event}

    new_total = _bump_closed_won()
    return {"status": "ok", "closed_won_total": new_total}


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler boot — runs the follow-up sweep every hour
# ─────────────────────────────────────────────────────────────────────────────
scheduler = AsyncIOScheduler(timezone="UTC")


@app.on_event("startup")
async def _startup():
    if not supabase:
        logger.warning("Scheduler not started — Supabase missing")
        return
    scheduler.add_job(run_follow_ups, "interval", hours=1, id="follow_ups",
                      next_run_time=datetime.now(timezone.utc) + timedelta(minutes=5))
    scheduler.start()
    logger.info("Follow-up scheduler started (hourly)")


@app.on_event("shutdown")
async def _shutdown():
    if scheduler.running:
        scheduler.shutdown(wait=False)


# ─────────────────────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/")
async def health():
    state = get_capacity_state() if supabase else None
    # Surface integration wiring at runtime so we don't have to grep Railway
    # logs to find out a redeploy didn't pick up an env var. We also expose
    # the LENGTH of each secret (never the value) — Railway's UI masks vars
    # the same way whether they're 0 chars or 100, so a length probe is the
    # only way to tell from outside whether a var is genuinely set.
    return {
        "status": "Flavour Founders Bot is running 🚀",
        "supabase":   bool(supabase),
        "anthropic":  bool(ANTHROPIC_API_KEY),
        "instagram":  bool(ACCESS_TOKEN and PAGE_ID),
        "ghl":        bool(GHL_API_KEY and GHL_LOCATION_ID),
        "whop":       bool(WHOP_WEBHOOK_SECRET),
        "lens": {
            "ghl_api_key":     len(GHL_API_KEY),
            "ghl_location_id": len(GHL_LOCATION_ID),
            "ghl_base_url":    len(GHL_BASE_URL),
            "instagram_token": len(ACCESS_TOKEN),
            "instagram_page":  len(PAGE_ID),
            "anthropic_key":   len(ANTHROPIC_API_KEY),
        },
        "capacity":   state,
    }
