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
# ─────────────────────────────────────────────────────────────────────────────

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# In-memory conversation store  {sender_id: [{"role": ..., "content": ...}]}
conversations: dict[str, list] = {}

# Track comments we've already replied to
processed_comments: set[str] = set()


def load_trigger_keywords() -> list[str]:
    """Load trigger keywords from the ## Trigger Keywords section of CLAUDE.md."""
    claude_md = Path(__file__).parent / "CLAUDE.md"
    if not claude_md.exists():
        logger.warning("CLAUDE.md not found, using default keywords")
        return ["info", "interested", "how much", "tell me more", "sign me up"]

    text = claude_md.read_text(encoding="utf-8")
    keywords = []
    in_section = False
    for line in text.splitlines():
        if line.strip().lower().startswith("## trigger keywords"):
            in_section = True
            continue
        if in_section and line.strip().startswith("## "):
            break
        if in_section and line.strip().startswith("- "):
            keywords.append(line.strip().lstrip("- ").strip().lower())

    logger.info(f"Loaded trigger keywords: {keywords}")
    return keywords if keywords else ["info", "interested", "how much", "tell me more", "sign me up"]


TRIGGER_KEYWORDS = load_trigger_keywords()

SYSTEM_PROMPT = """You are a friendly assistant managing DMs for John Hawes — a business consultant who helps bakery and café owners get profitable and work less than 8 hours a week.

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


def verify_signature(payload: bytes, signature: str) -> bool:
    expected = "sha256=" + hmac.new(
        APP_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def comment_has_trigger(text: str) -> bool:
    """Check if a comment contains any trigger keyword."""
    text_lower = text.lower()
    return any(kw in text_lower for kw in TRIGGER_KEYWORDS)


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


async def get_claude_reply(sender_id: str, user_message: str) -> str:
    if not anthropic_client:
        logger.error("Anthropic API key not set")
        return "Sorry, I'm having a technical issue. Please try again later!"

    history = conversations.setdefault(sender_id, [])
    history.append({"role": "user", "content": user_message})

    # Keep last 20 messages to stay within context limits
    trimmed = history[-20:]

    logger.info(f"Calling Claude for sender {sender_id}")
    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=trimmed,
        )
        reply = response.content[0].text
        history.append({"role": "assistant", "content": reply})
        logger.info(f"Claude reply: {reply[:50]}...")
        return reply
    except Exception as e:
        logger.error(f"Claude API error for sender {sender_id}: {e}")
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

                    if comment_has_trigger(comment_text):
                        logger.info(f"Trigger keyword matched in comment {comment_id}")
                        await reply_to_comment(comment_id, "Hey! Just sent you a DM 👀")
                        opening = "Hey! Noticed your comment 👀 quick one — you running a bakery or café?"
                        await send_dm(commenter_id, opening)
                        conversations[commenter_id] = [
                            {"role": "assistant", "content": opening},
                        ]

                # ── Handle DMs (v25 format) ──────────────────────────
                elif field == "messages":
                    sender_id = value.get("sender", {}).get("id")
                    text = value.get("message", {}).get("text")

                    if not text or not sender_id:
                        continue

                    logger.info(f"Message from {sender_id}: {text}")
                    reply = await get_claude_reply(sender_id, text)
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
                reply = await get_claude_reply(sender_id, text)
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
