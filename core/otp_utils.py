# -*- coding: utf-8 -*-
"""
OTP 检测与抽取通用工具，被 outlook_client / imap_mail_client / generic_api_mail_client 使用。

同时支持 **Muse by Meta** 和 **OpenAI / ChatGPT** 两种品牌：
    - Muse 发件人: noreply@muse.ai, Muse, Meta
    - OpenAI 发件人: noreply@openai.com, OpenAI, ChatGPT
"""
import re

# ============ 品牌关键字 (合并 Muse + OpenAI) ============

# 发件人匹配（小写子串）
_SENDER_HINTS = (
    "openai", "chatgpt",
    "muse", "meta",
    "noreply@muse.ai", "no-reply@muse.ai",
    "noreply@openai.com",
)

# 主题/正文关键字（多语言）
_BRAND_KEYWORDS = (
    # 品牌名
    "chatgpt", "openai", "muse", "meta",
    # 英文
    "verification code", "code is", "your code", "verify your email",
    # 中文
    "代码", "验证码", "确认码",
    # 日文
    "認証コード", "検証コード", "確認コード", "一時検証", "認証",
    # 韩文
    "인증 코드", "확인 코드",
)

# OTP 上下文关键字（用于在多个 6 位数中挑出真正的验证码）
_OTP_CONTEXT_KEYWORDS = (
    "code", "verify", "verification",
    "代码", "验证", "确认",
    "コード", "認証", "検証", "確認",
    "코드", "인증",
)

_OTP_REGEX = re.compile(r"\b(\d{6})\b")


def _get_field(item: dict, *names: str) -> str:
    """
    从邮件 dict 中按顺序尝试多个可能的字段名，返回第一个非空字符串。
    用于兼容不同邮件 API 的字段命名约定（例如 sendEmail / from / fromEmail / from.address）。
    """
    for name in names:
        if "." in name:
            # 支持 "from.emailAddress.address" 这种点路径
            value = item
            for part in name.split("."):
                if not isinstance(value, dict):
                    value = None
                    break
                value = value.get(part)
            if isinstance(value, str) and value:
                return value
        else:
            value = item.get(name)
            if isinstance(value, str) and value:
                return value
    return ""


def looks_like_openai_email(item: dict) -> bool:
    """
    判断邮件是否来自 OpenAI / ChatGPT / Muse / Meta 等需要 OTP 的品牌。
    （函数名保持不变以兼容调用方；已扩展识别所有 OTP 品牌邮件。）

    多语言、多字段名兼容。
    """
    sender = _get_field(item, "sendEmail", "from", "fromEmail", "from.emailAddress.address").lower()
    sender_name = _get_field(item, "sendName", "fromName", "from.emailAddress.name").lower()
    subject = _get_field(item, "subject").lower()
    text = _get_field(item, "text", "bodyPreview", "bodyText").lower()
    content = _get_field(item, "content", "body", "html", "body.content", "bodyHtml").lower()

    # 发件人命中任意品牌
    if any(h in sender or h in sender_name for h in _SENDER_HINTS):
        return True

    return any(k in s for s in (subject, text, content) for k in _BRAND_KEYWORDS)


# Muse 品牌专用别名 (调用方可显式使用)
def looks_like_muse_email(item: dict) -> bool:
    sender = _get_field(item, "sendEmail", "from", "fromEmail", "from.emailAddress.address").lower()
    sender_name = _get_field(item, "sendName", "fromName", "from.emailAddress.name").lower()
    subject = _get_field(item, "subject").lower()
    text = _get_field(item, "text", "bodyPreview", "bodyText").lower()
    content = _get_field(item, "content", "body", "html", "body.content", "bodyHtml").lower()

    if "muse" in sender or "muse" in sender_name or "meta" in sender:
        return True
    if "muse" in subject or "muse" in text or "muse" in content:
        return True
    return any(k in s for s in (subject, text, content) for k in _BRAND_KEYWORDS)


def extract_otp(item: dict) -> str | None:
    """
    从邮件中抽出 6 位 OTP。

    抽取顺序：
        1. subject（OpenAI 部分邮件直接把 6 位数放在主题里，例 "Your OpenAI code is 525210"）
        2. 纯文本字段（text / bodyPreview / bodyText）
        3. HTML 字段（content / html / body / body.content / bodyHtml，去标签后）

    若 body 中含多个 6 位数，优先选择离 "验证码 / code / 認証" 等关键字最近的那个。
    """
    # 1. 主题里如果直接有 6 位数，最可信
    subject = _get_field(item, "subject")
    if subject:
        codes_in_subject = _OTP_REGEX.findall(subject)
        if len(codes_in_subject) == 1:
            # 主题里恰好只有一个 6 位数，几乎肯定就是 OTP
            return codes_in_subject[0]

    # 2. body 字段
    candidates = [
        ("text", _get_field(item, "text", "bodyPreview", "bodyText")),
        ("html", _get_field(item, "content", "html", "body", "body.content", "bodyHtml")),
    ]

    for kind, body in candidates:
        if not body:
            continue
        # 无论 text 还是 html，都先去 HTML 标签和 style 属性
        # （QQ 邮箱转发的 OpenAI 邮件，text 字段也可能含 HTML）
        body = re.sub(r"<style[^>]*>.*?</style>", " ", body, flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r"<[^>]+>", " ", body)
        all_codes = _OTP_REGEX.findall(body)
        if not all_codes:
            continue
        body_lower = body.lower()
        # 优先选离上下文关键字最近的 6 位数
        for code in all_codes:
            idx = body_lower.find(code)
            if idx < 0:
                continue
            window = body_lower[max(0, idx - 60): idx + 6 + 60]
            if any(k.lower() in window for k in _OTP_CONTEXT_KEYWORDS):
                return code
        return all_codes[0]
    return None
