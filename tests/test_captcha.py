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
import time
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

    def test_token_rejection_is_distinguished_from_missing_token(self):
        rejected = [
            '{"error":"invalid_captcha","message":"CAPTCHA verification failed. Please try again."}',
            "CAPTCHA verification failed. Please try again.",
            "invalid_captcha",
        ]
        for msg in rejected:
            with self.subTest(msg=msg):
                self.assertTrue(app.is_captcha_token_rejected(400, msg))
                self.assertTrue(app.is_captcha_error(400, msg))

        # 未提交令牌时服务端提示「请完成验证」，不能算作令牌被拒
        for msg in ("Please complete the CAPTCHA verification.", "captcha required"):
            with self.subTest(msg=msg):
                self.assertFalse(app.is_captcha_token_rejected(400, msg))

    def test_token_rejection_requires_400(self):
        self.assertFalse(app.is_captcha_token_rejected(500, "CAPTCHA verification failed."))
        self.assertFalse(app.is_captcha_token_rejected(400, ""))


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
        self.sleep_patcher = patch("app.time.sleep")
        self.sleep_patcher.start()

    def tearDown(self):
        self.sleep_patcher.stop()

    def _stub_client(self, client_cls, renew_results, refreshed_servers=None):
        client = MagicMock()
        client.get_me.return_value = {"login": "testuser"}
        has_success = any(
            (r[0] if isinstance(r, (tuple, list)) else bool(r))
            for r in renew_results
            if isinstance(r, (tuple, list)) and len(r) > 0
        )
        default_refresh = (
            [dict(self.server, expiresAt=self.now + 48 * app.ONE_HOUR_MS)]
            if has_success
            else [self.server]
        )
        second_servers = refreshed_servers if refreshed_servers is not None else default_refresh
        client.get_servers.side_effect = [
            [self.server],
            second_servers,
            second_servers,
        ]
        client.renew_server.side_effect = renew_results
        client_cls.return_value = client
        return client

    @patch("app.send_notification")
    @patch("app.renew_with_captcha", return_value=(None, "browser path disabled in tests"))
    @patch("app.obtain_captcha_token", return_value=(VALID_TOKEN, ""))
    @patch("app.VyceClient")
    def test_fallback_uses_token_for_direct_renewal(self, client_cls, obtain, browser, notify):
        client = self._stub_client(client_cls, [(True, 200, "Renewed successfully")])
        self.assertTrue(app.process_account(self.account, {}))
        client.renew_server.assert_called_once_with("srv-captcha", VALID_TOKEN)
        self.assertEqual(notify.call_args.kwargs["status_title"], "Vyce VPS 续期成功")

    @patch("app.send_notification")
    @patch("app.renew_with_captcha", return_value=(None, "browser path disabled in tests"))
    @patch("app.obtain_captcha_token", side_effect=[(None, "Chrome 调试端口未就绪"), (VALID_TOKEN, "")])
    @patch("app.VyceClient")
    def test_captcha_rejection_retries_once_with_fresh_token(self, client_cls, obtain, browser, notify):
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
    @patch("app.renew_with_captcha", return_value=(None, "browser path disabled in tests"))
    @patch("app.obtain_captcha_token", return_value=(None, "未找到 Chrome/Chromium，可通过 CHROME_BIN 指定可执行文件路径"))
    @patch("app.VyceClient")
    def test_failure_notification_includes_captcha_reason(self, client_cls, obtain, browser, notify):
        client = self._stub_client(client_cls, [
            (False, 400, "Please complete the CAPTCHA verification."),
            (False, 400, "Please complete the CAPTCHA verification."),
        ])
        self.assertFalse(app.process_account(self.account, {}))
        # 两次尝试都失败后才告警
        self.assertEqual(client.renew_server.call_count, 2)
        notify.assert_called_once()
        self.assertIn("CAPTCHA", notify.call_args.kwargs["error_msg"])
        self.assertIn("CHROME_BIN", notify.call_args.kwargs["error_msg"])

    @patch("app.send_notification")
    @patch("app.renew_with_captcha")
    @patch("app.obtain_captcha_token")
    @patch("app.VyceClient")
    def test_disabled_solver_sends_no_token(self, client_cls, obtain, browser, notify):
        client = self._stub_client(client_cls, [(True, 200, "Renewed successfully")])
        with patch.object(app, "CAPTCHA_ENABLED", False):
            self.assertTrue(app.process_account(self.account, {}))
        obtain.assert_not_called()
        browser.assert_not_called()
        client.renew_server.assert_called_once_with("srv-captcha", None)

    @patch("app.send_notification")
    @patch("app.renew_with_captcha", return_value=((True, 200, "Renewed successfully"), ""))
    @patch("app.obtain_captcha_token")
    @patch("app.VyceClient")
    def test_browser_renewal_result_is_authoritative(self, client_cls, obtain, browser, notify):
        client = self._stub_client(client_cls, [(True, 200, "Renewed successfully")])
        self.assertTrue(app.process_account(self.account, {}))
        # 浏览器内续期成功时不再走脚本直连
        client.renew_server.assert_not_called()
        obtain.assert_not_called()
        self.assertEqual(browser.call_args.args[1], "srv-captcha")
        self.assertEqual(browser.call_args.args[2], self.account["cookie"])
        self.assertEqual(notify.call_args.kwargs["status_title"], "Vyce VPS 续期成功")

    @patch("app.send_notification")
    @patch("app.renew_with_captcha", side_effect=[
        ((False, 400, "CAPTCHA verification failed. Please try again."), ""),
        ((True, 200, "Renewed successfully"), ""),
    ])
    @patch("app.obtain_captcha_token")
    @patch("app.VyceClient")
    def test_browser_renewal_retries_once_on_captcha_error(self, client_cls, obtain, browser, notify):
        client = self._stub_client(client_cls, [(True, 200, "Renewed successfully")])
        self.assertTrue(app.process_account(self.account, {}))
        self.assertEqual(browser.call_count, 2)
        client.renew_server.assert_not_called()

    @patch("app.send_notification")
    @patch("app.renew_with_captcha", return_value=(
        (False, 400, '{"error":"invalid_captcha","message":"CAPTCHA verification failed. Please try again."}'),
        "",
    ))
    @patch("app.obtain_captcha_token")
    @patch("app.VyceClient")
    def test_rejected_token_notification_points_at_server_side(self, client_cls, obtain, browser, notify):
        """令牌已生成却被拒：告警必须指向服务端校验故障，而不是本机环境。"""
        client = self._stub_client(client_cls, [
            (False, 400, "CAPTCHA verification failed."),
            (False, 400, "CAPTCHA verification failed."),
        ])
        self.assertFalse(app.process_account(self.account, {}))
        self.assertEqual(browser.call_count, 2)
        error_msg = notify.call_args.kwargs["error_msg"]
        self.assertIn("invalid_captcha", error_msg)
        self.assertIn("官网手动续期同样报此错", error_msg)
        self.assertNotIn("CHROME_BIN", error_msg)

    @patch("app.send_notification")
    @patch("app.renew_with_captcha", return_value=(None, "browser path disabled in tests"))
    @patch("app.obtain_captcha_token", return_value=(VALID_TOKEN, ""))
    @patch("app.VyceClient")
    def test_obtained_token_clears_earlier_browser_error(self, client_cls, obtain, browser, notify):
        """直连兜底拿到令牌后，不应再把浏览器路径的报错写进告警。"""
        client = self._stub_client(client_cls, [
            (False, 400, "CAPTCHA verification failed."),
            (False, 400, "CAPTCHA verification failed."),
        ])
        self.assertFalse(app.process_account(self.account, {}))
        error_msg = notify.call_args.kwargs["error_msg"]
        self.assertNotIn("browser path disabled in tests", error_msg)

    @patch("app.send_notification")
    @patch("app.renew_with_captcha", return_value=(
        (False, 400, '{"error":"invalid_captcha","message":"CAPTCHA verification failed. Please try again."}'),
        "",
    ))
    @patch("app.obtain_captcha_token")
    @patch("app.VyceClient")
    def test_browser_renewal_succeeds_if_server_expires_updated_despite_400(self, client_cls, obtain, browser, notify):
        """续期接口报 400 invalid_captcha 但服务端实际已延期：二次核验判定成功。"""
        renewed_server = dict(self.server, expiresAt=self.now + 48 * app.ONE_HOUR_MS)
        client = self._stub_client(
            client_cls,
            [(False, 400, "CAPTCHA verification failed.")],
            refreshed_servers=[renewed_server],
        )
        state = {}
        ok = app.process_account(self.account, state)
        self.assertTrue(ok)
        self.assertEqual(state["srv-captcha"]["status"], "success")
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["status_title"], "Vyce VPS 续期成功")

    @patch("app.send_notification")
    @patch("app.renew_with_captcha", return_value=(None, "browser path disabled in tests"))
    @patch("app.obtain_captcha_token", return_value=(VALID_TOKEN, ""))
    @patch("app.VyceClient")
    def test_direct_renewal_succeeds_if_server_expires_updated_despite_400(self, client_cls, obtain, browser, notify):
        """直连提交报 400 但二次核验发现机器到期时间已更新：判定为成功。"""
        renewed_server = dict(self.server, expiresAt=self.now + 48 * app.ONE_HOUR_MS)
        client = self._stub_client(
            client_cls,
            [
                (False, 400, "CAPTCHA verification failed."),
                (False, 400, "CAPTCHA verification failed."),
            ],
            refreshed_servers=[renewed_server],
        )
        state = {}
        ok = app.process_account(self.account, state)
        self.assertTrue(ok)
        self.assertEqual(state["srv-captcha"]["status"], "success")
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["status_title"], "Vyce VPS 续期成功")


