#!/usr/bin/env python3
"""Automated top.gg voting via nodriver, cookie-first auth, and Discord OAuth fallback."""

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

import nodriver as uc
import requests

WIB = timezone(timedelta(hours=7))
DISCORD_LOGIN_URL = "https://discord.com/login"
DEFAULT_BOT_IDS = ["830530156048285716"]
TIMEOUT_OAUTH_SEC = 25
TIMEOUT_VOTE_SEC = 30
DELAY_BETWEEN_BOTS_SEC = 3
DELAY_BETWEEN_ACCOUNTS_SEC = 5
MAX_RETRIES = 3
RETRY_DELAY_SEC = 10
FINAL_STATUSES = frozenset({"success", "cooldown", "captcha_required"})
TRANSIENT_STATUSES = frozenset({"error", "auth_failed", "uncertain"})
BROWSER_START_RETRIES = 5
BROWSER_START_RETRY_SEC = 2


class BrowserStartupError(RuntimeError):
    """Credential-free nodriver startup failure safe for workflow logs."""

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
DEBUG = os.environ.get("DEBUG", "").strip() == "1"
SEND_ERROR_SCREENSHOTS = os.environ.get("SEND_ERROR_SCREENSHOTS", "").strip() == "1"


def dbg(msg: str) -> None:
    if DEBUG:
        print(f"    [dbg] {msg}")


def safe_exception_detail(exc: Exception) -> str:
    """Redact configured credentials before exposing a short diagnostic."""
    detail = str(exc).replace("\n", " ").strip()
    secrets = [
        TG_BOT_TOKEN,
        TG_CHAT_ID,
        os.environ.get("TOPGG_COOKIES_JSON", ""),
        *load_tokens(),
    ]
    for secret in secrets:
        if secret:
            detail = detail.replace(secret, "***")
    return detail[:200] or "no detail"


async def screenshot(tab: Any, path: str) -> None:
    if DEBUG:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        await tab.save_screenshot(filename=path, format="png")


async def error_screenshot(tab: Any, path: str) -> str | None:
    if not SEND_ERROR_SCREENSHOTS:
        return None
    try:
        os.makedirs("screenshots", exist_ok=True)
        await tab.save_screenshot(filename=path, format="png")
        return path
    except Exception as exc:
        dbg(f"Error screenshot failed: {type(exc).__name__}")
        return None


def send_telegram_photo(path: str, caption: str = "") -> bool:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return False
    try:
        with open(path, "rb") as file:
            response = requests.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
                data={"chat_id": TG_CHAT_ID, "caption": caption},
                files={"photo": file},
                timeout=30,
            )
        return response.status_code == 200
    except Exception as exc:
        dbg(f"Telegram photo failed: {type(exc).__name__}")
        return False


async def notify_error_screenshot(bot_id: str, path: str, detail: str) -> None:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    caption = f"❌ Vote failed for {bot_id}\n{detail}"
    sent = await asyncio.to_thread(send_telegram_photo, path, caption)
    print("  📸 Error screenshot sent to Telegram" if sent else "  ⚠️  Could not send error screenshot to Telegram")


def load_tokens() -> list[str]:
    raw = os.environ.get("TOKENS", "").strip()
    return [line.strip() for line in raw.splitlines() if line.strip()] if raw else []


def load_bot_ids() -> list[str]:
    raw = os.environ.get("BOT_IDS", "").strip()
    ids = [line.strip() for line in raw.splitlines() if line.strip()] if raw else DEFAULT_BOT_IDS
    invalid = [bot_id for bot_id in ids if not (bot_id.isdigit() and 17 <= len(bot_id) <= 20)]
    if invalid:
        raise ValueError(f"Invalid BOT_IDS value: {invalid[0]!r}; expected a 17-20 digit Discord ID")
    return ids


def account_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def is_retryable_result(result: dict) -> bool:
    return result.get("status") in TRANSIENT_STATUSES


def retryable_bot_ids(results: list[dict]) -> list[str]:
    return [
        str(result["bot_id"])
        for result in results
        if is_retryable_result(result) and result.get("bot_id") not in {None, "all"}
    ]


