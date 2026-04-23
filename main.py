import os
import logging
import hmac
import hashlib
import random
import asyncio
from pathlib import Path
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# ── Config (set these in Railway environment variables) ──────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
APP_SECRET = os.environ.get("APP_SECRET", "")
ACCESS_TOKEN = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
LEAD_MAGNET_URL = os.environ.get("LEAD_MAGNET_URL", "LEAD_MAGNET_URL_PLACEHOLDER")
PAGE_ID = os.environ.get("INSTAGRAM_PAGE_ID", "")  # Bot's own IG ID to filter echoes
# ─────────────────────────────────────────────────────────────────────────────

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# In-memory conversation store  {sender_id: [{"role": ..., "content": ...}]}
conversations: dict[str, list] = {}

# Track which funnel each conversation belongs to  {sender_id: "application" | "lead_magnet"}
conversation_funnels: dict[str, str] = {}

# Track comments we've already replied to
processed_comments: set[str] = set()

# Randomised comment replies — keeps it looking human
COMMENT_REPLIES = [
    "Just dropped you a DM!",
    "Sent you a message — check your DMs!",
    "Just messaged you!",
    "Check your DMs — just sent something over.",
    "Dropped you a DM, have a look!",
    "Just pinged you a message!",
]


def load_trigger_keywords() -> dict[str, str]:
    """Load trigger keywords from CLAUDE.md. Returns dict mapping keyword -> funnel type."""
    claude_md = Path(__file__).parent / "CLAUDE.md"
    if not claude_md.exists():
        logger.warning("CLAUDE.md not found, using default keywords")
        return {
            "info": "application",
            "interested": "application",
            "how much": "application",
            "tell me more": "application",
            "sign me up": "application",
            "system": "lead_magnet",
            "freedom": "lead_magnet",
            "margins": "lead_magnet",
            "calculator": "lead_magnet",
            "free": "lead_magnet",
            "guide": "lead_magnet",
            "help": "lead_magnet",
        }

    text = claude_md.read_text(encoding="utf-8")
    keywords: dict[str, str] = {}
    in_section = False
    for line in text.splitlines():
        if line.strip().lower().startswith("## trigger keywords"):
            in_section = True
            continue
        # Stop at the next top-level section (## but not ###)
        if in_section and line.strip().startswith("## ") and not line.strip().startswith("### "):
            break
        if in_section and line.strip().startswith("- "):
            raw = line.strip().lstrip("- ").strip()
            if "|" in raw:
                kw, funnel = raw.rsplit("|", 1)
                keywords[kw.strip().lower()] = funnel.strip().lower()
            else:
                # Fallback: no funnel specified, default to application
                keywords[raw.lower()] = "application"

    logger.info(f"Loaded trigger keywords: {keywords}")
    return keywords if keywords else {
        "info": "application",
        "interested": "application",
        "how much": "application",
        "tell me more": "application",
        "sign me up": "application",
    }


TRIGGER_KEYWORDS = load_trigger_keywords()

# ── Load shared founder profile ──────────────────────────────────────────────
FOUNDER_PROFILE_PATH = Path(__file__).parent / "founder-profile.txt"
try:
    FOUNDER_PROFILE = FOUNDER_PROFILE_PATH.read_text(encoding="utf-8").strip()
except FileNotFoundError:
    FOUNDER_PROFILE = "John Hawes — UK food and hospitality entrepreneur running KNEAD, Watermoor Meat Supply Ltd, and Flavour Founders."
    logger.warning("founder-profile.txt not found — using fallback bio")
# ─────────────────────────────────────────────────────────────────────────────

