# -*- coding: utf-8 -*-
"""RoxyBrowser 本地 API 客户端。"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlparse

import requests

from config import roxybrowser as _cfg

logger = logging.getLogger(__name__)

# Roxy 的 /browser/create 在多 worker 同时到达时可能返回"正在创建中"。
# 为所有客户端实例共享创建时隙，确保进程内请求起始时间至少错开配置的间隔。
_CREATE_SLOT_LOCK = threading.Lock()
_NEXT_CREATE_SLOT = 0.0
# 独立 Profile 可以并行打开。旧实现把“创建 Profile + 打开窗口”整个流程放在
# 同一把锁里，Roxy 首次启动一个新环境可能等待数分钟，导致后续并发任务一直
# 卡在锁外。Profile 创建仍由 _PROFILE_LOCK 保护；窗口打开不再全局串行。

# 已打开的 Roxy 环境占用的窗口槽位。按 profile 记录，关闭环境后释放，
# 这样并发任务会填充最靠前的空位，不会因为历史任务导致窗口越排越远。
_WINDOW_SLOT_LOCK = threading.Lock()
_WINDOW_SLOTS: dict[str, int] = {}


def reset_window_layout() -> None:
    """开始一轮注册前重置窗口槽位。

    槽位只描述当前这一轮要打开的窗口，不应把上一轮已经结束/保留的
    Profile 数量带入下一轮，否则第二轮会直接从下一排开始排列。
    """
    with _WINDOW_SLOT_LOCK:
        _WINDOW_SLOTS.clear()
    logger.info("[Roxy] 已重置本轮窗口排列槽位")


def _reserve_window_slot(profile_id: str) -> int:
    """为可见 Roxy 窗口分配一个稳定的网格槽位。"""
    with _WINDOW_SLOT_LOCK:
        if profile_id in _WINDOW_SLOTS:
            return _WINDOW_SLOTS[profile_id]
        used = set(_WINDOW_SLOTS.values())
        slot = 0
        while slot in used:
            slot += 1
        _WINDOW_SLOTS[profile_id] = slot
        return slot


def _release_window_slot(profile_id: str) -> None:
    with _WINDOW_SLOT_LOCK:
        _WINDOW_SLOTS.pop(str(profile_id), None)


def _window_geometry(slot: int) -> tuple[int, int, int, int]:
    """计算窗口网格坐标；超过最大行数后复用最后一行位置。"""
    columns = max(1, int(getattr(_cfg, "ROXY_WINDOW_LAYOUT_COLUMNS", 4) or 4))
    max_rows = max(1, int(getattr(_cfg, "ROXY_WINDOW_LAYOUT_MAX_ROWS", 2) or 2))
    gap = max(0, int(getattr(_cfg, "ROXY_WINDOW_GAP", 0) or 0))
    origin_x = int(getattr(_cfg, "ROXY_WINDOW_ORIGIN_X", 0) or 0)
    origin_y = int(getattr(_cfg, "ROXY_WINDOW_ORIGIN_Y", 0) or 0)
    width = max(320, int(getattr(_cfg, "ROXY_WINDOW_WIDTH", 720) or 720))
    height = max(240, int(getattr(_cfg, "ROXY_WINDOW_HEIGHT", 820) or 820))

    # 在 Windows 上按当前桌面大小切成 4 列 × 2 行；失败时回退到配置尺寸。
    if bool(getattr(_cfg, "ROXY_WINDOW_FIT_SCREEN", True)):
        try:
            import ctypes
            import os
            if os.name == "nt":
                user32 = ctypes.windll.user32
                screen_width = int(user32.GetSystemMetrics(0))
                screen_height = int(user32.GetSystemMetrics(1))
                if screen_width > 0 and screen_height > 0:
                    width = max(320, (screen_width - gap * (columns - 1)) // columns)
                    height = max(240, (screen_height - gap * (max_rows - 1)) // max_rows)
        except Exception:
            pass

    column = int(slot) % columns
    row = min(int(slot) // columns, max_rows - 1)
    return (
        origin_x + column * (width + gap),
        origin_y + row * (height + gap),
        width,
        height,
    )


def _apply_window_layout(params: dict, profile_id: str) -> tuple[dict, int | None]:
    """向 /browser/open 参数加入不重叠的 Chromium 窗口位置/尺寸。"""
    if bool(getattr(_cfg, "ROXY_OPEN_HEADLESS", False)):
        return params, None
    if not bool(getattr(_cfg, "ROXY_WINDOW_LAYOUT_ENABLED", True)):
        return params, None

    slot = _reserve_window_slot(str(profile_id))
    x, y, width, height = _window_geometry(slot)

    raw_args = params.get("args")
    args = list(raw_args) if isinstance(raw_args, (list, tuple)) else ([str(raw_args)] if raw_args else [])
    # 布局开关打开时由本程序统一管理这两个参数，避免旧配置覆盖位置。
    args = [arg for arg in args if not str(arg).startswith(("--window-position=", "--window-size="))]
    args.extend([f"--window-position={x},{y}", f"--window-size={width},{height}"])
    params["args"] = args
    logger.info("[Roxy] 窗口排列：profile=%s slot=%d position=%d,%d size=%dx%d", profile_id, slot, x, y, width, height)
    return params, slot


def _extract_process_id(payload: dict) -> int | None:
    """从 Roxy /browser/open 响应中读取浏览器进程 PID。"""
    candidates = (
        ("pid",), ("processId",), ("process_id",), ("browserPid",),
        ("data", "pid"), ("data", "processId"), ("data", "process_id"),
        ("data", "browserPid"),
    )
    for path in candidates:
        value = payload
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        try:
            pid = int(value)
        except (TypeError, ValueError):
            continue
        if pid > 0:
            return pid
    return None


def _position_roxy_window(process_id: int | None, slot: int | None) -> bool:
    """用 Windows 原生窗口句柄定位 Roxy 窗口。"""
    if process_id is None or slot is None:
        return False
    try:
        import ctypes
        import os
        if os.name != "nt":
            return False
        x, y, width, height = _window_geometry(slot)

        user32 = ctypes.windll.user32
        enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        found = {"hwnd": 0}

        @enum_proc_type
        def enum_proc(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            window_pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
            if int(window_pid.value) == int(process_id):
                found["hwnd"] = hwnd
                return False
            return True

        user32.EnumWindows(enum_proc, 0)
        hwnd = found["hwnd"]
        if not hwnd:
            return False
        flags = 0x0010 | 0x0040  # SWP_NOACTIVATE | SWP_SHOWWINDOW
        ok = bool(user32.SetWindowPos(hwnd, 0, x, y, width, height, flags))
        if ok:
            logger.info("[Roxy] 原生窗口已排列：pid=%s slot=%s position=%d,%d size=%dx%d", process_id, slot, x, y, width, height)
        return ok
    except Exception as exc:
        logger.debug("[Roxy] 原生窗口排列失败（继续）：%s", exc)
        return False


def _position_roxy_window_when_ready(process_id: int | None, slot: int | None) -> None:
    """等待 Roxy 顶层窗口出现后定位，避免 API 先返回而窗口句柄尚未注册。"""
    if process_id is None or slot is None:
        return
    for _ in range(20):
        if _position_roxy_window(process_id, slot):
            return
        time.sleep(0.25)
    logger.debug("[Roxy] 未找到待排列窗口：pid=%s slot=%s", process_id, slot)


def _wait_for_create_slot() -> None:
    global _NEXT_CREATE_SLOT

    interval = max(0.0, float(getattr(_cfg, "ROXY_CREATE_INTERVAL", 1.5) or 0.0))
    if interval <= 0:
        return

    with _CREATE_SLOT_LOCK:
        now = time.monotonic()
        wait_for = max(0.0, _NEXT_CREATE_SLOT - now)
        _NEXT_CREATE_SLOT = max(now, _NEXT_CREATE_SLOT) + interval

    if wait_for > 0:
        logger.info("[Roxy] /browser/create 请求错峰，等待 %.2fs", wait_for)
        time.sleep(wait_for)


# Roxy 同一 workspace 的 profile 创建/打开/关闭必须串行，
# 并发会触发「正在创建中，请稍等！」或窗口状态错乱。
# 用模块级 Lock 保证跨 worker 线程互斥。
_PROFILE_LOCK = threading.Lock()


@dataclass
class RoxyOpenResult:
    profile_id: str
    raw: dict
    debugger_address: str | None = None
    webdriver_url: str | None = None
    ws_endpoint: str | None = None
    created_by_run: bool = False


def _strip_slashes(value: str) -> str:
    return str(value or "").strip().strip("/")


def _join_url(base: str, path: str) -> str:
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _mask_proxy(proxy_url: str) -> str:
    parsed = urlparse(str(proxy_url or "").strip())
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://***:***@{host}{port}"
    return str(proxy_url or "").strip()


def _proxy_url_to_roxy_info(proxy_url: str) -> dict:
    """
    将 config/proxy.py 里的代理 URL 转成 Roxy /browser/create 的 proxyInfo。

    支持：
      http://user:pass@host:port
      https://user:pass@host:port
      socks5://user:pass@host:port
      socks5h://user:pass@host:port  -> Roxy 侧按 SOCKS5 处理
    """
    text = str(proxy_url or "").strip()
    if not text:
        raise ValueError("代理为空")
    parsed = urlparse(text)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https", "socks5", "socks5h"):
        raise ValueError(f"Roxy 暂不支持该代理协议: {scheme or '-'}")
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"代理格式缺少 host/port: {_mask_proxy(text)}")

    protocol = {
        "http": "HTTP",
        "https": "HTTPS",
        "socks5": "SOCKS5",
        "socks5h": "SOCKS5",
    }[scheme]
    # Roxy /browser/create 官方字段是：
    # proxyMethod / proxyCategory / ipType / protocol / host / port / proxyUserName / proxyPassword / checkChannel
    # 之前误用了 proxyType/proxyHost/proxyPort/proxyAccount，Roxy 会忽略，导致创建窗口实际未设置代理。
    info = {
        "moduleId": 0,
        "proxyMethod": "custom",
        "proxyCategory": protocol,
        "ipType": "IPV4",
        "protocol": protocol,
        "host": parsed.hostname,
        "port": str(parsed.port),
    }
    if parsed.username:
        info["proxyUserName"] = unquote(parsed.username)
    if parsed.password:
        info["proxyPassword"] = unquote(parsed.password)
    check_channel = str(getattr(_cfg, "ROXY_PROXY_CHECK_CHANNEL", "") or "").strip()
    if check_channel:
        info["checkChannel"] = check_channel
    return info


def _dig(payload: dict, *keys: str):
    cur = payload
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _first(payload: dict, paths: list[tuple[str, ...]]) -> str:
    for path in paths:
        value = _dig(payload, *path)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _workspace_id_value() -> str | int:
    raw = str(getattr(_cfg, "ROXY_WORKSPACE_ID", "") or "").strip()
    if not raw:
        return ""
    return int(raw) if raw.isdigit() else raw


def _project_id_value() -> str | int:
    raw = str(getattr(_cfg, "ROXY_PROJECT_ID", "") or "").strip()
    if not raw:
        return ""
    return int(raw) if raw.isdigit() else raw


def _apply_data_saver_open_args(params: dict) -> dict:
    """在 Roxy 启动参数中尽早关闭图片加载，覆盖无扩展名图片 URL。

    Network.setBlockedURLs 只能按 URL 后缀拦截，而 Roxy 浏览器在 Selenium 连接
    前就已经启动；使用 Chromium 开关可以让图片在首个页面请求前就被禁用。该开关
    只在用户明确开启省流量模式且包含 image 类型时追加。
    """
    try:
        from config import browser as _browser_cfg

        if not bool(getattr(_browser_cfg, "BROWSER_DATA_SAVER_MODE", False)):
            return params
        raw_types = getattr(_browser_cfg, "BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES", [])
        if isinstance(raw_types, str):
            types = {item.strip().lower() for item in raw_types.replace(",", "\n").splitlines() if item.strip()}
        else:
            types = {str(item or "").strip().lower() for item in (raw_types or []) if str(item or "").strip()}
        if "image" not in types and "images" not in types and "img" not in types:
            return params

        current = params.get("args")
        if isinstance(current, (list, tuple)):
            args = list(current)
        elif current:
            args = [str(current)]
        else:
            args = []
        switch = "--blink-settings=imagesEnabled=false"
        if switch not in args:
            args.append(switch)
        params["args"] = args
    except Exception as exc:
        logger.debug("[Roxy] 添加省流量图片启动参数失败，继续使用原参数：%s", exc)
    return params


def _random_roxy_os() -> str:
    raw = str(getattr(_cfg, "ROXY_RANDOM_OS_CHOICES", "Windows,macOS") or "Windows,macOS")
    choices = [
        x.strip()
        for part in raw.replace("\n", ",").replace(";", ",").split(",")
        for x in [part]
        if x.strip()
    ]
    valid = {"Windows", "macOS", "Linux", "IOS", "Android"}
    choices = [x for x in choices if x in valid]
    if not choices:
        choices = ["Windows", "macOS"]
    return random.choice(choices)


def _random_roxy_profile_name() -> str:
    prefix = str(getattr(_cfg, "ROXY_PROFILE_NAME_PREFIX", "rb") or "rb").strip() or "rb"
    # Roxy 环境名每次创建都不同：前缀 + 毫秒时间戳 + 随机 4 位十六进制。
    return f"{prefix}-{int(time.time() * 1000)}-{random.randrange(0x10000):04x}"


class RoxyBrowserClient:
    def __init__(self, api_base: str | None = None, token: str | None = None):
        self.api_base = (api_base or _cfg.ROXY_API_BASE).strip()
        self.token = (token if token is not None else _cfg.ROXY_API_TOKEN).strip()
        self._proxy_pool_relay = None
        self._proxy_pool_target = ""
        self.http = requests.Session()
        if self.token:
            # 官方文档要求所有接口请求头必须加 token。这里同时兼容 token / Authorization。
            self.http.headers.update({
                "token": self.token,
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            })

    @staticmethod
    def _is_retryable_error(exc: Exception) -> bool:
        text = str(exc or "").lower()
        return (
            "timeout" in text
            or "timed out" in text
            or "connection" in text
            or "temporarily" in text
            or "http 500" in text
            or "http 502" in text
            or "http 503" in text
            or "http 504" in text
            or "http 429" in text
        )

    def request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> dict:
        url = _join_url(self.api_base, path)
        method_u = method.upper()
        is_create = str(path or "").rstrip("/").endswith("/create") or "browser/create" in str(path or "")
        if is_create:
            max_attempts = max(1, int(getattr(_cfg, "ROXY_CREATE_RETRIES", 3) or 3))
            base_delay = max(0.5, float(getattr(_cfg, "ROXY_CREATE_RETRY_DELAY", 3) or 3))
        else:
            max_attempts = max(1, int(getattr(_cfg, "ROXY_API_RETRIES", 3) or 3))
            base_delay = max(0.5, float(getattr(_cfg, "ROXY_API_RETRY_DELAY", 2) or 2))
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                logger.debug(
                    "[Roxy] %s %s params=%s body=%s attempt=%s/%s",
                    method, url, params, json_body, attempt, max_attempts,
                )
                resp = self.http.request(
                    method_u,
                    url,
                    params=params or None,
                    json=json_body if json_body is not None else None,
                    timeout=max(5, int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)),
                )
                text = resp.text or ""
                try:
                    payload = resp.json()
                except Exception:
                    payload = {"raw": text}
                if not (200 <= resp.status_code < 300):
                    raise RuntimeError(f"Roxy API 请求失败 {method_u} {path} HTTP {resp.status_code}: {text[:500]}")
                if isinstance(payload, dict):
                    code = payload.get("code")
                    ok = payload.get("ok")
                    success = payload.get("success")
                    if code not in (None, 0, 200, "0", "200") and ok is not True and success is not True:
                        msg = payload.get("msg") or payload.get("message") or payload.get("error") or json.dumps(payload, ensure_ascii=False)[:500]
                        raise RuntimeError(f"Roxy API 返回失败 {method_u} {path}: {msg}")
                if attempt > 1:
                    logger.info("[Roxy] API 重试成功：%s %s attempt=%s/%s", method_u, path, attempt, max_attempts)
                return payload if isinstance(payload, dict) else {"data": payload}
            except Exception as exc:
                last_exc = exc
                retryable = self._is_retryable_error(exc)
                if attempt >= max_attempts or not retryable:
                    raise
                delay = base_delay * attempt
                logger.warning(
                    "[Roxy] API 请求失败，将在 %.1fs 后使用相同%s重试：%s %s attempt=%s/%s error=%s",
                    delay,
                    " create payload/name " if is_create else "请求参数",
                    method_u, path, attempt, max_attempts, exc,
                )
                time.sleep(delay)
        raise last_exc or RuntimeError(f"Roxy API 请求失败 {method_u} {path}")

    def try_request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None) -> tuple[bool, dict | str]:
        """宽松请求：用于探测不同 Roxy 版本接口，失败不抛出。"""
        try:
            return True, self.request(method, path, params=params, json_body=json_body)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _extract_workspace_items(payload: dict) -> list[dict]:
        """解析 /browser/workspace：团队 rows + project_details 项目列表；兼容递归兜底。"""
        out = []

        # 官方结构：data.rows[].id/workspaceName/project_details[].projectId/projectName
        rows = None
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                rows = data.get("rows") or data.get("list") or data.get("records")
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                wid = row.get("id") or row.get("workspaceId") or row.get("workspace_id")
                wname = row.get("workspaceName") or row.get("workspace_name") or row.get("name") or str(wid or "")
                projects = row.get("project_details") or row.get("projectDetails") or row.get("projects") or []
                if isinstance(projects, list) and projects:
                    for proj in projects:
                        if not isinstance(proj, dict):
                            continue
                        pid = proj.get("projectId") or proj.get("project_id") or proj.get("id")
                        pname = proj.get("projectName") or proj.get("project_name") or proj.get("name") or str(pid or "")
                        if wid:
                            out.append({
                                "id": str(wid),
                                "name": str(wname),
                                "projectId": str(pid or ""),
                                "projectName": str(pname or ""),
                                "label": f"{wname} / {pname} ({wid}/{pid})" if pid else f"{wname} ({wid})",
                                "raw": {"workspace": row, "project": proj},
                            })
                elif wid:
                    out.append({
                        "id": str(wid),
                        "name": str(wname),
                        "projectId": "",
                        "projectName": "",
                        "label": f"{wname} ({wid})",
                        "raw": row,
                    })

        if out:
            return out

        # 兜底：递归抽 workspace/team/company 结构。
        def pick_id_name(item: dict) -> tuple[str, str]:
            wid = _first(item, [
                ("workspaceId",), ("workspace_id",), ("workspaceID",),
                ("teamId",), ("team_id",), ("teamID",),
                ("companyId",), ("company_id",), ("orgId",), ("org_id",),
                ("id",), ("value",), ("key",),
            ])
            name = _first(item, [
                ("workspaceName",), ("workspace_name",),
                ("teamName",), ("team_name",),
                ("companyName",), ("company_name",),
                ("orgName",), ("org_name",),
                ("name",), ("label",), ("title",), ("remark",),
            ])
            return wid, name

        def looks_like_workspace(item: dict) -> bool:
            keys = {str(k).lower() for k in item.keys()}
            joined = " ".join(keys)
            return any(x in joined for x in ("workspace", "team", "company", "org")) or ("id" in keys and "name" in keys)

        def walk(node):
            if isinstance(node, dict):
                wid, name = pick_id_name(node)
                if wid and looks_like_workspace(node):
                    out.append({"id": wid, "name": name or wid, "projectId": "", "projectName": "", "label": f"{name or wid} ({wid})", "raw": node})
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)
        dedup = {}
        for item in out:
            raw_keys = {str(k).lower() for k in (item.get("raw") or {}).keys()}
            if "dirid" in raw_keys and not any(k in raw_keys for k in ("workspaceid", "teamid", "companyid")):
                continue
            key = f"{item.get('id')}::{item.get('projectId','')}"
            dedup[key] = item
        return list(dedup.values())

    def list_workspaces(self) -> dict:
        """
        获取 Roxy 团队/工作区列表。
        Roxy 不同版本路径可能有差异，因此先试配置路径，再试常见路径。
        """
        configured = str(getattr(_cfg, "ROXY_WORKSPACE_LIST_PATH", "") or "").strip()
        method = str(getattr(_cfg, "ROXY_WORKSPACE_LIST_METHOD", "GET") or "GET").upper()
        candidates = []
        if configured:
            candidates.append((method, configured))
        candidates.extend([
            ("GET", "/browser/workspace"),
            ("POST", "/browser/workspace"),
            ("GET", "/workspace/list"),
            ("POST", "/workspace/list"),
            ("GET", "/workspace"),
            ("POST", "/workspace"),
            ("GET", "/team/list"),
            ("POST", "/team/list"),
            ("GET", "/team"),
            ("POST", "/team"),
            ("GET", "/workspaces"),
            ("GET", "/teams"),
            ("GET", "/user/workspace/list"),
            ("POST", "/user/workspace/list"),
            ("GET", "/user/team/list"),
            ("POST", "/user/team/list"),
            ("GET", "/api/workspace/list"),
            ("POST", "/api/workspace/list"),
            ("GET", "/api/team/list"),
            ("POST", "/api/team/list"),
            ("GET", "/browser/workspace/list"),
            ("POST", "/browser/workspace/list"),
            ("GET", "/browser/team/list"),
            ("POST", "/browser/team/list"),
        ])

        errors = []
        seen = set()
        for m, path in candidates:
            key = (m, path)
            if key in seen:
                continue
            seen.add(key)
            ok, payload = self.try_request(m, path)
            if not ok:
                errors.append({"method": m, "path": path, "error": payload})
                continue
            items = self._extract_workspace_items(payload if isinstance(payload, dict) else {})
            if items:
                return {"ok": True, "path": path, "method": m, "items": items, "raw": payload}
            errors.append({"method": m, "path": path, "error": "响应中未解析到团队/工作区列表", "payload": payload})

        return {"ok": False, "items": [], "errors": errors}

    def create_profile(self, payload: dict | None = None, account_index: int | None = None, session_id: str | None = None) -> str:
        body = dict(getattr(_cfg, "ROXY_PROFILE_CREATE_PAYLOAD", {}) or {})
        if payload:
            body.update(payload)
        random_name_enabled = bool(getattr(_cfg, "ROXY_RANDOM_PROFILE_NAME_ON_CREATE", True))
        if random_name_enabled:
            # 覆盖 ROXY_PROFILE_CREATE_PAYLOAD 里的固定 name，避免所有 Roxy 窗口同名。
            body["name"] = _random_roxy_profile_name()
        random_os_enabled = bool(getattr(_cfg, "ROXY_RANDOM_OS_ON_CREATE", True))
        if random_os_enabled:
            # 每次创建环境随机 Windows / macOS；覆盖 ROXY_PROFILE_CREATE_PAYLOAD 里的固定 os。
            body["os"] = _random_roxy_os()
            # osVersion 跟 os 强绑定，随机 OS 时不沿用固定版本，避免 macOS 版本传给 Windows。
            body.pop("osVersion", None)
        else:
            default_os = str(getattr(_cfg, "ROXY_DEFAULT_OS", "macOS") or "macOS").strip()
            if default_os:
                # Roxy 官方枚举大小写敏感：Windows / macOS / Linux / IOS / Android。
                body.setdefault("os", default_os)
            default_os_version = str(getattr(_cfg, "ROXY_DEFAULT_OS_VERSION", "") or "").strip()
            if default_os_version:
                body.setdefault("osVersion", default_os_version)
        workspace_id = _workspace_id_value()
        if workspace_id:
            # Roxy 官方 /browser/create 要求 workspaceId。
            body.setdefault("workspaceId", workspace_id)
        project_id = _project_id_value()
        if project_id:
            body.setdefault("projectId", project_id)
        if bool(getattr(_cfg, "ROXY_CREATE_USE_PROXY_POOL", False)) and not body.get("proxyInfo"):
            from config import proxy as _proxy_cfg
            import secrets as _secrets

            # account_index 走 Rola 子账号换 IP（.1 .2 .3… 每个后缀独立固定 IP）
            # session_id 额外叠加 sticky sessiontime=900，防止子账号偶尔漂移
            _idx = account_index if account_index is not None else random.randint(1, 256)
            _sid = session_id or _secrets.token_hex(6)
            # 直接由 Roxy 创建环境时也视为一次使用，领取后立即消耗代理池条目。
            proxy_url = _proxy_cfg.pick_proxy(
                account_index=_idx,
                session_id=_sid,
                session_time=900,
                consume=True,
            )
            if proxy_url:
                proxy_info = _proxy_url_to_roxy_info(proxy_url)
                body["proxyInfo"] = proxy_info
                logger.info(
                    "[Roxy] 创建环境启用代理池：idx=%d session=%s proxy=%s type=%s host=%s port=%s",
                    _idx, _sid[:8],
                    _mask_proxy(proxy_url),
                    proxy_info.get("protocol") or proxy_info.get("proxyCategory"),
                    proxy_info.get("host"),
                    proxy_info.get("port"),
                )
            else:
                logger.warning("[Roxy] 已启用 ROXY_CREATE_USE_PROXY_POOL，但 PROXY_POOL 为空，本次创建环境不设置代理")
        if not body.get("workspaceId"):
            raise RuntimeError(
                "Roxy 创建环境需要 workspaceId。请在 config/roxybrowser.py 或 WebUI 的 RoxyBrowser 配置中填写 ROXY_WORKSPACE_ID，"
                "或直接在 ROXY_PROFILE_CREATE_PAYLOAD 里加入 {'workspaceId': '你的工作区ID'}。"
            )
        logger.info(
            "[Roxy] 创建环境参数：workspaceId=%s projectId=%s name=%s random_name=%s os=%s osVersion=%s random_os=%s",
            body.get("workspaceId"),
            body.get("projectId") or "-",
            body.get("name") or "-",
            random_name_enabled,
            body.get("os") or "-",
            body.get("osVersion") or "-",
            random_os_enabled,
        )
        # 错峰：避免多 worker 同时发起 /browser/create
        _wait_for_create_slot()
        # Roxy 同一 workspace 只允许串行创建；并发会返回「正在创建中，请稍等！」
        # 加模块级 Lock + retry 来保护
        max_retries = 5
        for attempt in range(max_retries + 1):
            with _PROFILE_LOCK:
                try:
                    result = self.request(_cfg.ROXY_CREATE_METHOD, _cfg.ROXY_CREATE_PATH, json_body=body)
                    break
                except RuntimeError as exc:
                    msg = str(exc)
                    if ("正在创建中" in msg or "创建中" in msg) and attempt < max_retries:
                        wait = 2 ** attempt + random.uniform(0.5, 1.5)
                        logger.warning(
                            "[Roxy] 创建环境冲突（第 %d/%d 次重试），等 %.1fs 后再试: %s",
                            attempt + 1, max_retries, wait, msg,
                        )
                        time.sleep(wait)
                        continue
                    raise
        else:
            raise RuntimeError(f"Roxy 创建环境重试 {max_retries} 次后仍失败: {msg}")
        profile_id = _first(result, [
            ("id",), ("dirId",), ("dir_id",), ("profile_id",), ("profileId",), ("browser_id",),
            ("data", "id"), ("data", "dirId"), ("data", "dir_id"),
            ("data", "profile_id"), ("data", "profileId"), ("data", "browser_id"),
        ])
        if not profile_id:
            raise RuntimeError(f"Roxy 创建环境成功但未返回 dirId/profile_id: {result}")
        return profile_id

    @staticmethod
    def _normalize_profile_id(value: str | None) -> str:
        text = str(value or "").strip()
        # WebUI/人工配置里常用 - 表示“未配置”，这里统一按空处理。
        if text in ("-", "—", "无", "空", "none", "None", "null", "NULL"):
            return ""
        return text

    def open_profile(self, profile_id: str | None = None) -> RoxyOpenResult:
        # 一号一环境时每个任务拥有独立 Profile，Roxy 可同时启动多个窗口。
        # 不要在这里串行化整个流程，否则首个慢启动会阻塞所有并发注册。
        return self._open_profile_locked(profile_id)

    def _open_profile_locked(self, profile_id: str | None = None) -> RoxyOpenResult:
        one_profile = bool(getattr(_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True))
        # profile_id=None → 读取配置里的固定 ROXY_PROFILE_ID；
        # 调用方显式传入刚创建的 pid (一号一环境正常路径) 不算固定配置。
        if profile_id is None:
            configured_pid = self._normalize_profile_id(getattr(_cfg, "ROXY_PROFILE_ID", ""))
            if one_profile and configured_pid:
                raise RuntimeError(
                    "已启用 ROXY_ONE_PROFILE_PER_ACCOUNT=True（一号一环境），"
                    "不能配置/传入固定 ROXY_PROFILE_ID；请留空以便每个账号创建新环境。"
                )
        else:
            configured_pid = self._normalize_profile_id(profile_id)

        pid = configured_pid
        created_by_run = False
        if not pid:
            pid = self.create_profile()
            created_by_run = True
            logger.info("[Roxy] 已创建临时环境：%s", pid)

        path = str(_cfg.ROXY_OPEN_PATH).format(profile_id=pid)
        params = dict(getattr(_cfg, "ROXY_OPEN_EXTRA_PARAMS", {}) or {})
        # Roxy 官方 /browser/open body: {workspaceId, dirId, args, forceOpen, headless}
        params.setdefault("workspaceId", _workspace_id_value())
        params.setdefault("dirId", int(pid) if str(pid).isdigit() else pid)
        params.setdefault("args", [])
        params.setdefault("forceOpen", True)
        _apply_data_saver_open_args(params)
        # ROXY_OPEN_HEADLESS 是显式开关，优先级应高于 ROXY_OPEN_EXTRA_PARAMS，
        # 否则 extra 里残留 headless=False 会导致 WebUI 保存无头后仍弹窗口。
        params["headless"] = bool(getattr(_cfg, "ROXY_OPEN_HEADLESS", False))
        params, window_slot = _apply_window_layout(params, pid)
        logger.info("[Roxy] open 参数：profile=%s headless=%s keep_open=%s", pid, params.get("headless"), getattr(_cfg, "ROXY_KEEP_BROWSER_OPEN", False))
        try:
            result = self.request(
                _cfg.ROXY_OPEN_METHOD,
                path,
                params=params if _cfg.ROXY_OPEN_METHOD.upper() == "GET" else None,
                json_body=params if _cfg.ROXY_OPEN_METHOD.upper() != "GET" else None,
            )
        except Exception:
            if window_slot is not None:
                _release_window_slot(pid)
            raise
        debugger_address = self._extract_debugger_address(result)
        logger.info("[Roxy] open 返回摘要: debugger=%s raw=%s", debugger_address, json.dumps(result, ensure_ascii=False)[:800])
        webdriver_url = _first(result, [
            ("webdriver",), ("webDriver",), ("webdriver_url",), ("webdriverUrl",),
            ("selenium",), ("selenium_url",), ("seleniumUrl",),
            ("data", "webdriver"), ("data", "webDriver"), ("data", "webdriver_url"), ("data", "webdriverUrl"),
            ("data", "selenium"), ("data", "selenium_url"), ("data", "seleniumUrl"),
        ]) or None
        ws_endpoint = _first(result, [
            ("ws",), ("wsEndpoint",), ("ws_endpoint",), ("debuggerWsUrl",),
            ("data", "ws"), ("data", "wsEndpoint"), ("data", "ws_endpoint"), ("data", "debuggerWsUrl"),
        ]) or None
        if not debugger_address and not webdriver_url:
            if window_slot is not None:
                _release_window_slot(pid)
            raise RuntimeError(f"Roxy 已打开环境但未返回 Selenium/调试地址，请检查 ROXY_OPEN_PATH 或接口响应: {result}")
        if window_slot is not None:
            _position_roxy_window_when_ready(_extract_process_id(result), window_slot)
        if self._proxy_pool_target:
            result = dict(result)
            result["proxy_pool_target"] = self._proxy_pool_target
        return RoxyOpenResult(
            pid,
            result,
            debugger_address=debugger_address,
            webdriver_url=webdriver_url,
            ws_endpoint=ws_endpoint,
            created_by_run=created_by_run,
        )

    def close_profile(self, profile_id: str) -> None:
        if not profile_id:
            return
        path = str(_cfg.ROXY_CLOSE_PATH).format(profile_id=profile_id)
        try:
            body = {
                "workspaceId": _workspace_id_value(),
                "dirId": int(profile_id) if str(profile_id).isdigit() else profile_id,
            }
            self.request(
                _cfg.ROXY_CLOSE_METHOD,
                path,
                params=body if str(_cfg.ROXY_CLOSE_METHOD).upper() == "GET" else None,
                json_body=body if str(_cfg.ROXY_CLOSE_METHOD).upper() != "GET" else None,
            )
            logger.info("[Roxy] 已关闭环境：%s", profile_id)
        except Exception as exc:
            logger.warning("[Roxy] 关闭环境失败：%s", exc)
        finally:
            _release_window_slot(str(profile_id))

    def delete_profile(self, profile_id: str) -> None:
        if not profile_id:
            return
        path = str(getattr(_cfg, "ROXY_DELETE_PATH", "/browser/delete")).format(profile_id=profile_id)
        method = str(getattr(_cfg, "ROXY_DELETE_METHOD", "POST") or "POST")
        try:
            body = {
                "workspaceId": _workspace_id_value(),
                "dirIds": [int(profile_id) if str(profile_id).isdigit() else profile_id],
            }
            self.request(
                method,
                path,
                params=body if method.upper() == "GET" else None,
                json_body=body if method.upper() != "GET" else None,
            )
            logger.info("[Roxy] 已删除环境：%s", profile_id)
        except Exception as exc:
            logger.warning("[Roxy] 删除环境失败：%s", exc)

    def cleanup_profile(self, opened: RoxyOpenResult | None) -> None:
        """任务结束清理：关闭窗口；一号一环境时删除本轮创建的 Profile。"""
        keep_open = bool(getattr(_cfg, "ROXY_KEEP_BROWSER_OPEN", False))
        try:
            if not opened or not opened.profile_id:
                return
            if not keep_open:
                self.close_profile(opened.profile_id)

            should_delete = (
                bool(getattr(_cfg, "ROXY_ONE_PROFILE_PER_ACCOUNT", True))
                and bool(getattr(_cfg, "ROXY_DELETE_PROFILE_AFTER_RUN", True))
                and bool(opened.created_by_run)
            )
            if should_delete:
                # 删除前尽量确保已关闭；若 keep_open=True 则不删除，便于调试保留现场。
                if keep_open:
                    logger.info("[Roxy] ROXY_KEEP_BROWSER_OPEN=True，跳过删除环境：%s", opened.profile_id)
                    return
                self.delete_profile(opened.profile_id)
        finally:
            if not keep_open:
                self.close_proxy_pool_relay()

    def close_proxy_pool_relay(self) -> None:
        relay, self._proxy_pool_relay = self._proxy_pool_relay, None
        if relay is not None:
            if relay.last_error:
                logger.warning(
                    "[Roxy] 代理链最后一次错误：target=%s upstream=%s error=%s",
                    _mask_proxy(self._proxy_pool_target),
                    _mask_proxy(getattr(relay.upstream, "raw", "")),
                    relay.last_error,
                )
            relay.close()

    def proxy_transport_snapshot(self) -> dict | None:
        """返回本轮 Roxy 经本地代理链传输的全浏览器流量。"""
        relay = self._proxy_pool_relay
        if relay is None:
            return None
        try:
            return relay.traffic_snapshot()
        except Exception:
            return None

    @staticmethod
    def _extract_debugger_address(payload: dict) -> str | None:
        value = _first(payload, [
            ("debuggerAddress",), ("debugger_address",), ("debugAddress",),
            ("debuggingPortUrl",), ("debugging_port_url",),
            ("remoteDebuggingAddress",), ("remote_debugging_address",),
            ("http",), ("debugHttp",), ("debug_http",),
            ("data", "debuggerAddress"), ("data", "debugger_address"), ("data", "debugAddress"),
            ("data", "debuggingPortUrl"), ("data", "debugging_port_url"),
            ("data", "remoteDebuggingAddress"), ("data", "remote_debugging_address"),
            ("data", "http"), ("data", "debugHttp"), ("data", "debug_http"),
        ])
        if value:
            value = value.strip()
            # 兼容 http://127.0.0.1:xxxx / 127.0.0.1:xxxx / :xxxx / 9222
            value = value.replace("http://", "").replace("https://", "").strip("/")
            if value.startswith(":") and value[1:].isdigit():
                return f"127.0.0.1{value}"
            if value.isdigit():
                return f"127.0.0.1:{value}"
            if ":" in value and not value.startswith(":"):
                return value
        port = _first(payload, [
            ("debuggingPort",), ("debugging_port",), ("debug_port",), ("port",),
            ("data", "debuggingPort"), ("data", "debugging_port"), ("data", "debug_port"), ("data", "port"),
        ])
        if port:
            port = str(port).strip()
            if port.startswith(":"):
                port = port[1:]
            if port.isdigit():
                return f"127.0.0.1:{port}"
        return None
