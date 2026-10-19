# -*- coding: utf-8 -*-
"""
代理池配置 (兼容 turb 架构)

协议说明:
    - socks5h://  SOCKS5 (DNS 在代理端解析，推荐)
    - http://      HTTP 代理
    - 链式代理: PROXY_POOL_UPSTREAM_PROXY 作为上游 → 目标代理

会话粘性:
    每个 account_index 会往 username 里插 ".N" 子账号后缀
    每个 session_id 会追加 "-session-xxx-sessiontime-900" 固定出口 IP
"""
import logging
import random
import threading
import time
from urllib.parse import quote, urlparse

from config.env_loader import apply_env_overrides

logger = logging.getLogger(__name__)


# ============================================================
# 注册代理池
# ============================================================

# 默认本地代理入口；实际出口地区以代理/分流规则为准
# Muse 目前只对美国/加拿大开放，出口必须在美加
PROXY_POOL = [
    # "socks5h://user:pass@us-proxy-1:1080",
    # "socks5h://user:pass@us-proxy-2:1080",
]

# 代理池使用的本地上游代理 (可选)
PROXY_POOL_UPSTREAM_PROXY = ""

# 提取式代理 API (cliproxy 白名单 API 等)
# 示例: https://api.cliproxy.io/white/api?region=US&num=5&time=10&format=n&type=txt
#   region  出口国家; num 每次提取条数; time 会话有效期(分钟); format=n → host:port:user:pass
# 池子为空时自动调 API 拉取, 按会话有效期缓存。
PROXY_EXTRACT_API = ""

# 提取缓存: time=N 分钟的会话, 缓存 80% 时长防频繁请求
_EXTRACT_CACHE: dict = {"pool": [], "ts": 0.0}

# 代理池可能被多个注册 worker 同时领取；选取和移除必须是原子操作。
_PROXY_POOL_LOCK = threading.Lock()

_EXTRACT_TTL_RATIO = 0.8


def _parse_extract_line(line: str) -> str:
    """
    解析提取 API 返回的单行代理:
        host:port:user:pass  → http://user:pass@host:port
        host:port            → http://host:port
        带 scheme 的完整 URL  → 原样
    """
    text = str(line or "").strip()
    if not text:
        return ""
    if "://" in text:
        return text
    parts = text.split(":")
    if len(parts) >= 4:
        host, port = parts[0], parts[1]
        user = ":".join(parts[2:-1])
        password = parts[-1]
        scheme = "socks5h" if "cliproxy.io" in host.lower() else "http"
        return f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    return ""


def _extract_ttl_from_url(url: str) -> int:
    """从 API URL 的 time=N 参数猜会话有效期(秒), 默认 600s。"""
    try:
        from urllib.parse import parse_qs, urlparse as _up
        qs = parse_qs(_up(url).query)
        minutes = int(qs.get("time", ["10"])[0])
        return max(minutes, 1) * 60
    except Exception:
        return 600


def _fetch_extract_pool() -> list[str]:
    """调用提取 API 拉一批代理。"""
    import requests

    resp = requests.get(PROXY_EXTRACT_API, timeout=15)
    text = resp.text or ""
    if "not added to whitelist" in text.lower() or resp.status_code != 200:
        logger.warning(f"[Proxy] 提取 API 返回异常: HTTP {resp.status_code} {text[:120]}")
        return []
    out: list[str] = []
    for line in text.splitlines():
        parsed = _parse_extract_line(line)
        if parsed and parsed not in out:
            out.append(parsed)
    return out


def _pool_with_extract() -> list[str]:
    """静态池优先; 空则走提取 API (带 TTL 缓存)。"""
    pool = normalize_proxy_list(PROXY_POOL)
    if pool:
        return pool
    if not PROXY_EXTRACT_API:
        return []

    now = time.time()
    cached = _EXTRACT_CACHE.get("pool") or []
    ttl = _extract_ttl_from_url(PROXY_EXTRACT_API) * _EXTRACT_TTL_RATIO
    if cached and now - _EXTRACT_CACHE["ts"] < ttl:
        return cached

    try:
        fetched = _fetch_extract_pool()
    except Exception as exc:
        logger.warning(f"[Proxy] 提取 API 请求失败: {type(exc).__name__}: {exc}")
        return cached  # 失败时沿用旧缓存
    if fetched:
        _EXTRACT_CACHE["pool"] = fetched
        _EXTRACT_CACHE["ts"] = now
        logger.info(f"[Proxy] 提取 API 拉到 {len(fetched)} 条代理")
    return fetched or cached


def normalize_proxy_url(value: str, default_scheme: str = "http") -> str:
    """标准化代理 URL 简写，也识别 host:port:user:password 格式。"""
    text = str(value or "").strip()
    if not text or "://" in text:
        return text
    parts = text.split(":")
    if len(parts) >= 4 and parts[1].isdigit():
        host, port = parts[0], parts[1]
        user = ":".join(parts[2:-1])
        password = parts[-1]
        scheme = "socks5h" if "cliproxy.io" in host.lower() else default_scheme
        return f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    return f"{default_scheme}://{text}"


