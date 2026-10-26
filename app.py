#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vycebump - Vyce.cloud 免费 VPS 自动续期与保活工具
灵感与架构源自 katabump (https://github.com/dengyie/katabump)

核心特性:
  1. 纯 Python 3 原生标准库实现 (零第三方依赖，无需 pip install)，开箱即用
  2. 支持 GitHub Actions 云端全自动调度运行 (Cron / Dispatch)
  3. 支持本地一键执行，自动通过 Chrome 9222 CDP 提取最新活跃登录 Cookie
  4. 精准计算 24 小时续期窗口与冷却时间，避免无效频繁请求
  5. 支持 Telegram 机器人通知推送 (账户脱敏、机器状态、到期预警)
  6. 状态持久化 (renew_state.json) 跨 run 缓存，支持多账号
"""

import os
import sys
import json
import time
import ssl
import shutil
import socket
import smtplib
import subprocess
import tempfile
import datetime
import email.message
import html
import ipaddress
import re
import urllib.request
import urllib.error
import urllib.parse
from typing import Dict, List, Optional, Tuple

# ============================================================
# 配置与环境变量
# ============================================================
BASE_URL = "https://vyce.cloud"
API_BASE = f"{BASE_URL}/api"


def _parse_smtp_port(raw: Optional[str]) -> int:
    """解析 SMTP 端口；非法值回退为 0（运行时取默认端口），绝不阻断模块加载。"""
    try:
        return int(raw or "0") or 0
    except ValueError:
        shown = str(raw)[:40]
        print(f"⚠️ SMTP_PORT={shown} 不是合法端口号，已忽略并使用默认端口。")
        return 0


def _parse_email_list(raw: Optional[str]) -> List[str]:
    """解析逗号分隔的收件邮箱列表，过滤空白项。"""
    return [addr.strip() for addr in (raw or "").split(",") if addr.strip()]

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""
TG_CHAT_ID = os.environ.get("TG_CHAT_ID") or ""
SMTP_HOST = os.environ.get("SMTP_HOST") or ""
SMTP_PORT = _parse_smtp_port(os.environ.get("SMTP_PORT"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME") or ""
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD") or ""
EMAIL_FROM = os.environ.get("EMAIL_FROM") or ""
EMAIL_TO = _parse_email_list(os.environ.get("EMAIL_TO"))
EMAIL_SUBJECT_PREFIX = os.environ.get("EMAIL_SUBJECT_PREFIX") or "[Vycebump]"
# One-Mail (https://github.com/dengyie/one-mail) HTTP 发信渠道：
#   ONEMAIL_MODE=user      → POST {BASE}/api/send_mail，Bearer <地址 JWT>，发件人=该地址
#   ONEMAIL_MODE=admin     → POST {BASE}/admin/send_mail，x-admin-auth: <管理密码>，FROM 指定发件人
ONEMAIL_BASE_URL = (os.environ.get("ONEMAIL_BASE_URL") or "").rstrip("/")
ONEMAIL_MODE = (os.environ.get("ONEMAIL_MODE") or "user").strip().lower()
ONEMAIL_CREDENTIAL = os.environ.get("ONEMAIL_CREDENTIAL") or ""
ONEMAIL_FROM_NAME = os.environ.get("ONEMAIL_FROM_NAME") or "Vycebump"
CDP_URL = os.environ.get("CDP_URL") or "http://127.0.0.1:9222"
FORCE_RENEW = os.environ.get("FORCE_RENEW", "").lower() in ("true", "1", "yes")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.environ.get("STATE_FILE") or os.path.join(SCRIPT_DIR, "renew_state.json")

# ------------------------------------------------------------
# CAPTCHA (Bestcap) 求解配置
#   Vyce 续期接口现在要求请求体携带 captchaResponse，由官方 Bestcap
#   组件签发。Bestcap 的挑战在 WebAssembly 虚拟机中执行，且每次会话
#   随机，无法用纯 Python 复刻，因此复用本机无头 Chrome 驱动官方组件。
#   CAPTCHA_SOLVER=off 可关闭自动求解（续期将按服务端要求失败）。
# ------------------------------------------------------------
CAPTCHA_SOLVER_MODE = (os.environ.get("CAPTCHA_SOLVER") or "auto").strip().lower()
CAPTCHA_ENABLED = CAPTCHA_SOLVER_MODE not in ("off", "false", "0", "no", "disabled")
CAPTCHA_SITEKEY_DEFAULT = os.environ.get("CAPTCHA_SITEKEY") or "bc_site_b05a3b42a31232929d6194f3"
CAPTCHA_PARENT_ORIGIN = "https://vyce.cloud"
CAPTCHA_SOLVER_SCRIPT = os.path.join(SCRIPT_DIR, "captcha_solver.mjs")
CHROME_BIN = os.environ.get("CHROME_BIN") or ""
CAPTCHA_SOLVE_TIMEOUT = 120  # 秒，含浏览器启动、frame 重试与挑战计算
_sitekey_cache: Dict[str, str] = {}

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36"
)

# 冷却期常量 (毫秒)
ONE_HOUR_MS = 3600 * 1000
ONE_DAY_MS = 24 * ONE_HOUR_MS


# ============================================================
# HTTP 请求辅助函数 (原生 urllib 实现，支持代理与 SSL)
# ============================================================
class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(urllib.parse.urljoin(req.full_url, newurl))
        try:
            old_port = old.port or (443 if old.scheme.lower() == "https" else 80)
            new_port = new.port or (443 if new.scheme.lower() == "https" else 80)
        except ValueError:
            old_port = new_port = None
        old_origin = (old.scheme.lower(), (old.hostname or "").lower(), old_port)
        new_origin = (new.scheme.lower(), (new.hostname or "").lower(), new_port)
        if old_origin != new_origin or old.scheme.lower() != "https" or new.scheme.lower() != "https":
            raise urllib.error.HTTPError(req.full_url, code, "Unsafe redirect rejected", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_error_text(value: str, limit: int = 240) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(__Host-vyce_sid\s*=\s*)[^\s&\"']+", r"\1[redacted]", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[redacted]", text)
    text = re.sub(r"(?i)(token|password|secret|authorization)(\s*[=:]\s*)[^\s,;\"']+", r"\1\2[redacted]", text)
    text = re.sub(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@", r"\1[redacted]@", text)
    text = " ".join(text.split())
    return text[:limit]


def _error_message(body: str, fallback: str) -> str:
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        if isinstance(body, str) and body.startswith("Request Exception:"):
            return safe_error_text(body, 120)
        return fallback
    if isinstance(data, dict):
        for key in ("message", "error", "code"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return safe_error_text(value)
    return fallback


def is_renewal_cooldown_error(code: int, msg: str) -> bool:
    """判定服务端返回的 429 是否属于预期的 24 小时续期冷却保护。"""
    if code != 429 or not msg:
        return False
    lower = str(msg).lower()
    cooldown_patterns = (
        "renewal_cooldown",
        "every 24 hours",
        "24 hour",
        "please wait",
        "cooldown",
        "not eligible for renewal",
    )
    return any(pattern in lower for pattern in cooldown_patterns)


def is_captcha_error(code: int, msg: str) -> bool:
    """判定服务端 400 是否因为缺少 / 无效的 CAPTCHA 验证。"""
    if code != 400 or not msg:
        return False
    return "captcha" in str(msg).lower()


def is_captcha_token_rejected(code: int, msg: str) -> bool:
    """区分「令牌被拒」与「没交令牌」。

    服务端返回 `invalid_captcha` / `CAPTCHA verification failed` 时，说明
    请求里带着令牌但校验没过——这一分支与本机求解无关：官网手动续期在同样
    情况下也会收到完全相同的响应。
    """
    if code != 400 or not msg:
        return False
    lower = str(msg).lower()
    return "invalid_captcha" in lower or "verification failed" in lower


# ============================================================
# Bestcap CAPTCHA 自动求解 (无头 Chrome + CDP)
# ============================================================
def _chrome_candidates() -> List[str]:
    candidates = [CHROME_BIN] if CHROME_BIN else []
    candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    return [path for path in candidates if path]


def find_chrome() -> Optional[str]:
    for path in _chrome_candidates():
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_cdp(port: int, proc: "subprocess.Popen", timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        status, _, _ = http_request(f"http://127.0.0.1:{port}/json/version", timeout=2)
        if status == 200:
            return True
        time.sleep(0.3)
    return False


def _stop_process(proc: Optional["subprocess.Popen"]) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        try:
            proc.kill()
            proc.wait(timeout=3)
        except (subprocess.TimeoutExpired, OSError):
            pass


def parse_solver_output(stdout: str) -> Optional[str]:
    """从求解器 stdout 中提取 "TOKEN:<token>"，否则返回 None。"""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("TOKEN:"):
            token = line[len("TOKEN:"):].strip()
            if token.startswith("bc1.") and len(token) > 20:
                return token
    return None


def fetch_captcha_sitekey(proxy: Optional[str] = None) -> str:
    """从 /api/config 读取当前 sitekey，读取失败时回退到内置默认值。"""
    cached = _sitekey_cache.get("value")
    if cached:
        return cached
    status, _, body = http_request(f"{API_BASE}/config", proxy=proxy, timeout=10)
    if status == 200:
        try:
            config = json.loads(body)
        except (TypeError, ValueError):
            config = None
        if isinstance(config, dict):
            sitekey = config.get("bestcapSitekey")
            if isinstance(sitekey, str) and re.fullmatch(r"bc_site_[A-Za-z0-9]+", sitekey):
                _sitekey_cache["value"] = sitekey
                return sitekey
    return CAPTCHA_SITEKEY_DEFAULT


def _run_captcha_solver(
    proxy: Optional[str] = None,
    server_id: Optional[str] = None,
    cookie: str = "",
) -> Tuple[str, str, int]:
    """启动无头 Chrome 运行求解脚本，返回 (stdout, stderr_tail, returncode)。

    server_id 非空时脚本还会在浏览器内提交续期请求（与官方前端同源、同
    UA、同出口 IP），cookie 通过 VYCE_COOKIE 环境变量传给脚本。
    环境不具备求解条件时返回 ("", reason, -1)。
    """
    if not CAPTCHA_ENABLED:
        return "", "CAPTCHA 自动求解已关闭 (CAPTCHA_SOLVER=off)", -1

    chrome = find_chrome()
    if not chrome:
        return "", "未找到 Chrome/Chromium，可通过 CHROME_BIN 指定可执行文件路径", -1
    node = shutil.which("node")
    if not node:
        return "", "未找到 node（求解脚本依赖 Node 18+）", -1
    if not os.path.isfile(CAPTCHA_SOLVER_SCRIPT):
        return "", f"缺少求解脚本 {os.path.basename(CAPTCHA_SOLVER_SCRIPT)}", -1

    sitekey = fetch_captcha_sitekey(proxy)
    port = _free_tcp_port()
    profile_dir = tempfile.mkdtemp(prefix="vycebump-chrome-")
    proc = None
    try:
        args = [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-sync",
            "--no-first-run",
            "--no-default-browser-check",
            "--mute-audio",
            f"--user-data-dir={profile_dir}",
            f"--remote-debugging-port={port}",
            "about:blank",
        ]
        if sys.platform.startswith("linux"):
            args.extend(["--no-sandbox", "--disable-dev-shm-usage"])
        if proxy:
            parsed = urllib.parse.urlsplit(proxy)
            if parsed.hostname and parsed.port:
                args.append(f"--proxy-server={parsed.scheme or 'http'}://{parsed.hostname}:{parsed.port}")
                args.append("--proxy-bypass-list=127.0.0.1;localhost")

        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if not _wait_for_cdp(port, proc):
            return "", "Chrome 调试端口未就绪", -1

        solver_args = [node, CAPTCHA_SOLVER_SCRIPT, f"http://127.0.0.1:{port}", sitekey, CAPTCHA_PARENT_ORIGIN]
        if server_id:
            solver_args.append(server_id)
        env = dict(os.environ)
        if cookie:
            env["VYCE_COOKIE"] = cookie

        result = subprocess.run(
            solver_args,
            capture_output=True,
            text=True,
            timeout=CAPTCHA_SOLVE_TIMEOUT,
            env=env,
        )
        stdout = result.stdout or ""
        if result.returncode != 0 or not stdout.strip():
            reason = (result.stderr or stdout or "").strip().splitlines()
            detail = reason[-1] if reason else f"solver exit {result.returncode}"
            return stdout, safe_error_text(detail, 160), result.returncode
        return stdout, "", result.returncode
    except subprocess.TimeoutExpired:
        return "", "CAPTCHA 求解超时", -1
    except (OSError, ValueError) as exc:
        return "", safe_error_text(exc, 160), -1
    finally:
        _stop_process(proc)
        shutil.rmtree(profile_dir, ignore_errors=True)


def obtain_captcha_token(proxy: Optional[str] = None) -> Tuple[Optional[str], str]:
    """仅求解令牌（浏览器不提交续期），返回 (token, 错误信息)。

    成功时返回 (token, "")；失败时返回 (None, reason)。
    """
    stdout, reason, rc = _run_captcha_solver(proxy)
    token = parse_solver_output(stdout)
    if token:
        return token, ""
    return None, reason or "求解器未返回令牌"


def parse_renew_output(stdout: str) -> Optional[Dict]:
    """从求解器 stdout 中提取 "RENEW:{...}" 的 JSON，否则返回 None。"""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("RENEW:"):
            payload = line[len("RENEW:"):].strip()
            try:
                data = json.loads(payload)
            except (TypeError, ValueError):
                return None
            if isinstance(data, dict):
                return data
    return None


def renew_with_captcha(
    proxy: Optional[str],
    server_id: str,
    cookie: str,
) -> Tuple[Optional[Tuple[bool, int, str]], str]:
    """在无头 Chrome 内完成 CAPTCHA 并提交续期，返回 ((ok, code, msg), "").

    浏览器路径不可用（缺 Chrome/Node、Cookie 被拒等）时返回 (None, reason)，
    由调用方决定是否回退到脚本直连。
    """
    stdout, reason, rc = _run_captcha_solver(proxy, server_id=server_id, cookie=cookie)
    data = parse_renew_output(stdout)
    if data is None:
        return None, reason or "求解器未返回浏览器内续期结果"
    status = int(data.get("status") or 0)
    body = data.get("body") or ""
    if status == 0:
        return None, safe_error_text(body, 160)
    return interpret_renew_response(status, body), ""


def interpret_renew_response(status: int, body: str) -> Tuple[bool, int, str]:
    """把续期 HTTP 响应解释为 (success, code, msg)，供 Python 与浏览器路径共用。"""
    if status in (200, 204):
        if status == 204 or not body.strip():
            return True, status, "Renewed successfully"
        try:
            response = json.loads(body)
        except (TypeError, ValueError):
            return False, status, "Invalid renewal response JSON"
        if isinstance(response, dict) and response.get("success") is not False:
            return True, status, safe_error_text(response.get("message") or "Renewed successfully")
        return False, status, _error_message(body, "Invalid renewal response schema")
    return False, status, _error_message(body, f"HTTP {status or 'network error'}")


def interpret_start_response(status: int, body: str) -> Tuple[bool, int, str]:
    """把开机/唤醒 HTTP 响应解释为 (success, code, msg)。"""
    if status in (200, 204):
        if status == 204 or not body.strip():
            return True, status, "Server started"
        try:
            response = json.loads(body)
        except (TypeError, ValueError):
            return False, status, "Invalid start response JSON"
        if isinstance(response, dict):
            srv = response.get("server")
            if isinstance(srv, dict):
                return True, status, "Server started"
            if response.get("success") is not False:
                return True, status, safe_error_text(response.get("message") or "Server started")
        return False, status, _error_message(body, "Invalid start response schema")
    return False, status, _error_message(body, f"HTTP {status or 'network error'}")


def http_request(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    data: Optional[bytes] = None,
    proxy: Optional[str] = None,
    timeout: int = 20,
) -> Tuple[int, Dict[str, str], str]:
    """
    通用 HTTP 请求封装
    返回: (status_code, response_headers, response_body_text)
    """
    # Always install an explicit proxy handler so ambient proxy environment
    # variables cannot silently reroute local CDP or API requests.
    if proxy:
        handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})]
    else:
        handlers = [urllib.request.ProxyHandler({})]
    handlers.append(SafeRedirectHandler())

    # 使用系统标准 CA 证书校验 HTTPS，防范中间人攻击
    ctx = ssl.create_default_context()
    handlers.append(urllib.request.HTTPSHandler(context=ctx))

    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, data=data, method=method)
    # urllib 默认 UA (Python-urllib/x) 会被 Cloudflare Bot Fight Mode 以 1010 直接 403，
    # 必须携带自定义 UA；headers 显式传入的 User-Agent 优先。
    req.add_unredirected_header("User-Agent", "Vycebump/1.0 (+https://github.com/dengyie/vycebump)")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)

    try:
        with opener.open(req, timeout=timeout) as resp:
            status = resp.status
            resp_headers = dict(resp.headers)
            body = resp.read().decode("utf-8", errors="replace")
            return status, resp_headers, body
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return e.code, dict(e.headers or {}), body
    except Exception as e:
        return 0, {}, f"Request Exception: {safe_error_text(e)}"


# ============================================================
# 消息通知 (Telegram + Email)
# ============================================================
def mask_username(username: str) -> str:
    """账号脱敏: dengyie -> de****ie"""
    if not username:
        return "未知用户"
    if "@" in username:
        name, domain = username.split("@", 1)
        if len(name) > 4:
            return f"{name[:2]}****{name[-2:]}@{domain}"
        return f"{name}@{domain}"
    if len(username) > 4:
        return f"{username[:2]}****{username[-2:]}"
    return username


def build_notification(
    status_icon: str,
    status_title: str,
    account_name: str,
    details: Optional[Dict] = None,
    error_msg: str = "",
) -> Dict[str, str]:
    """构建通知正文（纯文本 + HTML 双格式），供 Telegram / Email 渠道共用。"""
    beijing_time = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M:%S")

    masked_acc = mask_username(account_name)
    escape = lambda value: html.escape(safe_error_text(value, 120), quote=True)
    fields: List[Tuple[str, str]] = [
        ("👤 账户", masked_acc),
    ]
    if details:
        if details.get("server_name"):
            fields.append(("🖥️ 服务器", details["server_name"]))
        if details.get("plan_id"):
            fields.append(("📦 规格", f"{details['plan_id']} ({details.get('os', '')})"))
        if details.get("time_left_str"):
            fields.append(("⏳ 剩余存活", details["time_left_str"]))
        if details.get("next_renewal_str"):
            fields.append(("⏰ 续期时间窗口", details["next_renewal_str"]))

    plain_lines = [
        "Vyce.cloud 续期通知",
        "",
        f"{status_icon} {status_title}",
    ]
    html_lines = [
        "🇪🇺 <b>Vyce.cloud 续期通知</b>",
        "",
        f"{escape(status_icon)} <b>{escape(status_title)}</b>",
    ]
    for label, value in fields:
        plain_lines.append(f"{label}: {value}")
        html_lines.append(f"{escape(label)}: <code>{escape(value)}</code>")

    if error_msg:
        plain_lines.append(f"⚠️ 详情: {error_msg}")
        html_lines.append(f"⚠️ 详情: {escape(error_msg)}")

    plain_lines.append(f"⏱️ 报告时间: {beijing_time} (UTC+8)")
    html_lines.append(f"⏱️ 报告时间: {beijing_time} (UTC+8)")

    subject = f"{EMAIL_SUBJECT_PREFIX} {status_icon} {status_title} - {mask_username(account_name)}"
    return {"plain": "\n".join(plain_lines), "html": "\n".join(html_lines), "subject": subject}


def send_notification(
    status_icon: str,
    status_title: str,
    account_name: str,
    details: Optional[Dict] = None,
    error_msg: str = "",
    proxy: Optional[str] = None,
):
    """统一通知入口：向所有已配置的渠道 (Telegram / Email / One-Mail) 推送同一消息。"""
    send_tg_message(status_icon, status_title, account_name, details, error_msg, proxy)
    send_email_message(status_icon, status_title, account_name, details, error_msg, proxy)
    send_onemail_message(status_icon, status_title, account_name, details, error_msg, proxy)


def send_tg_message(
    status_icon: str,
    status_title: str,
    account_name: str,
    details: Optional[Dict] = None,
    error_msg: str = "",
    proxy: Optional[str] = None,
):
    """发送 Telegram 通知"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return

    text = build_notification(status_icon, status_title, account_name, details, error_msg)["html"]
    tg_url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    status, _, res = http_request(tg_url, method="POST", headers=headers, data=payload, proxy=proxy, timeout=15)
    if status == 200:
        print("📩 Telegram 通知发送成功！")
    else:
        print(f"⚠️ Telegram 发送失败 ({status}): {safe_error_text(_error_message(res, 'request failed'))}")


def send_email_message(
    status_icon: str,
    status_title: str,
    account_name: str,
    details: Optional[Dict] = None,
    error_msg: str = "",
    proxy: Optional[str] = None,
):
    """通过 SMTP 发送邮件通知。仅需标准库 smtplib/email，端口 465 走隐式 TLS，其余走 STARTTLS。"""
    if not (SMTP_HOST and EMAIL_TO):
        return
    if SMTP_USERNAME and not SMTP_PASSWORD:
        print("⚠️ 已配置 SMTP_USERNAME 但缺少 SMTP_PASSWORD，跳过邮件通知。")
        return

    content = build_notification(status_icon, status_title, account_name, details, error_msg)

    message = email.message.EmailMessage()
    message["Subject"] = content["subject"]
    message["From"] = EMAIL_FROM or SMTP_USERNAME
    message["To"] = ", ".join(EMAIL_TO)
    message.set_content(content["plain"])
    message.add_alternative(
        f"<html><body style='font-family: -apple-system, sans-serif; white-space: pre-wrap;'>"
        f"{content['html']}</body></html>",
        subtype="html",
    )

    try:
        host = SMTP_HOST
        port = SMTP_PORT or 587
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as smtp:
                if SMTP_USERNAME:
                    smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                if SMTP_USERNAME:
                    smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
                smtp.send_message(message)
        print(f"📩 邮件通知发送成功 -> {', '.join(EMAIL_TO)}")
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        print(f"⚠️ 邮件发送失败: {safe_error_text(str(exc), 200)}")


def send_onemail_message(
    status_icon: str,
    status_title: str,
    account_name: str,
    details: Optional[Dict] = None,
    error_msg: str = "",
    proxy: Optional[str] = None,
):
    """通过自建 One-Mail 实例的 HTTP API 发送通知邮件。

    ONEMAIL_MODE=user  (默认): Bearer <地址 JWT> 调 POST /api/send_mail，
        发件人即该 JWT 对应地址，要求账户 send_balance > 0。
    ONEMAIL_MODE=admin: x-admin-auth <管理密码> 调 POST /admin/send_mail，
        需同时以 EMAIL_FROM 指定发件地址。
    One-Mail 侧需已配置发信通道 (Resend / SMTP / SEND_MAIL binding)。
    """
    if not (ONEMAIL_BASE_URL and ONEMAIL_CREDENTIAL and EMAIL_TO):
        return

    content = build_notification(status_icon, status_title, account_name, details, error_msg)

    if ONEMAIL_MODE == "admin":
        if not EMAIL_FROM:
            print("⚠️ One-Mail admin 模式需要 EMAIL_FROM 指定发件地址，跳过邮件通知。")
            return
        url = f"{ONEMAIL_BASE_URL}/admin/send_mail"
        headers = {"x-admin-auth": ONEMAIL_CREDENTIAL, "Content-Type": "application/json"}
        base_body = {"from_mail": EMAIL_FROM, "from_name": ONEMAIL_FROM_NAME}
    else:
        url = f"{ONEMAIL_BASE_URL}/api/send_mail"
        headers = {
            "Authorization": f"Bearer {ONEMAIL_CREDENTIAL}",
            "Content-Type": "application/json",
        }
        base_body = {"from_name": ONEMAIL_FROM_NAME}

    # One-Mail 的 to_mail 为单值，多收件人逐个发送
    sent_to, failed_to = [], []
    for to_mail in EMAIL_TO:
        body = {**base_body, "to_mail": to_mail, "to_name": "", "subject": content["subject"],
                "content": content["html"], "is_html": True}
        status, _, res = http_request(
            url, method="POST", headers=headers,
            data=json.dumps(body).encode("utf-8"), proxy=proxy, timeout=20,
        )
        if status == 200:
            sent_to.append(to_mail)
        else:
            failed_to.append(to_mail)
            print(f"⚠️ One-Mail 发送失败 ({status}) -> {to_mail}: {safe_error_text(_error_message(res, 'request failed'))}")
    if sent_to:
        print(f"📩 One-Mail 通知发送成功 -> {', '.join(sent_to)}")


# ============================================================
# 本地 Chrome CDP Cookie 提取
# ============================================================
def _loopback_cdp_endpoint(value: str) -> Optional[urllib.parse.SplitResult]:
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme.lower() != "http" or parsed.username or parsed.password:
            return None
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            return None
        host = parsed.hostname
        if not host or not ipaddress.ip_address(host).is_loopback:
            return None
        port = parsed.port or 9222
        if not 1 <= port <= 65535:
            return None
        return parsed
    except (ValueError, TypeError):
        return None


def get_cookie_from_cdp(cdp_base_url: str = CDP_URL) -> Optional[str]:
    """通过受限的本地 Chrome CDP 接口提取 Vyce Session Cookie。"""
    endpoint = _loopback_cdp_endpoint(cdp_base_url)
    if not endpoint:
        print("⚠️ 拒绝不安全的 CDP_URL：仅允许无凭据的 loopback HTTP endpoint。")
        return None
    host = endpoint.hostname.lower()
    port = endpoint.port or 9222
    json_url = f"http://{host}:{port}/json"
    status, _, body = http_request(json_url, timeout=3)
    if status != 200:
        return None

    try:
        tabs = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(tabs, list):
        return None

    target_tab = None
    for tab in tabs:
        if not isinstance(tab, dict) or tab.get("type") != "page":
            continue
        try:
            page = urllib.parse.urlsplit(tab.get("url", ""))
            ws = urllib.parse.urlsplit(tab.get("webSocketDebuggerUrl", ""))
            ws_port = ws.port or (443 if ws.scheme.lower() == "wss" else 80)
        except ValueError:
            continue
        if (page.scheme.lower() == "https" and page.hostname == "vyce.cloud"
                and ws.scheme.lower() == "ws" and ws.hostname == host
                and ws_port == port and ws.username is None and ws.password is None
                and ws.path.startswith("/devtools/page/")):
            target_tab = tab
            break

    if not target_tab:
        return None
    ws_url = target_tab["webSocketDebuggerUrl"]

    node_code = """
    const wsUrl = process.argv[1];
    if (!wsUrl) process.exit(2);
    const ws = new WebSocket(wsUrl);
    let finished = false;
    const fail = (code) => { if (!finished) { finished = true; process.exit(code); } };
    ws.addEventListener("open", () => {
      ws.send(JSON.stringify({id: 1, method: "Network.getCookies", params: {urls: ["https://vyce.cloud"]}}));
    });
    ws.addEventListener("message", (event) => {
      let res;
      try { res = JSON.parse(event.data); } catch { return fail(3); }
      if (res.id !== 1) return;
      if (res.error || !res.result || !Array.isArray(res.result.cookies)) return fail(4);
      const target = res.result.cookies.find(c => c && c.name === "__Host-vyce_sid" && typeof c.value === "string" && c.value.length > 0);
      if (target) process.stdout.write("__Host-vyce_sid=" + target.value);
      finished = true;
      ws.close();
      process.exit(target ? 0 : 5);
    });
    ws.addEventListener("error", () => fail(6));
    setTimeout(() => fail(7), 6000);
    """
    try:
        p = subprocess.run(
            ["node", "--input-type=module", "-e", node_code, ws_url],
            capture_output=True,
            text=True,
            timeout=8,
        )
        out = p.stdout.strip()
        if p.returncode == 0 and re.fullmatch(r"__Host-vyce_sid=[^\s]+", out):
            return out
    except FileNotFoundError:
        print("ℹ️ 本地环境未检测到 node 命令，跳过 CDP WebSocket 自动化提取。")
    except subprocess.TimeoutExpired:
        print("⚠️ CDP 提取超时。")
    except OSError:
        print("⚠️ CDP 提取进程启动失败。")
    return None


# ============================================================
# 账号加载模块
# ============================================================
def load_accounts() -> List[Dict[str, str]]:
    """
    支持多种账号输入形式:
      1. USERS_JSON: '[{"cookie": "__Host-vyce_sid=...", "name": "dengyie"}]'
      2. VYCE_COOKIE: '__Host-vyce_sid=...' 或纯 Token 字符串
      3. 本地 Chrome CDP (9222 端口) 自动探测
    """
    accounts = []

    # 1. USERS_JSON
    raw_users = os.environ.get("USERS_JSON", "").strip()
    if raw_users:
        try:
            parsed = json.loads(raw_users)
            if isinstance(parsed, list):
                for item in parsed:
                    cookie = item.get("cookie") or item.get("token") or ""
                    if cookie:
                        if not cookie.startswith("__Host-vyce_sid="):
                            cookie = f"__Host-vyce_sid={cookie}"
                        accounts.append({
                            "name": item.get("name") or item.get("login") or "VyceUser",
                            "cookie": cookie
                        })
        except Exception as e:
            print(f"⚠️ USERS_JSON 解析出错: {e}")

    # 2. 单账号 VYCE_COOKIE
    single_cookie = os.environ.get("VYCE_COOKIE", "").strip()
    if single_cookie:
        if not single_cookie.startswith("__Host-vyce_sid="):
            single_cookie = f"__Host-vyce_sid={single_cookie}"
        accounts.append({
            "name": os.environ.get("VYCE_USER", "VyceUser"),
            "cookie": single_cookie
        })

    # 3. 本地 CDP 探测
    if not accounts:
        print("🔍 尝试从本地 Chrome 9222 CDP 探测活跃登录态...")
        cdp_cookie = get_cookie_from_cdp(CDP_URL)
        if cdp_cookie:
            print("✅ 成功从本地 Chrome 提取到最新 Session Cookie！")
            accounts.append({
                "name": "LocalChromeUser",
                "cookie": cdp_cookie
            })

    return accounts


# ============================================================
# 时间格式化辅助
# ============================================================
def format_duration(seconds: float) -> str:
    """把秒数格式化为 1d 12h 或 12h 20m 格式"""
    total_sec = max(0, int(seconds))
    days = total_sec // 86400
    hours = (total_sec % 86400) // 3600
    minutes = (total_sec % 3600) // 60
    secs = total_sec % 60

    if days >= 1:
        return f"{days}d {hours}h"
    if hours >= 1:
        return f"{hours}h {minutes:02d}m"
    if minutes >= 1:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def ms_to_beijing_str(timestamp_ms: Optional[int]) -> str:
    if not timestamp_ms:
        return "未知"
    dt = datetime.datetime.fromtimestamp(
        timestamp_ms / 1000.0,
        tz=datetime.timezone(datetime.timedelta(hours=8))
    )
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# Vyce.cloud API 交互
# ============================================================
class VyceClient:
    def __init__(self, cookie: str, proxy: Optional[str] = None):
        self.cookie = cookie
        self.proxy = proxy
        self.headers = {
            "User-Agent": DEFAULT_UA,
            "Accept": "application/json",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
            "Cookie": cookie,
        }
        self.last_error = ""

    def get_me(self) -> Optional[Dict]:
        """获取当前用户信息: GET /api/me"""
        status, _, body = http_request(f"{API_BASE}/me", headers=self.headers, proxy=self.proxy, timeout=15)
        if status == 200:
            try:
                data = json.loads(body)
            except (TypeError, ValueError):
                self.last_error = "Invalid JSON response"
                return None
            user = data.get("user") if isinstance(data, dict) else None
            if isinstance(user, dict) and isinstance(user.get("login"), str):
                return user
            self.last_error = "Invalid user response schema"
            return None
        self.last_error = _error_message(body, f"HTTP {status or 'network error'}")
        return None

    def get_servers(self) -> Optional[List[Dict]]:
        """Return a server list, [] for valid empty results, or None on failure."""
        status, _, body = http_request(f"{API_BASE}/servers", headers=self.headers, proxy=self.proxy, timeout=15)
        if status == 200:
            try:
                data = json.loads(body)
            except (TypeError, ValueError):
                self.last_error = "Invalid JSON response"
                return None
            servers = data.get("servers") if isinstance(data, dict) else None
            if not isinstance(servers, list) or any(not isinstance(server, dict) for server in servers):
                self.last_error = "Invalid servers response schema"
                return None
            for server in servers:
                if (not isinstance(server.get("id"), str) or not server["id"]
                        or not isinstance(server.get("source"), str)
                        or not isinstance(server.get("status"), str)
                        or not isinstance(server.get("expiresAt"), (int, float))):
                    self.last_error = "Invalid server record schema"
                    return None
            return servers
        self.last_error = _error_message(body, f"HTTP {status or 'network error'}")
        return None

    def renew_server(self, server_id: str, captcha_response: Optional[str] = None) -> Tuple[bool, int, str]:
        """Return success, HTTP status, and a bounded non-sensitive message."""
        url = f"{API_BASE}/servers/{urllib.parse.quote(server_id, safe='')}/renew"
        headers = dict(self.headers)
        payload = None
        if captcha_response:
            headers["Content-Type"] = "application/json"
            payload = json.dumps({"captchaResponse": captcha_response}).encode("utf-8")
        status, _, body = http_request(
            url, method="POST", headers=headers, data=payload, proxy=self.proxy, timeout=25
        )
        return interpret_renew_response(status, body)

    def start_server(self, server_id: str) -> Tuple[bool, int, str]:
        """发起开机/复活请求: POST /api/servers/{id}/start，返回 (success, code, msg)。"""
        url = f"{API_BASE}/servers/{urllib.parse.quote(server_id, safe='')}/start"
        headers = dict(self.headers)
        headers["Content-Type"] = "application/json"
        headers["Origin"] = BASE_URL
        headers["Referer"] = f"{BASE_URL}/"
        status, _, body = http_request(
            url, method="POST", headers=headers, data=b"{}", proxy=self.proxy, timeout=25
        )
        return interpret_start_response(status, body)


# ============================================================
# 状态持久化与单服务器续期逻辑
# ============================================================
def load_state() -> Dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state: Dict) -> bool:
    try:
        temporary = f"{STATE_FILE}.tmp"
        with open(temporary, "w", encoding="utf-8") as f:
            os.chmod(temporary, 0o600)
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(temporary, STATE_FILE)
        return True
    except OSError as e:
        print(f"⚠️ 保存状态文件失败: {safe_error_text(e)}")
        return False


