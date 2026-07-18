#!/usr/bin/env python3
"""
auto-vote-dcbot — Automated daily voting on top.gg via Playwright.

Flow per account:
  1. Inject Discord token → OAuth authorize → logged into top.gg
  2. For each bot ID: navigate vote page → wait Turnstile → click Vote
  3. Report results via Telegram

Environment variables (GitHub Secrets):
    TOKENS        — Newline-separated Discord user tokens
    BOT_IDS       — Newline-separated Discord bot IDs (default: 830530156048285716)
    TG_BOT_TOKEN  — (optional) Telegram bot token for notifications
    TG_CHAT_ID    — (optional) Telegram chat ID
    DEBUG         — (optional) Set to "1" to enable verbose URL logging & screenshots
"""

import asyncio
import os
import sys
import requests
from datetime import datetime, timezone, timedelta
from playwright.async_api import async_playwright, Page, Browser

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WIB = timezone(timedelta(hours=7))

DISCORD_LOGIN_URL = "https://discord.com/login"

DEFAULT_BOT_IDS = ["830530156048285716"]

TIMEOUT_OAUTH_MS = 20_000
TIMEOUT_VOTE_MS = 30_000
DELAY_BETWEEN_BOTS_SEC = 3
DELAY_BETWEEN_ACCOUNTS_SEC = 5
MAX_RETRIES = 3
RETRY_DELAY_SEC = 10

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
DEBUG = os.environ.get("DEBUG", "").strip() == "1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def dbg(msg: str) -> None:
    """Print only when DEBUG=1."""
    if DEBUG:
        print(f"    [dbg] {msg}")


def screenshot(page: Page, path: str):
    """Take screenshot only in DEBUG mode."""
    if DEBUG:
        return page.screenshot(path=path)
    # Return a no-op coroutine
    async def _noop():
        pass
    return _noop()


async def error_screenshot(page: Page, path: str) -> str | None:
    """Always take screenshot on errors, regardless of DEBUG mode. Returns path or None."""
    try:
        os.makedirs("screenshots", exist_ok=True)
        await page.screenshot(path=path)
        return path
    except Exception:
        return None


def send_telegram_photo(path: str, caption: str = "") -> bool:
    """Send a photo to Telegram. Returns True on success."""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return False
    try:
        with open(path, "rb") as f:
            resp = requests.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
                data={"chat_id": TG_CHAT_ID, "caption": caption},
                files={"photo": f},
                timeout=30,
            )
        return resp.status_code == 200
    except Exception:
        return False


def notify_error_screenshot(bot_id: str, path: str, detail: str) -> None:
    """Send error screenshot to Telegram if configured."""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    caption = f"❌ Vote failed for {bot_id}\n{detail}"
    ok = send_telegram_photo(path, caption)
    if ok:
        print(f"  📸 Error screenshot sent to Telegram")
    else:
        print(f"  ⚠️  Could not send error screenshot to Telegram")


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_tokens() -> list[str]:
    raw = os.environ.get("TOKENS", "").strip()
    if not raw:
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def load_bot_ids() -> list[str]:
    raw = os.environ.get("BOT_IDS", "").strip()
    if not raw:
        return DEFAULT_BOT_IDS
    ids = [line.strip() for line in raw.splitlines() if line.strip()]
    return ids if ids else DEFAULT_BOT_IDS


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

def send_notification(message: str) -> None:
    print("\n" + "=" * 45)
    print(message)
    print("=" * 45)

    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("ℹ️  TG_BOT_TOKEN / TG_CHAT_ID not set — skip notification.")
        return

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        if resp.status_code == 200:
            print("📨 Telegram notification sent.")
        else:
            print(f"⚠️  Notification failed: {resp.status_code}")
    except Exception as exc:
        print(f"⚠️  Notification exception: {exc}")


# ---------------------------------------------------------------------------
# Discord OAuth login
# ---------------------------------------------------------------------------

