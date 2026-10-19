# -*- coding: utf-8 -*-
"""
Muse 指纹浏览器注册入口 - RoxyBrowser + Selenium

启动链路 (复刻 turb):
    RoxyBrowserClient() → create_profile() → open_profile() → Selenium WebDriver → 注册页面操作

代理注入流程:
    1) 从 config.proxy.PROXY_POOL 取代理
    2) 通过 roxybrowser_client._proxy_url_to_roxy_info() 转成 Roxy 的 proxyInfo
    3) create_profile 时直接写入 proxyInfo 或 proxyUrl

注册流程 (Muse 专属):
    https://muse.ai/join → 填邮箱 → 等 OTP → 提交 OTP → (个人信息) → (银行卡 $1 预授权) → 完成
"""
from __future__ import annotations

import json
import logging
import os
import time
import random
from datetime import date
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


def generate_random_birthday() -> str:
    """生成 18-60 岁随机生日 (YYYY-MM-DD)。Muse 要求 ≥ 18 岁。"""
    year = random.randint(1965, 2004)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return f"{year}-{month:02d}-{day:02d}"


def generate_random_name() -> tuple[str, str]:
    first = random.choice(["Alex", "Sam", "Jordan", "Taylor", "Casey", "Morgan", "Riley", "Avery", "Quinn", "Drew"])
    last = random.choice(["Smith", "Johnson", "Brown", "Garcia", "Miller", "Davis", "Wilson", "Anderson", "Taylor"])
    return first, last


def run_roxy_registration(
    email: str | None = None,
    birthday: str | None = None,
    proxy: str | None = None,
    otp_code: str | None = None,
    on_email_acquired: Callable[[str], None] | None = None,
) -> dict:
    """
    通过 RoxyBrowser 指纹浏览器执行 Muse 注册。

    Returns:
        {
            "ok": True/False,
            "email": "...",
            "access_token": "...",
            "proxy": "...",
            "driver": "roxy",
            "profile_id": "...",
            "error": "...",
        }
    """
    from config import roxybrowser as roxy_cfg
    from core.roxybrowser_client import RoxyBrowserClient, _mask_proxy, _proxy_url_to_roxy_info

    # 1) 创建 Roxy 客户端
    client = RoxyBrowserClient()
    profile_id: str | None = None
    driver = None
    _ok = False
    try:
        # 2) 确定代理: 每个账号一个独立 sid = 一个固定住宅 IP (一号一 IP)
        import secrets as _secrets

        session_id = _secrets.token_hex(6)
        proxy_url = proxy
        if proxy_url is None and roxy_cfg.ROXY_CREATE_USE_PROXY_POOL:
            try:
                from config import proxy as proxy_cfg
                from config.proxy import pick_proxy
                # 注册账号领取代理后立即从代理池移除，避免下一个账号重复使用同一条。
                proxy_url = pick_proxy(session_id=session_id, consume=True) or ""
                if not proxy_url and str(getattr(proxy_cfg, "PROXY_EXTRACT_API", "") or "").strip():
                    proxy_url = _fetch_proxy_via_roxy_browser(
                        client, str(proxy_cfg.PROXY_EXTRACT_API).strip()
                    )
            except Exception:
                logger.exception("[Roxy→Proxy] 浏览器网络提取代理失败")
                proxy_url = ""

        logger.info(f"[Roxy→Muse] 会话={session_id} 代理: {proxy_url or '无'}")

        # 3) 创建/获取 Profile (窗口代理 = 流程代理, 同一个 sid)
        create_payload = None
        if proxy_url:
            try:
                create_payload = {"proxyInfo": _proxy_url_to_roxy_info(proxy_url)}
            except Exception as exc:
                logger.warning(f"[Roxy→Muse] 代理转换失败，窗口将不带代理: {exc}")
                proxy_url = ""
        profile_id = roxy_cfg.ROXY_PROFILE_ID or client.create_profile(payload=create_payload)

        # 4) 打开浏览器 → 拿到 Selenium driver
        open_result = client.open_profile(profile_id)
        driver = _build_roxy_driver(open_result)

        # 5) 执行 Muse 注册页面操作
        flow_state: dict = {}
        result = _run_muse_page_flow(
            driver=driver,
            email=email,
            otp_code=otp_code,
            proxy_url=proxy_url,
            flow_state=flow_state,
        )
        # 浏览器注册完成后，在 Muse 设置页兑换邀请码。
        try:
            from config import muse as muse_cfg
            invite_code = str(getattr(muse_cfg, "INVITE_CODE", "") or "").strip()
            if result.get("ok") and invite_code:
                invite_result = _redeem_invite_with_fallback(
                    driver, invite_code, proxy_url=proxy_url,
                    access_token=str(result.get("access_token") or ""),
                )
                result["invite_code"] = invite_code
                result["invite_result"] = invite_result
        except Exception as exc:
            logger.warning("[Invite] Roxy 浏览器兑换邀请码失败: %s", exc)
        result.update({
            "driver": "roxy",
            "profile_id": profile_id,
            "proxy": proxy_url,
            "email_consumed": bool(flow_state.get("otp_submitted")),
        })
        _ok = bool(result.get("ok"))
        return result

    except Exception as exc:
        # 任务停止必须继续向 registration_service 传播，不能被这里的通用异常
        # 处理转换成 failed，否则 WebUI 会永久显示 stopping。
        try:
            from core.registration_service import StopRequested
            if isinstance(exc, StopRequested):
                raise
        except ImportError:
            pass
        logger.exception(f"[Roxy→Muse] 注册失败: {type(exc).__name__}: {exc}")
        consumed = False
        try:
            consumed = bool(flow_state.get("otp_submitted"))
        except Exception:
            pass
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            "driver": "roxy",
            "profile_id": profile_id,
            "email_consumed": consumed,
        }
    finally:
        # 6) 关闭 Selenium; 成功才清理环境, 失败保留供调试 (用户要求)
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        if profile_id and _ok:
            try:
                client.close_profile(profile_id)
                if roxy_cfg.ROXY_DELETE_PROFILE_AFTER_RUN:
                    client.delete_profile(profile_id)
            except Exception as e:
                logger.warning(f"[Roxy→Muse] 关闭/删除 profile 失败: {e}")
        elif profile_id:
            logger.info(f"[Roxy→Muse] 任务失败, 保留环境 {profile_id} 供调试 (不关闭不删除)")