def normalize_proxy_list(values) -> list[str]:
    """标准化代理列表，去空。"""
    if values is None:
        return []
    if isinstance(values, str):
        values = values.splitlines()
    return [
        normalized
        for value in values
        if (normalized := normalize_proxy_url(value))
    ]


def _persist_static_pool(values: list[str]) -> None:
    """把静态代理池同步到项目 .env，避免重启后恢复已消耗的代理。"""
    try:
        from pathlib import Path

        env_path = Path(__file__).resolve().parent.parent / ".env"
        lines: list[str] = []
        existing: dict[str, str] = {}
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    key, value = line.split("=", 1)
                    existing[key.strip()] = value.strip()
                else:
                    lines.append(line)
        serialized = ";".join(normalize_proxy_list(values))
        if serialized:
            existing["PROXY_POOL"] = serialized
        else:
            existing.pop("PROXY_POOL", None)
        for key, value in existing.items():
            lines.append(f"{key}={value}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as exc:
        # 代理已经从内存池移除，持久化失败只会导致重启后恢复；不能阻断当前注册。
        logger.warning("[Proxy] 保存已消耗代理池失败: %s", exc)


def _take_proxy(consume: bool = False) -> str:
    """从静态池或提取缓存取一个原始代理；consume=True 时取出即删除。"""
    with _PROXY_POOL_LOCK:
        static_pool = normalize_proxy_list(PROXY_POOL)
        if static_pool:
            selected = random.choice(static_pool)
            if consume:
                remaining = [item for item in static_pool if item != selected]
                PROXY_POOL[:] = remaining
                _persist_static_pool(remaining)
            return selected

        # 没有静态池时才使用提取缓存/API。缓存代理也只在本次进程内消耗，
        # 到期后会重新向 API 提取新的批次。
        pool = _pool_with_extract()
        if not pool:
            return ""
        selected = random.choice(pool)
        if consume:
            _EXTRACT_CACHE["pool"] = [item for item in pool if item != selected]
        return selected


def pick_proxy(
    account_index: int | str | None = None,
    *,
    session_id: str | None = None,
    session_time: int = 900,
    consume: bool = False,
) -> str:
    """
    从代理池随机抽取一个代理 URL。

    consume=True 时会在领取成功后立即从池中删除，并同步 .env；用于注册
    流程的一次性代理。默认不消耗，供需要在同一会话内重复读取代理的辅助逻辑使用。

    cliproxy 格式 (username 含 -sid-XXXX): 粘性会话就是 sid 本身。
        每次领取用 session_id (或随机值) 替换 sid → 一号一固定住宅 IP。
        不追加任何后缀 (cliproxy 用户名插别的内容会认证失败)。
    Rola 等格式: account_index 插 `.N` 子账号后缀 + -session-xxx-sessiontime-N。
    """
    import re as _re
    import secrets as _secrets

    url = _take_proxy(consume=consume)
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.username:
        return url

    new_username = parsed.username

    sid_match = _re.search(r"-sid-([^-]+)", new_username)
    if sid_match:
        # cliproxy: 换新 sid = 新 sticky 会话 = 新出口 IP
        new_sid = str(session_id or _secrets.token_hex(6)).replace("-", "")[:16] or _secrets.token_hex(6)
        new_username = new_username[:sid_match.start()] + f"-sid-{new_sid}" + new_username[sid_match.end():]
        return _rebuild_proxy_url(parsed, new_username)

    # Rola 专用: 子账号后缀 (cliproxy 的 -sid- 用户名插了会 403)
    if account_index is not None:
        idx = str(account_index).strip()
        if idx:
            first_dash = new_username.find("-")
            if first_dash > 0:
                new_username = new_username[:first_dash] + "." + idx + new_username[first_dash:]
            else:
                new_username = new_username + "." + idx

    # session 参数
    if session_id:
        new_username = f"{new_username}-session-{session_id}-sessiontime-{session_time}"

    if new_username == parsed.username:
        return url

    return _rebuild_proxy_url(parsed, new_username)


def _rebuild_proxy_url(parsed, new_username: str) -> str:
    try:
        auth = quote(new_username, safe="")
        if parsed.password:
            auth += f":{parsed.password}"
        new_netloc = f"{auth}@{parsed.hostname or ''}"
        if parsed.port:
            new_netloc += f":{parsed.port}"
        return parsed._replace(netloc=new_netloc).geturl()
    except Exception:
        return parsed.geturl()


# ---- .env overrides ----
apply_env_overrides(globals(), {
    "PROXY_POOL": "list_str_multiline",
    "PROXY_POOL_UPSTREAM_PROXY": "str",
    "PROXY_EXTRACT_API": "str",
})
PROXY_POOL = normalize_proxy_list(PROXY_POOL)