async def _handle_discord_oauth(page: Page) -> bool:
    """Handle the Discord OAuth dialog (scroll past 'Keep Scrolling...' then click Authorize)."""
    await asyncio.sleep(2)
    await screenshot(page, "screenshots/03_oauth_page.png")
    dbg(f"OAuth page URL: {page.url}")

    # Discord OAuth: "Keep Scrolling..." guard — scroll inner dialog to reveal Authorize button
    print("  → Handling Discord OAuth dialog...")
    authorized = False
    for attempt in range(12):
        # Check if Authorize button is visible
        authorize_btn = page.locator("button:has-text('Authorize'), button:has-text('Authorise')").first
        try:
            if await authorize_btn.is_visible(timeout=2000):
                dbg(f"Authorize button found (attempt {attempt+1})")
                await authorize_btn.click()
                authorized = True
                break
        except Exception:
            pass

        # Scroll the inner scrollable container of the Discord OAuth dialog
        scrolled = await page.evaluate("""() => {
            const selectors = [
                'div[class*="scroller"]',
                'div[class*="scrollerBase"]',
                'div[class*="overflow"]',
                'div[class*="body"]',
                'div[class*="content"]',
            ];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el && el.scrollHeight > el.clientHeight) {
                    el.scrollTop += 300;
                    return `scrolled ${sel} (scrollTop=${el.scrollTop})`;
                }
            }
            window.scrollBy(0, 300);
            return 'fallback window.scrollBy';
        }""")
        dbg(f"Scroll attempt {attempt+1}: {scrolled}")
        await asyncio.sleep(1.5)

    if not authorized:
        print("  ❌ Could not find/click Authorize button")
        await screenshot(page, "screenshots/04_no_auth_btn.png")
    return authorized


