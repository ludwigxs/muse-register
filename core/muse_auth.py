# -*- coding: utf-8 -*-
"""
Muse by Meta 注册核心流程

API 端点（真浏览器抓包验证, 2026-10-02）:
    GET  /                       - 预热, 拿 datr cookie
    POST /api/auth/native/restart         - 重置认证会话 → 返回 csrf_token
    POST /api/auth/native/send-otp        - body: {"contact_point": email, "region": "US"}
    POST /api/auth/native/confirm-otp     - body: {"otp_code": "123456"}
    POST /api/auth/caa-events             - CAA 客户端事件上报
    POST /api/falco                       - 反爬/风控检测上报

Hatch 头 (send-otp / confirm-otp 必带):
    X-Hatch-CSRF-Token: <restart 返回的 csrf_token>
    X-Hatch-Waterfall-Id: <uuid4, 整个流程共用>
    X-Hatch-Caa-Reg-Entry-Point: login_home

注册流程:
    GET / -> restart -> send_otp -> (收码) -> confirm_otp -> 账户创建完成
    confirm 成功后落地 muse.ai/access (若出口 IP 非 US, 提示区域未开放)
"""
import json
import logging
import re
import time
import uuid
from typing import Any

from config import BASE_URL
from core.session import MuseSession

logger = logging.getLogger(__name__)

# 账户区域: Muse 仅美加开放, 注册时声明 US
MUSE_REGION = "US"


class MuseAuthError(RuntimeError):
    """Muse 认证过程中的业务错误基类。"""
    pass


class OtpInvalidError(MuseAuthError):
    """OTP 验证码错误或过期。"""
    pass


class RateLimitError(MuseAuthError):
    """触发速率限制或风控。"""
    pass


# ============================================================
# 内部工具
# ============================================================

def _url(path: str) -> str:
    """拼接完整 URL。"""
    if path.startswith("http"):
        return path
    return f"{BASE_URL}{path}"


def _parse_json(resp) -> dict[str, Any]:
    """安全解析 JSON 响应。"""
    try:
        return resp.json()
    except Exception:
        text = getattr(resp, "text", "")[:500]
        logger.warning(f"JSON 解析失败，原始响应: {text}")
        return {}


_OWN_INVITE_KEYS = {
    "invite_code", "invitecode", "referral_code", "referralcode",
    "invite_code_value", "referral_code_value",
}
_INVITE_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


def _extract_own_invite_code(data: Any, parent_key: str = "") -> str:
    """从邀请码查询接口的兼容响应中提取账号自己的邀请码。"""
    if isinstance(data, dict):
        for key, value in data.items():
            normalized_key = str(key).replace("-", "_").lower()
            if isinstance(value, str):
                is_code_field = normalized_key in _OWN_INVITE_KEYS
                is_nested_code = normalized_key == "code" and any(
                    marker in parent_key.lower() for marker in ("invite", "referral")
                )
                candidate = value.strip()
                if (is_code_field or is_nested_code) and _INVITE_CODE_RE.fullmatch(candidate):
                    return candidate
            found = _extract_own_invite_code(value, normalized_key)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _extract_own_invite_code(item, parent_key)
            if found:
                return found
    return ""


def _check_errors(resp, data: dict) -> None:
    """
    检查业务层错误。
    Muse 错误格式可能是 {error: "..."} 或 {code: "CSRF_FAILED"}。
    """
    if not isinstance(data, dict):
        return
    error_msg = (
        data.get("error")
        or data.get("message")
        or data.get("detail")
    )
    error_code = data.get("code") or data.get("type")
    status = getattr(resp, "status_code", 200)

    if status == 429:
        raise RateLimitError(f"HTTP 429 速率限制: {error_msg or 'too many requests'}")

    if error_code in ("csrfFailed", "CSRF_FAILED"):
        raise MuseAuthError(
            f"CSRF 校验失败 (HTTP {status}): {error_msg or 'Refresh the page and try again'}"
            " —— 需先访问 muse.ai 页面获取 CSRF cookie"
        )

    if error_msg:
        raise MuseAuthError(f"HTTP {status}: {error_msg}")


# ============================================================
# MuseAuth 认证类
# ============================================================

