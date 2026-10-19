# -*- coding: utf-8 -*-
"""
银行卡池模块 - 带 AES-GCM 加密

银行卡号和 CVV 在数据库中加密存储。
密钥从 .env CARD_ENCRYPTION_KEY 读取（base64 编码 32 字节 key），
未配置则自动生成并落盘到 .card_key。
"""
import base64
import json
import logging
import os
import re
import secrets
from datetime import datetime
from pathlib import Path

from core import db

logger = logging.getLogger(__name__)

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad, unpad
    _CRYPTO_OK = True
except ImportError:
    _CRYPTO_OK = False
    logger.warning("[CardPool] pycryptodome 未安装，银行卡加密功能不可用")


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_KEY_FILE = _PROJECT_ROOT / ".card_key"
_key_cache: bytes | None = None


def _load_or_create_key() -> bytes:
    """加载或生成 32 字节 AES key。"""
    global _key_cache
    if _key_cache:
        return _key_cache

    # 1) 先读环境变量
    env_key = os.environ.get("CARD_ENCRYPTION_KEY", "").strip()
    if env_key:
        try:
            key = base64.b64decode(env_key)
            if len(key) == 32:
                _key_cache = key
                return key
        except Exception:
            pass

    # 2) 再读 key 文件
    if _KEY_FILE.exists():
        try:
            key = base64.b64decode(_KEY_FILE.read_text().strip())
            if len(key) == 32:
                _key_cache = key
                return key
        except Exception:
            pass

    # 3) 生成新 key 并保存
    key = secrets.token_bytes(32)
    _KEY_FILE.write_text(base64.b64encode(key).decode())
    _KEY_FILE.chmod(0o600)
    _key_cache = key
    logger.info(f"[CardPool] 已生成新的加密密钥: {_KEY_FILE}")
    return key


def _encrypt(plaintext: str) -> str:
    """AES-GCM 加密，返回 base64 字符串 nonce(12B)+ciphertext+tag(16B)。"""
    if not _CRYPTO_OK or not plaintext:
        return plaintext
    key = _load_or_create_key()
    nonce = secrets.token_bytes(12)  # 显式 12 字节, 与 _decrypt 的切分对齐
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext.encode())
    return base64.b64encode(nonce + ciphertext + tag).decode()


def _decrypt(ciphertext_b64: str) -> str:
    """AES-GCM 解密。"""
    if not _CRYPTO_OK or not ciphertext_b64:
        return ciphertext_b64
    key = _load_or_create_key()
    raw = base64.b64decode(ciphertext_b64)
    nonce = raw[:12]
    ciphertext_tag = raw[12:]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    plaintext = cipher.decrypt_and_verify(ciphertext_tag[:-16], ciphertext_tag[-16:])
    return plaintext.decode()


# ============================================================
# 公共接口
# ============================================================

def add_card(
    card_number: str,
    expiry: str,
    cvv: str,
    holder_name: str,
    billing_zip: str,
    billing_country: str = "US",
    note: str = "",
) -> int:
    """添加一张银行卡到池。卡号和 CVV 自动加密。"""
    # 清理卡号中的空格和连字符
    clean_number = card_number.replace(" ", "").replace("-", "")
    card_number_enc = _encrypt(clean_number)
    cvv_enc = _encrypt(cvv)
    return db.create_card(
        card_number_enc=card_number_enc,
        expiry=expiry.strip(),
        cvv_enc=cvv_enc,
        holder_name=holder_name.strip(),
        billing_zip=billing_zip.strip(),
        billing_country=billing_country.strip(),
        note=note,
    )