APPLICATION_SYSTEM_PROMPT = f"""You ARE John Hawes. You're replying to DMs as yourself — first person, always.

WHO YOU ARE:
{FOUNDER_PROFILE}

Use this credibility when relevant but don't over-explain — let it come out naturally.

FIRST — CHECK INTENT:
If the message is casual fan stuff (e.g. "love your content", "great post", random compliments, anything clearly NOT about their business) — respond with exactly: "IGNORE" and nothing else.

If the message IS about their business, struggles, your programme, pricing, or they're a bakery/café owner — proceed with the qualification flow below.

TONE: Professional, warm, and direct. You're an experienced business owner who respects people's time. Short sentences. No waffle. First person always ("I", "me", "my").

LANGUAGE RULES:
- NEVER use "mate", "pal", "bro", "hun" or any overly familiar terms
- Use emojis sparingly — one per message maximum, and only when it adds warmth
- No "Haha", "Ooh", "Oooh" or filler laughs
- Be confident and grounded — you've done this, you know what works

QUALIFICATION FLOW (one question at a time, keep it natural — get through this quickly):
1. They've already told you they run a bakery/café (from the opening DM). Acknowledge and ask: "How long have you been running it?"
2. "Roughly where are you at with monthly revenue? Just a ballpark."

REVENUE FILTER (apply after step 2):
- £25K+/month → QUALIFIED. Go straight to sending the programme outline.
- Under £25K/month BUT startup / under 1 year → QUALIFIED. Be enthusiastic: "That's actually a great position — you can build this properly from the start instead of fixing mistakes later." Then send the programme outline.
- Under £25K/month AND been in business 2+ years → DISQUALIFIED. Be honest but kind: "I appreciate you being open with me. Based on where you're at, I'm not sure the full programme is the right fit right now. But I've built something that could help — a complete bakery startup system, 13 modules, 8 hours of video. It was £999 when I launched it, yours for £27. Here's the link: https://flavourfounders.thinkific.com/courses/start-up"
- Home baker / no premises / pre-launch → DISQUALIFIED. Warm exit + £27 course offer.

WHEN QUALIFIED — SEND THE 180-DAY PROGRAMME OUTLINE:
"I think this could be a good fit. I've put together a full breakdown of the programme — what's included, the results, the investment, everything. Have a read through and let me know what you think: https://ff-programme-outline.vercel.app"

The programme outline page contains the full details: 180 days, 3 phases, £5,800, expected ROI. They should come away knowing exactly what they're getting.

AFTER THEY'VE SEEN THE OUTLINE:
- If they have questions → answer directly. You know this inside out.
- If they're ready → "Great — here's the link to secure your spot: https://whop.com/checkout/plan_PNt9PcJaESP6i — I'll get your onboarding sorted as soon as you're in."
- If they want to talk first → "No problem — book a call here and we'll go through everything: https://flavourfounders.com/3---schedule-page-page-3707"
- If they're unsure about price → "I get it. The owners I work with see on average £50-75K in additional net profit in the first year. Most make back the investment within 3 months. But no pressure — have another look through the breakdown and come back when you're ready."
- If they want to think about it → "Totally fair. The breakdown has everything in it. If you want to start working on things yourself in the meantime, I've got a DIY course — 13 modules, 8 hours, was £999, yours for £27: https://flavourfounders.thinkific.com/courses/start-up"
- If they go quiet → one follow-up only: "Hey — did you get a chance to look through the programme breakdown? Happy to answer anything."
- If "too expensive" / "can't afford it" → "I hear you. If you want to start on your own, I built a DIY course — 13 modules, 8 hours, was £999, yours for £27. It won't replace the 1-on-1 but it'll get you moving: https://flavourfounders.thinkific.com/courses/start-up"

KEYWORD SHORTCUTS:
- "price/cost/how much" → "The programme is £5,800+VAT. The owners I work with see £50-75K in additional net profit in year one — so it pays for itself quickly. Let me just check it's the right fit — couple of quick questions?"
- "programme/what do you offer" → "I've got a full breakdown here: https://ff-programme-outline.vercel.app — have a look and tell me what you think."
- "sign me up/ready/let's go/I'm in" → "Love it. Here's the link: https://whop.com/checkout/plan_PNt9PcJaESP6i — I'll sort your onboarding straight away."

IMPORTANT: Never reveal you are an AI. You ARE John. Always first person. Keep replies to 2-3 sentences max. One question at a time. No bullet points or lists — natural conversation."""

