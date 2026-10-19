# -*- coding: utf-8 -*-
"""
RoxyBrowser 指纹浏览器自动化注册配置。

官方文档：
- API 默认 host: http://127.0.0.1:50000
- 所有接口请求头必须带 token
- 可配合 Selenium / Puppeteer / Playwright 自动化
"""
from config.env_loader import env_str, apply_env_overrides


# 注册驱动：
#   "protocol"     = 原有 curl_cffi 纯协议注册（容易封号，不建议）
#   "roxy"         = 调用 RoxyBrowser 指纹浏览器 + Selenium 自动化注册
#   "cloak"        = 调用 CloakBrowser + Playwright/Selenium 适配层注册
#   "browser_use"  = Browser Use Cloud stealth Chromium + Playwright
#   "skyvern"      = Skyvern Browser Sessions + Playwright
REGISTRATION_DRIVER: str = "roxy"

# RoxyBrowser 本地 API
ROXY_API_BASE: str = "http://127.0.0.1:50100"
ROXY_API_TOKEN: str = env_str("ROXY_API_TOKEN", "")

# Roxy 环境/Profile ID；留空时使用 ROXY_PROFILE_CREATE_* 先创建临时环境（如果接口支持）
ROXY_PROFILE_ID: str = ""

# Roxy 工作区 ID。Roxy 创建 Profile 时接口要求 workspaceId，必须填写。
# 可在 Roxy 工作区/团队页面或 API 返回中查看。
ROXY_WORKSPACE_ID: str = ""

# Roxy 项目 ID。/browser/workspace 返回 project_details.projectId；创建 Profile 时一并提交。
ROXY_PROJECT_ID: str = ""

# 获取团队/工作区列表接口路径。不同版本若不同，可在 WebUI 修改；客户端也会自动尝试多个常见路径。
ROXY_WORKSPACE_LIST_PATH: str = "/browser/workspace"
ROXY_WORKSPACE_LIST_METHOD: str = "GET"

# 接口路径模板。不同版本如有差异，只改这里即可。
# {profile_id} 会替换为 ROXY_PROFILE_ID。
ROXY_OPEN_PATH: str = "/browser/open"
ROXY_CLOSE_PATH: str = "/browser/close"
ROXY_CREATE_PATH: str = "/browser/create"

# 接口方法：常见 open/close 为 GET；若你的版本要求 POST，可在 WebUI/配置里改。
ROXY_OPEN_METHOD: str = "POST"
ROXY_CLOSE_METHOD: str = "POST"
ROXY_CREATE_METHOD: str = "POST"

# 打开浏览器时是否无头启动：
#   False = 显示 Roxy 浏览器窗口（便于观察/调试）
#   True  = 无头启动，不显示窗口（如果当前 Roxy 版本支持 headless）
ROXY_OPEN_HEADLESS: bool = False

# 打开浏览器时附加参数；会合并到 /browser/open 请求体，优先级高于默认值。
ROXY_OPEN_EXTRA_PARAMS: dict = {}

# 并发注册时 Roxy 窗口排列。Roxy 会把 args 传给 Chromium，使用网格位置避免窗口互相重叠。
# 关闭布局后保留 Roxy 默认的窗口位置；无头模式不会占用布局槽位。
ROXY_WINDOW_LAYOUT_ENABLED: bool = True
ROXY_WINDOW_LAYOUT_COLUMNS: int = 4
ROXY_WINDOW_LAYOUT_MAX_ROWS: int = 2
ROXY_WINDOW_FIT_SCREEN: bool = True
ROXY_WINDOW_WIDTH: int = 720
ROXY_WINDOW_HEIGHT: int = 820
ROXY_WINDOW_GAP: int = 0
ROXY_WINDOW_ORIGIN_X: int = 0
ROXY_WINDOW_ORIGIN_Y: int = 0

# Selenium 行为
ROXY_SELENIUM_TIMEOUT: int = 90
ROXY_KEEP_BROWSER_OPEN: bool = False

# Roxy 跨 Profile 本地静态资源缓存（仅缓存 JS/CSS/font/image，认证/API 始终联网）。
# 开启 config.browser.BROWSER_DATA_SAVER_MODE 后会自动启用，无需单独修改此开关。
ROXY_LOCAL_ASSET_CACHE_ENABLED: bool = False
ROXY_LOCAL_ASSET_CACHE_MODE: str = "auto"
ROXY_LOCAL_ASSET_CACHE_DIR: str = "./cache/roxy-assets"
ROXY_LOCAL_ASSET_CACHE_MAX_AGE: int = 86400
ROXY_LOCAL_ASSET_CACHE_MAX_ITEM_BYTES: int = 25 * 1024 * 1024

# Roxy API transient 错误重试。create 接口默认不重试，避免超时后重复创建孤儿环境；open/close/delete 会重试。
ROXY_API_RETRIES: int = 3
ROXY_API_RETRY_DELAY: int = 2
ROXY_CREATE_RETRIES: int = 3
ROXY_CREATE_RETRY_DELAY: int = 3

# 多线程注册时，所有 worker 共用的 /browser/create 请求起始间隔（秒）。
# Roxy 在上一个环境仍处于创建中时可能返回“正在创建中”，默认错开 1.5 秒。
ROXY_CREATE_INTERVAL: float = 1.5

