#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bestcap CAPTCHA 相关单元测试。

覆盖令牌提取、续期请求体、求解器前置条件与失败重试逻辑，
所有用例都不启动真实 Chrome / Node。
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app

VALID_TOKEN = "bc1." + "a" * 40


class TestCaptchaErrorDetection(unittest.TestCase):
    def test_server_captcha_message_is_detected(self):
        for msg in (
            "Please complete the CAPTCHA verification.",
            "captcha required",
            "CAPTCHA_FAILED",
        ):
            with self.subTest(msg=msg):
                self.assertTrue(app.is_captcha_error(400, msg))

    def test_other_failures_are_not_captcha(self):
        cases = [
            (400, "renewal_nothing_to_do"),
            (401, "unauthenticated"),
            (429, "renewal_cooldown"),
            (500, "Please complete the CAPTCHA verification."),
            (400, ""),
        ]
        for code, msg in cases:
            with self.subTest(code=code, msg=msg):
                self.assertFalse(app.is_captcha_error(code, msg))


class TestSolverOutputParsing(unittest.TestCase):
    def test_extracts_token_from_noisy_stdout(self):
        stdout = "\n".join([
            "[solver] booting",
            "TOKEN:" + VALID_TOKEN,
            "some trailing noise",
        ])
        self.assertEqual(app.parse_solver_output(stdout), VALID_TOKEN)

    def test_rejects_malformed_tokens(self):
        for stdout in ("", "TOKEN:short", "TOKEN:xyz" + "a" * 40, "no token here"):
            with self.subTest(stdout=stdout):
                self.assertIsNone(app.parse_solver_output(stdout))

    def test_none_input_is_safe(self):
        self.assertIsNone(app.parse_solver_output(None))


class TestRenewRequestBody(unittest.TestCase):
    @patch("app.http_request", return_value=(200, {}, '{"success":true}'))
    def test_token_is_sent_as_json_body(self, request):
        client = app.VyceClient("__Host-vyce_sid=secret")
        ok, code, _ = client.renew_server("srv-1", VALID_TOKEN)
        self.assertTrue(ok)
        self.assertEqual(code, 200)

        kwargs = request.call_args.kwargs
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(json.loads(kwargs["data"]), {"captchaResponse": VALID_TOKEN})
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")

    @patch("app.http_request", return_value=(200, {}, '{"success":true}'))
    def test_without_token_body_stays_empty(self, request):
        client = app.VyceClient("__Host-vyce_sid=secret")
        ok, _, _ = client.renew_server("srv-1")
        self.assertTrue(ok)
        self.assertIsNone(request.call_args.kwargs["data"])
        self.assertNotIn("Content-Type", request.call_args.kwargs["headers"])

    @patch("app.http_request", return_value=(400, {}, '{"error":"Please complete the CAPTCHA verification."}'))
    def test_captcha_rejection_is_reported(self, request):
        client = app.VyceClient("__Host-vyce_sid=secret")
        ok, code, msg = client.renew_server("srv-1", VALID_TOKEN)
        self.assertFalse(ok)
        self.assertEqual(code, 400)
        self.assertTrue(app.is_captcha_error(code, msg))