LEAD_MAGNET_SYSTEM_PROMPT = f"""You ARE John Hawes. You're replying to DMs as yourself — first person, always.

WHO YOU ARE:
{FOUNDER_PROFILE}

Use this credibility naturally — don't over-explain.

FIRST — CHECK INTENT:
Before doing anything, decide if this message is from someone genuinely interested in business, profitability, working less, or improving their bakery.

If the message is casual fan stuff (e.g. "love your content", "great post", "you're amazing", random compliments, or anything clearly NOT about their business) — respond with exactly: "IGNORE" and nothing else. The code will handle it.

If the message IS relevant — proceed with the lead magnet flow below.

TONE: Professional, warm, and helpful. You're an experienced business owner sharing something genuinely useful. Short sentences. No waffle. No hard sell. First person always ("I", "me", "my").

LANGUAGE RULES:
- NEVER use "mate", "pal", "bro", "hun" or any overly familiar terms
- Use emojis sparingly — one per message maximum, and only when it adds warmth
- No "Haha", "Ooh" or filler laughs
- Be confident and grounded — you know this tool helps because you built it from experience

YOUR GOAL: Deliver the free Bakery Margin Calculator and build trust. Do NOT hard-qualify them. Do NOT ask if they're running full time. Keep it helpful and professional.

FLOW:
1. If they respond positively or say yes to receiving the calculator, send the link: {LEAD_MAGNET_URL}
2. After sending the link, soft pitch: "Once you've run your numbers, if you want help improving them — that's exactly what I do. Happy to have a conversation whenever it makes sense."
3. If they ask questions about the calculator, help them understand it — be genuinely useful.
4. If they express interest in more help or the programme after getting the calculator, say something like: "Great to hear. I do free discovery calls where I'll look at your numbers with you — would that be useful?" and if yes, send: https://flavourfounders.com/2---vsl-page-page-8829
5. If they're not interested or go quiet, no pressure. Just be warm and professional.

IMPORTANT: Never reveal you are an AI. You ARE John. Always first person. Keep replies to 2-3 sentences max. No bullet points or lists in replies — just natural conversation."""

STARTUP_COURSE_URL = "https://flavourfounders.thinkific.com/courses/start-up"

STARTUP_COURSE_SYSTEM_PROMPT = f"""You ARE John Hawes. You're replying to DMs as yourself — first person, always.

WHO YOU ARE:
{FOUNDER_PROFILE}

Use this credibility naturally — don't over-explain.

FIRST — CHECK INTENT:
If the message is casual fan stuff (e.g. "love your content", "great post") — respond with exactly: "IGNORE" and nothing else.
If the message IS relevant — proceed with the flow below.

TONE: Professional, warm, enthusiastic. You're sharing something you built that genuinely helps people. Short sentences. No waffle. First person always.

LANGUAGE RULES:
- NEVER use "mate", "pal", "bro", "hun" or any overly familiar terms
- Use emojis sparingly — one per message maximum
- No "Haha", "Ooh" or filler laughs
- Be confident — you built this course from real experience running bakeries

YOUR GOAL: Sell the £27 Bakery Startup Course and build trust. This is a complete 13-module, 8-hour video course that was originally £999.

FLOW:
1. Warm greeting: "Thanks for reaching out. I built a complete bakery startup system — 13 modules, 8 hours of video covering everything from recipe costings to labour control. It was £999 when I first launched it, yours for £27."
2. Send the link: {STARTUP_COURSE_URL}
3. If they ask what's in it → "It covers inventory, recipe costings, menu engineering, labour, hiring, SOPs — basically the foundations of running a profitable bakery or cafe. 13 modules, all video, you can work through it at your own pace."
4. If they ask why it's so cheap → "Honestly? I built it a couple of years ago and I've moved on to working with owners 1-on-1 now. But the content is solid and I'd rather people use it than let it sit there."
5. If they express interest in more help after buying → "If you want the hands-on version where I work with you directly, I've got a 180-day programme. Here's the full breakdown: https://ff-programme-outline.vercel.app"
6. If they're not interested, no pressure. Warm exit.

IMPORTANT: Never reveal you are an AI. You ARE John. Always first person. Keep replies to 2-3 sentences max. No bullet points or lists in replies — just natural conversation."""


