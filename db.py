# -*- coding: utf-8 -*-
"""
SQLite 持久化层 (turb 风格 + Muse 扩展)

数据库: muse.sqlite3

核心表:
    accounts          — 已注册的 Muse 账号
    email_pool        — 邮箱池 (outlook / generic_api / imap 等来源统一存放，source 字段区分)
    card_pool         — 银行卡池 (AES 加密)
    registration_jobs — 注册任务记录

email_pool 表的 payload JSON 字段承载不同来源的专属素材:
    outlook:     {"password": ..., "client_id": ..., "refresh_token": ..., "recovery_email": ..., "recovery_code": ..., "copy_line": ...}
    generic_api: {"code_url": ..., "copy_line": ...}
    imap:        {"imap_server": ..., "imap_port": 993, "imap_ssl": 1, "imap_username": ..., "imap_password": ..., "imap_mailbox": "INBOX"}
"""
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _PROJECT_ROOT / "data"
_SQLITE_PATH = _PROJECT_ROOT / "muse.sqlite3"
_SQLITE_LOCK = threading.RLock()
_SQLITE_READY = False


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_storage() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)


def _sqlite_conn() -> sqlite3.Connection:
    _ensure_storage()
    conn = sqlite3.connect(str(_SQLITE_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def ensure_sqlite() -> None:
    global _SQLITE_READY
    if _SQLITE_READY:
        return
    with _SQLITE_LOCK:
        if _SQLITE_READY:
            return
        conn = _sqlite_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL DEFAULT '',
                access_token TEXT NOT NULL DEFAULT '',
                invite_code TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                archived INTEGER NOT NULL DEFAULT 0,
                email_source TEXT NOT NULL DEFAULT '',
                proxy_used TEXT NOT NULL DEFAULT '',
                card_used_id INTEGER,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS email_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'available',
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS card_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_number_enc TEXT NOT NULL DEFAULT '',
                expiry TEXT NOT NULL DEFAULT '',
                cvv_enc TEXT NOT NULL DEFAULT '',
                holder_name TEXT NOT NULL DEFAULT '',
                billing_zip TEXT NOT NULL DEFAULT '',
                billing_country TEXT NOT NULL DEFAULT 'US',
                status TEXT NOT NULL DEFAULT 'available',
                preauth_amount REAL NOT NULL DEFAULT 1.0,
                preauth_at TEXT NOT NULL DEFAULT '',
                refunded_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS registration_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL DEFAULT 'pending',
                email_source TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                account_id INTEGER,
                started_at TEXT NOT NULL DEFAULT '',
                completed_at TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                log_file TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS app_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
        """)

        # 索引
        for table in ("accounts", "email_pool", "registration_jobs"):
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_status ON {table}(status, id DESC)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_email ON {table}(email COLLATE NOCASE)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_created ON {table}(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_email_pool_source_status ON email_pool(source, status, id DESC)")

        conn.commit()
        conn.close()
        _SQLITE_READY = True


ensure_sqlite()


# ============================================================
# 内部工具
# ============================================================

def _payload_set(payload: dict | None, **kwargs) -> str:
    p = dict(payload or {})
    p.update({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in kwargs.items()})
    return json.dumps(p, ensure_ascii=False)


def _row_email_pool(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        payload = json.loads(d.get("payload") or "{}")
    except Exception:
        payload = {}
    # 把 payload 中的字段提升到顶层，方便客户端直接访问
    for key in ("password", "client_id", "refresh_token", "recovery_email", "recovery_code", "copy_line",
                "code_url",
                "imap_server", "imap_port", "imap_ssl", "imap_username", "imap_password", "imap_mailbox"):
        if key in payload:
            d[key] = payload[key]
    d["payload"] = payload
    return d


# ============================================================
# accounts 账号表
# ============================================================

def create_account(
    email: str,
    access_token: str = "",
    invite_code: str = "",
    email_source: str = "",
    proxy_used: str = "",
    card_used_id: int | None = None,
    payload: dict | None = None,
) -> int:
    now = _now()
    conn = _sqlite_conn()
    try:
        cur = conn.execute(
            """INSERT INTO accounts
               (email, access_token, invite_code, email_source, proxy_used, card_used_id,
                created_at, updated_at, payload)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (email, access_token, invite_code, email_source, proxy_used, card_used_id,
             now, now, json.dumps(payload or {}, ensure_ascii=False)),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def get_account_by_email(email: str) -> dict | None:
    conn = _sqlite_conn()
    try:
        row = conn.execute("SELECT * FROM accounts WHERE email=? COLLATE NOCASE", (email,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["payload"] = json.loads(d.get("payload") or "{}")
        except Exception:
            d["payload"] = {}
        return d
    finally:
        conn.close()


def list_accounts(limit: int = 500, offset: int = 0) -> list[dict]:
    conn = _sqlite_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM accounts ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_accounts() -> int:
    conn = _sqlite_conn()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM accounts WHERE archived=0").fetchone()[0])
    finally:
        conn.close()


# ============================================================
# email_pool 邮箱池 (turb 风格)
# ============================================================

def _claim_next_email(source: str, extra_where: str = "") -> dict | None:
    """原子地 claim 一个 available 的邮箱并标记为 using (注册中)。"""
    now = _now()
    conn = _sqlite_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        sql = "SELECT * FROM email_pool WHERE source=? AND status='available' AND archived=0"
        if extra_where:
            sql += f" AND {extra_where}"
        sql += " ORDER BY id ASC LIMIT 1"
        row = conn.execute(sql, (source,)).fetchone()
        if row is None:
            conn.rollback()
            return None
        conn.execute(
            "UPDATE email_pool SET status='using', updated_at=? WHERE id=?",
            (now, row["id"]),
        )
        conn.commit()
        return _row_email_pool(row)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _release_email(email: str, status: str = "available", source: str | None = None, note: str | None = None) -> None:
    now = _now()
    conn = _sqlite_conn()
    try:
        # disabled 是管理员手动停用状态, 任务流程 (失败回池/成功标记) 不得覆盖
        if source:
            conn.execute(
                "UPDATE email_pool SET status=?, updated_at=? WHERE email=? AND source=? AND status!='disabled'",
                (status, now, email, source),
            )
        else:
            conn.execute(
                "UPDATE email_pool SET status=?, updated_at=? WHERE email=? AND status!='disabled'",
                (status, now, email),
            )
        conn.commit()
    finally:
        conn.close()


def _release_unconsumed_email(email: str, source: str, note: str | None = None) -> bool:
    """只回收仍停留在 using 的任务领取，已注册(used)/已停用(disabled)状态不动。"""
    now = _now()
    conn = _sqlite_conn()
    try:
        cur = conn.execute(
            "UPDATE email_pool SET status='available', updated_at=? WHERE email=? AND source=? AND status='using'",
            (now, email, source),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _get_email_by_source(source: str, email: str) -> dict | None:
    conn = _sqlite_conn()
    try:
        row = conn.execute(
            "SELECT * FROM email_pool WHERE source=? AND email=? COLLATE NOCASE LIMIT 1",
            (source, email),
        ).fetchone()
        return _row_email_pool(row) if row else None
    finally:
        conn.close()


def set_email_status(email: str, status: str, source: str | None = None) -> bool:
    """
    手动设置邮箱状态 (WebUI 停用/启用用)。

    status: available(可用) / disabled(停用) / used(已用) / using(注册中)
    返回是否更新了行。
    """
    if status not in ("available", "disabled", "used", "using"):
        raise ValueError(f"非法邮箱状态: {status}")
    now = _now()
    conn = _sqlite_conn()
    try:
        if source:
            cur = conn.execute(
                "UPDATE email_pool SET status=?, updated_at=? WHERE email=? AND source=?",
                (status, now, email, source),
            )
        else:
            cur = conn.execute(
                "UPDATE email_pool SET status=?, updated_at=? WHERE email=?",
                (status, now, email),
            )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _pool_summary(source: str) -> dict:
    conn = _sqlite_conn()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM email_pool WHERE source=? AND archived=0 GROUP BY status",
            (source,),
        ).fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        conn.close()


# ---------- Outlook ----------

def claim_next_outlook() -> dict | None:
    return _claim_next_email("outlook")


def get_outlook_by_email(email: str) -> dict | None:
    return _get_email_by_source("outlook", email)


def release_outlook(email: str, status: str = "available", note: str | None = None) -> None:
    _release_email(email, status=status, source="outlook", note=note)


def release_unconsumed_outlook(email: str, note: str | None = None) -> bool:
    return _release_unconsumed_email(email, source="outlook", note=note)


def outlook_pool_summary() -> dict:
    return _pool_summary("outlook")


def import_outlook_accounts(records: list[dict]) -> tuple[int, int]:
    """
    批量导入 Outlook 账号。

    records 每项是 dict: {
        "email", "password", "client_id", "refresh_token",
        "recovery_email" (可选), "recovery_code" (可选)
    }
    """
    now = _now()
    conn = _sqlite_conn()
    added, skipped = 0, 0
    try:
        for rec in records:
            email = str(rec.get("email") or "").strip()
            if not email:
                skipped += 1
                continue
            existing = conn.execute(
                "SELECT id FROM email_pool WHERE source='outlook' AND email=? COLLATE NOCASE AND archived=0",
                (email,),
            ).fetchone()
            if existing:
                # 更新素材 (允许刷新 refresh_token 等)
                payload = {
                    "password": rec.get("password", ""),
                    "client_id": rec.get("client_id", ""),
                    "refresh_token": rec.get("refresh_token", ""),
                    "recovery_email": rec.get("recovery_email", ""),
                    "recovery_code": rec.get("recovery_code", ""),
                    "copy_line": email,
                }
                conn.execute(
                    "UPDATE email_pool SET payload=?, status='available', updated_at=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), now, existing["id"]),
                )
                skipped += 1
                continue

            payload = {
                "password": rec.get("password", ""),
                "client_id": rec.get("client_id", ""),
                "refresh_token": rec.get("refresh_token", ""),
                "recovery_email": rec.get("recovery_email", ""),
                "recovery_code": rec.get("recovery_code", ""),
                "copy_line": email,
            }
            conn.execute(
                """INSERT INTO email_pool
                   (email, source, status, created_at, updated_at, payload)
                   VALUES (?, 'outlook', 'available', ?, ?, ?)""",
                (email, now, now, json.dumps(payload, ensure_ascii=False)),
            )
            added += 1
        conn.commit()
        return added, skipped
    finally:
        conn.close()


# ---------- Generic API ----------

def claim_next_generic_api_email() -> dict | None:
    return _claim_next_email("generic_api")


def get_generic_api_email_by_email(email: str) -> dict | None:
    return _get_email_by_source("generic_api", email)


def release_generic_api_email(email: str, status: str = "available", note: str | None = None) -> None:
    _release_email(email, status=status, source="generic_api", note=note)


def release_unconsumed_generic_api_email(email: str, note: str | None = None) -> bool:
    return _release_unconsumed_email(email, source="generic_api", note=note)


def generic_api_email_pool_summary() -> dict:
    return _pool_summary("generic_api")


def import_generic_api_emails(records: list[dict]) -> tuple[int, int]:
    """批量导入通用 API 邮箱。records 每项: {"email", "code_url"}"""
    now = _now()
    conn = _sqlite_conn()
    added, skipped = 0, 0
    try:
        for rec in records:
            email = str(rec.get("email") or "").strip()
            code_url = str(rec.get("code_url") or "").strip()
            if not email or not code_url:
                skipped += 1
                continue
            existing = conn.execute(
                "SELECT id FROM email_pool WHERE source='generic_api' AND email=? COLLATE NOCASE AND archived=0",
                (email,),
            ).fetchone()
            payload = {"code_url": code_url, "copy_line": f"{email}----{code_url}"}
            if existing:
                conn.execute(
                    "UPDATE email_pool SET payload=?, status='available', updated_at=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), now, existing["id"]),
                )
                skipped += 1
                continue
            conn.execute(
                """INSERT INTO email_pool
                   (email, source, status, created_at, updated_at, payload)
                   VALUES (?, 'generic_api', 'available', ?, ?, ?)""",
                (email, now, now, json.dumps(payload, ensure_ascii=False)),
            )
            added += 1
        conn.commit()
        return added, skipped
    finally:
        conn.close()


# ---------- IMAP ----------

def claim_next_imap_email() -> dict | None:
    return _claim_next_email("imap")


def get_imap_email_by_email(email: str) -> dict | None:
    return _get_email_by_source("imap", email)


def release_imap_email(email: str, status: str = "available", note: str | None = None) -> None:
    _release_email(email, status=status, source="imap", note=note)


def release_unconsumed_imap_email(email: str, note: str | None = None) -> bool:
    return _release_unconsumed_email(email, source="imap", note=note)


def imap_email_pool_summary() -> dict:
    return _pool_summary("imap")


def import_imap_emails(records: list[dict]) -> tuple[int, int]:
    """批量导入 IMAP 邮箱。"""
    from config.email import IMAP_DEFAULT_PORT, IMAP_USE_SSL, IMAP_MAILBOX
    now = _now()
    conn = _sqlite_conn()
    added, skipped = 0, 0
    try:
        for rec in records:
            email = str(rec.get("email") or "").strip()
            server = str(rec.get("server") or rec.get("imap_server") or "").strip()
            if not email or not server:
                skipped += 1
                continue
            existing = conn.execute(
                "SELECT id FROM email_pool WHERE source='imap' AND email=? COLLATE NOCASE AND archived=0",
                (email,),
            ).fetchone()
            payload = {
                "imap_server": server,
                "imap_port": int(rec.get("port") or rec.get("imap_port") or IMAP_DEFAULT_PORT),
                "imap_ssl": 1 if (rec.get("use_ssl") if "use_ssl" in rec else IMAP_USE_SSL) else 0,
                "imap_username": str(rec.get("username") or rec.get("imap_username") or email),
                "imap_password": str(rec.get("password") or rec.get("imap_password") or ""),
                "imap_mailbox": str(rec.get("mailbox") or rec.get("imap_mailbox") or IMAP_MAILBOX),
                "copy_line": email,
            }
            if existing:
                conn.execute(
                    "UPDATE email_pool SET payload=?, status='available', updated_at=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), now, existing["id"]),
                )
                skipped += 1
                continue
            conn.execute(
                """INSERT INTO email_pool
                   (email, source, status, created_at, updated_at, payload)
                   VALUES (?, 'imap', 'available', ?, ?, ?)""",
                (email, now, now, json.dumps(payload, ensure_ascii=False)),
            )
            added += 1
        conn.commit()
        return added, skipped
    finally:
        conn.close()


# ---------- OmniMail ----------

def claim_next_omnimail() -> dict | None:
    return _claim_next_email("omnimail")


def get_omnimail_by_email(email: str) -> dict | None:
    return _get_email_by_source("omnimail", email)


def release_omnimail(email: str, status: str = "available", note: str | None = None) -> None:
    _release_email(email, status=status, source="omnimail", note=note)


def release_unconsumed_omnimail(email: str, note: str | None = None) -> bool:
    return _release_unconsumed_email(email, source="omnimail", note=note)


def omnimail_pool_summary() -> dict:
    return _pool_summary("omnimail")


def import_omnimail_mailboxes(mailboxes: list[dict]) -> tuple[int, int]:
    """
    从 OmniMail API 返回的 mailboxes 列表批量入库.
    每项: {"address":"user@example.com", "domain":"example.com", "isActive":true, "isPrimary":false}
    """
    now = _now()
    conn = _sqlite_conn()
    added, skipped = 0, 0
    try:
        for mb in mailboxes:
            address = str(mb.get("address") or "").strip().lower()
            if not address:
                skipped += 1
                continue
            existing = conn.execute(
                "SELECT id FROM email_pool WHERE source='omnimail' AND email=? COLLATE NOCASE AND archived=0",
                (address,),
            ).fetchone()
            payload = {
                "domain": mb.get("domain") or address.split("@", 1)[1] if "@" in address else "",
                "isActive": bool(mb.get("isActive", True)),
                "isPrimary": bool(mb.get("isPrimary", False)),
            }
            if existing:
                # 只更新 payload, 不动用户/任务设置的状态 (available/using/used/disabled)
                conn.execute(
                    "UPDATE email_pool SET updated_at=?, payload=? WHERE id=?",
                    (now, json.dumps(payload, ensure_ascii=False), existing[0]),
                )
                skipped += 1
            else:
                conn.execute(
                    "INSERT INTO email_pool (email, source, status, archived, created_at, updated_at, payload)"
                    " VALUES (?, 'omnimail', 'available', 0, ?, ?, ?)",
                    (address, now, now, json.dumps(payload, ensure_ascii=False)),
                )
                added += 1
        conn.commit()
        return added, skipped
    finally:
        conn.close()


def import_omnimail_icloud_aliases(aliases: list[dict]) -> tuple[int, int]:
    """
    从 OmniMail API 返回的 iCloud Hide My Email 别名批量入库.
    每项: {"accountId":"icloud_xxx", "email":"xxx@icloud.com", "realEmail":"hdy980@icloud.com", "label":"gpt6"}
    payload.kind = 'icloud_alias' → fetch_latest_otp 时自动走 /icloud/inbox 端点取码.
    """
    now = _now()
    conn = _sqlite_conn()
    added, skipped = 0, 0
    try:
        for alias in aliases:
            address = str(alias.get("email") or "").strip().lower()
            if not address or "@icloud.com" not in address:
                skipped += 1
                continue
            existing = conn.execute(
                "SELECT id FROM email_pool WHERE source='omnimail' AND email=? COLLATE NOCASE AND archived=0",
                (address,),
            ).fetchone()
            payload = {
                "kind": "icloud_alias",
                "accountId": alias.get("accountId") or "",
                "realEmail": alias.get("realEmail") or "",
                "label": alias.get("label") or "",
            }
            if existing:
                # 只更新 payload, 不动用户/任务设置的状态 (available/using/used/disabled)
                conn.execute(
                    "UPDATE email_pool SET updated_at=?, payload=? WHERE id=?",
                    (now, json.dumps(payload, ensure_ascii=False), existing[0]),
                )
                skipped += 1
            else:
                conn.execute(
                    "INSERT INTO email_pool (email, source, status, archived, created_at, updated_at, payload)"
                    " VALUES (?, 'omnimail', 'available', 0, ?, ?, ?)",
                    (address, now, now, json.dumps(payload, ensure_ascii=False)),
                )
                added += 1
        conn.commit()
        return added, skipped
    finally:
        conn.close()


def import_omnimail_from_api(
    base: str,
    email: str,
    password: str,
    device_name: str = "muse-register",
) -> tuple[int, int]:
    """登录 OmniMail API, 拉全量 mailbox + iCloud 别名, 自动入库."""
    from core.omnimail_client import OmniMailClient
    client = OmniMailClient(base=base, email=email, password=password, device_name=device_name)
    client.login()
    added_m, skipped_m = import_omnimail_mailboxes(client.list_mailboxes())
    added_a, skipped_a = 0, 0
    try:
        added_a, skipped_a = import_omnimail_icloud_aliases(client.get_all_icloud_aliases())
    except Exception as exc:
        # iCloud 别名拉取失败不阻塞主流程 (没绑定 iCloud 账户也会 404)
        import logging
        logging.getLogger(__name__).warning(f"OmniMail iCloud 别名同步失败: {exc}")
    return added_m + added_a, skipped_m + skipped_a


# ---------- 通用 email_pool ----------

def list_email_pool(source: str | None = None, status: str | None = None, limit: int = 500) -> list[dict]:
    conn = _sqlite_conn()
    try:
        sql = "SELECT * FROM email_pool WHERE archived=0"
        args: list = []
        if source:
            sql += " AND source=?"
            args.append(source)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        rows = conn.execute(sql, args).fetchall()
        return [_row_email_pool(r) for r in rows]
    finally:
        conn.close()


def delete_email_pool_entry(email: str, source: str | None = None) -> None:
    now = _now()
    conn = _sqlite_conn()
    try:
        if source:
            conn.execute(
                "UPDATE email_pool SET archived=1, status='deleted', updated_at=? WHERE email=? AND source=?",
                (now, email, source),
            )
        else:
            conn.execute(
                "UPDATE email_pool SET archived=1, status='deleted', updated_at=? WHERE email=?",
                (now, email),
            )
        conn.commit()
    finally:
        conn.close()


# ============================================================
# card_pool 银行卡池 (保留现有实现，仅增加 CRUD)
# ============================================================

def create_card(
    card_number_enc: str, expiry: str, cvv_enc: str, holder_name: str,
    billing_zip: str, billing_country: str = "US", note: str = "",
) -> int:
    now = _now()
    conn = _sqlite_conn()
    try:
        cur = conn.execute(
            """INSERT INTO card_pool
               (card_number_enc, expiry, cvv_enc, holder_name, billing_zip, billing_country,
                note, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (card_number_enc, expiry, cvv_enc, holder_name, billing_zip, billing_country,
             note, now, now),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def get_available_card() -> dict | None:
    now = _now()
    conn = _sqlite_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM card_pool WHERE status='available' LIMIT 1"
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        conn.execute("UPDATE card_pool SET status='used', updated_at=? WHERE id=?", (now, row["id"]))
        conn.commit()
        return dict(row)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def update_card_status(card_id: int, status: str, last_error: str = "") -> None:
    now = _now()
    conn = _sqlite_conn()
    try:
        conn.execute(
            "UPDATE card_pool SET status=?, last_error=?, updated_at=? WHERE id=?",
            (status, last_error, now, card_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_card_preauth(card_id: int, amount: float = 1.0) -> None:
    now = _now()
    conn = _sqlite_conn()
    try:
        conn.execute(
            "UPDATE card_pool SET status='preauthorized', preauth_amount=?, preauth_at=?, updated_at=? WHERE id=?",
            (amount, now, now, card_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_cards(status: str | None = None) -> list[dict]:
    conn = _sqlite_conn()
    try:
        if status:
            rows = conn.execute(
                "SELECT id, expiry, holder_name, billing_zip, billing_country, status, "
                "preauth_amount, preauth_at, refunded_at, last_error, note, created_at, updated_at "
                "FROM card_pool WHERE status=? ORDER BY id DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, expiry, holder_name, billing_zip, billing_country, status, "
                "preauth_amount, preauth_at, refunded_at, last_error, note, created_at, updated_at "
                "FROM card_pool ORDER BY id DESC",
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_card(card_id: int) -> None:
    conn = _sqlite_conn()
    try:
        conn.execute("DELETE FROM card_pool WHERE id=?", (card_id,))
        conn.commit()
    finally:
        conn.close()


def count_cards() -> dict:
    conn = _sqlite_conn()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM card_pool GROUP BY status",
        ).fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        conn.close()


# ============================================================
# registration_jobs 任务表
# ============================================================

def create_job(email_source: str = "outlook") -> dict:
    now = _now()
    conn = _sqlite_conn()
    try:
        cur = conn.execute(
            """INSERT INTO registration_jobs
               (status, email_source, started_at, note, payload)
               VALUES ('pending',?,?,?,'{}')""",
            (email_source, now, ""),
        )
        conn.commit()
        job_id = int(cur.lastrowid)
        log_dir = _DATA_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = str(log_dir / f"job_{job_id}_{datetime.now().strftime('%H%M%S')}.log")
        conn.execute("UPDATE registration_jobs SET log_file=? WHERE id=?", (log_file, job_id))
        conn.commit()
        return get_job(job_id)
    finally:
        conn.close()


def get_job(job_id: int) -> dict | None:
    conn = _sqlite_conn()
    try:
        row = conn.execute("SELECT * FROM registration_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_job(job_id: int, **fields) -> None:
    if not fields:
        return
    now = _now()
    conn = _sqlite_conn()
    try:
        sets = []
        vals = []
        for k, v in fields.items():
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k}=?")
            vals.append(v)
        sets.append("updated_at=?")
        vals.append(now)
        vals.append(job_id)
        conn.execute(f"UPDATE registration_jobs SET {','.join(sets)} WHERE id=?", vals)
        conn.commit()
    finally:
        conn.close()


def list_jobs(limit: int = 100, offset: int = 0) -> list[dict]:
    conn = _sqlite_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM registration_jobs ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_job(job_id: int) -> dict | None:
    """删除一条已结束的任务记录，返回删除前的记录以便调用方清理日志。"""
    conn = _sqlite_conn()
    try:
        row = conn.execute("SELECT * FROM registration_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        conn.execute("DELETE FROM registration_jobs WHERE id=?", (job_id,))
        conn.commit()
        return item
    finally:
        conn.close()