async def discord_oauth_login(page: Page, token: str, bot_ids: list[str]) -> bool:
    """
    Login flow:
      1. Inject Discord token into discord.com
      2. Navigate to top.gg vote page → click Login (top.gg generates OAuth URL with state + PKCE)
      3. Handle Discord OAuth dialog → redirect back to top.gg with session set
    Returns True if logged into top.gg successfully.
    """
    if DEBUG:
        os.makedirs("screenshots", exist_ok=True)

    # Step 1: Inject token into Discord
    print("  → Injecting Discord token...")
    await page.goto(DISCORD_LOGIN_URL, wait_until="load")
    await screenshot(page, "screenshots/01_discord_login.png")

    await page.evaluate("""(token) => {
        const iframe = document.body.appendChild(document.createElement('iframe'));
        if (iframe.contentWindow) {
            const ls = iframe.contentWindow.localStorage;
            ls.setItem('token', `"${token}"`);
            ls.setItem('tokens', JSON.stringify({"default": token}));
        }
        iframe.remove();
    }""", token)

    await page.reload(wait_until="load")
    await asyncio.sleep(3)
    await screenshot(page, "screenshots/02_after_reload.png")
    dbg(f"After token inject: {page.url}")

    # Step 2: Navigate to top.gg vote page and click Login
    # top.gg generates OAuth URL with proper state + PKCE parameters
    first_bot = bot_ids[0] if bot_ids else "830530156048285716"
    vote_url = f"https://top.gg/bot/{first_bot}/vote"
    print("  → Navigating to top.gg to initiate login...")
    await page.goto(vote_url, wait_until="load")
    await asyncio.sleep(3)

    login_btn = page.locator("a:has-text('Login'), button:has-text('Login')").first
    try:
        if await login_btn.is_visible(timeout=5000):
            async with page.expect_navigation(url="**discord.com/oauth2/authorize**", timeout=15000):
                await login_btn.click()
        else:
            # Check if already logged in
            page_text = await page.inner_text("body")
            if "must be logged in" not in page_text.lower():
                print("  ✅ Already logged into top.gg")
                return True
            raise Exception("Login button not visible")
    except Exception as exc:
        print(f"  ❌ Could not trigger top.gg login: {exc}")
        await screenshot(page, "screenshots/03_login_failed.png")
        return False

    # Step 3: Handle Discord OAuth dialog
    authorized = await _handle_discord_oauth(page)

    # Wait for redirect back to top.gg
    try:
        await page.wait_for_url("**/top.gg/**", timeout=TIMEOUT_OAUTH_MS)
        final_url = page.url
        dbg(f"Redirected to: {final_url}")

        # If landed on callback URL, wait a moment for final redirect
        if "callback" in final_url:
            await asyncio.sleep(3)
            final_url = page.url

        page_text = await page.inner_text("body")
        if "404" in page_text or "could not be found" in page_text.lower():
            print("  ❌ top.gg callback returned 404")
            await screenshot(page, "screenshots/05_oauth_404.png")
            return False

        print("  ✅ Logged into top.gg")
        await screenshot(page, "screenshots/05_topgg_logged_in.png")
        return True
    except Exception as exc:
        await screenshot(page, "screenshots/05_oauth_failed.png")
        print(f"  ❌ OAuth redirect failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Vote for a single bot
# ---------------------------------------------------------------------------

async def vote_for_bot(page: Page, bot_id: str) -> dict:
    """Navigate to vote page and click Vote. Returns result dict."""
    vote_url = f"https://top.gg/bot/{bot_id}/vote"
    print(f"  → Voting for bot {bot_id}...")

    await page.goto(vote_url, wait_until="load")
    await asyncio.sleep(3)
    await screenshot(page, f"screenshots/vote_{bot_id}.png")
    dbg(f"Vote page URL: {page.url}")

    # If still showing "not logged in", reload once (session cookie race condition)
    page_text = await page.inner_text("body")
    text_lower = page_text.lower()
    if "must be logged in" in text_lower or "login to vote" in text_lower:
        dbg("Session not applied yet, reloading...")
        await page.reload(wait_until="load")
        await asyncio.sleep(3)
        page_text = await page.inner_text("body")
        text_lower = page_text.lower()

    # Check 404
    if "404" in await page.title() or "could not be found" in page_text.lower():
        print(f"  ❌ Vote page 404 for {bot_id}")
        return {"bot_id": bot_id, "status": "error", "detail": "Vote page 404"}

    # Check not logged in
    if "must be logged in" in text_lower or "login to vote" in text_lower:
        print(f"  ❌ Not logged into top.gg")
        err_path = await error_screenshot(page, f"screenshots/vote_{bot_id}_not_logged_in.png")
        if err_path:
            notify_error_screenshot(bot_id, err_path, "Not logged into top.gg")
        return {"bot_id": bot_id, "status": "error", "detail": "Not logged into top.gg"}

    # Check cooldown
    if any(kw in text_lower for kw in ["vote again in", "already voted", "come back", "cooldown"]):
        print(f"  ⏳ Already voted for {bot_id} (cooldown)")
        return {"bot_id": bot_id, "status": "cooldown", "detail": "Cooldown active"}

    # Find Vote button
    vote_btn = None
    for selector in [
        "button:has-text('Vote')",
        ".chakra-button:has-text('Vote')",
        "[data-testid='vote-button']",
        "a[href*='vote']:has-text('Vote')",
    ]:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=3000):
                vote_btn = btn
                break
        except Exception:
            continue

    if not vote_btn:
        print(f"  ❌ Vote button not found for {bot_id}")
        err_path = await error_screenshot(page, f"screenshots/vote_{bot_id}_no_btn.png")
        if err_path:
            notify_error_screenshot(bot_id, err_path, "Vote button not found")
        return {"bot_id": bot_id, "status": "error", "detail": "Vote button not found"}

    # Wait for Turnstile to auto-solve (button becomes enabled)
    print("  → Waiting for Turnstile captcha...")
    is_disabled = await vote_btn.is_disabled()
    retries = 0
    while is_disabled and retries < 15:
        await asyncio.sleep(2)
        is_disabled = await vote_btn.is_disabled()
        retries += 1

    if is_disabled:
        print(f"  ❌ Vote button still disabled (Turnstile timeout)")
        err_path = await error_screenshot(page, f"screenshots/vote_{bot_id}_disabled.png")
        if err_path:
            notify_error_screenshot(bot_id, err_path, "Turnstile solve timeout")
        return {"bot_id": bot_id, "status": "error", "detail": "Turnstile solve timeout"}

    # Click Vote
    print("  → Clicking Vote...")
    await vote_btn.click()
    await asyncio.sleep(5)

    await screenshot(page, f"screenshots/vote_{bot_id}_after_click.png")

    # Check immediately for "Thanks for voting!" (appears right after successful vote)
    page_text_after = await page.inner_text("body")
    text_after_lower = page_text_after.lower()

    if "thanks for voting" in text_after_lower:
        print(f"  ✅ Successfully voted for {bot_id}")
        await screenshot(page, f"screenshots/vote_{bot_id}_success.png")
        return {"bot_id": bot_id, "status": "success", "detail": "Vote successful"}

    # Fallback: reload page and check server-side state
    dbg("No immediate success text, reloading to verify...")
    await page.reload(wait_until="load")
    await asyncio.sleep(3)
    page_text_after = await page.inner_text("body")
    text_after_lower = page_text_after.lower()
    await screenshot(page, f"screenshots/vote_{bot_id}_after.png")

    # "You have already voted" / "You can vote again in about 12 hours" = success
    if any(kw in text_after_lower for kw in [
        "you have already voted",
        "already voted",
        "vote again in",
        "can vote again",
        "thanks for voting",
        "thank you",
    ]):
        print(f"  ✅ Successfully voted for {bot_id}")
        return {"bot_id": bot_id, "status": "success", "detail": "Vote successful"}
    else:
        print(f"  ⚠️  Vote clicked, result unclear for {bot_id}")
        return {"bot_id": bot_id, "status": "uncertain", "detail": "Clicked, result unclear"}


