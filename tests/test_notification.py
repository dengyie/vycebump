#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通知层测试: 消息构建 / 渠道分发 / 邮件 SMTP 边界 (原生 unittest，零第三方依赖)"""

import email
import json
import os
import smtplib
import ssl
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app


class TestBuildNotification(unittest.TestCase):
    def test_plain_and_html_share_same_fields(self):
        content = app.build_notification(
            "🎉",
            "Vyce VPS 续期成功",
            "someone@example.com",
            details={
                "server_name": "vps-mini",
                "plan_id": "free",
                "os": "debian",
                "time_left_str": "1d 12h",
                "next_renewal_str": "2026-10-21 00:27:23",
            },
        )
        for keyword in ("vps-mini", "free", "1d 12h", "2026-10-21 00:27:23", "so****ne@example.com"):
            self.assertIn(keyword, content["plain"])
            self.assertIn(keyword, content["html"])

    def test_error_message_is_html_escaped(self):
        content = app.build_notification("❌", "失败", "user", error_msg="<script>alert(1)</script>")
        self.assertNotIn("<script>", content["html"])
        self.assertIn("&lt;script&gt;", content["html"])
        # 纯文本保持原样
        self.assertIn("<script>alert(1)</script>", content["plain"])

    def test_missing_details_only_render_account_and_time(self):
        content = app.build_notification("❌", "Cookie 失效", "user")
        self.assertNotIn("服务器", content["plain"])
        self.assertIn("报告时间", content["plain"])


class TestNotificationDispatch(unittest.TestCase):
    def test_dispatch_fans_out_to_all_channels_when_notify_on_success(self):
        with patch.object(app, "NOTIFY_ON_SUCCESS", True), \
                patch("app.send_tg_message") as mock_tg, \
                patch("app.send_email_message") as mock_email, \
                patch("app.send_onemail_message") as mock_onemail:
            app.send_notification("🎉", "续期成功", "user", details={"server_name": "vps-mini"}, proxy="http://127.0.0.1:8080")
        expected = ("🎉", "续期成功", "user", {"server_name": "vps-mini"}, "", "http://127.0.0.1:8080")
        mock_tg.assert_called_once_with(*expected)
        mock_email.assert_called_once_with(*expected)
        mock_onemail.assert_called_once_with(*expected)

    def test_dispatch_silences_success_by_default(self):
        with patch.object(app, "NOTIFY_ON_SUCCESS", False), \
                patch("app.send_tg_message") as mock_tg, \
                patch("app.send_email_message") as mock_email:
            app.send_notification("🎉", "续期成功", "user", details={"server_name": "vps-mini"})
        mock_tg.assert_not_called()
        mock_email.assert_not_called()

    def test_failure_delegates_to_agent_and_suppresses_tg(self):
        with patch("app.delegate_alert_to_agent", return_value=True) as mock_agent, \
                patch("app.send_tg_message") as mock_tg, \
                patch("app.send_email_message") as mock_email, \
                patch("app.send_onemail_message") as mock_onemail:
            app.send_notification("❌", "续期失败", "user", error_msg="HTTP 500")
        mock_agent.assert_called_once()
        mock_tg.assert_not_called()
        mock_email.assert_not_called()
        mock_onemail.assert_not_called()

    def test_failure_falls_back_to_tg_when_agent_delegation_fails(self):
        with patch("app.delegate_alert_to_agent", return_value=False) as mock_agent, \
                patch("app.send_tg_message") as mock_tg, \
                patch("app.send_email_message") as mock_email, \
                patch("app.send_onemail_message") as mock_onemail:
            app.send_notification("❌", "续期失败", "user", error_msg="HTTP 500")
        mock_agent.assert_called_once()
        mock_tg.assert_called_once()
        mock_email.assert_called_once()
        mock_onemail.assert_called_once()

    def test_agent_delegation_http_payload(self):
        with patch.object(app, "AUTONEXUS_AGENT_ENABLED", True), \
                patch.object(app, "AUTONEXUS_ALERT_TOKEN", "test-token"), \
                patch.object(app, "AUTONEXUS_ALERT_URL", "https://monitor.mangoqwq.com/api/v1/alerts/ingest"), \
                patch("app.http_request", return_value=(200, {}, json.dumps({"success": True}))) as mock_http:
            res = app.delegate_alert_to_agent("❌", "续期失败", "myuser@example.com", error_msg="HTTP 500", is_critical=True)
            self.assertTrue(res)
            mock_http.assert_called_once()
            _, kwargs = mock_http.call_args
            req_data = json.loads(kwargs["data"].decode("utf-8"))
            self.assertEqual(req_data["source"], "vycebump")
            self.assertEqual(req_data["severity"], "CRITICAL")
            self.assertEqual(req_data["rule"], "vyce_renew_failed")
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-token")

    def test_build_notification_subject_shared(self):
        content = app.build_notification("🎉", "续期成功", "someone@example.com")
        self.assertEqual(content["subject"], "[Vycebump] 🎉 续期成功 - so****ne@example.com")

    def test_process_account_uses_unified_entrypoint(self):
        # 保证未来的续期分支不会被改回单渠道调用
        import inspect
        source = inspect.getsource(app.process_account)
        self.assertNotIn("send_tg_message(", source)
        self.assertNotIn("send_email_message(", source)
        self.assertIn("send_notification(", source)