class TestRenewOutputParsing(unittest.TestCase):
    def test_parses_renew_json_line(self):
        stdout = 'TOKEN:bc1.abc\nRENEW:{"status":200,"body":"{}"}\n'
        self.assertEqual(app.parse_renew_output(stdout), {"status": 200, "body": "{}"})

    def test_returns_none_when_renew_line_missing_or_broken(self):
        for stdout in ("", "TOKEN:bc1.x\n", 'RENEW:not-json\n', 'RENEW:[1,2]\n'):
            with self.subTest(stdout=stdout):
                self.assertIsNone(app.parse_renew_output(stdout))

    def test_interpret_maps_status_and_body(self):
        self.assertEqual(app.interpret_renew_response(204, ""), (True, 204, "Renewed successfully"))
        self.assertEqual(
            app.interpret_renew_response(200, '{"success":true,"message":"ok"}'),
            (True, 200, "ok"),
        )
        ok, code, msg = app.interpret_renew_response(
            400, '{"error":"CAPTCHA verification failed. Please try again."}'
        )
        self.assertFalse(ok)
        self.assertEqual(code, 400)
        self.assertTrue(app.is_captcha_error(code, msg))

    @patch("app._run_captcha_solver", return_value=(
        'TOKEN:bc1.' + "a" * 40 + '\nRENEW:{"status":400,"body":"{\\"error\\":\\"CAPTCHA verification failed. Please try again.\\"}"}\n',
        "", 0,
    ))
    def test_renew_with_captcha_interprets_browser_result(self, run):
        result, reason = app.renew_with_captcha(None, "srv-1", "__Host-vyce_sid=secret")
        self.assertEqual(reason, "")
        ok, code, msg = result
        self.assertFalse(ok)
        self.assertEqual(code, 400)
        self.assertTrue(app.is_captcha_error(code, msg))
        # 浏览器内续期必须把服务端 ID 与 Cookie 传给求解器
        self.assertEqual(run.call_args.kwargs["server_id"], "srv-1")
        self.assertEqual(run.call_args.kwargs["cookie"], "__Host-vyce_sid=secret")

    @patch("app._run_captcha_solver", return_value=('TOKEN:bc1.' + "a" * 40 + "\n", "", 0))
    def test_renew_with_captcha_without_renew_line_is_unusable(self, run):
        result, reason = app.renew_with_captcha(None, "srv-1", "cookie")
        self.assertIsNone(result)
        self.assertIn("续期结果", reason)

    @patch("app._run_captcha_solver", return_value=("", "未找到 Chrome/Chromium", -1))
    def test_renew_with_captcha_reports_environment_error(self, run):
        result, reason = app.renew_with_captcha(None, "srv-1", "cookie")
        self.assertIsNone(result)
        self.assertIn("Chrome", reason)