def _normalize_cookie(cookie: dict) -> dict:
    normalized = {
        "name": str(cookie["name"]),
        "value": str(cookie["value"]),
        "domain": str(cookie.get("domain", ".top.gg")),
        "path": str(cookie.get("path", "/")),
        "secure": bool(cookie.get("secure", True)),
        "httpOnly": bool(cookie.get("httpOnly", False)),
    }
    expiration = cookie.get("expirationDate", cookie.get("expires"))
    if expiration and float(expiration) > 0:
        normalized["expires"] = float(expiration)
    same_site = str(cookie.get("sameSite", "Lax")).lower()
    normalized["sameSite"] = {
        "lax": "Lax",
        "strict": "Strict",
        "none": "None",
        "no_restriction": "None",
        "unspecified": "Lax",
    }.get(same_site, "Lax")
    return normalized


def load_topgg_cookies() -> list[list[dict]]:
    raw = os.environ.get("TOPGG_COOKIES_JSON", "")
    if not raw.strip():
        return []
    account_cookies = []
    for index, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line or line == "[]":
            account_cookies.append([])
            continue
        try:
            cookies = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid TOPGG_COOKIES_JSON at line {index}") from exc
        if not isinstance(cookies, list):
            raise ValueError(f"TOPGG_COOKIES_JSON line {index} must be a JSON array")
        account_cookies.append([
            _normalize_cookie(cookie)
            for cookie in cookies
            if cookie.get("domain") in {"top.gg", ".top.gg"}
            and "authjs" in str(cookie.get("name", "")).lower()
        ])
    return account_cookies


def send_notification(message: str) -> None:
    print("\n" + "=" * 45)
    print(message)
    print("=" * 45)
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("ℹ️  TG_BOT_TOKEN / TG_CHAT_ID not set — skip notification.")
        return
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        print("📨 Telegram notification sent." if response.status_code == 200 else f"⚠️  Notification failed: {response.status_code}")
    except Exception as exc:
        dbg(f"Telegram message failed: {type(exc).__name__}")
        print("⚠️  Notification exception; enable DEBUG for error type.")


async def evaluate(tab: Any, expression: str) -> Any:
    remote_object, exception = await tab.send(uc.cdp.runtime.evaluate(
        expression=expression,
        user_gesture=True,
        await_promise=True,
        return_by_value=True,
        allow_unsafe_eval_blocked_by_csp=True,
    ))
    if exception:
        raise RuntimeError("JavaScript evaluation failed")
    return remote_object.value if remote_object else None


async def body_text(tab: Any) -> str:
    return str(await evaluate(tab, "document.body ? document.body.innerText : ''") or "")


async def current_url(tab: Any) -> str:
    return str(await evaluate(tab, "location.href") or "")


async def wait_for_url(tab: Any, needle: str, timeout: int) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if needle in await current_url(tab):
            return True
        await asyncio.sleep(1)
    return False


async def _mark_exact_element(tab: Any, selector: str, texts: list[str], marker: str) -> bool:
    script = f"""(() => {{
        const wanted = {json.dumps(texts)};
        const nodes = [...document.querySelectorAll({json.dumps(selector)})];
        const element = nodes.find(node => wanted.includes((node.textContent || '').trim()));
        if (!element) return false;
        element.setAttribute({json.dumps(marker)}, '1');
        return true;
    }})()"""
    return bool(await evaluate(tab, script))


async def _click_marked(tab: Any, marker: str) -> bool:
    try:
        element = await tab.select(f'[{marker}="1"]', timeout=2)
        await element.scroll_into_view()
        await element.click()
        return True
    except Exception as exc:
        dbg(f"Marked click failed: {type(exc).__name__}")
        return False


async def inject_topgg_cookies(browser: Any, cookies: list[dict]) -> None:
    params = []
    for cookie in cookies:
        same_site = uc.cdp.network.CookieSameSite(cookie["sameSite"])
        expires = uc.cdp.network.TimeSinceEpoch(cookie["expires"]) if cookie.get("expires") else None
        params.append(uc.cdp.network.CookieParam(
            name=cookie["name"],
            value=cookie["value"],
            domain=cookie["domain"],
            path=cookie["path"],
            secure=cookie["secure"],
            http_only=cookie["httpOnly"],
            same_site=same_site,
            expires=expires,
        ))
    if params:
        await browser.cookies.set_all(params)