class TestEmailChannel(unittest.TestCase):
    def setUp(self):
        # 每个用例在干净的配置下运行，避免宿主环境变量串扰
        for name in ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO", "EMAIL_SUBJECT_PREFIX"):
            self.addCleanup(restore_env, name, os.environ.get(name))
            os.environ.pop(name, None)
        reload_app_config()

    def test_skipped_without_host_or_recipient(self):
        with patch("app.smtplib.SMTP") as mock_smtp:
            app.send_email_message("🎉", "续期成功", "user")  # SMTP_HOST 为空
            mock_smtp.assert_not_called()

        os.environ["SMTP_HOST"] = "smtp.example.com"
        with patch("app.smtplib.SMTP") as mock_smtp:
            app.send_email_message("🎉", "续期成功", "user")  # EMAIL_TO 为空
            mock_smtp.assert_not_called()

    def test_skipped_with_username_but_no_password(self):
        os.environ.update({
            "SMTP_HOST": "smtp.example.com",
            "SMTP_USERNAME": "notify@mangoqwq.com",
            "EMAIL_TO": "notify@mangoqwq.com",
        })
        reload_app_config()
        with patch("app.smtplib.SMTP") as mock_smtp:
            app.send_email_message("🎉", "续期成功", "user")
            mock_smtp.assert_not_called()

    def test_starttls_on_default_port_587(self):
        os.environ.update({
            "SMTP_HOST": "smtp.example.com",
            "SMTP_USERNAME": "notify@mangoqwq.com",
            "SMTP_PASSWORD": "secret",
            "EMAIL_TO": "a@example.com, b@example.com",
        })
        reload_app_config()
        with patch("app.smtplib.SMTP") as mock_smtp_cls:
            instance = MagicMock()
            mock_smtp_cls.return_value.__enter__.return_value = instance
            app.send_email_message("🎉", "续期成功", "someone@example.com")
        mock_smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=15)
        instance.starttls.assert_called_once()
        instance.login.assert_called_once_with("notify@mangoqwq.com", "secret")
        sent = instance.send_message.call_args[0][0]
        self.assertEqual(sent["To"], "a@example.com, b@example.com")
        self.assertIn("so****ne@example.com", sent["Subject"])

    def test_implicit_tls_on_port_465(self):
        os.environ.update({
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "465",
            "SMTP_USERNAME": "notify@mangoqwq.com",
            "SMTP_PASSWORD": "secret",
            "EMAIL_TO": "a@example.com",
        })
        reload_app_config()
        with patch("app.smtplib.SMTP_SSL") as mock_smtp_cls:
            instance = MagicMock()
            mock_smtp_cls.return_value.__enter__.return_value = instance
            app.send_email_message("🎉", "续期成功", "user")
        mock_smtp_cls.assert_called_once_with("smtp.example.com", 465, timeout=15)
        instance.login.assert_called_once_with("notify@mangoqwq.com", "secret")

    def test_anonymous_relay_without_credentials(self):
        os.environ.update({
            "SMTP_HOST": "smtp.example.com",
            "EMAIL_TO": "a@example.com",
        })
        reload_app_config()
        with patch("app.smtplib.SMTP") as mock_smtp_cls:
            instance = MagicMock()
            mock_smtp_cls.return_value.__enter__.return_value = instance
            app.send_email_message("🎉", "续期成功", "user")
        instance.login.assert_not_called()

    def test_smtp_failure_does_not_raise(self):
        os.environ.update({
            "SMTP_HOST": "smtp.example.com",
            "SMTP_USERNAME": "notify@mangoqwq.com",
            "SMTP_PASSWORD": "secret",
            "EMAIL_TO": "a@example.com",
        })
        reload_app_config()
        with patch("app.smtplib.SMTP", side_effect=smtplib.SMTPException("relay denied")):
            # 不应抛异常中断续期主流程
            app.send_email_message("🎉", "续期成功", "user")

    def test_email_message_structure(self):
        os.environ.update({
            "SMTP_HOST": "smtp.example.com",
            "SMTP_USERNAME": "notify@mangoqwq.com",
            "SMTP_PASSWORD": "secret",
            "EMAIL_TO": "notify@mangoqwq.com",
            "EMAIL_SUBJECT_PREFIX": "[Vycebump]",
        })
        reload_app_config()
        captured = {}

        def fake_send(msg):
            captured["msg"] = msg

        with patch("app.smtplib.SMTP") as mock_smtp_cls:
            instance = MagicMock()
            mock_smtp_cls.return_value.__enter__.return_value = instance
            instance.send_message.side_effect = fake_send
            app.send_email_message("🎉", "续期成功", "someone@example.com", details={"server_name": "vps-mini"})

        msg = captured["msg"]
        self.assertIsInstance(msg, email.message.EmailMessage)
        self.assertEqual(msg["From"], "notify@mangoqwq.com")
        self.assertIn("[Vycebump]", msg["Subject"])
        self.assertTrue(msg.is_multipart())
        parts = list(msg.walk())
        content_types = {part.get_content_type() for part in parts}
        self.assertIn("text/plain", content_types)
        self.assertIn("text/html", content_types)