class TestStartServerFallback(unittest.TestCase):
    def test_interpret_start_response_mapping(self):
        self.assertEqual(app.interpret_start_response(204, ""), (True, 204, "Server started"))
        self.assertEqual(
            app.interpret_start_response(200, '{"server":{"id":"srv-1","status":"running"}}'),
            (True, 200, "Server started"),
        )
        self.assertEqual(
            app.interpret_start_response(200, '{"success":true,"message":"Started"}'),
            (True, 200, "Started"),
        )
        self.assertEqual(
            app.interpret_start_response(400, '{"error":"bad_request"}'),
            (False, 400, "bad_request"),
        )
        self.assertEqual(
            app.interpret_start_response(200, 'not-json'),
            (False, 200, "Invalid start response JSON"),
        )

    @patch("app.http_request", return_value=(200, {}, '{"server":{"id":"srv-1","status":"running"}}'))
    def test_start_server_request_headers_and_body(self, request):
        client = app.VyceClient("__Host-vyce_sid=secret")
        ok, code, msg = client.start_server("srv-1")
        self.assertTrue(ok)
        self.assertEqual(code, 200)
        self.assertEqual(msg, "Server started")

        kwargs = request.call_args.kwargs
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["data"], b"{}")
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
        self.assertEqual(kwargs["headers"]["Origin"], "https://vyce.cloud")
        self.assertEqual(kwargs["headers"]["Referer"], "https://vyce.cloud/")

    @patch("app.send_notification")
    @patch("app.renew_with_captcha")
    @patch("app.obtain_captcha_token")
    @patch("app.VyceClient")
    def test_stopped_server_prefers_start_api(self, client_cls, obtain, browser, notify):
        """已关机的免费机型优先走 start 接口完成 Renew and start，无需触碰验证码。"""
        now = int(time.time() * 1000)
        stopped_srv = {
            "id": "srv-stopped",
            "name": "Stopped VPS",
            "source": "free",
            "status": "stopped",
            "expiresAt": now - 3600_000,
        }
        started_srv = dict(stopped_srv, status="running", expiresAt=now + 48 * app.ONE_HOUR_MS)

        client = MagicMock()
        client.get_me.return_value = {"login": "testuser"}
        client.get_servers.side_effect = [[stopped_srv], [started_srv]]
        client.start_server.return_value = (True, 200, "Server started")
        client_cls.return_value = client

        state = {}
        ok = app.process_account({"cookie": "secret", "name": "test"}, state)
        self.assertTrue(ok)
        client.start_server.assert_called_once_with("srv-stopped")
        client.renew_server.assert_not_called()
        browser.assert_not_called()
        obtain.assert_not_called()
        self.assertEqual(state["srv-stopped"]["status"], "success")
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["status_title"], "Vyce VPS 续期成功")

    @patch("app.send_notification")
    @patch("app.renew_with_captcha")
    @patch("app.obtain_captcha_token")
    @patch("app.VyceClient")
    def test_expired_server_prefers_start_api(self, client_cls, obtain, browser, notify):
        """存活时间已尽 (time_left <= 0) 的服务器优先走 start 接口。"""
        now = int(time.time() * 1000)
        expired_srv = {
            "id": "srv-expired",
            "name": "Expired VPS",
            "source": "free",
            "status": "running",
            "expiresAt": now - 1000,
        }
        renewed_srv = dict(expired_srv, status="running", expiresAt=now + 48 * app.ONE_HOUR_MS)

        client = MagicMock()
        client.get_me.return_value = {"login": "testuser"}
        client.get_servers.side_effect = [[expired_srv], [renewed_srv]]
        client.start_server.return_value = (True, 200, "Server started")
        client_cls.return_value = client

        state = {}
        ok = app.process_account({"cookie": "secret", "name": "test"}, state)
        self.assertTrue(ok)
        client.start_server.assert_called_once_with("srv-expired")
        client.renew_server.assert_not_called()
        self.assertEqual(state["srv-expired"]["status"], "success")

    @patch("app.send_notification")
    @patch("app.renew_with_captcha", return_value=(
        (False, 400, '{"error":"invalid_captcha","message":"CAPTCHA verification failed. Please try again."}'),
        "",
    ))
    @patch("app.obtain_captcha_token")
    @patch("app.VyceClient")
    def test_failed_captcha_falls_back_to_start_if_server_becomes_stopped(self, client_cls, obtain, browser, notify):
        """验证码续期失败后，若核验到机器已停机，自动触发 start 兜底并判定成功。"""
        now = int(time.time() * 1000)
        srv_running = {
            "id": "srv-fb",
            "name": "Fallback VPS",
            "source": "free",
            "status": "running",
            "expiresAt": now + 3600_000,
        }
        srv_stopped = dict(srv_running, status="stopped")
        srv_revived = dict(srv_running, status="running", expiresAt=now + 48 * app.ONE_HOUR_MS)

        client = MagicMock()
        client.get_me.return_value = {"login": "testuser"}
        client.get_servers.side_effect = [
            [srv_running],  # 首次读取
            [srv_stopped],  # 续期失败后二次查询发现已停机
            [srv_revived],  # start 兜底调用后状态刷新
        ]
        client.start_server.return_value = (True, 200, "Server started")
        client_cls.return_value = client

        state = {}
        ok = app.process_account({"cookie": "secret", "name": "test"}, state)
        self.assertTrue(ok)
        client.start_server.assert_called_once_with("srv-fb")
        self.assertEqual(state["srv-fb"]["status"], "success")
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["status_title"], "Vyce VPS 续期成功")

    @patch("app.send_notification")
    @patch("app.renew_with_captcha", return_value=(
        (False, 400, '{"error":"invalid_captcha","message":"CAPTCHA verification failed. Please try again."}'),
        "",
    ))
    @patch("app.obtain_captcha_token")
    @patch("app.VyceClient")
    def test_critical_alert_sent_when_time_left_under_two_hours(self, client_cls, obtain, browser, notify):
        """存活不足 2 小时且验证码被拒：告警升级为紧急图标与标题。"""
        now = int(time.time() * 1000)
        critical_srv = {
            "id": "srv-crit",
            "name": "Critical VPS",
            "source": "free",
            "status": "running",
            "expiresAt": now + 3600_000,  # 仅剩 1 小时
        }
        client = MagicMock()
        client.get_me.return_value = {"login": "testuser"}
        client.get_servers.side_effect = [[critical_srv], [critical_srv]]
        client_cls.return_value = client

        state = {}
        ok = app.process_account({"cookie": "secret", "name": "test"}, state)
        self.assertFalse(ok)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["status_icon"], "🚨")
        self.assertEqual(notify.call_args.kwargs["status_title"], "Vyce VPS 续期紧急告警 (临近到期)")
        self.assertIn("不足 2 小时", notify.call_args.kwargs["error_msg"])


if __name__ == "__main__":
    unittest.main()