async def is_topgg_authenticated(tab: Any) -> bool:
    result = await evaluate(tab, """(async () => {
        try {
            const response = await fetch('/api/auth/session', {credentials: 'include'});
            if (!response.ok) return false;
            const session = await response.json();
            return Boolean(session && session.user);
        } catch (_) {
            return false;
        }
    })()""")
    return bool(result)


async def login_with_cookies(tab: Any, cookies: list[dict], bot_ids: list[str]) -> bool:
    if not cookies:
        return False
    print("  → Injecting top.gg Auth.js cookies...")
    await inject_topgg_cookies(tab.browser, cookies)
    await tab.get(f"https://top.gg/bot/{bot_ids[0]}/vote")
    await asyncio.sleep(3)
    authenticated = await is_topgg_authenticated(tab)
    print("  ✅ Authenticated via top.gg cookies" if authenticated else "  ⚠️  Cookie session invalid or expired")
    return authenticated


async def _handle_discord_oauth(tab: Any) -> bool:
    print("  → Handling Discord OAuth dialog...")
    for attempt in range(12):
        if "top.gg" in await current_url(tab):
            return True
        marker = "data-auto-oauth"
        if await _mark_exact_element(tab, "button", ["Authorize", "Authorise"], marker):
            if await _click_marked(tab, marker):
                dbg(f"Authorize clicked (attempt {attempt + 1})")
                return True
        await evaluate(tab, """(() => {
            const nodes = [...document.querySelectorAll('div')]
                .filter(el => el.scrollHeight > el.clientHeight);
            const target = nodes.sort((a, b) => b.scrollHeight - a.scrollHeight)[0];
            if (target) target.scrollTop += 400;
            else window.scrollBy(0, 400);
        })()""")
        await asyncio.sleep(1.5)
    return False


async def discord_oauth_login(tab: Any, token: str, bot_ids: list[str]) -> bool:
    print("  → Injecting Discord token...")
    await tab.get(DISCORD_LOGIN_URL)
    await asyncio.sleep(2)
    await evaluate(tab, f"""(() => {{
        const token = {json.dumps(token)};
        const frame = document.body.appendChild(document.createElement('iframe'));
        if (frame.contentWindow) {{
            frame.contentWindow.localStorage.setItem('token', JSON.stringify(token));
            frame.contentWindow.localStorage.setItem('tokens', JSON.stringify({{"default": token}}));
        }}
        frame.remove();
    }})()""")
    await tab.reload()
    await asyncio.sleep(3)

    print("  → Navigating to top.gg to initiate login...")
    await tab.get(f"https://top.gg/bot/{bot_ids[0]}/vote")
    await asyncio.sleep(3)
    if await is_topgg_authenticated(tab):
        print("  ✅ Already logged into top.gg")
        return True

    marker = "data-auto-login"
    if not await _mark_exact_element(tab, "a,button", ["Login"], marker):
        print("  ❌ Could not find top.gg Login button")
        return False
    if not await _click_marked(tab, marker):
        print("  ❌ Could not click top.gg Login button")
        return False
    if not await wait_for_url(tab, "discord.com/oauth2/authorize", TIMEOUT_OAUTH_SEC):
        print("  ❌ Discord OAuth page did not open")
        return False
    if not await _handle_discord_oauth(tab):
        print("  ❌ Could not authorize top.gg")
        return False
    if not await wait_for_url(tab, "top.gg", TIMEOUT_OAUTH_SEC):
        print("  ❌ OAuth redirect failed")
        return False
    await asyncio.sleep(3)
    authenticated = await is_topgg_authenticated(tab)
    print("  ✅ Logged into top.gg" if authenticated else "  ❌ top.gg session not established")
    return authenticated


async def is_turnstile_present(tab: Any) -> bool:
    return bool(await evaluate(tab, """(() => {
        const body = document.body ? document.body.innerText.toLowerCase() : '';
        if (body.includes('verify you are human') ||
            body.includes('please solve the captcha to continue') ||
            body.includes('let us know you are human')) return true;
        if (document.querySelector('iframe[src*="challenges.cloudflare.com"]')) return true;
        if (document.querySelector('input[name="cf-turnstile-response"]')) return true;
        return Boolean(document.querySelector('.cf-turnstile'));
    })()"""))