# ---------------------------------------------------------------------------
# Per-account flow
# ---------------------------------------------------------------------------

async def _run_account(
    browser: Browser,
    token: str,
    bot_ids: list[str],
    token_preview: str,
) -> list[dict]:
    """Single attempt: login + vote for all bots. Raises on unexpected error."""
    results = []

    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 720},
    )
    page = await context.new_page()

    try:
        logged_in = await discord_oauth_login(page, token, bot_ids)
        if not logged_in:
            results.append({"bot_id": "all", "status": "auth_failed", "detail": "Discord OAuth login failed", "token_preview": token_preview})
            return results

        for i, bot_id in enumerate(bot_ids):
            result = await vote_for_bot(page, bot_id)
            result["token_preview"] = token_preview
            results.append(result)
            if i < len(bot_ids) - 1:
                await asyncio.sleep(DELAY_BETWEEN_BOTS_SEC)
    finally:
        await context.close()

    return results


async def process_account(
    browser: Browser,
    token: str,
    bot_ids: list[str],
    index: int,
    total: int,
) -> list[dict]:
    """Run full vote flow for one account, with up to MAX_RETRIES attempts."""
    prefix = f"[{index}/{total}]"
    token_preview = token[:10] + "..." + token[-5:]
    print(f"\n{'─' * 45}")
    print(f"{prefix} Processing account...")

    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if attempt > 1:
                print(f"{prefix} ↺ Retry {attempt}/{MAX_RETRIES} (waiting {RETRY_DELAY_SEC}s)...")
                await asyncio.sleep(RETRY_DELAY_SEC)
            results = await _run_account(browser, token, bot_ids, token_preview)
            # If we got any non-error status, consider it done
            if results:
                return results
        except Exception as exc:
            last_exc = exc
            print(f"{prefix} ❌ Attempt {attempt} failed: {str(exc)[:120]}")

    # All retries exhausted
    err_msg = str(last_exc)[:200] if last_exc else "Unknown error after retries"
    print(f"{prefix} ❌ All {MAX_RETRIES} attempts failed")
    return [{"bot_id": "all", "status": "error", "detail": f"Failed after {MAX_RETRIES} retries: {err_msg}", "token_preview": token_preview}]


# ---------------------------------------------------------------------------
# Build notification
# ---------------------------------------------------------------------------

def build_notification(all_results: list[list[dict]], now: str) -> str:
    lines = [
        "🗳️ <b>Top.gg Auto Vote Report</b>",
        f"⏱️ {now}",
        "",
    ]

    for account_results in all_results:
        if not account_results:
            continue

        token_preview = account_results[0].get("token_preview", "?")
        lines.append(f"👤 <b>Account {token_preview}</b>")

        for r in account_results:
            bot_id = r.get("bot_id", "?")
            status = r.get("status", "?")
            detail = r.get("detail", "")

            icon = {"success": "✅", "cooldown": "⏳", "uncertain": "⚠️"}.get(status, "❌")
            lines.append(f"  {icon} {bot_id}: {detail}")

        lines.append("")

    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    tokens = load_tokens()
    if not tokens:
        print(
            "❌ No tokens found.\n"
            "   Set TOKENS secret (one Discord user token per line)."
        )
        sys.exit(1)

    bot_ids = load_bot_ids()
    now = datetime.now(WIB).strftime("%Y-%m-%d %H:%M WIB")
    total = len(tokens)

    print("🚀 auto-vote-dcbot starting")
    print(f"   Tokens  : {total}")
    print(f"   Bots    : {len(bot_ids)}")
    print(f"   Time    : {now}")
    if DEBUG:
        print("   Mode    : DEBUG (screenshots enabled)")

    all_results: list[list[dict]] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        for i, token in enumerate(tokens, 1):
            results = await process_account(browser, token, bot_ids, i, total)
            all_results.append(results)
            if i < total:
                await asyncio.sleep(DELAY_BETWEEN_ACCOUNTS_SEC)

        await browser.close()

    # Summary
    print(f"\n{'=' * 45}")
    print(f"📊 Done — {total} account(s) processed")

    message = build_notification(all_results, now)
    send_notification(message)


if __name__ == "__main__":
    asyncio.run(main())
