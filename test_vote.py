import os
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