def run_local_registration(
    email: str | None = None,
    birthday: str | None = None,
    otp_code: str | None = None,
    on_email_acquired: Callable[[str], None] | None = None,
) -> dict:
    """使用本机普通 Chrome 注册，不创建指纹环境，也不使用代理池。"""
    from selenium import webdriver

    driver = None
    flow_state: dict = {}
    try:
        options = webdriver.ChromeOptions()
        # 不注入项目代理池；保留 Chrome 的系统网络设置（例如系统代理/PAC），
        # 这样本机其它浏览器可用的网络环境也能被复用。
        logger.info("[Local→Muse] 启动本机 Chrome（使用系统网络设置，不注入项目代理）")

        # 优先使用 Selenium Manager 已缓存的驱动，避免首次启动时联网下载而长时间无响应。
        driver_path = os.environ.get("MUSE_CHROMEDRIVER", "").strip()
        if not driver_path:
            cache_root = Path.home() / ".cache" / "selenium" / "chromedriver" / "win64"
            cached = sorted(cache_root.glob("*/chromedriver.exe"), key=lambda p: p.parent.name, reverse=True)
            if cached:
                driver_path = str(cached[0])
        if driver_path and Path(driver_path).exists():
            from selenium.webdriver.chrome.service import Service
            logger.info("[Local→Muse] 使用本地 ChromeDriver: %s", driver_path)
            driver = webdriver.Chrome(service=Service(driver_path), options=options)
        else:
            logger.warning("[Local→Muse] 未找到本地 ChromeDriver，将回退 Selenium Manager")
            driver = webdriver.Chrome(options=options)
        result = _run_muse_page_flow(
            driver=driver,
            email=email,
            otp_code=otp_code,
            proxy_url="",
            flow_state=flow_state,
            local_mode=True,
        )
        try:
            from config import muse as muse_cfg
            invite_code = str(getattr(muse_cfg, "INVITE_CODE", "") or "").strip()
            if result.get("ok") and invite_code:
                invite_result = _redeem_invite_with_fallback(
                    driver, invite_code,
                    access_token=str(result.get("access_token") or ""),
                )
                result["invite_code"] = invite_code
                result["invite_result"] = invite_result
        except Exception as exc:
            logger.warning("[Invite] 本地浏览器兑换邀请码失败: %s", exc)
        result.update({
            "driver": "local",
            "proxy": "",
            "payload": {"driver": "local"},
            "email_consumed": bool(flow_state.get("otp_submitted")),
        })
        return result
    except Exception as exc:
        logger.exception("[Local→Muse] 注册失败: %s: %s", type(exc).__name__, exc)
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            "driver": "local",
            "proxy": "",
            "email_consumed": bool(flow_state.get("otp_submitted")),
        }
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception as exc:
                logger.warning("[Local→Muse] 关闭浏览器失败: %s", exc)


def _build_roxy_driver(open_result):
    """
    从 RoxyBrowserClient.open_profile() 的返回构造 Selenium WebDriver。

    优先用 open 返回的 selenium 协调地址 (webdriver.Remote, 无需本地 chromedriver)；
    其次用 debuggerAddress 挂接到 Roxy 内核 (selenium>=4.6 会自动下载驱动)。
    """
    from selenium import webdriver

    ws = (getattr(open_result, "webdriver_url", None) or "").strip()
    if ws:
        url = ws if ws.startswith("http") else f"http://{ws}"
        logger.info(f"[Roxy→Muse] Selenium Remote -> {url}")
        return webdriver.Remote(command_executor=url, options=webdriver.ChromeOptions())

    da = (getattr(open_result, "debugger_address", None) or "").strip()
    if da:
        logger.info(f"[Roxy→Muse] Selenium debuggerAddress -> {da}")
        opts = webdriver.ChromeOptions()
        opts.add_experimental_option("debuggerAddress", da)
        # 优先用 Roxy 自带 chromedriver, 避免 Selenium Manager 联网下载导致 hang
        raw = getattr(open_result, "raw", None) or {}
        driver_path = str((raw.get("data") or {}).get("driver") or "").strip()
        if driver_path and Path(driver_path).exists():
            from selenium.webdriver.chrome.service import Service
            logger.info(f"[Roxy→Muse] 使用 Roxy chromedriver: {driver_path}")
            return webdriver.Chrome(service=Service(driver_path), options=opts)
        logger.warning("[Roxy→Muse] Roxy chromedriver 路径不可用, 回退 Selenium Manager")
        return webdriver.Chrome(options=opts)

    raise RuntimeError(
        f"Roxy open 未返回 Selenium/调试地址: webdriver_url={ws!r} debugger={da!r}"
    )


def _fetch_proxy_via_roxy_browser(client, extract_api: str) -> str:
    """通过临时 Roxy 环境的浏览器网络调用代理提取 API。

    本机网络无法访问某些代理服务时，不能由 requests/SOCKS 在本机直连；
    此处让 RoxyBrowser 负责该请求，再将返回的住宅代理注入正式注册环境。
    """
    from config.proxy import _parse_extract_line
    from core.roxybrowser_client import _mask_proxy

    opened = None
    driver = None
    try:
        logger.info("[Roxy→Proxy] 使用 Roxy 浏览器网络提取代理")
        profile_id = client.create_profile(payload={})
        opened = client.open_profile(profile_id)
        driver = _build_roxy_driver(opened)
        driver.set_page_load_timeout(40)
        driver.get(extract_api)
        time.sleep(2)
        raw = str(driver.execute_script("return document.body ? document.body.innerText : ''") or "")
        for line in raw.splitlines():
            proxy_url = _parse_extract_line(line)
            if proxy_url:
                logger.info("[Roxy→Proxy] 成功提取住宅代理: %s", _mask_proxy(proxy_url))
                return proxy_url
        raise RuntimeError(f"提取接口未返回有效代理: {raw[:240]}")
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        if opened is not None:
            client.cleanup_profile(opened)


