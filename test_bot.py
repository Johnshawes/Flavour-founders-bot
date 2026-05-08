"""
Smoke-test the bot's Claude reply without sending a real IG DM.
Hits /admin/test-claude on the deployed bot — no DB writes, no IG calls.

Setup:
    pip install httpx python-dotenv
    Set BOT_URL and ADMIN_KEY in your environment (or a local .env).

Usage:
    # Single message (treated as the user's first reply):
    python test_bot.py "I run a bakery in Bristol, £30K/month"

    # Different funnel:
    python test_bot.py --funnel lead_magnet "yes please send the calculator"

    # Multi-turn conversation (alternating user/bot/user/bot/.../user):
    python test_bot.py --history \\
        "Hi how are you" \\
        "Hey, thanks for reaching out. You running a bakery?" \\
        "Yes, 5 years now, doing about £25K/month" \\
        "Nice. What's keeping you up at night — money, hours, or team?" \\
        "Honestly the hours, I'm doing 70 a week"
"""
import argparse
import os
import sys

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def main():
    ap = argparse.ArgumentParser(description="Smoke-test the bot's Claude reply")
    ap.add_argument(
        "--funnel",
        default="application",
        choices=["application", "lead_magnet", "startup_course"],
    )
    ap.add_argument(
        "--history",
        action="store_true",
        help=(
            "Treat trailing args as alternating user/assistant/user/.../user "
            "(odd count = last is the new user message)."
        ),
    )
    ap.add_argument("--url", default=os.environ.get("BOT_URL", "https://web-production-3aebb.up.railway.app"))
    ap.add_argument("messages", nargs="+", help="Test message(s)")
    args = ap.parse_args()

    admin_key = os.environ.get("ADMIN_KEY")
    if not admin_key:
        sys.exit("ERROR: Set ADMIN_KEY in env (same value as on Railway).")

    if args.history:
        if len(args.messages) % 2 == 0:
            sys.exit("With --history, pass an ODD number of messages (alternating user/assistant ending with user).")
        history = []
        for i, m in enumerate(args.messages[:-1]):
            history.append({"role": "user" if i % 2 == 0 else "assistant", "content": m})
        message = args.messages[-1]
    else:
        history = []
        message = " ".join(args.messages)

    payload = {"funnel": args.funnel, "message": message, "history": history}

    try:
        r = httpx.post(
            f"{args.url}/admin/test-claude",
            json=payload,
            headers={"X-Admin-Key": admin_key},
            timeout=60.0,
        )
    except httpx.HTTPError as e:
        sys.exit(f"Network error: {e}")

    if r.status_code != 200:
        sys.exit(f"HTTP {r.status_code}: {r.text}")

    data = r.json()
    sep = "─" * 70
    print()
    print(sep)
    print(f"Funnel: {args.funnel}")
    if history:
        print(f"History: {len(history)} prior messages")
    print()
    print(f"User : {message}")
    print(f"Bot  : {data['reply']}")
    if data.get("flagged_high_value"):
        print()
        print("🔥 FLAGGED AS HIGH-VALUE LEAD")
    print(sep)
    print()


if __name__ == "__main__":
    main()