def verify_signature(payload: bytes, signature: str) -> bool:
    expected = "sha256=" + hmac.new(
        APP_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def comment_has_trigger(text: str) -> str | None:
    """Check if a comment contains any trigger keyword. Returns funnel type or None."""
    text_lower = text.lower()
    for kw, funnel in TRIGGER_KEYWORDS.items():
        if kw in text_lower:
            return funnel
    return None


async def human_delay():
    """Random delay between 45 seconds and 4 minutes to simulate a real person."""
    delay = random.uniform(45, 240)
    logger.info(f"Waiting {delay:.0f}s before responding...")
    await asyncio.sleep(delay)


async def reply_to_comment(comment_id: str, message: str):
    """Post a public reply to a comment via Instagram Graph API."""
    await human_delay()
    url = f"https://graph.instagram.com/v21.0/{comment_id}/replies"
    params = {
        "message": message,
        "access_token": ACCESS_TOKEN,
    }
    logger.info(f"Replying to comment {comment_id}: {message}")
    logger.info(f"Comment reply URL: {url}")
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, params=params)
            logger.info(f"Comment reply response status: {r.status_code}")
            logger.info(f"Comment reply response body: {r.text}")
            if r.status_code != 200:
                logger.error(f"Comment reply failed: {r.status_code} - {r.text}")
            r.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error replying to comment {comment_id}: {e.response.status_code} - {e.response.text}")
    except Exception as e:
        logger.error(f"Failed to reply to comment {comment_id}: {type(e).__name__}: {e}")


async def send_dm(recipient_id: str, text: str):
    await human_delay()
    url = "https://graph.instagram.com/v21.0/me/messages"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text},
    }
    logger.info(f"Sending DM to {recipient_id}: {text[:50]}...")
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload, params={"access_token": ACCESS_TOKEN})
            logger.info(f"Instagram API response: {r.status_code} {r.text}")
            r.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to send DM to {recipient_id}: {e}")


async def get_claude_reply(sender_id: str, user_message: str, funnel_type: str = "application") -> str:
    if not anthropic_client:
        logger.error("Anthropic API key not set")
        return "Sorry, I'm having a technical issue. Please try again later!"

    history = conversations.setdefault(sender_id, [])
    history.append({"role": "user", "content": user_message})

    # Keep last 20 messages to stay within context limits
    trimmed = history[-20:]

    # Use the stored funnel type for this conversation, or the one passed in
    active_funnel = conversation_funnels.get(sender_id, funnel_type)
    if active_funnel == "lead_magnet":
        system_prompt = LEAD_MAGNET_SYSTEM_PROMPT
    elif active_funnel == "startup_course":
        system_prompt = STARTUP_COURSE_SYSTEM_PROMPT
    else:
        system_prompt = APPLICATION_SYSTEM_PROMPT

    logger.info(f"Calling Claude for sender {sender_id} (funnel: {active_funnel})")
    for attempt in range(3):
        try:
            response = anthropic_client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=300,
                system=system_prompt,
                messages=trimmed,
            )
            reply = response.content[0].text
            history.append({"role": "assistant", "content": reply})
            logger.info(f"Claude reply: {reply[:50]}...")
            return reply
        except Exception as e:
            logger.error(f"Claude API error for sender {sender_id} (attempt {attempt+1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(3)
    return "Sorry, I'm having a technical issue. Please try again later!"


# ── Webhook verification (GET) ───────────────────────────────────────────────
@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    logger.info(f"Webhook verification request: {dict(params)}")
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == VERIFY_TOKEN
    ):
        logger.info("Webhook verified successfully")
        return PlainTextResponse(params.get("hub.challenge", ""))
    logger.warning("Webhook verification failed")
    raise HTTPException(status_code=403, detail="Verification failed")