class TestObtainCaptchaToken(unittest.TestCase):
    def setUp(self):
        self.sitekey_patcher = patch("app.fetch_captcha_sitekey", return_value="bc_site_test")
        self.sitekey_patcher.start()
        self.addCleanup(self.sitekey_patcher.stop)

    def test_disabled_mode_does_not_touch_browser(self):
        with patch.object(app, "CAPTCHA_ENABLED", False), \
                patch("app.find_chrome") as find_chrome:
            token, reason = app.obtain_captcha_token(None)
        self.assertIsNone(token)
        self.assertIn("关闭", reason)
        find_chrome.assert_not_called()

    def test_missing_chrome_reports_actionable_reason(self):
        with patch.object(app, "CAPTCHA_ENABLED", True), \
                patch("app.find_chrome", return_value=None):
            token, reason = app.obtain_captcha_token(None)
        self.assertIsNone(token)
        self.assertIn("CHROME_BIN", reason)

    def test_missing_node_reports_actionable_reason(self):
        with patch.object(app, "CAPTCHA_ENABLED", True), \
                patch("app.find_chrome", return_value="/usr/bin/google-chrome"), \
                patch("app.shutil.which", return_value=None):
            token, reason = app.obtain_captcha_token(None)
        self.assertIsNone(token)
        self.assertIn("node", reason)

    def test_missing_solver_script_reports_reason(self):
        with patch.object(app, "CAPTCHA_ENABLED", True), \
                patch("app.find_chrome", return_value="/usr/bin/google-chrome"), \
                patch("app.shutil.which", return_value="/usr/bin/node"), \
                patch("app.os.path.isfile", return_value=False):
            token, reason = app.obtain_captcha_token(None)
        self.assertIsNone(token)
        self.assertIn("captcha_solver.mjs", reason)

    def _run_success_path(self, stdout, stderr="", returncode=0):
        proc = MagicMock()
        proc.poll.return_value = None
        completed = MagicMock(stdout=stdout, stderr=stderr, returncode=returncode)
        with patch.object(app, "CAPTCHA_ENABLED", True), \
                patch("app.find_chrome", return_value="/usr/bin/google-chrome"), \
                patch("app.shutil.which", return_value="/usr/bin/node"), \
                patch("app.os.path.isfile", return_value=True), \
                patch("app._free_tcp_port", return_value=19444), \
                patch("app.tempfile.mkdtemp", return_value="/tmp/vycebump-test"), \
                patch("app.subprocess.Popen", return_value=proc) as popen, \
                patch("app._wait_for_cdp", return_value=True), \
                patch("app.subprocess.run", return_value=completed) as run, \
                patch("app._stop_process") as stop, \
                patch("app.shutil.rmtree") as rmtree:
            result = app.obtain_captcha_token("http://127.0.0.1:8080")
        return result, popen, run, stop, rmtree

    def test_success_returns_token_and_cleans_up(self):
        (token, reason), popen, run, stop, rmtree = self._run_success_path(
            "TOKEN:" + VALID_TOKEN + "\n"
        )
        self.assertEqual(token, VALID_TOKEN)
        self.assertEqual(reason, "")
        # 求解脚本收到 CDP 地址与 sitekey
        argv = run.call_args.args[0]
        self.assertEqual(argv[1], app.CAPTCHA_SOLVER_SCRIPT)
        self.assertEqual(argv[2], "http://127.0.0.1:19444")
        self.assertEqual(argv[3], "bc_site_test")
        # Chrome 进程与临时 profile 都被回收
        stop.assert_called_once()
        rmtree.assert_called_once_with("/tmp/vycebump-test", ignore_errors=True)
        # 代理被透传给 Chrome
        chrome_args = " ".join(popen.call_args.args[0])
        self.assertIn("--proxy-server=http://127.0.0.1:8080", chrome_args)

    def test_solver_failure_surfaces_last_stderr_line(self):
        (token, reason), *_ = self._run_success_path(
            "", stderr="line one\nERROR: verify rejected\n", returncode=1
        )
        self.assertIsNone(token)
        self.assertIn("verify rejected", reason)

    def test_cdp_not_ready_is_reported(self):
        with patch.object(app, "CAPTCHA_ENABLED", True), \
                patch("app.find_chrome", return_value="/usr/bin/google-chrome"), \
                patch("app.shutil.which", return_value="/usr/bin/node"), \
                patch("app.os.path.isfile", return_value=True), \
                patch("app._free_tcp_port", return_value=19444), \
                patch("app.tempfile.mkdtemp", return_value="/tmp/vycebump-test"), \
                patch("app.subprocess.Popen", return_value=MagicMock()), \
                patch("app._wait_for_cdp", return_value=False), \
                patch("app._stop_process"), \
                patch("app.shutil.rmtree"):
            token, reason = app.obtain_captcha_token(None)
        self.assertIsNone(token)
        self.assertIn("调试端口", reason)