def parse_pasted_cards(text: str) -> tuple[list[dict], list[str]]:
    """解析 WebUI 粘贴的银行卡信息，返回 (有效卡片, 行级错误)。

    支持每行以 ``----``、竖线、逗号或 Tab 分隔：
      卡号----MM/YY----CVV----邮编----国家----备注
      卡号----MM/YY----CVV----持卡人----邮编----国家----备注
    """
    cards: list[dict] = []
    errors: list[str] = []
    for line_no, raw_line in enumerate(str(text or "").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in re.split(r"\s*(?:----|\||,|\t)\s*", line)]
        if len(parts) < 3:
            errors.append(f"第 {line_no} 行：至少需要卡号、有效期、CVV")
            continue

        number = re.sub(r"[\s-]", "", parts[0])
        expiry_match = re.fullmatch(r"(0?[1-9]|1[0-2])\s*[/\\-]\s*(\d{2}|\d{4})", parts[1])
        cvv = re.sub(r"\s+", "", parts[2])
        if not re.fullmatch(r"\d{13,19}", number):
            errors.append(f"第 {line_no} 行：卡号格式无效")
            continue
        if not expiry_match:
            errors.append(f"第 {line_no} 行：有效期应为 MM/YY")
            continue
        if not re.fullmatch(r"\d{3,4}", cvv):
            errors.append(f"第 {line_no} 行：CVV 应为 3 或 4 位数字")
            continue

        month, year = expiry_match.groups()
        expiry = f"{int(month):02d}/{year[-2:]}"
        holder_name = billing_zip = note = ""
        billing_country = "US"
        rest = parts[3:]
        if rest:
            # 邮编是 CVV 后的第一个纯数字字段；此前的文本按持卡人处理。
            zip_index = next((i for i, value in enumerate(rest) if re.fullmatch(r"\d{3,10}(?:-\d{4})?", value)), None)
            if zip_index is not None:
                holder_name = " ".join(rest[:zip_index])
                billing_zip = rest[zip_index]
                rest = rest[zip_index + 1:]
            elif re.fullmatch(r"[A-Za-z]{2}", rest[0]):
                pass
            else:
                holder_name = rest.pop(0)
        if rest and re.fullmatch(r"[A-Za-z]{2}", rest[0]):
            billing_country = rest.pop(0).upper()
        if rest:
            note = " ".join(rest)

        cards.append({
            "card_number": number,
            "expiry": expiry,
            "cvv": cvv,
            "holder_name": holder_name,
            "billing_zip": billing_zip,
            "billing_country": billing_country,
            "note": note,
        })
    return cards, errors


def import_pasted_cards(text: str) -> tuple[int, list[str]]:
    """解析并加密保存粘贴的银行卡信息。"""
    cards, errors = parse_pasted_cards(text)
    for card in cards:
        add_card(**card)
    return len(cards), errors


def get_card_for_use() -> dict | None:
    """取一张可用银行卡并解密返回明文字段。"""
    card = db.get_available_card()
    if not card:
        return None
    card["card_number"] = _decrypt(card.pop("card_number_enc", ""))
    card["cvv"] = _decrypt(card.pop("cvv_enc", ""))
    return card


def release_card(card_id: int, status: str = "available", error: str = "") -> None:
    """归还银行卡到池。"""
    db.update_card_status(card_id, status, last_error=error)


def mark_card_preauth(card_id: int, amount: float = 1.0) -> None:
    """标记卡片已完成 $1 预授权（Muse 年龄验证）。"""
    db.mark_card_preauth(card_id, amount)


def list_cards(status: str | None = None, include_sensitive: bool = False) -> list[dict]:
    """列出银行卡。include_sensitive=True 时返回解密后的卡号后 4 位。"""
    cards = db.list_cards(status)
    if include_sensitive and _CRYPTO_OK:
        # 解密后掩码显示
        raw_conn = db._sqlite_conn()
        try:
            for c in cards:
                row = raw_conn.execute(
                    "SELECT card_number_enc FROM card_pool WHERE id=?", (c["id"],)
                ).fetchone()
                if row:
                    number = _decrypt(row["card_number_enc"])
                    c["card_masked"] = "*" * (len(number) - 4) + number[-4:]
        finally:
            raw_conn.close()
    return cards


def remove_card(card_id: int) -> None:
    """删除银行卡。"""
    db.delete_card(card_id)


def import_cards_csv(file_path: str) -> int:
    """从 CSV 导入银行卡池。格式: number,expiry,cvv,holder,zip,country,note"""
    import csv
    count = 0
    with open(file_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or row[0].startswith("#") or row[0].lower() == "number":
                continue
            try:
                number, expiry, cvv, holder, zip_code = row[:5]
                country = row[5] if len(row) > 5 else "US"
                note = row[6] if len(row) > 6 else ""
                add_card(number, expiry, cvv, holder, zip_code, country, note)
                count += 1
            except Exception as e:
                logger.warning(f"[CardPool] 导入失败: {row} - {e}")
    logger.info(f"[CardPool] 成功导入 {count} 张卡")
    return count


def card_stats() -> dict:
    """银行卡池状态统计。"""
    return db.count_cards()
