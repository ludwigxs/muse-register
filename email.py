# -*- coding: utf-8 -*-
"""
邮箱池配置 (兼容 turb 架构)

支持来源:
    outlook                — Outlook / Hotmail 邮箱 (Graph + IMAP 双协议取码)
    generic_api            — 通用 API 取码邮箱 (mailnest / yangyang / newzoe / 自建 API 等)
    imap                   — 通用 IMAP 邮箱
    outlook,generic_api    — 多来源按顺序兜底

OTP 等待参数:
    OTP_MAX_WAIT          — 单个任务最长等秒数
    OTP_POLL_INTERVAL     — 轮询间隔秒数
    OTP_SETTLE_SECONDS    — 抓到第一封后再多等几秒看是否有更新的 OTP
"""
from config.env_loader import env_str, apply_env_overrides


# ===== 基础开关 =====
USE_EMAIL_SERVICE = True
EMAIL_SOURCE = "outlook"   # 逗号分隔多来源: outlook,generic_api,imap


# ===== IMAP =====
IMAP_DEFAULT_PORT = 993
IMAP_USE_SSL = True
IMAP_MAILBOX = "INBOX"


# ===== OTP 轮询 =====
OTP_POLL_INTERVAL = 5
OTP_MAX_WAIT = 360  # Meta 发码偶发延迟 2-3 分钟, 180s 不够
OTP_SETTLE_SECONDS = 3   # 抓到第一封后等几秒看有没有更新的


# ===== Outlook =====
# 可选的远端邮箱服务地址。请仅在本机 .env 中配置，不要提交真实地址。
# 未配置时 outlook_client 会自动走 Microsoft Graph 直连模式。
OUTLOOK_API_BASE = env_str("OUTLOOK_API_BASE", "")

# Outlook FETCH 模式: auto / direct / graph / imap
#   auto   — 远端可用时用远端；远端 402/禁用后自动直连 Graph
#   direct — 强制 Microsoft Graph 直连（需要 client_id + refresh_token）
OUTLOOK_FETCH_MODE = env_str("OUTLOOK_FETCH_MODE", "auto")

# Outlook 账号文件（首次从 JSON/TXT 迁移导入用）
OUTLOOK_ACCOUNTS_FILE = env_str("OUTLOOK_ACCOUNTS_FILE", "用于注册的邮箱.txt")


# ===== Generic API 取码专用代理 =====
# 通用 API 取码时是否走代理池；空字符串表示直连
GENERIC_API_PROXY = ""


# ===== OmniMail 自有域名邮箱 =====
# 一个 OmniMail 账户 = 一个 manager, 管理自己域名下 N 个 mailbox.
# 登录 POST /api/auth/token 拿 accessToken + refreshToken
# mailboxes 共用同一个 Bearer token, payload 里不需要存每个邮箱的凭证
OMNIMAIL_BASE = env_str("OMNIMAIL_BASE", "")
OMNIMAIL_EMAIL = env_str("OMNIMAIL_EMAIL", "")
OMNIMAIL_PASSWORD = env_str("OMNIMAIL_PASSWORD", "")
OMNIMAIL_DEVICE_NAME = env_str("OMNIMAIL_DEVICE_NAME", "muse-register")


apply_env_overrides(globals(), {
    "USE_EMAIL_SERVICE": "bool",
    "EMAIL_SOURCE": "str",
    "OTP_POLL_INTERVAL": "int",
    "OTP_MAX_WAIT": "int",
    "OTP_SETTLE_SECONDS": "int",
    "IMAP_DEFAULT_PORT": "int",
    "IMAP_USE_SSL": "bool",
    "IMAP_MAILBOX": "str",
    "OUTLOOK_API_BASE": "str",
    "OUTLOOK_FETCH_MODE": "str",
    "GENERIC_API_PROXY": "str",
    "OMNIMAIL_BASE": "str",
    "OMNIMAIL_EMAIL": "str",
    "OMNIMAIL_PASSWORD": "str",
    "OMNIMAIL_DEVICE_NAME": "str",
})
