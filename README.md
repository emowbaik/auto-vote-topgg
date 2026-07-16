# auto-vote-topgg

Automated daily voting bot for [top.gg](https://top.gg) using Playwright (headless Chromium) + GitHub Actions. Supports multiple Discord accounts and multiple bots.

## Features

- 🗳️ Auto-vote **2× per day** (07:00 & 19:00 WIB) to maximize cooldown cycles
- 👥 **Multi-account** — vote with multiple Discord tokens in one run
- 🤖 **Multi-bot** — vote for multiple bots per account
- 🔐 **Secure OAuth** — lets top.gg generate its own OAuth URL (with `state` + PKCE), no hardcoded auth URLs
- ⚡ **Turnstile auto-solve** — Cloudflare Turnstile resolves naturally in headless Chromium
- 📨 **Telegram notifications** — per-account vote results sent to your chat
- 🧹 **Auto-cleanup** — keeps only the latest GitHub Actions run log

## How It Works

```
Discord Token
    ↓ inject via localStorage
discord.com (authenticated)
    ↓ navigate to top.gg vote page → click Login
discord.com/oauth2/authorize (with state + PKCE from top.gg)
    ↓ scroll OAuth dialog → click Authorize
top.gg (session established)
    ↓ navigate to vote page → wait Turnstile → click Vote
✅ Vote submitted
```

## Setup

### 1. Fork this repository

Fork to your own GitHub account so you can add Secrets and run Actions.

### 2. Get your Discord Token

> [!CAUTION]
> Discord user tokens are sensitive credentials. Never share them.

**Via Network Tab (recommended):**
1. Open [discord.com](https://discord.com) in your browser → press `F12`
2. Go to **Network** tab → filter by **Fetch/XHR**
3. Click any channel or DM to trigger a request
4. Click any request to `discord.com/api/...`
5. In **Request Headers**, find the `Authorization` header → that's your token

**Via Local Storage:**
1. Open [discord.com](https://discord.com) → press `F12`
2. Go to **Application** tab → **Local Storage** → `https://discord.com`
3. Find key `token` → copy the value (without surrounding quotes)

### 3. Configure GitHub Secrets

Go to your repo **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Required | Description |
|--------|:--------:|-------------|
| `TOKENS` | ✅ | Discord user token(s) — one per line for multi-account |
| `BOT_IDS` | ❌ | Bot ID(s) to vote for — one per line. Default: `830530156048285716` |
| `TG_BOT_TOKEN` | ❌ | Telegram bot token (from [@BotFather](https://t.me/BotFather)) |
| `TG_CHAT_ID` | ❌ | Telegram chat/user ID for vote result notifications |

**`TOKENS` multi-account example:**
```
NzI4MjA0NDU4MjcxMjg2NzMy.XXXXXX.YYYYYYYYYYYY
OTQxNjM3NDU4MjcxMDA2NDAz.XXXXXX.ZZZZZZZZZZZZ
```

**`BOT_IDS` multi-bot example:**
```
830530156048285716
123456789012345678
```

### 4. Enable GitHub Actions

Go to your repo **Actions** tab → click **"I understand my workflows, go ahead and enable them"**.

The bot will automatically vote at:
- **07:00 WIB** (00:00 UTC)
- **19:00 WIB** (12:00 UTC)

You can also trigger manually: **Actions → Top.gg Auto Vote → Run workflow**.

## Debugging

To enable verbose logging and screenshots locally, set `DEBUG=1`:

```bash
# Windows
set DEBUG=1 && python vote.py

# Linux / macOS
DEBUG=1 python vote.py
```

Screenshots will be saved to `screenshots/` (gitignored). In normal mode (GitHub Actions), no screenshots or URLs are logged to protect your credentials.

## Project Structure

```
auto-vote-topgg/
├── vote.py                          # Main voting script
├── requirements.txt                 # Python dependencies
├── .github/
│   └── workflows/
│       └── vote.yml                 # GitHub Actions schedule & job
└── .gitignore
```

## Requirements

- Python 3.11+
- `playwright` + `requests` (see `requirements.txt`)
- Chromium (installed automatically by `playwright install chromium`)

## ⚠️ Disclaimer

This project automates interactions using Discord user tokens. Using self-bots violates [Discord's Terms of Service](https://discord.com/terms). Use at your own risk. The author is not responsible for any account bans or other consequences.