def process_account(account: Dict, state: Dict, proxy: Optional[str] = None) -> bool:
    cookie = account["cookie"]
    account_label = account.get("name", "VyceUser")

    print(f"\n==================================================")
    print(f"🔑 正在处理账户: {mask_username(account_label)}")
    print(f"==================================================")

    client = VyceClient(cookie, proxy=proxy)
    user_info = client.get_me()

    if not user_info:
        print("❌ 账户登录失效或 Cookie 无效，请更新 __Host-vyce_sid Cookie！")
        send_notification(
            status_icon="❌",
            status_title="Cookie 失效 / 登录失败",
            account_name=account_label,
            error_msg=client.last_error or "无法通过 /api/me 获取用户信息。",
            proxy=proxy,
        )
        return False

    real_username = user_info.get("login") or account_label
    print(f"✅ 登录成功！用户: {mask_username(real_username)}")

    servers = client.get_servers()
    if servers is None:
        error = client.last_error or "无法读取服务器列表"
        print(f"❌ 获取服务器列表失败: {error}")
        send_notification(
            status_icon="❌",
            status_title="服务器列表读取失败",
            account_name=real_username,
            error_msg=error,
            proxy=proxy,
        )
        return False
    if not servers:
        print("ℹ️ 该账户名下无任何服务器。")
        return True

    now_ms = int(time.time() * 1000)
    account_ok = True

    for srv in servers:
        srv_id = srv["id"]
        srv_name = safe_error_text(srv.get("name") or "Unnamed", 80)
        source = srv["source"]
        plan_id = safe_error_text(srv.get("planId") or "", 40)
        status = srv["status"]
        expires_at = srv["expiresAt"]

        print(f"\n🖥️ 检查服务器: {srv_name}")
        print(f"   类型: {source} ({plan_id}) | 状态: {safe_error_text(status, 40)}")

        # 仅针对免费服务器执行 24h 续期逻辑
        if source != "free":
            print(f"   ℹ️ 非免费机型 (source={source})，跳过续期。")
            continue

        # 读取并比对本地持久化缓存
        cached_entry = state.get(srv_id)
        if cached_entry and cached_entry.get("next_renewal_ts"):
            cached_next = cached_entry.get("next_renewal_ts")
            print(f"   📋 缓存记录下次续期: {ms_to_beijing_str(cached_next)}")

        # 计算剩余存活时间与冷却时间
        time_left_ms = expires_at - now_ms
        time_left_sec = time_left_ms / 1000.0
        time_left_str = format_duration(time_left_sec)

        # 冷却判定: 官方规则 expiresAt 初始为 48h，当 time_left > 24h 时处于 Cooldown 状态
        cooldown_left_ms = time_left_ms - ONE_DAY_MS
        cooldown_left_sec = cooldown_left_ms / 1000.0
        in_cooldown = cooldown_left_ms > 0 and status == "running"

        next_avail_ts = now_ms + cooldown_left_ms if in_cooldown else now_ms
        next_avail_str = ms_to_beijing_str(next_avail_ts)

        details = {
            "server_name": srv_name,
            "plan_id": plan_id,
            "os": srv.get("os", ""),
            "time_left_str": time_left_str,
            "next_renewal_str": next_avail_str,
        }

        print(f"   ⏳ 剩余存活时间: {time_left_str} (到期: {ms_to_beijing_str(expires_at)})")

        if in_cooldown and not FORCE_RENEW:
            cd_str = format_duration(cooldown_left_sec)
            print(f"   ❄️ 当前处于冷却期中 (Cooldown)，无需续期。")
            print(f"   ⏰ 预计下次可续期时间: {next_avail_str} (约 {cd_str} 后)")
            
            # 更新状态记录
            state[srv_id] = {
                "name": srv_name,
                "user": real_username,
                "status": "cooldown",
                "time_left_str": time_left_str,
                "next_renewal_ts": next_avail_ts,
                "next_renewal_str": next_avail_str,
                "last_check_ts": now_ms,
            }
            continue

        # 满足续期条件 (进入可续期窗口 或 机器已停止 或 强制续期)
        print(f"   🚀 进入续期窗口或被强制触发，发起续期请求...")
        captcha_error = ""
        success, code, msg = False, 0, ""
        cookie_header = account.get("cookie") or ""
        fresh_srv = None

        # 策略 1: 机器已关机或存活时间已用尽 (time_left_ms <= 0)，优先走官方原生 start 兜底接口
        # 官方平台机制：对 stopped 或已过期的服务器调用 start 接口时直接按 "Renew and start" 处理，无需 CAPTCHA
        if status == "stopped" or time_left_ms <= 0:
            print(f"   ⚡ 检测到服务器状态为 {status} 且存活时间已到期或关机，优先调用 start 接口唤醒/续期 (Renew and start)...")
            start_ok, start_code, start_msg = client.start_server(srv_id)
            if start_ok:
                fresh_servers = client.get_servers()
                if fresh_servers:
                    check_srv = next((s for s in fresh_servers if s.get("id") == srv_id), None)
                    if check_srv and (
                        check_srv.get("expiresAt", 0) > expires_at + 60_000
                        or (check_srv.get("status") == "running" and check_srv.get("expiresAt", 0) > now_ms)
                    ):
                        print(f"   🎉 start 接口调用成功，服务器已唤醒并成功续期！(到期: {ms_to_beijing_str(check_srv['expiresAt'])})")
                        success = True
                        code = start_code
                        msg = "Renewed and started via start API"
                        fresh_srv = check_srv
            if not success:
                print(f"   ⚠️ start 接口未直接完成续期 (HTTP {start_code}: {safe_error_text(start_msg)})，继续尝试 CAPTCHA 续期流程...")

        if not success:
            attempts = 2 if CAPTCHA_ENABLED else 1
            for attempt in range(attempts):
                if attempt > 0:
                    # 重新尝试前短暂等待，避免连续请求触发 Bestcap 频控或状态竞争
                    time.sleep(3)
                if CAPTCHA_ENABLED:
                    # 令牌与请求必须来自同一浏览器会话：服务端会校验来源/出口特征
                    print(f"   🤖 正在通过无头 Chrome 求解 Bestcap CAPTCHA 并在浏览器内提交续期...")
                    browser_result, browser_error = renew_with_captcha(proxy, srv_id, cookie_header)
                    if browser_result is not None:
                        success, code, msg = browser_result
                    else:
                        # 浏览器路径不可用：退回脚本直连（仍会尝试提交令牌）
                        captcha_error = browser_error
                        print(f"   ⚠️ 浏览器内续期不可用: {browser_error}，改用脚本直连提交...")
                        captcha_token, token_error = obtain_captcha_token(proxy)
                        if token_error:
                            captcha_error = token_error
                        elif captcha_token:
                            # 已经有令牌，浏览器路径的告警不再适用于本次结果
                            captcha_error = ""
                            print(f"   ✅ 已获取续期令牌。")
                        success, code, msg = client.renew_server(srv_id, captcha_token)
                else:
                    success, code, msg = client.renew_server(srv_id, None)

                if success or not is_captcha_error(code, msg) or attempt + 1 >= attempts:
                    break
                print(f"   🔁 服务端要求 CAPTCHA（{safe_error_text(msg)}），重新求解并重试一次...")

        if not success and fresh_srv is None:
            # 无论续期请求返回何种非 200 响应（HTTP 400 invalid_captcha、429 等），
            # 均二次查询服务端服务器列表，核验 expiresAt 是否实际上已被服务端延期
            fresh_servers = client.get_servers()
            if fresh_servers:
                check_srv = next((s for s in fresh_servers if s.get("id") == srv_id), None)
                if check_srv and isinstance(check_srv.get("expiresAt"), (int, float)):
                    if check_srv["expiresAt"] > expires_at + 60_000:
                        print(
                            f"   ✨ 续期请求返回 HTTP {code} ({safe_error_text(msg)})，"
                            f"但经状态核验到期时间已更新（{ms_to_beijing_str(expires_at)} -> {ms_to_beijing_str(check_srv['expiresAt'])}），"
                            f"实际续期已成功！"
                        )
                        success = True
                        msg = f"Renewed successfully (verified via server expiresAt: {ms_to_beijing_str(check_srv['expiresAt'])})"
                        fresh_srv = check_srv
                    elif check_srv.get("status") == "stopped" or check_srv.get("expiresAt", 0) <= now_ms:
                        # 续期未成功且机器处于停机或已超时状态，追加尝试 start 兜底唤醒
                        print(f"   ⚡ 续期未成功且机器处于 {check_srv.get('status')} 状态，追加尝试 start 兜底...")
                        st_ok, st_code, st_msg = client.start_server(srv_id)
                        if st_ok:
                            st_servers = client.get_servers()
                            if st_servers:
                                st_srv = next((s for s in st_servers if s.get("id") == srv_id), None)
                                if st_srv and (
                                    st_srv.get("expiresAt", 0) > expires_at + 60_000
                                    or (st_srv.get("status") == "running" and st_srv.get("expiresAt", 0) > now_ms)
                                ):
                                    print(f"   🎉 追加 start 兜底成功，服务器已启动并续期！(到期: {ms_to_beijing_str(st_srv['expiresAt'])})")
                                    success = True
                                    msg = "Renewed and started via start API fallback"
                                    fresh_srv = st_srv

        if success:
            print(f"   🎉 续期成功！({msg})")
            if fresh_srv is None:
                # 重新拉取服务器最新状态
                fresh_servers = client.get_servers()
            else:
                fresh_servers = [fresh_srv]
            if fresh_servers is None:
                error = client.last_error or "续期后无法刷新服务器状态"
                print(f"   ⚠️ 续期请求已接受，但状态刷新失败: {error}")
                account_ok = False
                state[srv_id] = {
                    "name": srv_name,
                    "user": real_username,
                    "status": "renewed_unverified",
                    "error": error,
                    "last_check_ts": now_ms,
                }
                send_notification(
                    status_icon="⚠️",
                    status_title="续期成功但状态未验证",
                    account_name=real_username,
                    details=details,
                    error_msg=error,
                    proxy=proxy,
                )
                continue
            fresh_srv = next((s for s in fresh_servers if s.get("id") == srv_id), None)
            if not fresh_srv or not isinstance(fresh_srv.get("expiresAt"), (int, float)):
                error = "续期后响应中未找到服务器"
                print(f"   ⚠️ 续期请求已接受，但无法验证服务器状态。")
                account_ok = False
                state[srv_id] = {
                    "name": srv_name,
                    "user": real_username,
                    "status": "renewed_unverified",
                    "error": error,
                    "last_check_ts": now_ms,
                }
                send_notification(
                    status_icon="⚠️",
                    status_title="续期成功但状态未验证",
                    account_name=real_username,
                    details=details,
                    error_msg=error,
                    proxy=proxy,
                )
                continue
            new_expires = fresh_srv["expiresAt"]
            new_left_sec = (new_expires - now_ms) / 1000.0
            details["time_left_str"] = format_duration(new_left_sec)
            details["next_renewal_str"] = ms_to_beijing_str(now_ms + ONE_DAY_MS)
            print(f"   ✨ 机器存活已重置为: {details['time_left_str']} (到期: {ms_to_beijing_str(new_expires)})")

            state[srv_id] = {
                "name": srv_name,
                "user": real_username,
                "status": "success",
                "time_left_str": details["time_left_str"],
                "last_renew_ts": now_ms,
                "last_renew_str": ms_to_beijing_str(now_ms),
                "next_renewal_ts": now_ms + ONE_DAY_MS,
                "next_renewal_str": ms_to_beijing_str(now_ms + ONE_DAY_MS),
            }

            send_notification(
                status_icon="🎉",
                status_title="Vyce VPS 续期成功",
                account_name=real_username,
                details=details,
                proxy=proxy,
            )
        else:
            expected_cooldown = is_renewal_cooldown_error(code, msg)
            if expected_cooldown:
                print(f"   ⏳ 官方服务端提示冷却未完成 (429): {safe_error_text(msg)}")
                wait_match = re.search(r"wait\s+(\d+)\s+more\s+hour", str(msg), re.IGNORECASE)
                effective_next_ts = (
                    now_ms + int(int(wait_match.group(1)) * ONE_HOUR_MS)
                    if wait_match else next_avail_ts
                )
                state[srv_id] = {
                    "name": srv_name,
                    "user": real_username,
                    "status": "cooldown_429",
                    "msg": safe_error_text(msg),
                    "next_renewal_ts": effective_next_ts,
                    "next_renewal_str": ms_to_beijing_str(effective_next_ts),
                    "last_check_ts": now_ms,
                }
            else:
                print(f"   ❌ 续期失败 (HTTP {code}): {safe_error_text(msg)}")
                account_ok = False
                state[srv_id] = {
                    "name": srv_name,
                    "user": real_username,
                    "status": "failed",
                    "error": safe_error_text(msg),
                    "last_check_ts": now_ms,
                }
                error_msg = f"HTTP {code}: {safe_error_text(msg)}"
                is_critical = time_left_sec <= 2 * 3600
                if is_captcha_error(code, msg):
                    if captcha_error:
                        reason = captcha_error
                    elif is_captcha_token_rejected(code, msg):
                        reason = (
                            "令牌已生成并被服务端拒绝（invalid_captcha）；"
                            "官网手动续期同样报此错，属 vyce.cloud/Bestcap 侧校验故障，"
                            "下次调度会自动重试；已就绪 start 兜底保护"
                        )
                        if is_critical:
                            reason += "；⚠️ 机器剩余存活已不足 2 小时，临近到期！"
                        print("   ℹ️ 官网手动续期在同一环境下同样失败，本机无需调整。")
                    else:
                        reason = "服务端未收到有效 CAPTCHA 令牌"
                    error_msg += f"（CAPTCHA: {reason}）"

                title_icon = "🚨" if is_critical else "❌"
                title_text = "Vyce VPS 续期紧急告警 (临近到期)" if is_critical else "Vyce VPS 续期失败"
                send_notification(
                    status_icon=title_icon,
                    status_title=title_text,
                    account_name=real_username,
                    details=details,
                    error_msg=error_msg,
                    proxy=proxy,
                )

    return account_ok