def reload_app_config():
    """重新读取模块级 SMTP/EMAIL 配置常量，复用 app 的解析函数（单一事实来源）。"""
    app.SMTP_HOST = os.environ.get("SMTP_HOST") or ""
    app.SMTP_PORT = app._parse_smtp_port(os.environ.get("SMTP_PORT"))
    app.SMTP_USERNAME = os.environ.get("SMTP_USERNAME") or ""
    app.SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD") or ""
    app.EMAIL_FROM = os.environ.get("EMAIL_FROM") or ""
    app.EMAIL_TO = app._parse_email_list(os.environ.get("EMAIL_TO"))
    app.EMAIL_SUBJECT_PREFIX = os.environ.get("EMAIL_SUBJECT_PREFIX") or "[Vycebump]"


class TestConfigParsing(unittest.TestCase):
    """配置解析健壮性: 非法值不得中断模块加载（P1 回归护栏）"""

    def test_invalid_smtp_port_falls_back_to_default(self):
        self.addCleanup(restore_env, "SMTP_PORT", os.environ.get("SMTP_PORT"))
        # 非法值回退 0 且不抛异常（模块导入路径不得因可选配置崩溃）
        os.environ["SMTP_PORT"] = "465/587（SSL）"
        reload_app_config()
        self.assertEqual(app.SMTP_PORT, 0)
        # 合法值正常解析；模块级常量与解析函数结果一致
        os.environ["SMTP_PORT"] = "465"
        reload_app_config()
        self.assertEqual(app.SMTP_PORT, 465)
        self.assertEqual(app._parse_smtp_port("465"), app.SMTP_PORT)


def restore_env(name, value):
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