def _run_muse_page_flow(
    driver,
    email: str | None = None,
    otp_code: str | None = None,
    proxy_url: str = "",
    flow_state: dict | None = None,
    local_mode: bool = False,
) -> dict:
    """
    Muse 注册页面自动化流程。

    muse.ai/join 的已知 API 端点 (turb 项目已抓):
        POST /api/auth/native/restart
        POST /api/auth/native/send-otp
        POST /api/auth/native/confirm-otp

    页面流程:
        1. 访问 muse.ai/join 或 muse.ai/auth/login
        2. 填邮箱 → 点 Continue → 触发 send-otp
        3. 等 OTP → 填验证码 → 点 Verify
        4. (可选) 填个人信息 (昵称/生日)
        5. (可选) 银行卡 $1 预授权
        6. 完成 → 读取 access_token / cookie
    """
    from core.email_provider import acquire_email, wait_for_otp
    from config import muse as muse_cfg
    import time as _time

    flow_state = flow_state if flow_state is not None else {}

    # --- Step 1: 打开注册页 ---
    MUSE_JOIN_URL = "https://muse.ai/join"
    MUSE_LOGIN_URL = "https://muse.ai/auth/login"

    logger.info(f"[Roxy→Muse] 打开注册页: {MUSE_JOIN_URL}")
    try:
        driver.set_page_load_timeout(60)
        driver.set_script_timeout(30)
    except Exception:
        pass
    try:
        driver.get(MUSE_JOIN_URL)
    except Exception as e:
        logger.warning(f"[Roxy→Muse] 页面加载超时/中断 (继续执行): {e}")
    _time.sleep(3)

    # --- Step 2: 输入邮箱 ---
    if not email:
        email = acquire_email()

    logger.info(f"[Roxy→Muse] 使用邮箱: {email}")

    # 尝试多种可能的输入框选择器 (页面慢, 带重试)
    email_input = None
    email_selectors = [
            'input[type="email"]',
            'input[name="email"]',
            'input[placeholder*="email" i]',
            'input[placeholder*="Mobile number" i]',
            'input[autocomplete="email"]',
            'input[type="text"]',
        ]
    # 本地普通浏览器当前落地页使用“手机号或邮箱”输入框；Roxy 流程保持原有选择器不变。
    if local_mode:
        email_selectors[0:0] = [
            'input[placeholder*="手机号或邮箱"]',
            'input[aria-label*="手机号或邮箱"]',
        ]

    for attempt in range(6):
        for selector in email_selectors:
            try:
                el = driver.find_element("css selector", selector)
                if el.is_displayed():
                    email_input = el
                    logger.info(f"[Roxy→Muse] 找到邮箱输入框: {selector} (第{attempt + 1}轮)")
                    break
            except Exception:
                continue
        if email_input:
            break
        logger.warning(f"[Roxy→Muse] 邮箱输入框未出现, 等5s重试 (第{attempt + 1}/6轮)")
        _time.sleep(5)

    if not email_input:
        _dump_page_state(driver, "no_email_input")
        raise RuntimeError("未找到邮箱输入框 (已dump页面状态)")

    # 清空 + 输入 (React 感知)
    _react_set_value(driver, email_input, email)
    _time.sleep(1)

    # 点 Continue / Next (无反应等几秒重试, 网络慢场景)
    submitted = False
    for attempt in range(4):
        continue_btn = None
        try:
            el = driver.find_element("css selector", 'button[type="submit"]')
            if el.is_displayed() and el.is_enabled():
                continue_btn = el
        except Exception:
            continue_btn = None

        if continue_btn:
            continue_btn.click()
            logger.info(f"[Roxy→Muse] 已点击 Continue (第{attempt + 1}次)")
        else:
            logger.warning(f"[Roxy→Muse] 按钮仍禁用, 回车提交 (第{attempt + 1}次)")
            from selenium.webdriver.common.keys import Keys
            email_input.send_keys(Keys.ENTER)

        # 等页面进入 OTP 态 (网络慢, 每轮最多 12s)
        for _ in range(6):
            _time.sleep(2)
            try:
                body = driver.execute_script("return document.body.innerText.slice(0,500).toLowerCase()")
                boxes = driver.find_elements("css selector", 'input[inputmode="numeric"], input[maxlength="1"]')
                if boxes or any(k in body for k in ("code", "验证码", "we sent")):
                    submitted = True
                    break
            except Exception:
                continue
        if submitted:
            break
        logger.warning("[Roxy→Muse] 页面未进入 OTP 态, 等5s再试")
        _time.sleep(5)

    _time.sleep(1)

    # --- Step 3: 等 OTP ---
    # 邮件 date 只到秒且可能等于/略早于点击时刻, after_ts 回拨 120s 防止把新码当旧码过滤;
    # 同邮箱两次任务间隔远大于 120s, 不会误吞上次的旧码 (候选也取最新一封)。
    otp_after_ts = _time.time() - 120
    current_otp = otp_code

    if not current_otp:
        logger.info(f"[Roxy→Muse] 等待 OTP...")
        try:
            from core.registration_service import check_stop, StopRequested
            current_otp = wait_for_otp(email, after_ts=otp_after_ts, stop_check=check_stop)
        except StopRequested:
            raise
        except Exception as exc:
            logger.error(f"[Roxy→Muse] OTP 等待超时: {exc}")
            raise RuntimeError(f"OTP 等待超时: {exc}")

    logger.info(f"[Roxy→Muse] 收到 OTP: {current_otp}")

    # --- Step 4: 输入 OTP (框可能延迟出现, 带重试) ---
    # Muse 的 OTP 通常是 6 个独立 input 框或一个 input
    otp_page_url = str(getattr(driver, "current_url", "") or "")
    otp_filled = False
    for attempt in range(4):
        otp_inputs = []
        try:
            otp_inputs = [o for o in driver.find_elements(
                "css selector", 'input[inputmode="numeric"], input[maxlength="1"][type*="text"]') if o.is_displayed()]
            if len(otp_inputs) < 4:
                otp_inputs = []
        except Exception:
            pass

        if otp_inputs and len(otp_inputs) >= 4:
            for i, digit in enumerate(current_otp):
                if i < len(otp_inputs):
                    otp_inputs[i].send_keys(digit)
                    _time.sleep(0.15)
            otp_filled = True
            logger.info(f"[Roxy→Muse] OTP 已填入 {len(otp_inputs)} 个框 (第{attempt + 1}次)")
            break

        # 单个 input 框
        otp_field = None
        for selector in [
            'input[inputmode="numeric"]',
            'input[autocomplete="one-time-code"]',
            'input[placeholder*="code" i]',
            'input[placeholder*="验证码" i]',
        ]:
            try:
                el = driver.find_element("css selector", selector)
                if el.is_displayed():
                    otp_field = el
                    break
            except Exception:
                continue

        if otp_field:
            _react_set_value(driver, otp_field, current_otp)
            otp_filled = True
            logger.info(f"[Roxy→Muse] OTP 已填入单框 (第{attempt + 1}次)")
            break

        logger.warning(f"[Roxy→Muse] OTP 输入框未出现, 等5s重试 (第{attempt + 1}次)")
        _time.sleep(5)

    if not otp_filled:
        logger.warning("[Roxy→Muse] 回退: active_element 直接打字")
        driver.switch_to.active_element.send_keys(current_otp)

    # Muse 在验证码正确时可能自动跳转；先观察，只有仍停留在验证码页才点击。
    advanced = False
    for _ in range(12):
        _time.sleep(0.25)
        if _otp_page_already_advanced(driver, otp_page_url):
            advanced = True
            break

    if advanced:
        flow_state["otp_submitted"] = True
        logger.info("[Roxy→Muse] 验证码已自动提交并进入下一步，跳过 Verify 点击")
    else:
        verify_btn = None
        for selector in [
            'button:has-text("Verify")',
            'button:has-text("Confirm")',
            'button:has-text("验证")',
            'button[type="submit"]',
        ]:
            try:
                el = driver.find_element("css selector", selector)
                if el.is_displayed() and el.is_enabled():
                    verify_btn = el
                    break
            except Exception:
                continue

        if verify_btn:
            verify_btn.click()
            flow_state["otp_submitted"] = True
            logger.info("[Roxy→Muse] 验证码页面仍在，已点击 Verify")
        else:
            logger.warning("[Roxy→Muse] 验证码页面未自动跳转且未找到 Verify 按钮")

    _time.sleep(3)

    # --- Step 5: 识别并处理后续页面 (生日/名字/年龄验证) 直到聊天窗口 ---
    final_state = _wait_for_chat_or_next_step(driver, timeout=180, local_mode=local_mode)

    # --- Step 6: 读取 cookie / access_token ---
    result = {
        "ok": final_state == "chat",
        "final_state": final_state,
        "email": email,
        "proxy": proxy_url,
        "email_consumed": True,  # 走到 Step 6 说明 OTP 必然已提交
    }
    if final_state != "chat":
        result["error"] = f"未进入聊天窗口 (state={final_state})"
        _dump_page_state(driver, f"final_{final_state}")

    try:
        cookies = driver.get_cookies()
        result["cookies"] = {c["name"]: c["value"] for c in cookies}
    except Exception:
        pass

    # 尝试从 localStorage 读 access_token (如果 Muse 存了)
    try:
        token = driver.execute_script(
            "return localStorage.getItem('access_token') || localStorage.getItem('auth_token') || ''"
        )
        if token:
            result["access_token"] = token
    except Exception:
        pass

    logger.info(f"[Roxy→Muse] 流程完成: ok={result['ok']}, state={final_state}, email={email}")
    return result