async def is_turnstile_solved(tab: Any) -> bool:
    return bool(await evaluate(tab, """(() => {
        const fields = document.querySelectorAll(
            'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
        );
        return [...fields].some(field => field.value && field.value.length > 10);
    })()"""))


async def solve_turnstile(tab: Any) -> bool:
    if await is_turnstile_solved(tab):
        return True
    if not await is_turnstile_present(tab):
        return True
    print("  → Turnstile detected, attempting verification...")
    try:
        await tab.verify_cf()
    except Exception as exc:
        dbg(f"verify_cf failed: {type(exc).__name__}")
    deadline = asyncio.get_running_loop().time() + TIMEOUT_VOTE_SEC
    while asyncio.get_running_loop().time() < deadline:
        if await is_turnstile_solved(tab) or not await is_turnstile_present(tab):
            return True
        await asyncio.sleep(2)
    return False


async def captcha_result(tab: Any, bot_id: str, detail: str) -> dict:
    print(f"  🔒 Interactive CAPTCHA required for {bot_id}")
    path = await error_screenshot(tab, f"screenshots/vote_{bot_id}_captcha.png")
    if path:
        await notify_error_screenshot(bot_id, path, detail)
    return {"bot_id": bot_id, "status": "captcha_required", "detail": detail}


async def wait_for_ad(tab: Any, bot_id: str) -> dict | None:
    deadline = asyncio.get_running_loop().time() + TIMEOUT_VOTE_SEC
    while asyncio.get_running_loop().time() < deadline:
        text = (await body_text(tab)).lower()
        if "you will be able to vote after this ad" not in text:
            return None
        print("  → Ad playing, waiting for completion...")
        await asyncio.sleep(3)
    path = await error_screenshot(tab, f"screenshots/vote_{bot_id}_ad_timeout.png")
    if path:
        await notify_error_screenshot(bot_id, path, "Ad countdown timeout")
    return {"bot_id": bot_id, "status": "error", "detail": "Ad countdown timeout"}


async def mark_vote_button(tab: Any) -> dict:
    return dict(await evaluate(tab, """(() => {
        document.querySelectorAll('[data-auto-vote]').forEach(el => el.removeAttribute('data-auto-vote'));
        const button = [...document.querySelectorAll('button')]
            .find(el => (el.textContent || '').trim() === 'Vote');
        if (!button) return {found: false, disabled: true};
        button.setAttribute('data-auto-vote', '1');
        return {
            found: true,
            disabled: Boolean(button.disabled || button.getAttribute('aria-disabled') === 'true')
        };
    })()""") or {})