class MuseAuth:
    """封装 Muse 原生邮箱认证流程。"""

    def __init__(self, session: MuseSession):
        self.session = session
        self.auth_state: dict[str, Any] = {}
        self.csrf_token: str = ""
        self.waterfall_id: str = str(uuid.uuid4())

    # ---------- 基础 API ----------

    def _hatch_headers(self) -> dict:
        """Hatch 认证头 (send-otp / confirm-otp 必带)。"""
        headers = {
            "X-Hatch-CSRF-Token": self.csrf_token,
            "X-Hatch-Waterfall-Id": self.waterfall_id,
            "X-Hatch-Caa-Reg-Entry-Point": "login_home",
        }
        return {k: v for k, v in headers.items() if v}

    def _warmup(self) -> None:
        """GET / 预热, 获取 datr 等 Meta cookie。"""
        try:
            self.session.get(f"{BASE_URL}/", headers={
                "Accept": "text/html,application/xhtml+xml,*/*",
                "Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document",
            })
        except Exception as e:
            logger.warning(f"[Auth] 预热失败（继续）: {type(e).__name__}: {e}")

    def restart(self) -> dict:
        """
        POST /api/auth/native/restart
        重置认证会话，返回 csrf_token (后续 send-otp/confirm-otp 必用)。
        """
        if not self.session.session.cookies.get("datr"):
            self._warmup()

        logger.info("[Auth] 重置认证会话 (restart)")
        resp = self.session.post(_url("/api/auth/native/restart"), json={})
        data = _parse_json(resp)
        _check_errors(resp, data)
        self.csrf_token = data.get("csrf_token") or ""
        if not self.csrf_token:
            raise MuseAuthError(f"restart 未返回 csrf_token: {json.dumps(data, ensure_ascii=False)[:200]}")
        logger.info(f"[Auth] csrf_token={self.csrf_token[:16]}... waterfall={self.waterfall_id[:8]}...")
        return data

    def send_otp(self, email: str) -> dict:
        """
        POST /api/auth/native/send-otp
        向指定邮箱发送 OTP 验证码。
        body: {"contact_point": email, "region": "US"}
        """
        logger.info(f"[Auth] 发送 OTP 到 {email}")
        resp = self.session.post(
            _url("/api/auth/native/send-otp"),
            json={"contact_point": email, "region": MUSE_REGION},
            headers=self._hatch_headers(),
        )
        data = _parse_json(resp)
        _check_errors(resp, data)

        # 记录 auth state
        self.auth_state["email"] = email
        self.auth_state["otp_sent_at"] = time.time()

        logger.info(f"[Auth] OTP 已发送 (HTTP {resp.status_code})")
        logger.debug(f"[Auth] send-otp 响应: {json.dumps(data, ensure_ascii=False)[:300]}")
        return data

    def confirm_otp(self, otp_code: str) -> dict:
        """
        POST /api/auth/native/confirm-otp
        提交 6 位 OTP 验证码进行验证。body: {"otp_code": "123456"}

        Raises:
            OtpInvalidError: 验证码错误或过期
            MuseAuthError: 其他业务错误
        """
        email = self.auth_state.get("email", "")
        logger.info(f"[Auth] 提交 OTP: {otp_code[:3]}*** (email={email})")

        resp = self.session.post(
            _url("/api/auth/native/confirm-otp"),
            json={"otp_code": otp_code},
            headers=self._hatch_headers(),
        )
        data = _parse_json(resp)

        # 识别 OTP 错误
        error_code = str(data.get("code") or data.get("type") or "").lower()
        error_msg = str(data.get("error") or data.get("message") or "").lower()
        if "otp" in error_msg or "otp" in error_code:
            if "invalid" in error_msg or "expired" in error_msg or "wrong" in error_msg:
                raise OtpInvalidError(f"OTP 错误: {error_msg or data}")

        _check_errors(resp, data)

        logger.info("[Auth] OTP 验证成功!")
        logger.debug(f"[Auth] confirm-otp 响应: {json.dumps(data, ensure_ascii=False)[:500]}")
        return data

    # ---------- 反爬事件上报 ----------

    def report_caa_events(self, events: list[dict] | None = None) -> None:
        """
        POST /api/auth/caa-events
        上报客户端行为事件（反爬检测）。
        浏览器会上报一些表单交互事件，模拟以降低风控概率。
        """
        default_events = events or [
            {"type": "page_view", "path": "/join", "ts": int(time.time() * 1000)},
        ]
        try:
            self.session.post(
                _url("/api/auth/caa-events"),
                json={"events": default_events},
            )
        except Exception as e:
            logger.debug(f"[CAA] 事件上报失败（可忽略）: {e}")

    def report_falco(self) -> None:
        """
        POST /api/falco
        Falco 风控检测上报。浏览器 JS 执行后会调用此接口。
        模拟请求体以通过基础检测。
        """
        try:
            self.session.post(
                _url("/api/falco"),
                json={
                    "device": self.session.device_id,
                    "events": [],
                },
            )
        except Exception as e:
            logger.debug(f"[Falco] 上报失败（可忽略）: {e}")

    # ---------- 完整注册流程 ----------

    def register(self, email: str, otp_callback=None) -> dict:
        """
        执行完整的 Muse 注册流程。

        Args:
            email: 注册用邮箱
            otp_callback: 当需要 OTP 时调用的回调函数，签名 callback(email) -> str
                          返回 6 位 OTP 验证码字符串

        Returns:
            dict: 注册结果，包含 access_token、account 信息等
        """
        logger.info(f"[Register] 开始注册 Muse 账号: {email}")

        # 步骤 1: 重置会话
        self.restart()

        # 步骤 2: 上报风控事件（可选）
        self.report_caa_events()
        self.report_falco()

        # 步骤 3: 发送 OTP
        self.send_otp(email)

        # 步骤 4: 获取 OTP
        if otp_callback is None:
            otp_code = input(f"[OTP] 请输入 {email} 收到的 6 位验证码: ").strip()
        else:
            otp_code = otp_callback(email)

        if not otp_code:
            raise MuseAuthError("未获取到 OTP 验证码")

        # 步骤 5: 验证 OTP
        confirm_result = self.confirm_otp(otp_code)
        self.auth_state["confirm_result"] = confirm_result

        # 步骤 6: 检查后续流程
        # OTP 验证通过后，服务端可能返回 action 指示下一步：
        #   - 直接完成注册（返回 access_token）
        #   - 需要填写个人信息（姓名/生日）
        #   - 需要完成银行卡验证
        next_action = confirm_result.get("action") or confirm_result.get("next") or ""
        logger.info(f"[Register] OTP 后续 action: {next_action or '(无)'}")

        # 尝试从响应中提取 token
        access_token = (
            confirm_result.get("access_token")
            or confirm_result.get("accessToken")
            or (confirm_result.get("data") or {}).get("access_token")
            or (confirm_result.get("data") or {}).get("accessToken")
        )

        if access_token:
            logger.info("[Register] 直接获取到 access_token")
        else:
            logger.info("[Register] OTP 后未直接返回 token，可能需要额外步骤")

        return {
            "success": True,
            "email": email,
            "access_token": access_token,
            "confirm_result": confirm_result,
            "auth_state": self.auth_state,
        }

    # ---------- 邀请码兑换 ----------

    def fetch_own_invite_code(self, access_token: str = "") -> str:
        """读取当前已登录账号的邀请码，不修改账号状态或邀请码状态。"""
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else None
        # Muse 前端版本间接口名称存在差异，只尝试幂等 GET 端点。
        paths = (
            "/api/auth/native/invite-code",
            "/api/auth/native/invite",
            "/api/invite/code",
            "/api/invite",
            "/api/referral/code",
            "/api/referral",
            "/api/user/invite-code",
            "/api/user/referral",
        )
        for path in paths:
            try:
                response = self.session.get(_url(path), headers=headers)
                if response.status_code != 200:
                    continue
                code = _extract_own_invite_code(_parse_json(response))
                if code:
                    logger.info("[Invite] 已读取当前账号邀请码")
                    return code
            except Exception as exc:
                logger.debug("[Invite] 查询账号邀请码失败 (%s): %s", path, exc)
        logger.warning("[Invite] 未从兼容接口读取到当前账号邀请码")
        return ""

    def redeem_invite_code(self, invite_code: str) -> dict:
        """
        兑换邀请码获得 10 亿 tokens。
        注意: 此接口需要先登录（有 access_token）。

        注册后 48 小时内可兑换，过期无效。
        """
        logger.info(f"[Invite] 兑换邀请码: {invite_code}")

        # 从 confirm_result 或后续 API 查找 redeem 端点
        # 先尝试常见路径
        redeem_paths = [
            "/api/auth/native/redeem",
            "/api/invite/redeem",
            "/api/redeem",
        ]

        last_error = None
        for path in redeem_paths:
            try:
                resp = self.session.post(
                    _url(path),
                    json={"code": invite_code},
                )
                if resp.status_code == 200:
                    data = _parse_json(resp)
                    # 有些版本用 HTTP 200 返回业务失败，需要继续尝试兼容端点。
                    if isinstance(data, dict) and data.get("success") is False:
                        last_error = str(data.get("error") or data.get("message") or f"业务失败 from {path}")
                        continue
                    logger.info(f"[Invite] 兑换成功 (path={path})")
                    return {"success": True, "data": data}
                else:
                    last_error = f"HTTP {resp.status_code} from {path}"
            except Exception as e:
                last_error = str(e)

        logger.warning(f"[Invite] 所有路径兑换失败: {last_error}")
        return {"success": False, "error": last_error}