def _dump_page_state(driver, tag):
    """把当前页面状态 dump 到 _probe/ 目录 (截图+DOM), 用于分析未知页面。"""
    try:
        out = Path(r"E:\zcj\muse-register\_probe")
        out.mkdir(exist_ok=True)
        ts = time.strftime("%H%M%S")
        try:
            driver.save_screenshot(str(out / f"{ts}_{tag}.png"))
        except Exception:
            pass
        info = driver.execute_script(
            "return {url: location.href, body: document.body.innerText.slice(0,800),"
            " els: Array.from(document.querySelectorAll('input,select,textarea,button,[role=button]')).map(el => ({"
            " t: el.tagName.toLowerCase(), ty: el.type||'', ph: el.placeholder||'',"
            " tx: (el.innerText||el.value||'').slice(0,30), en: !el.disabled,"
            " vis: !!(el.offsetParent||el.getClientRects().length), aria: el.getAttribute('aria-label')||''}))}"
        )
        (out / f"{ts}_{tag}.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
        logger.info(f"[Roxy→Muse] 页面状态已dump: {tag} url={info.get('url')}")
        return info
    except Exception as e:
        logger.warning(f"[Roxy→Muse] dump 失败: {e}")
        return None


def _react_set_value(driver, el, value):
    """输入并确保 React 感知。兼容视觉隐藏框 (sr-only): click 失败不致命, JS 原生 setter 兜底。"""
    try:
        el.click()
    except Exception:
        pass  # sr-only 元素不可点击, 忽略
    try:
        el.clear()
    except Exception:
        pass
    try:
        el.send_keys(value)
        driver.execute_script(
            "var el=arguments[0]; el.dispatchEvent(new Event('input',{bubbles:true}));"
            "el.dispatchEvent(new Event('change',{bubbles:true}));", el)
        return
    except Exception:
        pass
    # 兜底: JS 原生 value setter + input 事件 (React 16+ 会被劫持, 必须用 native setter)
    driver.execute_script(
        "var el=arguments[0], v=arguments[1]; el.focus();"
        "var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
        "s.call(el,v);"
        "el.dispatchEvent(new Event('input',{bubbles:true}));"
        "el.dispatchEvent(new Event('change',{bubbles:true}));", el, value)


_SET_VALUE_STRICT_JS = """
var el=arguments[0], v=arguments[1];
el.focus();
var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
s.call(el,''); el.dispatchEvent(new Event('input',{bubbles:true}));
s.call(el,v); el.dispatchEvent(new Event('input',{bubbles:true}));
el.dispatchEvent(new Event('change',{bubbles:true}));
el.dispatchEvent(new Event('blur',{bubbles:true}));
el.dispatchEvent(new Event('focusout',{bubbles:true}));
"""


def _set_value_strict(driver, el, value):
    """清空→设值→完整事件链 (input/change/blur/focusout)。用于 Meta 支付表单。"""
    driver.execute_script(_SET_VALUE_STRICT_JS, el, value)


def _otp_page_already_advanced(driver, before_url: str = "") -> bool:
    """判断验证码填入后网页是否已自行进入下一步。"""
    try:
        url = str(getattr(driver, "current_url", "") or "")
        body = str(driver.execute_script("return document.body.innerText.slice(0,1200)") or "").lower()
        if before_url and url and url != before_url:
            return True
        if any(word in body for word in (
            "what's your birthday", "what is your birthday", "birthday",
            "出生日期", "生日", "your name", "first name", "confirm age",
            "verify your age", "credit card",
        )):
            return True
        if _looks_like_chat_window(driver):
            return True
        otp_inputs = [el for el in driver.find_elements(
            "css selector", 'input[inputmode="numeric"], input[autocomplete="one-time-code"], input[placeholder*="code" i], input[placeholder*="验证码" i]'
        ) if _is_visible(el)]
        otp_words = ("verification code", "enter code", "验证码", "one-time code", "we sent")
        return not otp_inputs and not any(word in body for word in otp_words)
    except Exception:
        return False


def _find_pay_button(driver):
    """找 Meta 支付页 Confirm 按钮。注意: 它可能渲染成 div 而非 button,
    inactive 时 aria-label 为 'Pay now, $1.00, inactive'。返回 {aria, ariaDis, el} 或 None。"""
    try:
        return driver.execute_script("""
          var found=null;
          document.querySelectorAll('[aria-label]').forEach(function(b){
            var a=(b.getAttribute('aria-label')||'');
            if (a.indexOf('Pay now')>=0 && (b.offsetParent||b.getClientRects().length) && !found) {
              found={aria:a, ariaDis:b.getAttribute('aria-disabled')||'', el:b};
            }
          });
          return found;
        """)
    except Exception:
        return None


def _selected_option(select_el):
    """返回下拉框当前有效选项；占位项视为未选择。"""
    from selenium.webdriver.support.ui import Select

    option = Select(select_el).first_selected_option
    text = (option.text or "").strip()
    value = (option.get_attribute("value") or "").strip()
    marker = f"{text} {value}".lower()
    if not text and not value:
        return None
    if value in ("", "-1", "0") or any(
        token in marker for token in ("select", "choose", "please select", "请选择", "选择")
    ):
        return None
    return option


def _verify_adult_birthday(selects, year_hint: int | None = None, year_select=None) -> None:
    """提交前确认月/日/年均已选，且出生年份对应年龄至少 18 岁。"""
    if len(selects) < 3 or any(_selected_option(select_el) is None for select_el in selects[:3]):
        raise RuntimeError("生日页面仍有未选择的日期项，禁止提交")

    year_select = year_select or selects[2]
    selected_year_option = _selected_option(year_select)
    if selected_year_option is None:
        raise RuntimeError("生日年份未选择，禁止提交")
    year_text = (selected_year_option.text or "").strip()
    year_value = (selected_year_option.get_attribute("value") or "").strip()
    import re
    years = [int(item) for item in re.findall(r"(?:19|20)\d{2}", f"{year_text} {year_value}")]
    selected_year = years[0] if years else int(year_hint or 0)
    if not selected_year or selected_year > date.today().year - 18:
        raise RuntimeError(f"生日年份无效或未满18岁: {year_text or year_value}")


def _select_birthday_option(driver, select_el, candidates) -> str:
    """设置生日下拉值并触发 React 事件；Muse 的原生 select 是隐藏的。"""
    result = driver.execute_script(
        """
        const el = arguments[0];
        const candidates = (arguments[1] || []).map(v => String(v).trim());
        const options = Array.from(el.options || []);
        const option = options.find(o => candidates.includes(String(o.textContent || '').trim())
          || candidates.includes(String(o.value || '').trim()));
        if (!option) return {ok:false, value:'', text:''};
        const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
        setter.call(el, option.value);
        for (const type of ['input', 'change', 'blur']) {
          el.dispatchEvent(new Event(type, {bubbles:true}));
        }
        return {ok: !!el.value, value: String(el.value || ''), text: String(option.textContent || '').trim()};
        """,
        select_el,
        [str(candidate) for candidate in candidates],
    ) or {}
    if not result.get("ok"):
        raise RuntimeError(f"生日选项不存在或未生效: {candidates}")
    time.sleep(0.35)
    return str(result.get("value") or "").strip()


def _birthday_selects(driver):
    """每次从 DOM 重新获取生日控件，避免 React 重渲染造成 stale element。"""
    selectors = {
        "month": 'select[autocomplete="bday-month"]',
        "day": 'select[autocomplete="bday-day"]',
        "year": 'select[autocomplete="bday-year"]',
    }
    found = {}
    for key, selector in selectors.items():
        items = driver.find_elements("css selector", selector)
        if items:
            found[key] = items[0]
    if len(found) == 3:
        return found
    # 兼容没有 autocomplete 的旧版本；隐藏的原生 select 也必须保留。
    items = driver.find_elements("css selector", "select")
    if len(items) >= 3:
        year_select, month_select, day_select = _classify_birthday_selects(driver, items[:3])
        if all((year_select, month_select, day_select)):
            return {"month": month_select, "day": day_select, "year": year_select}
    return {}


def _fill_birthday_name_fields(driver) -> bool:
    """生日表单同时要求姓名；按 placeholder/name 分别填写名和姓。"""
    first, last = generate_random_name()
    inputs = [i for i in driver.find_elements("css selector", 'input[type="text"], input:not([type])') if _is_visible(i)]
    if not inputs:
        return False
    first_el = last_el = None
    for el in inputs:
        attrs = " ".join([
            str(el.get_attribute("placeholder") or ""),
            str(el.get_attribute("name") or ""),
            str(el.get_attribute("aria-label") or ""),
        ]).lower()
        if first_el is None and ("first" in attrs or "given" in attrs):
            first_el = el
        elif last_el is None and ("last" in attrs or "family" in attrs or "surname" in attrs):
            last_el = el
    if first_el is None and inputs:
        first_el = inputs[0]
    if last_el is None and len(inputs) > 1:
        last_el = inputs[1]
    try:
        if first_el:
            _react_set_value(driver, first_el, first)
        if last_el:
            _react_set_value(driver, last_el, last)
        logger.info("[Roxy→Muse] 生日页已填写姓名: %s %s", first, last)
        return bool(first_el and last_el)
    except Exception as exc:
        logger.warning("[Roxy→Muse] 生日页姓名填写失败: %s", exc)
        return False


def _birthday_select_state(driver, selects) -> list[dict]:
    """读取生日下拉框实际 DOM 状态，用于失败诊断。"""
    try:
        return driver.execute_script(
            """
            return arguments[0].map((el, i) => ({
              index: i,
              value: el.value || '',
              text: el.options[el.selectedIndex]?.textContent?.trim() || '',
              options: Array.from(el.options || []).slice(0, 4).map(o => ({value:o.value || '', text:(o.textContent || '').trim()}))
            }));
            """,
            selects,
        ) or []
    except Exception:
        return []


def _classify_birthday_selects(driver, selects):
    """按选项内容识别年月日下拉，避免依赖 DOM 顺序。"""
    from selenium.webdriver.support.ui import Select
    year_select = month_select = day_select = None
    current_year = date.today().year
    for select_el in selects:
        texts = [(option.text or '').strip() for option in Select(select_el).options]
        values = [(option.get_attribute('value') or '').strip() for option in Select(select_el).options]
        combined = texts + values
        numeric = set()
        for item in combined:
            import re
            match = re.fullmatch(r'\s*(\d{1,4})\s*[年月日]?\s*', item)
            if match:
                numeric.add(int(match.group(1)))
        if year_select is None and any(1900 <= value <= current_year for value in numeric):
            year_select = select_el
        elif month_select is None and any(month.lower() in item.lower() for item in combined for month in (
            'january', 'february', 'march', 'april', 'may', 'june', 'july',
            'august', 'september', 'october', 'november', 'december')):
            month_select = select_el
        elif day_select is None and any(1 <= value <= 31 for value in numeric):
            day_select = select_el
    return year_select, month_select, day_select


def _handle_birthday_page(driver) -> bool:
    """生日页: Month/Day/Year 三个下拉, 随机选 >18 岁生日后点 Confirm"""
    months = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]
    year = random.randint(1980, 2005)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    logger.info(f"[Roxy→Muse] 生日页: {year}-{month:02d}-{day:02d}")

    for attempt in range(5):
        selects = []
        try:
            # 生日控件在 Roxy 页面通常是 hidden 原生 select，不能用 is_displayed 过滤。
            birthday = _birthday_selects(driver)
            if len(birthday) != 3:
                raise RuntimeError("未找到完整生日下拉框")
            selects = [birthday["month"], birthday["day"], birthday["year"]]

            _fill_birthday_name_fields(driver)
            _select_birthday_option(driver, birthday["month"], [months[month - 1], str(month)])
            birthday = _birthday_selects(driver)
            _select_birthday_option(driver, birthday["day"], [str(day)])
            birthday = _birthday_selects(driver)
            _select_birthday_option(driver, birthday["year"], [str(year)])
            time.sleep(0.8)
            birthday = _birthday_selects(driver)
            _verify_adult_birthday(
                [birthday["month"], birthday["day"], birthday["year"]],
                year_hint=year,
                year_select=birthday["year"],
            )
            # React 重新渲染可能清空姓名，提交前再补一次。
            _fill_birthday_name_fields(driver)
            buttons = driver.find_elements("css selector", 'button[type="submit"], button')
            for button in buttons:
                try:
                    label = (button.text or button.get_attribute("aria-label") or "").strip().lower()
                    if button.is_displayed() and button.is_enabled() and (
                        button.get_attribute("type") == "submit" or "confirm" in label
                    ):
                        button.click()
                        logger.info("[Roxy→Muse] 已点击生日 Confirm")
                        time.sleep(3)
                        return True
                except Exception:
                    continue
            raise RuntimeError("生日 Confirm 仍不可用")
        except Exception as e:
            logger.warning("[Roxy→Muse] 生日下拉实际状态: %s", _birthday_select_state(driver, selects) if 'selects' in locals() else [])
            logger.warning(f"[Roxy→Muse] 生日选择重试{attempt + 1}: {e}")
            time.sleep(2)
    return False


