# -*- coding: utf-8 -*-
"""
Muse by Meta 注册相关配置
"""
from config.env_loader import apply_env_overrides

# 注册邮箱 (留空 + USE_EMAIL_SERVICE=True 时从池里自动取)
REGISTER_EMAIL = ""

# 邀请码 (注册成功后自动兑换, 留空跳过)
INVITE_CODE = ""

# curl_cffi impersonate 目标
IMPERSONATE = "chrome131"

# 请求超时 (秒)
REQUEST_TIMEOUT = 30

# User-Agent
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Muse API 基础 URL
BASE_URL = "https://muse.ai"

# 银行卡预授权跟踪
# Muse 注册可能需要 $1 预授权验证年龄, 5-7 天自动退款
CARD_PREAUTH_REFUND_DAYS = 7

# ---- .env overrides ----
apply_env_overrides(globals(), {
    "REGISTER_EMAIL": "str",
    "INVITE_CODE": "str",
    "IMPERSONATE": "str",
    "REQUEST_TIMEOUT": "int",
    "BASE_URL": "str",
    "CARD_PREAUTH_REFUND_DAYS": "int",
})
