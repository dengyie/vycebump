import json
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app


class TestServerApiContract(unittest.TestCase):
    @patch("app.http_request")
    def test_503_is_failure_not_empty_list(self, request):
        request.return_value = (503, {}, '{"error":"maintenance"}')
        client = app.VyceClient("__Host-vyce_sid=secret")
        self.assertIsNone(client.get_servers())
        self.assertEqual(client.last_error, "maintenance")

    @patch("app.http_request")
    def test_network_error_is_failure_not_empty_list(self, request):
        request.return_value = (0, {}, "Request Exception: timed out")
        client = app.VyceClient("__Host-vyce_sid=secret")
        self.assertIsNone(client.get_servers())
        self.assertIn("timed out", client.last_error)

    @patch("app.http_request")
    def test_invalid_json_and_schema_are_failures(self, request):
        client = app.VyceClient("__Host-vyce_sid=secret")
        for body in ("not-json", '{"servers":[{"id":"srv"}]}'):
            request.return_value = (200, {}, body)
            self.assertIsNone(client.get_servers())

    @patch("app.http_request")
    def test_valid_empty_list_is_success(self, request):
        request.return_value = (200, {}, '{"servers":[]}')
        client = app.VyceClient("__Host-vyce_sid=secret")
        self.assertEqual(client.get_servers(), [])


class TestRedirectBoundary(unittest.TestCase):
    def setUp(self):
        self.handler = app.SafeRedirectHandler()
        self.request = app.urllib.request.Request("https://vyce.cloud/api/me")

    def redirect(self, target, code=302):
        return self.handler.redirect_request(self.request, None, code, "Found", {}, target)

    def test_same_origin_https_redirect_is_allowed(self):
        redirected = self.redirect("https://vyce.cloud/api/v2/me")
        self.assertEqual(redirected.full_url, "https://vyce.cloud/api/v2/me")

    def test_cross_host_and_downgrade_redirects_are_rejected(self):
        for target in ("https://attacker.example/collect", "http://vyce.cloud/api/me"):
            with self.subTest(target=target), self.assertRaises(app.urllib.error.HTTPError):
                self.redirect(target)


class TestAccountFailureAndCooldown(unittest.TestCase):
    def setUp(self):
        self.account = {"name": "TestUser", "cookie": "__Host-vyce_sid=secret"}
        self.now = int(time.time() * 1000)
        self.server = {
            "id": "srv-test", "name": "Test VPS", "source": "free", "status": "running",
            "expiresAt": self.now + app.ONE_HOUR_MS,
        }
        # 单元测试中绝不启动真实 Chrome 求解 CAPTCHA
        captcha_patcher = patch("app.obtain_captcha_token", return_value=("bc1-test-token", ""))
        captcha_patcher.start()
        self.addCleanup(captcha_patcher.stop)

    @patch("app.send_tg_message")
    @patch("app.VyceClient")
    def test_server_query_failure_fails_account(self, client_cls, notify):
        client = MagicMock()
        client.get_me.return_value = {"login": "test"}
        client.get_servers.return_value = None
        client.last_error = "HTTP 503"
        client_cls.return_value = client
        self.assertFalse(app.process_account(self.account, {}, proxy=None))
        notify.assert_called_once()

    @patch("app.send_tg_message")
    @patch("app.VyceClient")
    def test_only_documented_429_is_expected_cooldown(self, client_cls, notify):
        client = MagicMock()
        client.get_me.return_value = {"login": "test"}
        client.get_servers.return_value = [self.server]
        client_cls.return_value = client

        # 1. 结构化错误代码
        client.renew_server.return_value = (False, 429, "renewal_cooldown")
        state = {}
        self.assertTrue(app.process_account(self.account, state))
        self.assertEqual(state["srv-test"]["status"], "cooldown_429")
        notify.assert_not_called()

        # 2. 真实服务端自然语言返回: "You can renew once every 24 hours. Please wait 7 more hour(s)."
        client.renew_server.return_value = (False, 429, "You can renew once every 24 hours. Please wait 7 more hour(s).")
        state = {}
        self.assertTrue(app.process_account(self.account, state))
        self.assertEqual(state["srv-test"]["status"], "cooldown_429")
        # 验证等待时间是否被正确解析计算 (7 小时后)
        self.assertAlmostEqual(state["srv-test"]["next_renewal_ts"], int(time.time() * 1000) + 7 * app.ONE_HOUR_MS, delta=2000)
        notify.assert_not_called()

        # 3. 非冷却相关的其他 429 频率限制依然被判定为异常并告警
        client.renew_server.return_value = (False, 429, "rate_limited")
        state = {}
        self.assertFalse(app.process_account(self.account, state))
        self.assertEqual(state["srv-test"]["status"], "failed")
        notify.assert_called_once()

    @patch("app.send_tg_message")
    @patch("app.VyceClient")
    def test_accepted_renewal_with_failed_refresh_is_unverified_failure(self, client_cls, notify):
        client = MagicMock()
        client.get_me.return_value = {"login": "test"}
        client.get_servers.side_effect = [[self.server], None]
        client.last_error = "HTTP 503"
        client.renew_server.return_value = (True, 200, "Renewed successfully")
        client_cls.return_value = client
        state = {}
        self.assertFalse(app.process_account(self.account, state))
        self.assertEqual(state["srv-test"]["status"], "renewed_unverified")
        notify.assert_called_once()


class TestOutputRedaction(unittest.TestCase):
    def test_sensitive_values_are_redacted(self):
        value = "__Host-vyce_sid=abc123 Bearer token123 https://user:pass@example.com"
        safe = app.safe_error_text(value)
        for secret in ("abc123", "token123", "user:pass"):
            self.assertNotIn(secret, safe)

    @patch("app.http_request", return_value=(200, {}, "{}"))
    def test_telegram_dynamic_content_is_html_escaped(self, request):
        with patch.object(app, "TG_BOT_TOKEN", "bot"), patch.object(app, "TG_CHAT_ID", "chat"):
            app.send_tg_message("!", "<bad>", "user", error_msg="<script>")
        payload = json.loads(request.call_args.kwargs["data"])
        self.assertIn("&lt;bad&gt;", payload["text"])
        self.assertIn("&lt;script&gt;", payload["text"])
        self.assertNotIn("<script>", payload["text"])


if __name__ == "__main__":
    unittest.main()