def _handle_local_birthday_page(driver) -> bool:
    """本地 Chrome 的中文生日页：按下拉选项识别年/月/日并选择成年日期。

    该页面与 Roxy 已有流程不同：下拉顺序为“年、月、日”，且可见文本是中文。
    """
    from selenium.webdriver.support.ui import Select

    try:
        selects = [s for s in driver.find_elements("css selector", "select") if s.is_displayed()]
        if len(selects) < 3:
            logger.warning("[Local→Muse] 生日页未找到完整的年/月/日下拉框")
            return False

        target_year = date.today().year - random.randint(21, 35)
        target_month = random.randint(1, 12)
        target_day = random.randint(1, 28)
        year_select = month_select = day_select = None

        for select_el in selects:
            texts = [o.text.strip() for o in Select(select_el).options]
            numeric = {int(t.rstrip("年月日 ")) for t in texts if t.rstrip("年月日 ").isdigit()}
            if any(1900 <= n <= date.today().year for n in numeric):
                year_select = select_el
            elif any("月" in t for t in texts):
                month_select = select_el
            elif any(1 <= n <= 31 for n in numeric):
                day_select = select_el

        if not all((year_select, month_select, day_select)):
            logger.warning("[Local→Muse] 无法识别生日下拉框：year=%s month=%s day=%s", bool(year_select), bool(month_select), bool(day_select))
            return False

        year_options = {o.text.strip() for o in Select(year_select).options}
        available_years = sorted(
            int(y.rstrip("年月日 ")) for y in year_options
            if y.rstrip("年月日 ").isdigit() and int(y.rstrip("年月日 ")) <= date.today().year - 18
        )
        if not available_years:
            raise RuntimeError("生日年份列表中没有已成年年份")
        target_year = min(available_years, key=lambda year: abs(year - target_year))

        _select_birthday_option(driver, year_select, [str(target_year), f"{target_year} 年"])
        _select_birthday_option(driver, month_select, [f"{target_month} 月", f"{target_month}月", str(target_month)])
        _select_birthday_option(driver, day_select, [str(target_day)])
        _verify_adult_birthday(
            [year_select, month_select, day_select],
            year_hint=target_year,
            year_select=year_select,
        )
        logger.info("[Local→Muse] 已选择成年生日: %s-%02d-%02d", target_year, target_month, target_day)
        time.sleep(0.5)

        for button in driver.find_elements("css selector", 'button[type="submit"], button'):
            try:
                label = (button.text or "").strip().lower()
                if button.is_displayed() and button.is_enabled() and label in ("确认", "confirm"):
                    button.click()
                    logger.info("[Local→Muse] 已提交生日")
                    time.sleep(3)
                    return True
            except Exception:
                continue
        logger.warning("[Local→Muse] 已选择生日但未找到确认按钮")
    except Exception as exc:
        logger.exception("[Local→Muse] 处理生日页失败: %s", exc)
    return False


