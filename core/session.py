# -*- coding: utf-8 -*-
"""
curl_cffi Session 封装 - Muse by Meta

支持代理池 session-sticky IP:
    pick_proxy(account_index, session_id) → 固定出口
"""
import logging
import uuid
from curl_cffi.requests import Session

from config import USER_AGENT, IMPERSONATE, REQUEST_TIMEOUT
from config.proxy import pick_proxy

logger = logging.getLogger(__name__)


class MuseSession:
    """模拟 Chrome 浏览器的 HTTP 会话管理器。"""

    def __init__(
        self,
        proxy: str | None = None,
        *,
        account_index: int | str | None = None,
        session_id: str | None = None,
    ):
        """
        Args:
            proxy: 显式代理 URL (不传则从池 pick)
            account_index: 代理池子账号后缀 (account_index.N)
            session_id: 代理池 session 粘滞 (session-xxx-sessiontime-900)
        """
        # 1) 确定代理
        if proxy is None:
            proxy = pick_proxy(account_index=account_index, session_id=session_id)
        self.proxy = proxy or None
        self.account_index = str(account_index) if account_index is not None else None
        self.session_sticky_id = session_id

        # 2) 会话标识
        self.session_id = session_id or str(uuid.uuid4())
        self.device_id = str(uuid.uuid4())

        # 3) 创建 curl_cffi
        self.session = Session(impersonate=IMPERSONATE)

        if self.proxy:
            self.session.proxies = {"http": self.proxy, "https": self.proxy}
            logger.info(
                f"[Session] proxy={self.proxy[:80]}..." if len(self.proxy) > 80
                else f"[Session] proxy={self.proxy}"
            )
        else:
            logger.info("[Session] direct (no proxy)")

        self.session.timeout = REQUEST_TIMEOUT

        # 4) 浏览器环境 Cookie
        self.session.cookies.set("wd", "1920x1080", domain=".muse.ai")
        self.session.cookies.set("dpr", "1", domain=".muse.ai")

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _base_headers(self, referer: str | None = None) -> dict:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://muse.ai",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Priority": "u=1, i",
        }
        if referer:
            headers["Referer"] = referer
        return headers

    def get(self, url: str, headers: dict | None = None, **kwargs):
        merged = self._base_headers()
        if headers:
            merged.update(headers)
        return self.session.get(url, headers=merged, **kwargs)

    def post(self, url: str, json: dict | None = None, headers: dict | None = None, **kwargs):
        merged = self._base_headers()
        if headers:
            merged.update(headers)
        return self.session.post(url, json=json, headers=merged, **kwargs)