# ============================================================
# 主执行入口
# ============================================================
def main():
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] vycebump 保活脚本启动")

    proxy_server = os.environ.get("PROXY_SERVER") or os.environ.get("https_proxy") or os.environ.get("http_proxy")
    if proxy_server:
        try:
            parsed_proxy = urllib.parse.urlsplit(proxy_server)
            host = parsed_proxy.hostname or "?"
            port = parsed_proxy.port or "?"
            print(f"🌐 使用代理出口: {parsed_proxy.scheme or 'unknown'}://{host}:{port}")
        except ValueError:
            print("🌐 使用代理出口: [invalid proxy configuration]")

    accounts = load_accounts()
    if not accounts:
        print("❌ 未检测到任何 Vyce.cloud 账号！")
        print("请通过以下方式之一配置凭证:")
        print("  1. 环境变量 VYCE_COOKIE='__Host-vyce_sid=...'")
        print("  2. 环境变量 USERS_JSON='[{\"cookie\": \"...\", \"name\": \"...\"}]'")
        print("  3. 本机启动 Chrome 并启用调试端口 (--remote-debugging-port=9222)")
        sys.exit(1)

    state = load_state()
    all_success = True

    for acc in accounts:
        try:
            ok = process_account(acc, state, proxy=proxy_server)
            if not ok:
                all_success = False
        except Exception as e:
            print(f"❌ 处理账户 {mask_username(acc.get('name', ''))} 时出现未捕获异常: {safe_error_text(e)}")
            all_success = False

    if not save_state(state):
        all_success = False
    print(f"\n✅ 任务处理完毕，状态已保存至 {STATE_FILE}。")
    if not all_success:
        print("❌ 检测到有账户处理失败或续期异常，以非零状态码退出 (exit 1)。")
        sys.exit(1)


if __name__ == "__main__":
    main()