def _handle_card_verification(driver, _retry_depth: int = 0) -> bool:
    """年龄验证页: 点 Confirm age → 新标签页 Meta 支付页填卡 ($1 预授权) → 提交。
    Returns True 表示已提交支付表单 (后续轮询等原页更新)。"""
    from core.card_pool import get_card_for_use, release_card

    # 银行卡验证失败时自动换下一张；限制递归深度，避免页面始终停留在卡验证页时死循环。
    if _retry_depth >= 20:
        logger.error("[Roxy→Muse] 银行卡连续验证失败，已达到自动换卡上限")
        return False

    card = get_card_for_use()
    if not card:
        logger.error("[Roxy→Muse] 银行卡池无可用卡")
        return False
    logger.info(f"[Roxy→Muse] 使用卡尾号 {str(card.get('card_number'))[-4:]} 过期 {card.get('expiry')} ZIP {card.get('billing_zip')}")

    # 1) 有些版本先显示“Confirm age”再打开支付页；当前版本直接在
    #    /access/verification 原页显示银行卡表单和 Confirm。
    card_field_probe = (
        'input[aria-label*="Card number" i], input[placeholder*="Card number" i], '
        'input[aria-label*="MM/YY" i], input[placeholder*="MM/YY" i], '
        'input[aria-label*="CVV" i], input[placeholder*="CVV" i]'
    )
    direct_form = any(_is_visible(el) for el in driver.find_elements("css selector", card_field_probe))
    before_handles = set(driver.window_handles)
    clicked = False
    if not direct_form:
        for b in driver.find_elements("css selector", 'button[type="submit"], button'):
            try:
                t = (b.text or "").strip().lower()
                aria = (b.get_attribute("aria-label") or "").strip().lower()
                if b.is_displayed() and b.is_enabled() and ("confirm age" in t or "confirm age" in aria):
                    b.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            release_card(card["id"], "available", "未找到 Confirm age 按钮")
            logger.error("[Roxy→Muse] 未找到 Confirm age 按钮或银行卡表单")
            return False
    else:
        logger.info("[Roxy→Muse] 当前页面已直接显示银行卡表单，跳过 Confirm age")

    # 2) 等新标签页出现 (网络慢, 最多 30s)
    pay_handle = None
    if direct_form:
        pay_handle = driver.current_window_handle
        origin_handle = []
    else:
        for _ in range(10):
            time.sleep(3)
            handles = driver.window_handles
            new = [h for h in handles if h not in before_handles]
            if new:
                pay_handle = new[0]
                break
        if not pay_handle:
            release_card(card["id"], "available", "支付标签页未打开")
            logger.error("[Roxy→Muse] 支付标签页未打开")
            return False
        origin_handle = [h for h in driver.window_handles if h != pay_handle]
        driver.switch_to.window(pay_handle)
    logger.info(f"[Roxy→Muse] 支付标签页: {driver.current_url[:80]}")

    try:
        # 3) 填卡表单 (Meta 支付页, 主文档, aria 定位)
        #    卡号用包含匹配: 表单有残留值时 aria-label 会变成 "Card number, ending in XXXX"
        fields = {
            # 不同支付页版本有时只渲染 placeholder，没有 aria-label；两种定位都保留。
            "card_number": 'input[aria-label*="Card number" i], input[placeholder*="Card number" i], input[name*="card" i], input[id*="card" i]',
            "expiry": 'input[aria-label*="MM/YY" i], input[placeholder*="MM/YY" i], input[placeholder*="expiry" i], input[name*="expir" i], input[id*="expir" i]',
            "cvv": 'input[aria-label*="CVV" i], input[placeholder*="CVV" i], input[placeholder*="security" i], input[name*="cvv" i], input[id*="cvv" i]',
        }
        expiry_raw = str(card.get("expiry") or "").strip()
        expiry_parts = expiry_raw.replace("-", "/").split("/")
        if len(expiry_parts) == 2 and expiry_parts[0].isdigit() and expiry_parts[1].isdigit():
            expiry_value = f"{int(expiry_parts[0]):02d}/{int(expiry_parts[1]):02d}"
        else:
            expiry_value = expiry_raw
        values = {
            "card_number": card["card_number"],
            "expiry": expiry_value,
            "cvv": card["cvv"],
        }
        for name, sel in fields.items():
            el = None
            for _ in range(5):
                try:
                    candidates = [item for item in driver.find_elements("css selector", sel) if _is_visible(item)]
                    if candidates:
                        el = candidates[0]
                        break
                except Exception:
                    pass
                time.sleep(3)
            if not el:
                raise RuntimeError(f"卡表单字段未找到: {name}")
            _set_value_strict(driver, el, values[name])
            time.sleep(0.6)
            logger.info(f"[Roxy→Muse] 已填 {name}")

        # ZIP: 优先按 placeholder/name，兼容无 aria 的旧支付页再按第 4 个输入框。
        zip_el = None
        for _ in range(5):
            zip_candidates = [i for i in driver.find_elements("css selector", 'input[placeholder*="ZIP" i], input[aria-label*="ZIP" i], input[name*="zip" i], input[id*="zip" i]') if _is_visible(i)]
            if zip_candidates:
                zip_el = zip_candidates[0]
                break
            inputs = [i for i in driver.find_elements("css selector", "input") if _is_visible(i) and (i.get_attribute("type") or "text") == "text"]
            if len(inputs) >= 4:
                zip_el = inputs[3]
                break
            time.sleep(3)
        if zip_el:
            _set_value_strict(driver, zip_el, card.get("billing_zip", ""))
            logger.info("[Roxy→Muse] 已填 ZIP")
        time.sleep(2)

        # 4) 等 Confirm 激活并点击 (按钮可能是 div; inactive 时 aria 带 ', inactive')
        confirmed = False
        for _ in range(10):
            st = _find_pay_button(driver)
            if st and "inactive" not in (st.get("aria") or "").lower() and st.get("ariaDis") != "true":
                driver.execute_script("arguments[0].click();", st["el"])
                confirmed = True
                break
            # 当前 Muse 表单使用普通 button[type=submit]，文案为 Confirm，
            # 没有 Pay now aria-label。
            if direct_form:
                for button in driver.find_elements("css selector", 'button[type="submit"], button'):
                    label = (button.text or button.get_attribute("aria-label") or "").strip().lower()
                    if button.is_displayed() and button.is_enabled() and label == "confirm":
                        button.click()
                        confirmed = True
                        break
                if confirmed:
                    break
            time.sleep(3)
        if not confirmed:
            _dump_page_state(driver, "card_confirm_not_ready")
            release_card(card["id"], "failed", "Confirm 按钮始终不可用")
            logger.error("[Roxy→Muse] 支付 Confirm 按钮不可用")
            # 当前卡无法通过验证，关闭支付页后回到原页，自动取下一张卡重试。
            if not direct_form:
                try:
                    driver.close()
                except Exception:
                    pass
            if origin_handle:
                try:
                    driver.switch_to.window(origin_handle[0])
                except Exception:
                    pass
            return _handle_card_verification(driver, _retry_depth + 1)

        logger.info("[Roxy→Muse] 已提交支付 ($1 预授权), 等待验证结果...")
        # 卡片归还可用: $1 预授权会退款, 同一张卡可反复用于年龄验证
        release_card(card["id"], "available", "")

        # 5) 轮询等待支付结果 (最长 120s):
        #    - 支付页可能闪过 3DS challenge (frictionless 一闪而过), 不据此判定失败
        #    - 成功后支付页跳回 muse.ai (ccv-return), 原页随后自动更新进入聊天
        deadline = time.time() + 120
        failure_markers = (
            "payment failed", "payment was declined", "card was declined",
            "card declined", "declined", "invalid card", "unable to authorize",
            "couldn't be authorized", "could not be authorized", "verification failed",
            "payment method was declined",
        )
        while time.time() < deadline:
            time.sleep(5)
            try:
                handles_now = driver.window_handles
                # 支付页在失败后可能自行关闭；如果原页还没有进入聊天，则视为
                # 当前卡验证失败并立即领取下一张，而不是继续等待到超时。
                if pay_handle not in handles_now:
                    try:
                        driver.switch_to.window(origin_handle[0])
                    except Exception:
                        pass
                    try:
                        if _looks_like_chat_window(driver):
                            return True
                    except Exception:
                        pass
                    reason = "支付标签页已关闭且未进入聊天"
                    logger.warning("[Roxy→Muse] 卡尾号 %s 验证失败，自动换下一张: %s", str(card.get("card_number"))[-4:], reason)
                    release_card(card["id"], "failed", reason)
                    return _handle_card_verification(driver, _retry_depth + 1)

                for h in handles_now:
                    driver.switch_to.window(h)
                    url = driver.current_url or ""
                    body_text = ""
                    try:
                        body_text = (driver.execute_script(
                            "return document.body ? document.body.innerText : ''"
                        ) or "").lower()
                    except Exception:
                        pass
                    matched_failure = ""
                    # 失败文案只看支付标签页，避免原 Muse 页面上的历史提示触发误换卡。
                    if h == pay_handle:
                        matched_failure = next((m for m in failure_markers if m in body_text), "")
                    if matched_failure:
                        reason = f"支付验证失败: {matched_failure}"
                        logger.warning("[Roxy→Muse] 卡尾号 %s 验证失败，自动换下一张: %s", str(card.get("card_number"))[-4:], matched_failure)
                        release_card(card["id"], "failed", reason)
                        if not direct_form:
                            try:
                                driver.close()
                            except Exception:
                                pass
                        if origin_handle:
                            try:
                                driver.switch_to.window(origin_handle[0])
                            except Exception:
                                pass
                        return _handle_card_verification(driver, _retry_depth + 1)
                    if "muse.ai" in url:
                        if "ccv-return" in url or _looks_like_chat_window(driver):
                            if origin_handle:
                                try:
                                    driver.switch_to.window(origin_handle[0])
                                except Exception:
                                    pass
                            return True
            except Exception:
                continue
        # 超时也回原 tab, 交给外层 _wait_for_chat_or_next_step 继续轮询
        if origin_handle:
            try:
                driver.switch_to.window(origin_handle[0])
            except Exception:
                pass
        return True
    except Exception as exc:
        logger.exception(f"[Roxy→Muse] 填卡失败: {exc}")
        try:
            _dump_page_state(driver, "card_fill_error")
        except Exception:
            pass
        release_card(card["id"], "failed", str(exc)[:120])
        # 回原 tab
        try:
            if origin_handle:
                driver.switch_to.window(origin_handle[0])
        except Exception:
            pass
        return _handle_card_verification(driver, _retry_depth + 1)