async def vote_for_bot(tab: Any, bot_id: str) -> dict:
    print(f"  → Voting for bot {bot_id}...")
    await tab.get(f"https://top.gg/bot/{bot_id}/vote")
    await asyncio.sleep(3)
    text = (await body_text(tab)).lower()

    if "must be logged in" in text or "login to vote" in text:
        dbg("top.gg session not applied yet; reloading once")
        await tab.reload()
        await asyncio.sleep(3)
        text = (await body_text(tab)).lower()

    if "could not be found" in text or "404" in str(await evaluate(tab, "document.title")):
        return {"bot_id": bot_id, "status": "error", "detail": "Vote page 404"}
    if "must be logged in" in text or "login to vote" in text:
        return {"bot_id": bot_id, "status": "auth_failed", "detail": "Not logged into top.gg"}
    if any(marker in text for marker in ("vote again in", "already voted", "come back", "cooldown")):
        print(f"  ⏳ Already voted for {bot_id} (cooldown)")
        return {"bot_id": bot_id, "status": "cooldown", "detail": "Cooldown active"}

    if await is_turnstile_present(tab) and not await solve_turnstile(tab):
        return await captcha_result(tab, bot_id, "Interactive CAPTCHA requires manual completion")

    ad_error = await wait_for_ad(tab, bot_id)
    if ad_error:
        return ad_error

    if await is_turnstile_present(tab) and not await solve_turnstile(tab):
        return await captcha_result(tab, bot_id, "Interactive CAPTCHA requires manual completion")

    deadline = asyncio.get_running_loop().time() + TIMEOUT_VOTE_SEC
    state = {}
    while asyncio.get_running_loop().time() < deadline:
        state = await mark_vote_button(tab)
        if state.get("found") and not state.get("disabled"):
            break
        if await is_turnstile_present(tab) and not await solve_turnstile(tab):
            return await captcha_result(tab, bot_id, "Interactive CAPTCHA requires manual completion")
        await asyncio.sleep(2)
    else:
        path = await error_screenshot(tab, f"screenshots/vote_{bot_id}_no_btn.png")
        if path:
            await notify_error_screenshot(bot_id, path, "Vote button unavailable")
        detail = "Vote button disabled" if state.get("found") else "Vote button not found"
        return {"bot_id": bot_id, "status": "error", "detail": detail}

    print("  → Clicking Vote...")
    if not await _click_marked(tab, "data-auto-vote"):
        return {"bot_id": bot_id, "status": "error", "detail": "Vote button click failed"}
    await asyncio.sleep(5)

    text = (await body_text(tab)).lower()
    if "thanks for voting" in text:
        print(f"  ✅ Successfully voted for {bot_id}")
        return {"bot_id": bot_id, "status": "success", "detail": "Vote successful"}
    if await is_turnstile_present(tab) and not await is_turnstile_solved(tab):
        return await captcha_result(tab, bot_id, "CAPTCHA appeared after clicking Vote")

    await tab.reload()
    await asyncio.sleep(3)
    text = (await body_text(tab)).lower()
    if any(marker in text for marker in (
        "you have already voted", "already voted", "vote again in",
        "can vote again", "thanks for voting", "thank you",
    )):
        print(f"  ✅ Successfully voted for {bot_id}")
        return {"bot_id": bot_id, "status": "success", "detail": "Vote successful"}
    if await is_turnstile_present(tab) and not await is_turnstile_solved(tab):
        return await captcha_result(tab, bot_id, "CAPTCHA present after vote verification")

    path = await error_screenshot(tab, f"screenshots/vote_{bot_id}_uncertain.png")
    if path:
        await notify_error_screenshot(bot_id, path, "Vote result unclear")
    return {"bot_id": bot_id, "status": "uncertain", "detail": "Clicked, result unclear"}


