#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vycebump 单元测试集 (原生 unittest，零第三方依赖)
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# 将项目根目录加入 sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import (
    format_duration,
    mask_username,
    ms_to_beijing_str,
    load_accounts,
    process_account,
    ONE_DAY_MS,
    ONE_HOUR_MS,
)


class TestFormattingUtils(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual(format_duration(0), "0s")
        self.assertEqual(format_duration(45), "45s")
        self.assertEqual(format_duration(60), "1m 00s")
        self.assertEqual(format_duration(125), "2m 05s")
        self.assertEqual(format_duration(3600), "1h 00m")
        self.assertEqual(format_duration(3665), "1h 01m")
        self.assertEqual(format_duration(86400), "1d 0h")
        self.assertEqual(format_duration(86400 + 3600 * 5), "1d 5h")

    def test_mask_username(self):
        self.assertEqual(mask_username(""), "未知用户")
        self.assertEqual(mask_username("tom"), "tom")
        self.assertEqual(mask_username("dengyie"), "de****ie")
        self.assertEqual(mask_username("admin@vyce.cloud"), "ad****in@vyce.cloud")
        self.assertEqual(mask_username("a@b.com"), "a@b.com")

    def test_ms_to_beijing_str(self):
        self.assertEqual(ms_to_beijing_str(None), "未知")
        self.assertEqual(ms_to_beijing_str(0), "未知")
        # 1700000000000 ms -> 2023-11-15 06:13:20 (UTC+8)
        beijing_str = ms_to_beijing_str(1700000000000)
        self.assertRegex(beijing_str, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class TestAccountLoading(unittest.TestCase):
    @patch.dict(os.environ, {"USERS_JSON": '[{"cookie": "raw_token", "name": "userA"}]', "VYCE_COOKIE": ""})
    def test_load_accounts_from_users_json(self):
        accounts = load_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["name"], "userA")
        self.assertTrue(accounts[0]["cookie"].startswith("__Host-vyce_sid="))
        self.assertEqual(accounts[0]["cookie"], "__Host-vyce_sid=raw_token")

    @patch.dict(os.environ, {"USERS_JSON": "", "VYCE_COOKIE": "token123", "VYCE_USER": "testuser"})
    def test_load_accounts_from_single_env(self):
        accounts = load_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["name"], "testuser")
        self.assertEqual(accounts[0]["cookie"], "__Host-vyce_sid=token123")


class TestCooldownAndRenewalLogic(unittest.TestCase):
    def setUp(self):
        self.account = {"name": "TestUser", "cookie": "__Host-vyce_sid=mock_cookie"}
        self.state = {}
        # 单元测试中绝不启动真实 Chrome 求解 CAPTCHA
        captcha_patcher = patch("app.obtain_captcha_token", return_value=("bc1-test-token", ""))
        self.mock_captcha = captcha_patcher.start()
        self.addCleanup(captcha_patcher.stop)
        # 强制走「脚本直连」回退路径，避免真实浏览器内续期
        browser_patcher = patch("app.renew_with_captcha", return_value=(None, "browser path disabled in tests"))
        self.mock_browser = browser_patcher.start()
        self.addCleanup(browser_patcher.stop)

    @patch("app.send_notification")
    @patch("app.VyceClient")
    def test_process_account_login_failure(self, mock_client_cls, mock_notify):
        client = MagicMock()
        client.get_me.return_value = None
        mock_client_cls.return_value = client

        ok = process_account(self.account, self.state)
        self.assertFalse(ok)
        mock_notify.assert_called_once()

    @patch("app.send_notification")
    @patch("app.VyceClient")
    def test_process_account_in_cooldown(self, mock_client_cls, mock_notify):
        client = MagicMock()
        client.get_me.return_value = {"login": "testuser", "id": "u123"}
        import time
        now_ms = int(time.time() * 1000)
        # 还有 36 小时到期，即 time_left > 24h，处于 Cooldown 状态
        client.get_servers.return_value = [{
            "id": "srv-1",
            "name": "MyVPS",
            "source": "free",
            "planId": "free-1",
            "status": "running",
            "ipv6": "2a01::1",
            "expiresAt": now_ms + int(36 * ONE_HOUR_MS),
        }]
        mock_client_cls.return_value = client

        ok = process_account(self.account, self.state)
        self.assertTrue(ok)
        # 不应调用 renew_server
        client.renew_server.assert_not_called()
        self.assertEqual(self.state["srv-1"]["status"], "cooldown")

    @patch("app.send_notification")
    @patch("app.VyceClient")
    def test_process_account_renewable_success(self, mock_client_cls, mock_notify):
        client = MagicMock()
        client.get_me.return_value = {"login": "testuser", "id": "u123"}
        import time
        now_ms = int(time.time() * 1000)
        # 还有 12 小时到期，进入可续期窗口 (<= 24h)
        server_obj = {
            "id": "srv-2",
            "name": "MyVPS2",
            "source": "free",
            "planId": "free-1",
            "status": "running",
            "ipv6": "2a01::2",
            "expiresAt": now_ms + int(12 * ONE_HOUR_MS),
        }
        client.get_servers.side_effect = [
            [server_obj],
            [dict(server_obj, expiresAt=now_ms + int(48 * ONE_HOUR_MS))],
        ]
        client.renew_server.return_value = (True, 200, "Renewed successfully")
        mock_client_cls.return_value = client

        ok = process_account(self.account, self.state)
        self.assertTrue(ok)
        client.renew_server.assert_called_once_with("srv-2", "bc1-test-token")
        self.assertEqual(self.state["srv-2"]["status"], "success")

    @patch("app.send_notification")
    @patch("app.VyceClient")
    def test_process_account_renewal_fatal_error(self, mock_client_cls, mock_notify):
        client = MagicMock()
        client.get_me.return_value = {"login": "testuser", "id": "u123"}
        import time
        now_ms = int(time.time() * 1000)
        # 还有 10 小时到期，发起续期但后端抛 403 致命错误
        server_obj = {
            "id": "srv-3",
            "name": "MyVPS3",
            "source": "free",
            "planId": "free-1",
            "status": "running",
            "ipv6": "2a01::3",
            "expiresAt": now_ms + int(10 * ONE_HOUR_MS),
        }
        client.get_servers.return_value = [server_obj]
        client.renew_server.return_value = (False, 403, "Forbidden")
        mock_client_cls.return_value = client

        ok = process_account(self.account, self.state)
        self.assertFalse(ok)
        self.assertEqual(self.state["srv-3"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