class TestOneMailChannel(unittest.TestCase):
    """One-Mail HTTP 发信渠道 (ONEMAIL_* 环境变量)"""

    def setUp(self):
        for name in ("ONEMAIL_BASE_URL", "ONEMAIL_MODE", "ONEMAIL_CREDENTIAL", "ONEMAIL_FROM_NAME", "EMAIL_FROM", "EMAIL_TO"):
            self.addCleanup(restore_env, name, os.environ.get(name))
            os.environ.pop(name, None)
        reload_onemail_config()

    def test_skipped_without_base_url_credential_or_recipient(self):
        with patch("app.http_request", return_value=(200, {}, '{"status":"ok"}')) as mock_req:
            app.send_onemail_message("🎉", "续期成功", "user")  # 全空
            os.environ["ONEMAIL_BASE_URL"] = "https://mail.example.com"
            reload_onemail_config()
            app.send_onemail_message("🎉", "续期成功", "user")  # 缺 credential
            os.environ["ONEMAIL_CREDENTIAL"] = "jwt-or-password"
            reload_onemail_config()
            app.send_onemail_message("🎉", "续期成功", "user")  # 缺 EMAIL_TO
            mock_req.assert_not_called()
            os.environ["EMAIL_TO"] = "me@example.com"
            reload_onemail_config()
            app.send_onemail_message("🎉", "续期成功", "user")
            mock_req.assert_called_once()

    def test_user_mode_posts_address_jwt_send_mail(self):
        os.environ.update({
            "ONEMAIL_BASE_URL": "https://mail.example.com/",
            "ONEMAIL_CREDENTIAL": "addr-jwt-token",
            "EMAIL_TO": "me@example.com",
        })
        reload_onemail_config()
        with patch("app.http_request", return_value=(200, {}, '{"status":"ok"}')) as mock_req:
            app.send_onemail_message("🎉", "续期成功", "someone@example.com", details={"server_name": "vps-mini"})
        args, kwargs = mock_req.call_args
        self.assertEqual(args[0], "https://mail.example.com/api/send_mail")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer addr-jwt-token")
        body = json.loads(kwargs["data"])
        self.assertEqual(body["to_mail"], "me@example.com")
        self.assertEqual(body["subject"].split(" ")[0], "[Vycebump]")
        self.assertTrue(body["is_html"])

    def test_admin_mode_uses_x_admin_auth_and_from(self):
        os.environ.update({
            "ONEMAIL_BASE_URL": "https://mail.example.com",
            "ONEMAIL_MODE": "admin",
            "ONEMAIL_CREDENTIAL": "admin-password",
            "EMAIL_FROM": "notify@mangoqwq.com",
            "EMAIL_TO": "me@example.com",
        })
        reload_onemail_config()
        with patch("app.http_request", return_value=(200, {}, '{"status":"ok"}')) as mock_req:
            app.send_onemail_message("❌", "续期失败", "user")
        args, kwargs = mock_req.call_args
        self.assertEqual(args[0], "https://mail.example.com/admin/send_mail")
        self.assertEqual(kwargs["headers"]["x-admin-auth"], "admin-password")
        body = json.loads(kwargs["data"])
        self.assertEqual(body["from_mail"], "notify@mangoqwq.com")

    def test_admin_mode_without_from_is_skipped(self):
        os.environ.update({
            "ONEMAIL_BASE_URL": "https://mail.example.com",
            "ONEMAIL_MODE": "admin",
            "ONEMAIL_CREDENTIAL": "admin-password",
            "EMAIL_TO": "me@example.com",
        })
        reload_onemail_config()
        with patch("app.http_request") as mock_req:
            app.send_onemail_message("🎉", "续期成功", "user")
            mock_req.assert_not_called()

    def test_api_failure_does_not_raise(self):
        os.environ.update({
            "ONEMAIL_BASE_URL": "https://mail.example.com",
            "ONEMAIL_CREDENTIAL": "jwt",
            "EMAIL_TO": "me@example.com",
        })
        reload_onemail_config()
        with patch("app.http_request", return_value=(403, {}, "forbidden")):
            app.send_onemail_message("🎉", "续期成功", "user")  # 只打警告，不抛异常

    def test_multi_recipient_sends_each_address(self):
        os.environ.update({
            "ONEMAIL_BASE_URL": "https://mail.example.com",
            "ONEMAIL_CREDENTIAL": "jwt",
            "EMAIL_TO": "a@example.com, b@example.com",
        })
        reload_onemail_config()
        with patch("app.http_request", return_value=(200, {}, '{"status":"ok"}')) as mock_req:
            app.send_onemail_message("🎉", "续期成功", "user")
        self.assertEqual(mock_req.call_count, 2)
        tos = [json.loads(call.kwargs["data"])["to_mail"] for call in mock_req.call_args_list]
        self.assertEqual(tos, ["a@example.com", "b@example.com"])

    def test_subject_not_leaked_when_admin_mode_missing_from(self):
        # admin 模式缺 EMAIL_FROM 时跳过且不发任何请求
        os.environ.update({
            "ONEMAIL_BASE_URL": "https://mail.example.com",
            "ONEMAIL_MODE": "admin",
            "ONEMAIL_CREDENTIAL": "admin-password",
            "EMAIL_TO": "me@example.com",
        })
        reload_onemail_config()
        with patch("app.http_request", return_value=(200, {}, "{}")) as mock_req:
            app.send_onemail_message("🎉", "续期成功", "user")
        mock_req.assert_not_called()


def reload_onemail_config():
    """重新读取模块级 One-Mail 配置常量 (含发信所需共享字段 EMAIL_TO/EMAIL_FROM)。"""
    app.ONEMAIL_BASE_URL = (os.environ.get("ONEMAIL_BASE_URL") or "").rstrip("/")
    app.ONEMAIL_MODE = (os.environ.get("ONEMAIL_MODE") or "user").strip().lower()
    app.ONEMAIL_CREDENTIAL = os.environ.get("ONEMAIL_CREDENTIAL") or ""
    app.ONEMAIL_FROM_NAME = os.environ.get("ONEMAIL_FROM_NAME") or "Vycebump"
    app.EMAIL_FROM = os.environ.get("EMAIL_FROM") or ""
    app.EMAIL_TO = app._parse_email_list(os.environ.get("EMAIL_TO"))


if __name__ == "__main__":
    unittest.main()
