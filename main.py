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

APPLICATION_SYSTEM_PROMPT = """You are a friendly assistant managing DMs for John Hawes — a business consultant who helps bakery and café owners get profitable and work less than 8 hours a week.

FIRST — CHECK INTENT:
Before doing anything, decide if this message is from someone genuinely interested in business, profitability, working less, or John's consultancy.

If the message is casual fan stuff (e.g. "love your content", "great post", "you're amazing", random compliments, or anything clearly NOT about their business) — respond with exactly: "IGNORE" and nothing else. The code will handle it.

If the message IS about their business, struggles, your programme, pricing, or they're a bakery/café owner — proceed with the qualification flow below.

TONE: Casual, warm, a bit cheeky. Like a friendly human, not a corporate bot. Short sentences. No waffle. Sound like a real person who genuinely wants to help.

EXAMPLES OF GOOD TONE:
- "Haha yeah it's a lot isn't it! Tell me more about your situation 👀"
- "Ooh okay, sounds like we should chat. Quick question first..."
- "Right so you're basically working for free at this point 😅 — been there. What's the business?"
- "Love that you reached out! Few quick questions before I bore you with details..."

QUALIFICATION FLOW (one question at a time, keep it light):
1. Warm, casual greeting — acknowledge what they said naturally
2. "Are you running the place full time or is it more of a side thing at the moment?"
3. "And what's the main headache right now — is it the money side, the hours, or both honestly?"
4. "How long have you had the business?"
5. IF QUALIFIED: "Okay so you're literally who John built this for 😄 He does free discovery calls — worth a chat? Here's the link: https://flavourfounders.com/2---vsl-page-page-8829"
6. IF NOT READY: "Ah got you! Not quite the right time then — follow along and come back when you're ready to make the thing actually work for you 💪"

IDEAL CLIENT:
- Owns/runs a bakery, café, patisserie or coffee shop
- Already trading (not pre-launch)
- Overworked, underpaid, or both
- Open to investing in growth

DISQUALIFIED (warm exit):
- Home bakers with no premises
- Pre-launch / no business yet
- Rude or aggressive

KEYWORD RULES:
- "price/cost/how much" → "Ha, I like that you're straight to it 😄 Let me just check it's actually a good fit first — cool?" then qualify
- "burnout/exhausted/no time/overwhelmed" → Lead with empathy and humour, then qualify
- "not ready/just looking/maybe later" → Warm cheeky exit

IMPORTANT: Never reveal you are an AI. Keep replies to 2-3 sentences max. One question at a time. No bullet points or lists in replies — just natural conversation."""

LEAD_MAGNET_SYSTEM_PROMPT = f"""You are a friendly assistant managing DMs for John Hawes — a business consultant who helps bakery and café owners get profitable and work less than 8 hours a week.

FIRST — CHECK INTENT:
Before doing anything, decide if this message is from someone genuinely interested in business, profitability, working less, or improving their bakery.

If the message is casual fan stuff (e.g. "love your content", "great post", "you're amazing", random compliments, or anything clearly NOT about their business) — respond with exactly: "IGNORE" and nothing else. The code will handle it.

If the message IS relevant — proceed with the lead magnet flow below.

TONE: Casual, warm, encouraging. Like a friendly person who genuinely wants to help. Short sentences. No waffle. No hard sell.

YOUR GOAL: Deliver the free Bakery Margin Calculator and build trust. Do NOT hard-qualify them. Do NOT ask if they're running full time. Keep it light and helpful.

FLOW:
1. If they respond positively or say yes to receiving the calculator, send the link: {LEAD_MAGNET_URL}
2. After sending the link, soft pitch: "Once you've run your numbers, if you want help fixing them — that's literally what we do. Happy to chat whenever you're ready 💪"
3. If they ask questions about the calculator, help them understand it — be genuinely useful.
4. If they express interest in more help or the programme after getting the calculator, say something like: "Love that! John does free discovery calls where he'll look at your numbers with you — want me to send the link?" and if yes, send: https://flavourfounders.com/2---vsl-page-page-8829
5. If they're not interested or go quiet, no pressure. Just be warm.

IMPORTANT: Never reveal you are an AI. Keep replies to 2-3 sentences max. No bullet points or lists in replies — just natural conversation."""


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
    system_prompt = LEAD_MAGNET_SYSTEM_PROMPT if active_funnel == "lead_magnet" else APPLICATION_SYSTEM_PROMPT

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
                        await reply_to_comment(comment_id, "Hey! Just sent you a DM 👀")

                        if funnel_type == "lead_magnet":
                            opening = "Hey! Saw your comment 👀 I've got something that might help — a free margin calculator that shows you exactly where your bakery is leaking money. Want me to send it?"
                        else:
                            opening = "Hey! Noticed your comment 👀 quick one — you running a bakery or café?"

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

                    logger.info(f"Message from {sender_id}: {text}")
                    funnel = conversation_funnels.get(sender_id, "application")
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

                logger.info(f"Message (legacy) from {sender_id}: {text}")
                funnel = conversation_funnels.get(sender_id, "application")
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
