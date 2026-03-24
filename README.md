# Flavour Founders Instagram DM Bot

An AI-powered Instagram DM chatbot built with Claude (Anthropic) + FastAPI.
Automatically qualifies leads for your high-ticket consultancy programme.

---

## What It Does

- Responds to every Instagram DM automatically
- Qualifies leads using a conversational flow
- Detects keywords (price, burnout, join, etc.) and responds appropriately
- Sends qualified leads your booking link
- Powered by Claude AI (sounds human, not robotic)

---

## Files

| File | Purpose |
|------|---------|
| `main.py` | The bot — webhook handler + Claude integration |
| `requirements.txt` | Python dependencies |
| `Procfile` | Tells Railway how to run the app |
| `.env.example` | Template for your environment variables |
| `.gitignore` | Keeps secrets out of GitHub |

---

## Deployment Steps

### 1. Add Your Booking Link
In `main.py`, find this line in the SYSTEM_PROMPT:
```
[INSERT YOUR BOOKING LINK]
```
Replace it with your actual Calendly or booking page URL.

### 2. Push to GitHub
```bash
git init
git add .
git commit -m "Initial bot setup"
# Create a new repo on github.com, then:
git remote add origin https://github.com/YOUR_USERNAME/flavour-founders-bot.git
git push -u origin main
```

### 3. Deploy to Railway
1. Go to railway.app and sign up (free)
2. Click "New Project" → "Deploy from GitHub repo"
3. Select your repo
4. Go to "Variables" tab and add these environment variables:

| Variable | Value |
|----------|-------|
| `ANTHROPIC_API_KEY` | Your key from console.anthropic.com |
| `INSTAGRAM_ACCESS_TOKEN` | Token from Meta Developer dashboard |
| `APP_SECRET` | App Secret from Meta App Settings → Basic |
| `VERIFY_TOKEN` | `flavourfounders2024` (or any string you choose) |

5. Railway will give you a public URL like: `https://your-app.railway.app`

### 4. Register Webhook in Meta
1. Go to your Meta Developer dashboard
2. Use cases → Customise → Configure webhooks
3. Callback URL: `https://your-app.railway.app/webhook`
4. Verify token: `flavourfounders2024` (must match your env variable)
5. Click Verify
6. Turn on Webhook Subscription toggle next to your Instagram account

### 5. Test It
Send a DM to your Instagram account from another account.
The bot should reply within seconds!

---

## Customising the Bot

All bot behaviour is controlled by the `SYSTEM_PROMPT` in `main.py`.
- Change Flo's name, tone, or qualification questions there
- Update your booking link
- Add/remove keyword triggers

---

## Getting Your Anthropic API Key
1. Go to console.anthropic.com
2. Sign up / log in
3. Go to "API Keys" → "Create Key"
4. Copy and save it securely
