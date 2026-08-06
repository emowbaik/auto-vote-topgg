import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import vote


class LoaderTests(unittest.TestCase):
    def test_load_tokens_ignores_blank_lines(self):
        with patch.dict(os.environ, {"TOKENS": "first\n\n second \n"}):
            self.assertEqual(vote.load_tokens(), ["first", "second"])

    def test_load_bot_ids_accepts_discord_snowflakes(self):
        with patch.dict(os.environ, {"BOT_IDS": "830530156048285716\n12345678901234567"}):
            self.assertEqual(vote.load_bot_ids(), ["830530156048285716", "12345678901234567"])

    def test_load_bot_ids_rejects_invalid_values(self):
        with patch.dict(os.environ, {"BOT_IDS": "not-a-snowflake"}):
            with self.assertRaisesRegex(ValueError, "expected a 17-20 digit"):
                vote.load_bot_ids()


class ReportingTests(unittest.TestCase):
    def test_account_fingerprint_is_stable_and_hides_token(self):
        token = "sensitive-token-value"
        fingerprint = vote.account_fingerprint(token)
        self.assertEqual(fingerprint, vote.account_fingerprint(token))
        self.assertEqual(len(fingerprint), 8)
        self.assertNotIn(token[:5], fingerprint)

    def test_notification_escapes_dynamic_html(self):
        message = vote.build_notification(
            [[{"account_id": "a&b", "bot_id": "<bot>", "status": "error", "detail": "x < y & z > q"}]],
            "2026-07-28 13:00 WIB",
        )
        self.assertIn("Account a&amp;b", message)
        self.assertIn("&lt;bot&gt;", message)
        self.assertIn("x &lt; y &amp; z &gt; q", message)


class RetryPolicyTests(unittest.TestCase):
    def test_final_statuses_are_not_retryable(self):
        for status in ("success", "cooldown"):
            with self.subTest(status=status):
                self.assertFalse(vote.is_retryable_result({"status": status}))

    def test_transient_statuses_are_retryable(self):
        for status in ("error", "auth_failed", "uncertain"):
            with self.subTest(status=status):
                self.assertTrue(vote.is_retryable_result({"status": status}))

    def test_retryable_bot_ids_excludes_final_and_account_results(self):
        results = [
            {"bot_id": "111", "status": "error"},
            {"bot_id": "222", "status": "success"},
            {"bot_id": "333", "status": "uncertain"},
            {"bot_id": "all", "status": "auth_failed"},
        ]
        self.assertEqual(vote.retryable_bot_ids(results), ["111", "333"])

    def test_captcha_required_is_terminal(self):
        result = {"bot_id": "111", "status": "captcha_required"}
        self.assertFalse(vote.is_retryable_result(result))
        self.assertEqual(vote.retryable_bot_ids([result]), [])


class CookieLoaderTests(unittest.TestCase):
    def test_cookie_lines_match_accounts_and_filter_non_authjs(self):
        first = [
            {"domain": ".top.gg", "name": "__Secure-authjs.session-token", "value": "one", "sameSite": "lax"},
            {"domain": ".top.gg", "name": "_ga", "value": "tracking"},
        ]
        second = [
            {"domain": "top.gg", "name": "__Host-authjs.csrf-token", "value": "two", "sameSite": "no_restriction"},
        ]
        raw = f"{json.dumps(first)}\n[]\n{json.dumps(second)}"
        with patch.dict(os.environ, {"TOPGG_COOKIES_JSON": raw}):
            cookies = vote.load_topgg_cookies()
        self.assertEqual(len(cookies), 3)
        self.assertEqual([cookie["name"] for cookie in cookies[0]], ["__Secure-authjs.session-token"])
        self.assertEqual(cookies[0][0]["sameSite"], "Lax")
        self.assertEqual(cookies[1], [])
        self.assertEqual(cookies[2][0]["sameSite"], "None")

    def test_empty_cookie_secret_returns_empty_list(self):
        with patch.dict(os.environ, {"TOPGG_COOKIES_JSON": ""}):
            self.assertEqual(vote.load_topgg_cookies(), [])

    def test_invalid_cookie_line_reports_account_number(self):
        with patch.dict(os.environ, {"TOPGG_COOKIES_JSON": "[]\nnot-json"}):
            with self.assertRaisesRegex(ValueError, "line 2"):
                vote.load_topgg_cookies()


class RetryOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    @patch("builtins.print")
    @patch("vote.asyncio.sleep", new_callable=AsyncMock)
    @patch("vote._run_account", new_callable=AsyncMock)
    async def test_only_transient_bots_are_retried(self, run_account, _sleep, _print):
        run_account.side_effect = [
            [
                {"bot_id": "111", "status": "success", "detail": "ok", "account_id": "id"},
                {"bot_id": "222", "status": "error", "detail": "temporary", "account_id": "id"},
            ],
            [{"bot_id": "222", "status": "cooldown", "detail": "done", "account_id": "id"}],
        ]

        results = await vote.process_account(object(), "token", ["111", "222"], 1, 1)

        self.assertEqual([result["status"] for result in results], ["success", "cooldown"])
        self.assertEqual(run_account.await_args_list[0].args[2], ["111", "222"])
        self.assertEqual(run_account.await_args_list[1].args[2], ["222"])

    @patch("builtins.print")
    @patch("vote.asyncio.sleep", new_callable=AsyncMock)
    @patch("vote._run_account", new_callable=AsyncMock)
    async def test_auth_failure_retries_account(self, run_account, _sleep, _print):
        run_account.side_effect = [
            [{"bot_id": "all", "status": "auth_failed", "detail": "auth", "account_id": "id"}],
            [{"bot_id": "111", "status": "success", "detail": "ok", "account_id": "id"}],
        ]

        results = await vote.process_account(object(), "token", ["111"], 1, 1)

        self.assertEqual(results[0]["status"], "success")
        self.assertEqual(run_account.await_count, 2)


class ScreenshotPrivacyTests(unittest.IsolatedAsyncioTestCase):
    @patch.object(vote, "SEND_ERROR_SCREENSHOTS", False)
    async def test_error_screenshot_is_disabled_by_default(self):
        tab = AsyncMock()
        self.assertIsNone(await vote.error_screenshot(tab, "screenshots/error.png"))
        tab.save_screenshot.assert_not_awaited()

    @patch.object(vote, "SEND_ERROR_SCREENSHOTS", True)
    async def test_error_screenshot_runs_when_opted_in(self):
        tab = AsyncMock()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "error.png")
            self.assertEqual(await vote.error_screenshot(tab, path), path)
        tab.save_screenshot.assert_awaited_once_with(filename=path, format="png")


if __name__ == "__main__":
    unittest.main()