# 环境生命周期：
#   True  = 一号一环境：每个账号强制创建新 Profile，用完关闭并删除，不允许复用 ROXY_PROFILE_ID
#   False = 可复用 ROXY_PROFILE_ID 或只关闭不删除
ROXY_ONE_PROFILE_PER_ACCOUNT: bool = True

# 一号一环境结束后是否删除 Profile。建议保持 True。
ROXY_DELETE_PROFILE_AFTER_RUN: bool = True

# 删除环境接口路径/方法；如你的 Roxy 版本不同，只改这里。
ROXY_DELETE_PATH: str = "/browser/delete"
ROXY_DELETE_METHOD: str = "POST"

# 创建 Roxy 环境时随机系统指纹；开启后每次 /browser/create 在 Windows / macOS 里随机选一个，
# 避免固定 macOS 指纹。
ROXY_RANDOM_OS_ON_CREATE: bool = True
ROXY_RANDOM_OS_CHOICES: str = "Windows,macOS"

# 创建 Roxy 环境时随机名称；开启后会覆盖 ROXY_PROFILE_CREATE_PAYLOAD 里的固定 name。
ROXY_RANDOM_PROFILE_NAME_ON_CREATE: bool = True
ROXY_PROFILE_NAME_PREFIX: str = "rb"

# 创建 Roxy 环境时默认系统指纹。仅在 ROXY_RANDOM_OS_ON_CREATE=False 时使用。
# Roxy 官方 os 枚举：Windows / macOS / Linux / IOS / Android。
ROXY_DEFAULT_OS: str = "macOS"
# 留空则使用 Roxy 对应系统的默认/最大版本；如需固定可填 15.3.2、14.7 等。
ROXY_DEFAULT_OS_VERSION: str = ""

# 创建 Roxy 环境时是否使用 config/proxy.py 的 PROXY_POOL：
#   False = 不主动给 Roxy 环境设置代理
#   True  = 每次创建环境时从 PROXY_POOL 随机取一个代理写入 proxyInfo
ROXY_CREATE_USE_PROXY_POOL: bool = False

# Roxy 代理检测通道；留空则不传 checkChannel。
ROXY_PROXY_CHECK_CHANNEL: str = "IPRust.io"

# 没有 ROXY_PROFILE_ID 时创建环境的最小 payload；按你的 Roxy 版本字段调整。
# 默认开启 ROXY_RANDOM_PROFILE_NAME_ON_CREATE，因此这里的 name 只是兜底值。
ROXY_PROFILE_CREATE_PAYLOAD: dict = {
    "name": "gpt-free-register",
    "os": "macOS",
}


# Roxy Codex 授权等待 callback 的最长秒数
ROXY_CODEX_CALLBACK_TIMEOUT: int = 180

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {'REGISTRATION_DRIVER': 'str', 'ROXY_API_BASE': 'str', 'ROXY_API_TOKEN': 'str', 'ROXY_PROFILE_ID': 'str', 'ROXY_WORKSPACE_ID': 'str', 'ROXY_PROJECT_ID': 'str', 'ROXY_WORKSPACE_LIST_PATH': 'str', 'ROXY_OPEN_PATH': 'str', 'ROXY_OPEN_HEADLESS': 'bool', 'ROXY_CLOSE_PATH': 'str', 'ROXY_KEEP_BROWSER_OPEN': 'bool', 'ROXY_WINDOW_LAYOUT_ENABLED': 'bool', 'ROXY_WINDOW_LAYOUT_COLUMNS': 'int', 'ROXY_WINDOW_LAYOUT_MAX_ROWS': 'int', 'ROXY_WINDOW_FIT_SCREEN': 'bool', 'ROXY_WINDOW_WIDTH': 'int', 'ROXY_WINDOW_HEIGHT': 'int', 'ROXY_WINDOW_GAP': 'int', 'ROXY_WINDOW_ORIGIN_X': 'int', 'ROXY_WINDOW_ORIGIN_Y': 'int', 'ROXY_API_RETRIES': 'int', 'ROXY_API_RETRY_DELAY': 'int', 'ROXY_CREATE_RETRIES': 'int', 'ROXY_CREATE_RETRY_DELAY': 'int', 'ROXY_LOCAL_ASSET_CACHE_ENABLED': 'bool', 'ROXY_LOCAL_ASSET_CACHE_MODE': 'str', 'ROXY_LOCAL_ASSET_CACHE_DIR': 'str', 'ROXY_LOCAL_ASSET_CACHE_MAX_AGE': 'int', 'ROXY_LOCAL_ASSET_CACHE_MAX_ITEM_BYTES': 'int', 'ROXY_ONE_PROFILE_PER_ACCOUNT': 'bool', 'ROXY_DELETE_PROFILE_AFTER_RUN': 'bool', 'ROXY_RANDOM_OS_ON_CREATE': 'bool', 'ROXY_RANDOM_OS_CHOICES': 'str', 'ROXY_RANDOM_PROFILE_NAME_ON_CREATE': 'bool', 'ROXY_PROFILE_NAME_PREFIX': 'str', 'ROXY_CREATE_USE_PROXY_POOL': 'bool', 'ROXY_PROXY_CHECK_CHANNEL': 'str', 'ROXY_DELETE_PATH': 'str', 'ROXY_CODEX_CALLBACK_TIMEOUT': 'int', 'ROXY_CREATE_INTERVAL': 'float'})
