# -*- coding: utf-8 -*-
"""
邮箱池模块 - 从数据库 SQLite 管理邮箱素材

支持:
    imap        — 通用 IMAP 邮箱 (服务器+账号密码)
    generic_api — 通用 API 取码邮箱池

每个邮箱素材包含:
    email, server, port, use_ssl, username, password, mailbox
"""
import logging
import time
from datetime import datetime
from typing import Any

from core import db
from config import email as email_cfg

logger = logging.getLogger(__name__)


# ============================================================
# 邮箱池领取/释放
# ============================================================

def acquire_email(source: str | None = None) -> str:
    """从池里领一个 available 的邮箱。"""
    if source is None:
        source = email_cfg.EMAIL_SOURCE.split(",")[0].strip()

    email_row = db.get_available_email(source=source)
    if not email_row:
        raise RuntimeError(f"邮箱池 ({source}) 中没有可用邮箱")

    email = email_row["email"]
    logger.info(f"[EmailPool] 领取邮箱: {email} (source={source})")
    return email


def release_email(email: str, status: str = "available", note: str = "") -> None:
    """归还邮箱到池。"""
    db.release_email(email, status=status, note=note)
    logger.info(f"[EmailPool] 归还邮箱: {email} -> {status}")


def get_email_row(email: str) -> dict | None:
    """查询一个邮箱素材的完整信息（用于 IMAP 取码）。"""
    conn = db._sqlite_conn()
    try:
        row = conn.execute(
            "SELECT * FROM email_pool WHERE email=?", (email,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ============================================================
# OTP 等待 (IMAP)
# ============================================================

def wait_for_otp(
    email: str,
    after_ts: float,
    max_wait: int | None = None,
    poll_interval: int | None = None,
) -> str:
    """
    自动轮询 IMAP 取 Muse OTP。

    Args:
        email: 邮箱地址
        after_ts: 只取该时间戳之后的邮件（秒）
        max_wait: 最大等待秒数
        poll_interval: 轮询间隔秒数

    Returns:
        6 位 OTP 验证码
    """
    import imaplib
    import email as email_lib
    import re
    from email.header import decode_header
    from email.utils import parsedate_to_datetime

    email_row = get_email_row(email)
    if not email_row:
        raise RuntimeError(f"邮箱 {email} 不在池里")

    server = email_row["server"]
    port = int(email_row.get("port") or 993)
    use_ssl = bool(email_row.get("use_ssl", 1))
    username = email_row.get("username") or email
    password = email_row.get("password") or ""
    mailbox = email_row.get("mailbox") or "INBOX"

    timeout = max_wait or email_cfg.OTP_MAX_WAIT
    interval = poll_interval or email_cfg.OTP_POLL_INTERVAL

    logger.info(
        f"[OTP] 开始轮询: {email} via IMAP {server}:{port} (ssl={use_ssl})"
    )

    deadline = time.time() + timeout
    while time.time() < deadline:
        otp = _imap_check_for_otp(
            server, port, use_ssl, username, password, mailbox, after_ts,
        )
        if otp:
            logger.info(f"[OTP] 成功获取: {otp}")
            return otp

        elapsed = int(time.time() - (deadline - timeout))
        logger.info(f"[OTP] 等待中... ({elapsed}/{timeout}s)")
        time.sleep(interval)

    raise TimeoutError(f"等待 Muse OTP 超时 ({timeout}s): {email}")


def _imap_check_for_otp(
    server: str, port: int, use_ssl: bool, username: str, password: str,
    mailbox: str, after_ts: float,
) -> str | None:
    """单次 IMAP 检查。"""
    mail = None
    try:
        if use_ssl:
            mail = imaplib.IMAP4_SSL(server, port)
        else:
            mail = imaplib.IMAP4(server, port)
        mail.login(username, password)
        mail.select(mailbox, readonly=True)

        _, msg_nums = mail.search(None, '(OR SUBJECT "Muse" SUBJECT "Meta" SUBJECT "verification" SUBJECT "code")')
        if not msg_nums or not msg_nums[0]:
            return None

        nums = msg_nums[0].split()[-10:]
        for num in reversed(nums):
            _, msg_data = mail.fetch(num, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue
            raw_email = msg_data[0][1]
            msg = email_lib.message_from_bytes(raw_email)

            date_str = msg.get("Date")
            if date_str:
                try:
                    msg_dt = parsedate_to_datetime(date_str)
                    if msg_dt.timestamp() < after_ts - 60:
                        continue
                except Exception:
                    pass

            sender = str(msg.get("From", "")).lower()
            if "muse" not in sender and "meta" not in sender:
                continue

            body = _extract_body(msg)
            match = re.search(r'\b(\d{6})\b', body)
            if match:
                return match.group(1)

        return None
    except Exception as e:
        logger.debug(f"[OTP] IMAP 检查异常: {type(e).__name__}: {e}")
        return None
    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass


def _extract_body(msg) -> str:
    parts = []
    try:
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                disp = str(part.get("Content-Disposition", ""))
                if "attachment" in disp:
                    continue
                if ct in ("text/plain", "text/html"):
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        try:
                            text = payload.decode(charset, errors="replace")
                        except Exception:
                            text = payload.decode("utf-8", errors="replace")
                        parts.append(text)
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                parts.append(payload.decode(charset, errors="replace"))
    except Exception:
        pass

    text = "\n".join(parts)
    import re as _re
    text = _re.sub(r"<[^>]+>", " ", text)
    text = _re.sub(r"\s+", " ", text).strip()
    return text


# ============================================================
# 管理接口
# ============================================================

def add_email_imap(
    email: str, server: str, port: int = 993, use_ssl: bool = True,
    username: str = "", password: str = "", mailbox: str = "INBOX",
    note: str = "",
) -> int:
    """添加一个 IMAP 邮箱素材到池。"""
    return db.create_email(
        email=email, source="imap", server=server, port=port, use_ssl=use_ssl,
        username=username or email, password=password, mailbox=mailbox, note=note,
    )


def list_emails(source: str = "imap", status: str | None = None) -> list[dict]:
    return db.list_emails(source=source, status=status)


def delete_email(email: str) -> None:
    db.delete_email(email)


def email_stats(source: str = "imap") -> dict:
    return db.count_emails(source)


def import_imap_csv(file_path: str) -> int:
    """
    从 CSV 导入 IMAP 邮箱池。
    格式: email,server,port,use_ssl,username,password,mailbox,note
    """
    import csv
    count = 0
    with open(file_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or row[0].startswith("#") or row[0].lower() == "email":
                continue
            try:
                email, server = row[0], row[1]
                port = int(row[2]) if len(row) > 2 and row[2] else 993
                use_ssl = row[3].lower() not in ("0", "false", "no") if len(row) > 3 else True
                username = row[4] if len(row) > 4 else email
                password = row[5] if len(row) > 5 else ""
                mailbox = row[6] if len(row) > 6 else "INBOX"
                note = row[7] if len(row) > 7 else ""
                add_email_imap(email, server, port, use_ssl, username, password, mailbox, note)
                count += 1
            except Exception as e:
                logger.warning(f"[EmailPool] 导入失败: {row} - {e}")
    logger.info(f"[EmailPool] 成功导入 {count} 个邮箱")
    return count
