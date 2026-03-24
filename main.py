import os
import hmac
import hashlib
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from anthropic import Anthropic

app = FastAPI()
anthropic = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# ── Config (set these in Railway environment variables) ──────────────────────
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
APP_SECRET = os.environ.get("APP_SECRET", "")
ACCESS_TOKEN = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
# ─────────────────────────────────────────────────────────────────────────────

# In-memory conversation store  {sender_id: [{"role": ..., "content": ...}]}
conversations: dict[str, list] = {}

SYSTEM_PROMPT = """You are Flo, a warm and professional assistant for John Hawes — a business consultant who helps bakery and café owners become more profitable and work less than 8 hours a week through his high-ticket consultancy.

YOUR GOAL: Qualify leads for a free discovery call with John. Do NOT reveal the programme price. Do NOT book calls yourself. Guide the conversation to determine if they are a good fit, then send them the booking link if they qualify.

IDEAL CLIENT:
- Owns and runs a bakery, café, patisserie, or coffee shop
- Already trading (not pre-launch)
- Feeling overworked, underpaid, or both
- Open to investing in their business growth

DISQUALIFIED (send warm exit):
- Home bakers with no premises
- People who haven't launched yet
- Anyone rude or aggressive

QUALIFICATION FLOW (follow this order, one question at a time):
1. Warm greeting, acknowledge their message naturally
2. Ask if they currently run their bakery/café full time
3. Ask what their biggest challenge is right now — profit, time, or both
4. Ask how long they've been in business
5. IF QUALIFIED: "You sound like exactly the kind of owner John loves working with! I'd love to get you booked in for a free discovery call with him — here's the link: https://flavourfounders.com/3---schedule-page-page-3707"
6. IF NOT YET READY: "Thanks so much for reaching out! John works best with owners who are already up and running. Follow along for tips and come back when you're ready to scale — we'd love to work with you! 🙌"

KEYWORD RULES:
- "price" / "cost" / "how much" → "Great question! Before I share details, I want to make sure it's the right fit for you first. Mind if I ask a couple of quick questions?"
- "burnout" / "exhausted" / "overwhelmed" / "no time" → Lead with empathy first, then move into qualification
- "not ready" / "just looking" / "maybe later" → Send warm exit message
- "programme" / "program" / "join" / "enrol" / "sign up" → Move into qualification flow

TONE: Warm, confident, encouraging. Never pushy or salesy. Sound like a helpful human, not a bot.
REPLY LENGTH: Keep replies to 2-3 sentences max. Ask only one question at a time.
IMPORTANT: Never reveal you are an AI unless directly and sincerely asked."""


def verify_signature(payload: bytes, signature: str) -> bool:
    expected = "sha256=" + hmac.new(
        APP_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def send_dm(recipient_id: str, text: str):
    url = "https://graph.instagram.com/v21.0/me/messages"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text},
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload, params={"access_token": ACCESS_TOKEN})
        r.raise_for_status()


async def get_claude_reply(sender_id: str, user_message: str) -> str:
    history = conversations.setdefault(sender_id, [])
    history.append({"role": "user", "content": user_message})

    # Keep last 20 messages to stay within context limits
    trimmed = history[-20:]

    response = anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=trimmed,
    )
    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})
    return reply


# ── Webhook verification (GET) ───────────────────────────────────────────────
@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == VERIFY_TOKEN
    ):
        return PlainTextResponse(params.get("hub.challenge", ""))
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

    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            sender_id = event.get("sender", {}).get("id")
            message = event.get("message", {})
            text = message.get("text")

            # Ignore echoes (messages sent by the page itself)
            if message.get("is_echo") or not text or not sender_id:
                continue

            reply = await get_claude_reply(sender_id, text)
            await send_dm(sender_id, reply)

    return {"status": "ok"}


# ── Health check ─────────────────────────────────────────────────────────────
@app.get("/")
async def health():
    return {"status": "Flavour Founders Bot is running 🚀"}