async def start_browser() -> Any:
    last_error = None
    for attempt in range(1, BROWSER_START_RETRIES + 1):
        browser = None
        try:
            browser = await uc.start(
                headless=False,
                sandbox=False,
                browser_executable_path=os.environ.get("CHROME_BIN") or None,
                browser_args=[
                    "--window-size=1280,720",
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            await browser.get("about:blank")
            return browser
        except Exception as exc:
            last_error = exc
            if browser:
                browser.stop()
            dbg(f"Browser startup {attempt}/{BROWSER_START_RETRIES} failed: {type(exc).__name__}")
            if attempt < BROWSER_START_RETRIES:
                await asyncio.sleep(BROWSER_START_RETRY_SEC)
    raise BrowserStartupError(
        f"{type(last_error).__name__}: {safe_exception_detail(last_error)}"
    ) from last_error


async def _run_account(
    _browser: Any,
    token: str,
    bot_ids: list[str],
    account_id: str,
    account_cookies: list[dict] | None = None,
) -> list[dict]:
    browser = await start_browser()
    tab = next(iter(browser))
    results = []
    try:
        authenticated = False
        if account_cookies:
            authenticated = await login_with_cookies(tab, account_cookies, bot_ids)
            if not authenticated:
                print("  → Cookie auth failed, falling back to Discord OAuth...")
                await browser.cookies.clear()
        if not authenticated:
            authenticated = await discord_oauth_login(tab, token, bot_ids)
        if not authenticated:
            return [{
                "bot_id": "all",
                "status": "auth_failed",
                "detail": "Top.gg authentication failed",
                "account_id": account_id,
            }]

        for position, bot_id in enumerate(bot_ids):
            result = await vote_for_bot(tab, bot_id)
            result["account_id"] = account_id
            results.append(result)
            if position < len(bot_ids) - 1:
                await asyncio.sleep(DELAY_BETWEEN_BOTS_SEC)
        return results
    finally:
        browser.stop()
        await asyncio.sleep(1)


async def process_account(
    browser: Any,
    token: str,
    bot_ids: list[str],
    index: int,
    total: int,
    account_cookies: list[dict] | None = None,
) -> list[dict]:
    prefix = f"[{index}/{total}]"
    account_id = account_fingerprint(token)
    pending = list(bot_ids)
    results_by_bot: dict[str, dict] = {}
    last_account_error: dict | None = None
    print(f"\n{'─' * 45}")
    print(f"{prefix} Processing account...")

    for attempt in range(1, MAX_RETRIES + 1):
        if attempt > 1:
            print(f"{prefix} ↺ Retry {attempt}/{MAX_RETRIES} (waiting {RETRY_DELAY_SEC}s)...")
            await asyncio.sleep(RETRY_DELAY_SEC)
        try:
            attempt_results = await _run_account(
                browser, token, pending, account_id, account_cookies
            )
        except Exception as exc:
            if isinstance(exc, BrowserStartupError):
                detail = f"Browser startup failed: {safe_exception_detail(exc)}"
            elif isinstance(exc, TypeError):
                detail = f"TypeError: {safe_exception_detail(exc)}"
            else:
                detail = f"{type(exc).__name__}: transient browser failure"
            last_account_error = {
                "bot_id": "all", "status": "error",
                "detail": detail, "account_id": account_id,
            }
            dbg(f"Account attempt failed: {type(exc).__name__}")
            print(f"{prefix} ❌ Attempt {attempt} failed: {detail}")
            continue

        if attempt_results and attempt_results[0].get("bot_id") == "all":
            last_account_error = attempt_results[0]
            print(f"{prefix} ❌ Authentication attempt {attempt} failed")
            continue
        for result in attempt_results:
            results_by_bot[str(result["bot_id"])] = result
        pending = retryable_bot_ids(attempt_results)
        if not pending:
            return [results_by_bot[bot_id] for bot_id in bot_ids]

    print(f"{prefix} ❌ All {MAX_RETRIES} attempts exhausted")
    if results_by_bot:
        return [results_by_bot[bot_id] for bot_id in bot_ids if bot_id in results_by_bot]
    return [last_account_error or {
        "bot_id": "all", "status": "error",
        "detail": f"Failed after {MAX_RETRIES} retries", "account_id": account_id,
    }]


def build_notification(all_results: list[list[dict]], now: str) -> str:
    lines = ["🗳️ <b>Top.gg Auto Vote Report</b>", f"⏱️ {now}", ""]
    for account_results in all_results:
        if not account_results:
            continue
        account_id = escape(str(account_results[0].get("account_id", "?")))
        lines.append(f"👤 <b>Account {account_id}</b>")
        for result in account_results:
            bot_id = escape(str(result.get("bot_id", "?")))
            status = result.get("status", "?")
            detail = escape(str(result.get("detail", "")))
            icon = {
                "success": "✅", "cooldown": "⏳", "uncertain": "⚠️",
                "captcha_required": "🔒",
            }.get(status, "❌")
            lines.append(f"  {icon} {bot_id}: {detail}")
        lines.append("")
    return "\n".join(lines).strip()


async def main() -> None:
    tokens = load_tokens()
    if not tokens:
        print("❌ No tokens found.\n   Set TOKENS secret (one Discord user token per line).")
        sys.exit(1)

    bot_ids = load_bot_ids()
    all_cookies = load_topgg_cookies()
    now = datetime.now(WIB).strftime("%Y-%m-%d %H:%M WIB")
    total = len(tokens)
    print("🚀 auto-vote-dcbot starting")
    print(f"   Tokens  : {total}")
    print(f"   Cookies : {sum(bool(cookies) for cookies in all_cookies)}/{total} account(s)")
    print(f"   Bots    : {len(bot_ids)}")
    print(f"   Time    : {now}")

    all_results = []
    for index, token in enumerate(tokens, 1):
        cookies = all_cookies[index - 1] if index <= len(all_cookies) else []
        results = await process_account(None, token, bot_ids, index, total, cookies)
        all_results.append(results)
        if index < total:
            await asyncio.sleep(DELAY_BETWEEN_ACCOUNTS_SEC)

    print(f"\n{'=' * 45}")
    print(f"📊 Done — {total} account(s) processed")
    send_notification(build_notification(all_results, now))


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