class TestRenewalCaptchaIntegration(unittest.TestCase):
    def setUp(self):
        self.account = {"name": "TestUser", "cookie": "__Host-vyce_sid=secret"}
        import time
        self.now = int(time.time() * 1000)
        self.server = {
            "id": "srv-captcha",
            "name": "Captcha VPS",
            "source": "free",
            "status": "running",
            "expiresAt": self.now + 10 * app.ONE_HOUR_MS,
        }

    def _stub_client(self, client_cls, renew_results):
        client = MagicMock()
        client.get_me.return_value = {"login": "testuser"}
        client.get_servers.side_effect = [
            [self.server],
            [dict(self.server, expiresAt=self.now + 48 * app.ONE_HOUR_MS)],
        ]
        client.renew_server.side_effect = renew_results
        client_cls.return_value = client
        return client

    @patch("app.send_notification")
    @patch("app.obtain_captcha_token", return_value=(VALID_TOKEN, ""))
    @patch("app.VyceClient")
    def test_token_is_used_for_renewal(self, client_cls, obtain, notify):
        client = self._stub_client(client_cls, [(True, 200, "Renewed successfully")])
        self.assertTrue(app.process_account(self.account, {}))
        client.renew_server.assert_called_once_with("srv-captcha", VALID_TOKEN)
        self.assertEqual(notify.call_args.kwargs["status_title"], "Vyce VPS 续期成功")

    @patch("app.send_notification")
    @patch("app.obtain_captcha_token", side_effect=[(None, "Chrome 调试端口未就绪"), (VALID_TOKEN, "")])
    @patch("app.VyceClient")
    def test_captcha_rejection_retries_once_with_fresh_token(self, client_cls, obtain, notify):
        client = self._stub_client(client_cls, [
            (False, 400, "Please complete the CAPTCHA verification."),
            (True, 200, "Renewed successfully"),
        ])
        self.assertTrue(app.process_account(self.account, {}))
        self.assertEqual(client.renew_server.call_count, 2)
        # 第一次无令牌失败，第二次带新令牌成功
        self.assertEqual(client.renew_server.call_args_list[0].args, ("srv-captcha", None))
        self.assertEqual(client.renew_server.call_args_list[1].args, ("srv-captcha", VALID_TOKEN))

    @patch("app.send_notification")
    @patch("app.obtain_captcha_token", return_value=(None, "未找到 Chrome/Chromium，可通过 CHROME_BIN 指定可执行文件路径"))
    @patch("app.VyceClient")
    def test_failure_notification_includes_captcha_reason(self, client_cls, obtain, notify):
        client = self._stub_client(client_cls, [(False, 400, "Please complete the CAPTCHA verification.")])
        self.assertFalse(app.process_account(self.account, {}))
        self.assertEqual(client.renew_server.call_count, 1)
        notify.assert_called_once()
        self.assertIn("CAPTCHA", notify.call_args.kwargs["error_msg"])
        self.assertIn("CHROME_BIN", notify.call_args.kwargs["error_msg"])

    @patch("app.send_notification")
    @patch("app.obtain_captcha_token")
    @patch("app.VyceClient")
    def test_disabled_solver_sends_no_token(self, client_cls, obtain, notify):
        client = self._stub_client(client_cls, [(True, 200, "Renewed successfully")])
        with patch.object(app, "CAPTCHA_ENABLED", False):
            self.assertTrue(app.process_account(self.account, {}))
        obtain.assert_not_called()
        client.renew_server.assert_called_once_with("srv-captcha", None)


if __name__ == "__main__":
    unittest.main()