def _handle_name_page(driver) -> bool:
    """名字页: 检测含 name 的输入页, 填随机姓名后提交"""
    info = _dump_page_state(driver, "name_page")
    if not info:
        return False
    body = (info.get("body") or "").lower()
    if "name" not in body:
        return False
    from selenium.webdriver.common.by import By
    first, last = generate_random_name()
    logger.info(f"[Roxy→Muse] 名字页: {first} {last}")
    filled = False
    text_inputs = [i for i in driver.find_elements(By.CSS_SELECTOR, 'input[type="text"], input:not([type])') if _is_visible(i)]
    if text_inputs:
        try:
            _react_set_value(driver, text_inputs[0], f"{first} {last}")
            filled = True
        except Exception as e:
            logger.warning(f"[Roxy→Muse] 名字填写失败: {e}")
    if filled:
        time.sleep(1)
        for sel in ('button[type="submit"]', "button"):
            for b in driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    t = (b.text or "").lower()
                    if b.is_displayed() and b.is_enabled() and any(k in t for k in ("next", "continue", "confirm", "submit")):
                        b.click()
                        logger.info(f"[Roxy→Muse] 已提交名字: {t}")
                        time.sleep(3)
                        return True
                except Exception:
                    continue
    return False


def _is_visible(el) -> bool:
    try:
        return el.is_displayed()
    except Exception:
        return False


def _looks_like_chat_window(driver) -> bool:
    """判定是否进入 Muse 聊天窗口"""
    try:
        body = driver.execute_script(
            "return document.body.innerText.slice(0,400).toLowerCase()")
        # 聊天输入框特征
        chat = driver.execute_script(
            "return !!document.querySelector('textarea, [contenteditable=true], input[placeholder*=\\'message\\' i], div[role=textbox]')")
        if chat:
            return True
        return any(k in body for k in ("ask muse", "new chat", "what can i help", "message muse"))
    except Exception:
        return False