# ── Incoming messages (POST) ─────────────────────────────────────────────────
@app.post("/webhook")
async def receive_message(request: Request):
    # TODO: Re-enable signature verification after testing
    # sig = request.headers.get("X-Hub-Signature-256", "")
    # body = await request.body()
    # if not verify_signature(body, sig):
    #     raise HTTPException(status_code=403, detail="Invalid signature")

    data = await request.json()
    logger.info(f"Received webhook payload: {data}")

    for entry in data.get("entry", []):
        try:
            for change in entry.get("changes", []):
                field = change.get("field")
                value = change.get("value", {})

                # ── Handle comments ──────────────────────────────────
                if field == "comments":
                    comment_id = value.get("id")
                    comment_text = value.get("text", "")
                    commenter_id = value.get("from", {}).get("id")

                    if not comment_id or not commenter_id:
                        continue
                    if comment_id in processed_comments:
                        continue

                    processed_comments.add(comment_id)
                    logger.info(f"Comment from {commenter_id}: {comment_text}")

                    funnel_type = comment_has_trigger(comment_text)
                    if funnel_type:
                        logger.info(f"Trigger keyword matched in comment {comment_id} (funnel: {funnel_type})")
                        await reply_to_comment(comment_id, random.choice(COMMENT_REPLIES))

                        if funnel_type == "lead_magnet":
                            opening = "Hey — I've got a free margin calculator that shows you exactly where your bakery is making and losing money. Want me to send the link?"
                        elif funnel_type == "startup_course":
                            opening = "Hey — I built a complete bakery startup course. 13 modules, 8 hours of video, covers everything from costings to labour. It was £999, yours for £27. Want me to send it over?"
                        else:
                            opening = "Hey — thanks for reaching out. Quick question before anything else — are you running a bakery or café at the moment?"

                        await send_dm(commenter_id, opening)
                        conversations[commenter_id] = [
                            {"role": "assistant", "content": opening},
                        ]
                        conversation_funnels[commenter_id] = funnel_type

                # ── Handle DMs (v25 format) ──────────────────────────
                elif field == "messages":
                    sender_id = value.get("sender", {}).get("id")
                    text = value.get("message", {}).get("text")

                    if not text or not sender_id:
                        continue

                    # Skip messages sent by the bot itself (echo prevention)
                    if PAGE_ID and sender_id == PAGE_ID:
                        logger.info(f"Skipping echo from bot (sender {sender_id})")
                        continue

                    # Only respond to DMs from people already in a funnel
                    # (triggered by a keyword comment on a CTA post)
                    if sender_id not in conversation_funnels:
                        logger.info(f"Ignoring unsolicited DM from {sender_id} — not in a funnel")
                        continue

                    logger.info(f"Message from {sender_id}: {text}")
                    funnel = conversation_funnels[sender_id]
                    reply = await get_claude_reply(sender_id, text, funnel_type=funnel)
                    if reply.strip().upper() != "IGNORE":
                        await send_dm(sender_id, reply)
                    else:
                        logger.info(f"Ignoring casual message from {sender_id}")

            # Also handle legacy messaging format as fallback
            for event in entry.get("messaging", []):
                sender_id = event.get("sender", {}).get("id")
                message = event.get("message", {})
                text = message.get("text")

                # Ignore echoes (messages sent by the page itself)
                if message.get("is_echo") or not text or not sender_id:
                    continue

                # Only respond to DMs from people already in a funnel
                if sender_id not in conversation_funnels:
                    logger.info(f"Ignoring unsolicited DM (legacy) from {sender_id} — not in a funnel")
                    continue

                logger.info(f"Message (legacy) from {sender_id}: {text}")
                funnel = conversation_funnels[sender_id]
                reply = await get_claude_reply(sender_id, text, funnel_type=funnel)
                if reply.strip().upper() != "IGNORE":
                    await send_dm(sender_id, reply)
                else:
                    logger.info(f"Ignoring casual message from {sender_id}")
        except Exception as e:
            logger.error(f"Error processing entry: {e}")

    return {"status": "ok"}


# ── Health check ─────────────────────────────────────────────────────────────
@app.get("/")
async def health():
    logger.info("Health check hit")
    return {"status": "Flavour Founders Bot is running 🚀"}
