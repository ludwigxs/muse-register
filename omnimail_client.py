# -*- coding: utf-8 -*-
"""
OmniMail HTTP API 客户端.

文档: https://github.com/mibgb65-cloud/OmniMail/blob/main/docs/API.md

认证:
    POST /api/auth/token           — 拿 accessToken (15min) + refreshToken (30d)
    POST /api/auth/token/refresh   — 用 refreshToken 续期

核心操作:
    GET  /api/mailboxes              — 列出全部邮箱地址 (auth)
    GET  /api/messages               — 收码 (auth, folder=inbox, mailbox=xxx@domain, limit=30)
    POST /api/mailboxes              — 创建新邮箱 (auth)

每个 OmniMail 账户 = 一个 manager, 管理自己域名下 N 个 mailbox.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

logger = logging.getLogger(__name__)


class OmniMailError(RuntimeError):
    """OmniMail 服务端错误."""


class OmniMailAuthError(OmniMailError):
    """401 认证失败 / token 过期."""


class OmniMailClient:
    """
    一个 OmniMail 账户的 API 客户端.

    用法一 (单例复用):
        client = OmniMailClient(base="https://mail.example.com/api",
                                email="user@example.com", password="...")
        client.login()
        mailboxes = client.list_mailboxes()
        messages  = client.poll_messages("user@example.com")

    用法二 (全局默认实例, 从 config/email 读取):
        from core.omnimail_client import default_client
        mailboxes = default_client().list_mailboxes()
    """

    def __init__(
        self,
        base: str,
        email: str,
        password: str,
        device_name: str = "muse-register",
    ) -> None:
        self.base = (base or "").rstrip("/")
        self.email = (email or "").strip()
        self.password = password or ""
        self.device_name = device_name
        self._lock = threading.RLock()
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._access_expires_at: float = 0.0   # unix 秒
        self._refresh_expires_at: float = 0.0

    # ---------- auth ----------

    def login(self) -> dict:
        """密码换 access_token + refresh_token. 立即缓存. 返回完整 token 响应."""
        with self._lock:
            resp = self._http(
                "POST", "/auth/token",
                json_body={
                    "email": self.email,
                    "password": self.password,
                    "deviceName": self.device_name,
                },
                auth=False,  # 登录时还没有 token
            )
            self._cache_tokens(resp)
            return resp

    def refresh(self) -> dict | None:
        """用 refresh_token 续期. 续期失败返回 None (不抛, 让上层决定是否重新 login)."""
        with self._lock:
            if not self._refresh_token:
                return None
            try:
                resp = self._http(
                    "POST", "/auth/token/refresh",
                    json_body={"refreshToken": self._refresh_token},
                    auth=False,  # refresh 用 body 里的 refreshToken 换新 token, 不能再带 Bearer (否则 _ensure_access 递归爆栈)
                )
                self._cache_tokens(resp)
                return resp
            except OmniMailAuthError:
                # refresh 也失效了 → 清除 token, 让上层重新 login
                self._access_token = ""
                self._refresh_token = ""
                return None

    def revoke(self) -> None:
        """主动撤销 refresh_token."""
        with self._lock:
            if self._refresh_token:
                try:
                    self._http(
                        "POST", "/auth/token/revoke",
                        json_body={"refreshToken": self._refresh_token},
                        auth=False,
                    )
                except Exception:
                    pass
            self._access_token = ""
            self._refresh_token = ""

    # ---------- high-level: auto-auth ----------

    def _ensure_access(self) -> str:
        """保证 access_token 有效 (先刷新, 刷新失败则重新 login). 返回可用的 access_token."""
        now = time.time()
        with self._lock:
            if self._access_token and self._access_expires_at - 30 > now:
                return self._access_token
            # 先尝试 refresh
            if self._refresh_token and self._refresh_expires_at - 60 > now:
                new = self.refresh()
                if new is not None:
                    return self._access_token
            # 实在不行重新 login
            self.login()
            return self._access_token

    def _cache_tokens(self, resp: dict) -> None:
        # OmniMail 响应同时包含 accessToken + refreshToken (refresh 也返回新的一对).
        at = resp.get("accessToken") or ""
        rt = resp.get("refreshToken") or ""
        exp_at = int(resp.get("expiresIn") or 900)
        exp_rt = int(resp.get("refreshExpiresIn") or 2_592_000)
        if at:
            self._access_token = at
            self._access_expires_at = time.time() + exp_at
        if rt:
            self._refresh_token = rt
            self._refresh_expires_at = time.time() + exp_rt

    # ---------- mailboxes ----------

    def list_mailboxes(self) -> list[dict]:
        """列出全部 mailbox. 返回 [{address, domain, isActive, isPrimary}, ...]"""
        r = self._http("GET", "/mailboxes", auth=True)
        if isinstance(r, dict):
            return r.get("mailboxes") or []
        return []

    def list_active_mailboxes(self) -> list[dict]:
        return [m for m in self.list_mailboxes() if m.get("isActive")]

    def create_mailbox(self, local_part: str, domain: str | None = None) -> dict:
        """创建新邮箱. local_part 如 'chatgpt05', domain 留空用用户主域名."""
        body: dict[str, Any] = {"localPart": local_part}
        if domain:
            body["domain"] = domain
        return self._http("POST", "/mailboxes", json_body=body, auth=True)

    # ---------- messages / OTP ----------

    def poll_messages(
        self,
        mailbox: str,
        folder: str = "inbox",
        limit: int = 30,
        version: int | None = None,
    ) -> dict:
        """
        拉取 mailbox 最近邮件.

        mailbox: 'user@example.com'
        返回:
            {
              "unchanged": bool,          # version 模式下列表没变化 → True
              "version": int,             # 下次传回去
              "messages": [ {id, subject, from, to, preview, ...} ],
              "counts": {unread, starred, sent, trash},
              "page":   {hasMore, nextCursor, limit},
            }
        注意: messages 里默认不包含 body. 要正文需 GET /api/messages/{id}.
        """
        params: dict[str, Any] = {
            "folder": folder,
            "mailbox": mailbox,
            "limit": min(max(int(limit or 1), 1), 100),
        }
        if version is not None:
            params["version"] = int(version)
        return self._http("GET", "/messages", params=params, auth=True)

    def get_message_body(self, message_id: str) -> dict:
        """获取邮件完整内容 (含 body HTML/纯文本)."""
        return self._http("GET", f"/messages/{urllib.parse.quote(message_id, safe='')}", auth=True)

    # ---------- iCloud Hide My Email ----------

    def list_icloud_accounts(self) -> list[dict]:
        """列出绑定的 iCloud 账户. [{id, name, realEmail, icloudEmail, host, status, aliasTotal, ...}]"""
        r = self._http("GET", "/icloud/accounts", auth=True)
        if isinstance(r, dict):
            return r.get("accounts") or []
        return []

    def list_icloud_aliases(self, account_id: str) -> list[dict]:
        """列出某 iCloud 账户的 Hide My Email 别名. [{email, anonymousId, label, active, createdAt}]"""
        r = self._http("GET", "/icloud/aliases", params={"accountId": account_id}, auth=True)
        if isinstance(r, dict):
            return r.get("aliases") or []
        return []

    def get_all_icloud_aliases(self) -> list[dict]:
        """全部活跃别名. 返回 [{accountId, email, anonymousId, label, realEmail}, ...]"""
        result: list[dict] = []
        for acc in self.list_icloud_accounts():
            acc_id = str(acc.get("id") or "")
            if not acc_id:
                continue
            real_email = str(acc.get("icloudEmail") or acc.get("realEmail") or "")
            for alias in self.list_icloud_aliases(acc_id):
                if not alias.get("active"):
                    continue
                email = str(alias.get("email") or "").strip().lower()
                if not email:
                    continue
                result.append({
                    "accountId": acc_id,
                    "realEmail": real_email,
                    "email": email,
                    "anonymousId": alias.get("anonymousId"),
                    "label": alias.get("label"),
                })
        return result

    def list_icloud_inbox(
        self,
        account_id: str,
        alias: str | None = None,
        limit: int = 20,
        days: int = 7,
    ) -> list[dict]:
        """拉取 iCloud 收件箱 (可按别名过滤). 返回 messages 列表 (含 uid/subject/from/date/text)."""
        params: dict[str, Any] = {
            "accountId": account_id,
            "limit": min(max(int(limit or 1), 1), 50),
            "days": min(max(int(days or 7), 0), 365),
        }
        if alias:
            params["alias"] = alias
        r = self._http("GET", "/icloud/inbox", params=params, auth=True)
        if isinstance(r, dict):
            return r.get("messages") or []
        return []

    def get_icloud_message(self, account_id: str, uid: str) -> dict | None:
        """获取 iCloud 邮件详情 (text/textBody/body/htmlBody)."""
        r = self._http(
            "GET",
            f"/icloud/inbox/{urllib.parse.quote(str(uid), safe='')}",
            params={"accountId": account_id},
            auth=True,
        )
        if isinstance(r, dict):
            return r.get("message") or r
        return None

    # ---------- config / self ----------

    def get_config(self) -> dict:
        """公开配置 (不需要 auth)."""
        return self._http("GET", "/config", auth=False)

    def whoami(self) -> dict:
        """登录后再拉 user 信息."""
        # OmniMail 没有单独 me 接口, 复用 login 响应里的 user. 这里兜底重拉一次.
        self.login()
        return {"id": "", "email": self.email}

    # ---------- low-level ----------

    def _http(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        auth: bool = True,
    ) -> Any:
        if requests is None:
            raise OmniMailError("requests 未安装, 请 pip install requests")

        url = self.base + path
        headers: dict[str, str] = {"Accept": "application/json"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = f"Bearer {self._ensure_access()}"

        try:
            resp = requests.request(
                method, url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=20,
            )
        except requests.RequestException as exc:
            raise OmniMailError(f"OmniMail {method} {path} 网络错误: {exc}") from exc

        body_text = resp.text or ""
        try:
            body = json.loads(body_text) if body_text.strip() else {}
        except json.JSONDecodeError:
            body = {"raw": body_text}

        if resp.status_code == 401 and auth:
            self._access_token = ""  # 强制下一次 refresh / login
            raise OmniMailAuthError(
                f"OmniMail 401 认证失败 ({method} {path}): {body_text[:200]}"
            )
        if not resp.ok:
            raise OmniMailError(
                f"OmniMail {method} {path} 返回 {resp.status_code}: {body_text[:300]}"
            )

        return body


# ---------- 全局默认实例 (从 config 读取) ----------
_DEFAULT_CLIENT: OmniMailClient | None = None
_DEFAULT_LOCK = threading.Lock()


def default_client() -> OmniMailClient | None:
    """懒加载. 未配置 OMNIMAIL_BASE / OMNIMAIL_EMAIL / OMNIMAIL_PASSWORD 时返回 None."""
    global _DEFAULT_CLIENT
    with _DEFAULT_LOCK:
        if _DEFAULT_CLIENT is not None:
            return _DEFAULT_CLIENT
        try:
            from config.email import (
                OMNIMAIL_BASE, OMNIMAIL_EMAIL, OMNIMAIL_PASSWORD,
            )
        except ImportError:
            return None
        if not OMNIMAIL_BASE or not OMNIMAIL_EMAIL or not OMNIMAIL_PASSWORD:
            return None
        _DEFAULT_CLIENT = OmniMailClient(
            base=OMNIMAIL_BASE,
            email=OMNIMAIL_EMAIL,
            password=OMNIMAIL_PASSWORD,
        )
        return _DEFAULT_CLIENT


def reset_default_client() -> None:
    global _DEFAULT_CLIENT
    with _DEFAULT_LOCK:
        if _DEFAULT_CLIENT is not None:
            _DEFAULT_CLIENT.revoke()
        _DEFAULT_CLIENT = None


# ============================================================
# OTP 提取 (与 outlook_client.fetch_latest_otp 同风格的公共接口)
# ============================================================

def _omni_message_to_item(summary: dict, body: dict | None) -> dict:
    """把 OmniMail 邮件摘要 + 正文转换成 otp_utils 兼容的 item dict."""
    frm = summary.get("from") or {}
    if isinstance(frm, dict):
        from_addr = frm.get("address") or ""
        from_name = frm.get("name") or ""
    else:
        from_addr, from_name = str(frm or ""), ""

    text = ""
    html = ""
    if isinstance(body, dict):
        b = body.get("message") or body
        text = str(b.get("text") or b.get("bodyText") or "")
        html = str(b.get("html") or b.get("bodyHtml") or "")

    return {
        "subject": str(summary.get("subject") or ""),
        "from": from_addr,
        "fromName": from_name,
        "text": text,
        "content": html,
    }


def _parse_mail_ts(created: str) -> float:
    """解析邮件时间字符串为 unix epoch。

    - ISO 带 Z / +00:00 后缀 → 按 UTC 转 epoch (iCloud 端点返回 UTC, 用 mktime 会差时区 8 小时导致新邮件被误判过期)
    - 纯数字 → 毫秒/秒时间戳
    - 其余 ISO → 本地时区解析; 失败则当作新邮件
    """
    import calendar as _calendar
    import time as _time

    raw = (created or "").strip()
    if not raw:
        return _time.time()
    upper = raw.upper()
    try:
        if upper.endswith("Z") or "+00:00" in upper:
            dt = _time.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S")
            return float(_calendar.timegm(dt))
    except Exception:
        pass
    if raw.replace(".", "").isdigit():
        try:
            ts = float(raw)
            return ts / 1000.0 if ts > 10_000_000_000 else ts
        except Exception:
            pass
    try:
        return _time.mktime(_time.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return _time.time()  # 解析不出时间就当作新邮件


def fetch_latest_otp(
    email: str,
    after_ts: float = 0.0,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    """
    轮询 OmniMail 收件箱，等待并提取 Muse/Meta/OpenAI 的 6 位 OTP。

    email:     完整邮箱地址 (mailbox 或 iCloud 别名)
    after_ts:  只接受该 unix 时间之后到达的邮件 (防旧邮件干扰)
    自动分流: iCloud Hide My Email 别名走 /icloud/inbox 端点,
              自建域名 mailbox 走 /messages 端点。
    返回 6 位数字字符串；超时抛 RuntimeError。
    """
    import time as _time

    from config.email import (
        OTP_MAX_WAIT, OTP_POLL_INTERVAL, OTP_SETTLE_SECONDS,
    )
    from core.otp_utils import looks_like_openai_email, extract_otp

    client = default_client()
    if client is None:
        raise OmniMailError("OmniMail 未配置 (OMNIMAIL_BASE/EMAIL/PASSWORD)")

    wait_total = int(max_wait if max_wait is not None else OTP_MAX_WAIT)
    interval = int(poll_interval if poll_interval is not None else OTP_POLL_INTERVAL)
    settle = int(settle_seconds if settle_seconds is not None else OTP_SETTLE_SECONDS)

    # 判断该邮箱是否为 iCloud 别名 (payload.kind == 'icloud_alias')
    account_id = ""
    email_l = (email or "").strip().lower()
    try:
        from core import db as _db
        row = _db.get_omnimail_by_email(email_l)
        if row:
            payload = row.get("payload")
            if isinstance(payload, str):
                payload = json.loads(payload or "{}")
            if isinstance(payload, dict) and payload.get("kind") == "icloud_alias":
                account_id = str(payload.get("accountId") or "")
    except Exception as exc:
        logger.warning(f"[OmniMail] 查询 {email} 池信息失败, 按普通 mailbox 处理: {exc}")

    deadline = _time.time() + wait_total
    candidate: tuple[float, str] | None = None  # (mail_ts, otp)
    poll_count = 0

    while _time.time() < deadline:
        poll_count += 1
        try:
            if account_id:
                # iCloud 别名: 直接拉该别名的收件箱
                msgs = client.list_icloud_inbox(account_id, alias=email_l, limit=20)
            else:
                res = client.poll_messages(email_l, folder="inbox", limit=20)
                msgs = res.get("messages") or []
        except OmniMailError as exc:
            logger.warning(f"[OmniMail] poll {email} 失败: {exc}")
            _time.sleep(interval)
            continue

        matched = filtered = nomatch = 0
        for m in msgs:
            created = str(
                m.get("createdAt") or m.get("date") or m.get("dateSent") or ""
            )
            mail_ts = _parse_mail_ts(created)
            if mail_ts < after_ts:
                filtered += 1
                continue

            item = _omni_message_to_item(m, None)
            if not looks_like_openai_email(item):
                nomatch += 1
                continue

            if account_id:
                uid = str(m.get("uid") or m.get("id") or "")
                try:
                    detail = client.get_icloud_message(account_id, uid)
                except OmniMailError as exc:
                    logger.warning(f"[OmniMail] iCloud 正文读取失败 uid={uid}: {exc}")
                    continue
                item = _omni_message_to_item(m, detail)
            else:
                try:
                    body = client.get_message_body(str(m.get("id") or ""))
                except OmniMailError as exc:
                    logger.warning(f"[OmniMail] 读取正文失败 id={m.get('id')}: {exc}")
                    continue
                item = _omni_message_to_item(m, body)

            matched += 1
            code = extract_otp(item)
            if code:
                if candidate is None or mail_ts > candidate[0]:
                    candidate = (mail_ts, code)
                break  # 这一封够了
            else:
                logger.info(
                    f"[OmniMail] 邮件匹配品牌但未提取到6位码: id={m.get('id')} subject={str(item.get('subject'))[:50]}"
                )

        if poll_count <= 3 or poll_count % 12 == 0 or candidate:
            logger.info(
                f"[OmniMail] poll#{poll_count} {email}: 收到{len(msgs)}封 "
                f"时间窗内{matched} 非品牌{nomatch} 过期{filtered} "
                f"(icloud={bool(account_id)} otp={candidate[1] if candidate else '-'})"
            )

        if candidate:
            if settle > 0:
                _time.sleep(settle)  # 等等看有没有更新的 OTP
            logger.info(f"[OmniMail] {email} 取到 OTP: {candidate[1]}")
            return candidate[1]

        _time.sleep(interval)

    raise RuntimeError(f"[OmniMail] {email} 等待 OTP 超时 ({wait_total}s)")