def _redeem_invite_in_browser(driver, invite_code: str) -> dict:
    """注册进入聊天后，通过 Settings 页面兑换邀请码。

    Muse 的浏览器流程不一定把 access_token 暴露给后端，因此这里复刻用户
    手动操作：打开 Settings → Redeem invite code → 输入邀请码并提交。
    兑换失败只返回失败结果，不影响已经完成的注册。
    """
    code = str(invite_code or "").strip()
    if not code:
        return {"success": False, "skipped": True, "error": "邀请码为空"}

    def visible_elements(selector: str):
        try:
            return [el for el in driver.find_elements("css selector", selector) if _is_visible(el)]
        except Exception:
            return []

    def click_element(el) -> bool:
        try:
            el.click()
            return True
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", el)
                return True
            except Exception:
                return False

    try:
        # 1) 打开 Settings。兼容按钮文本、aria-label、title 三种版本。
        settings = None
        for el in visible_elements("button, [role='button'], a"):
            attrs = " ".join([
                str(getattr(el, "text", "") or ""),
                str(el.get_attribute("aria-label") or ""),
                str(el.get_attribute("title") or ""),
            ]).lower()
            if "settings" in attrs or "设置" in attrs:
                settings = el
                break
        if not settings or not click_element(settings):
            return {"success": False, "error": "未找到 Settings 按钮"}
        time.sleep(1.5)

        # 底部齿轮第一次点击只会展开快捷菜单，需再点击菜单里的 Settings。
        settings_menu = None
        for el in visible_elements("button, [role='menuitem'], [role='button'], a, div"):
            text = str(getattr(el, "text", "") or "").strip().lower()
            if text == "settings" or text == "设置":
                # 排除底部齿轮本身，优先菜单项。
                if el.get_attribute("data-testid") != "hatch-dock-more":
                    settings_menu = el
                    break
        if settings_menu is not None:
            click_element(settings_menu)
            time.sleep(1.5)

        # 2) 点击 General 页面中的 Redeem invite code。
        redeem_entry = None
        # 优先真正可点击的元素，避免先命中包含整块文本的外层 div。
        for el in visible_elements("button, [role='button'], a"):
            attrs = " ".join([
                str(getattr(el, "text", "") or ""),
                str(el.get_attribute("aria-label") or ""),
                str(el.get_attribute("title") or ""),
            ]).strip().lower()
            if "redeem invite code" in attrs or "兑换邀请码" in attrs:
                redeem_entry = el
                break
        if redeem_entry is None:
            for el in visible_elements("div"):
                attrs = str(getattr(el, "text", "") or "").strip().lower()
                if attrs in ("redeem invite code", "兑换邀请码"):
                    redeem_entry = el
                    break
        if not redeem_entry or not click_element(redeem_entry):
            return {"success": False, "error": "未找到 Redeem invite code"}
        time.sleep(1)

        # 3) 找邀请码输入框。优先 placeholder/aria，其次取弹窗内可见文本框。
        invite_input = None
        for el in visible_elements("input"):
            attrs = " ".join([
                str(el.get_attribute("placeholder") or ""),
                str(el.get_attribute("aria-label") or ""),
                str(el.get_attribute("name") or ""),
            ]).lower()
            if any(k in attrs for k in ("invite", "code", "邀请码")):
                invite_input = el
                break
        if invite_input is None:
            candidates = visible_elements("input[type='text'], input:not([type]), input")
            if candidates:
                invite_input = candidates[-1]
        if invite_input is None:
            return {"success": False, "error": "未找到邀请码输入框"}
        _react_set_value(driver, invite_input, code)
        time.sleep(0.5)

        # 4) 点击兑换/提交按钮，并读取页面反馈。
        submit = None
        for el in visible_elements("button, [role='button']"):
            text = " ".join([
                str(getattr(el, "text", "") or ""),
                str(el.get_attribute("aria-label") or ""),
            ]).strip().lower()
            if any(k in text for k in ("redeem", "兑换", "submit", "confirm", "确定")):
                submit = el
                break
        if not submit or not click_element(submit):
            return {"success": False, "error": "未找到兑换提交按钮"}
        time.sleep(2)

        body = str(driver.execute_script("return document.body ? document.body.innerText : ''") or "").lower()
        failure_words = ("invalid", "expired", "already used", "failed", "error", "无效", "过期", "失败", "已使用")
        # 只检查 alert/toast 等反馈节点，避免页面其它隐藏文案里的 error 误判。
        feedback = " ".join(
            str(getattr(el, "text", "") or "")
            for el in visible_elements("[role='alert'], [role='status'], [class*='toast' i], [class*='error' i]")
        ).lower()
        if any(word in feedback for word in failure_words):
            logger.warning("[Invite] 浏览器兑换失败，页面提示: %s", feedback[-300:])
            return {"success": False, "error": "页面提示兑换失败", "message": feedback[-300:]}
        logger.info("[Invite] 浏览器兑换邀请码已提交: %s", code)
        return {"success": True}
    except Exception as exc:
        logger.warning("[Invite] 浏览器兑换异常: %s: %s", type(exc).__name__, exc)
        return {"success": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def _redeem_invite_protocol(driver, invite_code: str, proxy_url: str = "", access_token: str = "") -> dict:
    """优先用协议接口兑换，复用浏览器刚建立的 Muse 登录 cookies/token。"""
    code = str(invite_code or "").strip()
    if not code:
        return {"success": False, "skipped": True, "error": "邀请码为空"}
    try:
        from core.session import MuseSession
        from core.muse_auth import MuseAuth

        session = MuseSession(proxy=proxy_url or None)
        session.session.timeout = 15
        try:
            for cookie in driver.get_cookies() or []:
                name = str(cookie.get("name") or "").strip()
                value = str(cookie.get("value") or "")
                if not name:
                    continue
                session.session.cookies.set(
                    name,
                    value,
                    domain=str(cookie.get("domain") or ".muse.ai"),
                    path=str(cookie.get("path") or "/"),
                )
        except Exception as exc:
            logger.debug("[Invite] 复制浏览器 cookies 失败: %s", exc)
        if access_token:
            session.session.headers.update({"Authorization": f"Bearer {access_token}"})
        result = MuseAuth(session).redeem_invite_code(code)
        session.close()
        return result
    except Exception as exc:
        logger.warning("[Invite] 协议兑换异常: %s: %s", type(exc).__name__, exc)
        return {"success": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def _redeem_invite_with_fallback(driver, invite_code: str, proxy_url: str = "", access_token: str = "") -> dict:
    """协议优先；协议失败后再执行网页 Settings 兑换。"""
    protocol_result = _redeem_invite_protocol(driver, invite_code, proxy_url, access_token)
    if protocol_result.get("success"):
        protocol_result["method"] = "protocol"
        return protocol_result
    logger.warning("[Invite] 协议兑换失败，切换网页兑换: %s", protocol_result.get("error") or protocol_result)
    browser_result = _redeem_invite_in_browser(driver, invite_code)
    browser_result["method"] = "browser"
    browser_result["protocol_error"] = protocol_result.get("error") or "协议兑换未成功"
    return browser_result


def _wait_for_chat_or_next_step(driver, timeout=90, local_mode: bool = False):
    """OTP/生日之后: 轮询识别下一页 (名字/信用卡/聊天), 处理能识别的, 未知则 dump 留档"""
    deadline = time.time() + timeout
    handled = set()
    while time.time() < deadline:
        time.sleep(4)
        if _looks_like_chat_window(driver):
            logger.info("[Roxy→Muse] 已进入聊天窗口!")
            return "chat"
        try:
            body = driver.execute_script("return document.body.innerText.slice(0,600).toLowerCase()")
        except Exception:
            continue
        if local_mode and any(k in body for k in ("生日", "年龄", "出生日期", "出生年月", "birth", "date of birth")) and "local_birthday" not in handled:
            handled.add("local_birthday")
            if _handle_local_birthday_page(driver):
                continue
        if not local_mode and "birthday" in body and "birthday" not in handled:
            # 只有成功提交后才标记 handled；失败时保留重试机会，避免永久卡在生日页。
            if _handle_birthday_page(driver):
                handled.add("birthday")
                continue
            time.sleep(2)
            continue
        # 欢迎披露页: 点 Get started 进入聊天
        if ("disclosure" in (driver.current_url or "") or "get started" in body) and "disclosure" not in handled:
            handled.add("disclosure")
            logger.info("[Roxy→Muse] 欢迎披露页, 点击 Get started")
            clicked = False
            disclosure_labels = ("get started", "开始使用")
            if local_mode:
                # 本地中文落地页的按钮文案是“开始”；Roxy 原逻辑保持不变。
                disclosure_labels = disclosure_labels + ("开始",)
            for b in driver.find_elements("css selector", 'button[type="submit"], button'):
                try:
                    t = (b.text or "").strip().lower()
                    if b.is_displayed() and b.is_enabled() and t in disclosure_labels:
                        b.click()
                        clicked = True
                        time.sleep(4)
                        break
                except Exception:
                    continue
            if not clicked:
                _dump_page_state(driver, "disclosure_no_btn")
            continue
        if "birthday" not in body and any(k in body for k in ("your name", "first name", "full name")) and "name" not in handled:
            handled.add("name")
            if _handle_name_page(driver):
                continue
        if any(k in body for k in ("confirm age", "credit card", "verify your age")) and "card" not in handled:
            handled.add("card")
            if _handle_card_verification(driver):
                # 支付已提交: 卡流程内部已耗 2 分钟+, 重新计时等原页更新进聊天
                deadline = time.time() + 180
                continue
            # 填卡失败: 重新计时看原页状态, 不再重试卡
            deadline = time.time() + 60
            continue
        # 未知页面: 定期 dump 一次
        if not handled:
            _dump_page_state(driver, "unknown_after_otp")
            handled.add("dumped")
    return "timeout"


def run_cloak_registration(
    email: str | None = None,
    birthday: str | None = None,
    proxy: str | None = None,
    otp_code: str | None = None,
    on_email_acquired: Callable[[str], None] | None = None,
) -> dict:
    """
    CloakBrowser (Playwright 风格) 执行 Muse 注册。
    接口与 run_roxy_registration 对齐。
    """
    from core.cloakbrowser_driver import build_cloak_driver

    driver = None
    try:
        driver, opened = build_cloak_driver(proxy=proxy)
        logger.info(f"[Cloak→Muse] 启动完成: profile={opened.profile_id}")

        result = _run_muse_page_flow(
            driver=driver,
            email=email,
            otp_code=otp_code,
            proxy_url=proxy or "",
        )
        try:
            from config import muse as muse_cfg
            invite_code = str(getattr(muse_cfg, "INVITE_CODE", "") or "").strip()
            if result.get("ok") and invite_code:
                invite_result = _redeem_invite_with_fallback(
                    driver, invite_code, proxy_url=proxy or "",
                    access_token=str(result.get("access_token") or ""),
                )
                result["invite_code"] = invite_code
                result["invite_result"] = invite_result
        except Exception as exc:
            logger.warning("[Invite] Cloak 浏览器兑换邀请码失败: %s", exc)
        result.update({"driver": "cloak", "profile_id": opened.profile_id})
        return result

    except Exception as exc:
        logger.exception(f"[Cloak→Muse] 注册失败: {type(exc).__name__}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:500]}", "driver": "cloak"}
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception as e:
                logger.warning(f"[Cloak→Muse] 关闭 driver 失败: {e}")
