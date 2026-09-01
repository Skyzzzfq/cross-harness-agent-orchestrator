from __future__ import annotations

import unittest

from orchestrator.core.sanitize import redact_sensitive


class SanitizeTests(unittest.TestCase):
    """P1-05：持久化前的统一脱敏。"""

    def test_api_key_is_redacted(self) -> None:
        out = redact_sensitive("failed with key sk-abc123456789xyz")
        self.assertNotIn("abc123456789xyz", out)  # 完整 token 不泄漏
        self.assertIn("***", out)

    def test_bearer_token_is_redacted(self) -> None:
        msg = "HTTP 401 Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        out = redact_sensitive(msg)
        self.assertNotIn("eyJhbGci", out)
        self.assertIn("***", out)

    def test_key_value_secret_is_redacted(self) -> None:
        msg = "request failed; api_key=super-secret-value-12345 retry"
        out = redact_sensitive(msg)
        self.assertNotIn("super-secret-value-12345", out)
        self.assertIn("api_key=", out)  # 键名保留，值脱敏

    def test_url_query_credentials_are_redacted(self) -> None:
        msg = "GET https://example.com/v1?model=gpt&api_key=sk-abcdefgh1234"
        out = redact_sensitive(msg)
        self.assertNotIn("sk-abcdefgh1234", out)
        self.assertIn("***", out)

    def test_password_field_is_redacted(self) -> None:
        out = redact_sensitive("auth failed password: hunter2hunter2 retry")
        self.assertNotIn("hunter2hunter2", out)

    def test_plain_text_is_unchanged(self) -> None:
        msg = "codex turn ended with status complete"
        self.assertEqual(redact_sensitive(msg), msg)

    def test_empty_and_none_are_safe(self) -> None:
        self.assertEqual(redact_sensitive(""), "")
        self.assertEqual(redact_sensitive(None), "")


if __name__ == "__main__":
    unittest.main()
