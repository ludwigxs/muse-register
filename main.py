# -*- coding: utf-8 -*-
"""
Muse by Meta 注册机 - CLI 入口 (保留手动单次注册能力)

批量注册推荐走 WebUI (python web.py)
"""
import argparse
import logging
import sys
import time
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    parser = argparse.ArgumentParser(description="Muse by Meta 注册机 CLI")
    parser.add_argument("--email", "-e", type=str, default="", help="注册邮箱 (留空交互式输入)")
    parser.add_argument("--imap-server", type=str, default="")
    parser.add_argument("--imap-user", type=str, default="")
    parser.add_argument("--imap-password", type=str, default="")
    parser.add_argument("--imap-port", type=int, default=993)
    parser.add_argument("--invite-code", type=str, default="")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    from core.session import MuseSession
    from core.muse_auth import MuseAuth, MuseAuthError, OtpInvalidError
    from core.db import create_account
    from core.email_pool import _extract_body
    import imaplib
    import email as email_lib
    import re
    from email.utils import parsedate_to_datetime

    email_addr = args.email or input("请输入注册邮箱: ").strip()
    if not email_addr:
        print("邮箱不能为空")
        sys.exit(1)

    print(f"\n[CLI] 开始注册 Muse: {email_addr}")

    with MuseSession() as session:
        auth = MuseAuth(session)
        auth.restart()
        auth.report_caa_events()
        auth.report_falco()
        auth.send_otp(email_addr)

        # 等 OTP
        otp_code = ""
        if args.imap_server and args.imap_password:
            print(f"[CLI] 通过 IMAP {args.imap_server}:{args.imap_port} 自动取码...")
            otp_deadline = time.time() + 180
            while time.time() < otp_deadline:
                mail = None
                try:
                    mail = imaplib.IMAP4_SSL(args.imap_server, args.imap_port)
                    mail.login(args.imap_user or email_addr, args.imap_password)
                    mail.select("INBOX", readonly=True)
                    _, msg_nums = mail.search(None, '(OR SUBJECT "Muse" SUBJECT "Meta" SUBJECT "verification")')
                    if msg_nums and msg_nums[0]:
                        for num in reversed(msg_nums[0].split()[-10:]):
                            _, md = mail.fetch(num, "(RFC822)")
                            msg = email_lib.message_from_bytes(md[0][1])
                            body = _extract_body(msg)
                            m = re.search(r'\b(\d{6})\b', body)
                            if m:
                                otp_code = m.group(1)
                                break
                except Exception as e:
                    print(f"[CLI] IMAP 检查异常: {e}")
                finally:
                    if mail:
                        try: mail.logout()
                        except: pass
                if otp_code:
                    break
                time.sleep(5)

        if not otp_code:
            otp_code = input("[CLI] 请输入 6 位 OTP: ").strip()

        if not otp_code:
            print("[CLI] 未获取到 OTP")
            sys.exit(1)

        print(f"[CLI] 提交 OTP...")
        reg = auth.confirm_otp(otp_code)

        # 邀请码
        invite = args.invite_code
        if invite:
            print(f"[CLI] 兑换邀请码 {invite}...")
            try:
                auth.redeem_invite_code(invite)
            except Exception as e:
                print(f"[CLI] 邀请码兑换失败: {e}")

        access_token = reg.get("access_token") or reg.get("accessToken") or ""
        account_id = create_account(
            email=email_addr, access_token=access_token,
            invite_code=invite, proxy_used=session.proxy or "",
        )

        print(f"\n{'='*50}")
        print(f"[CLI] 注册成功!")
        print(f"  账号 ID: {account_id}")
        print(f"  邮箱: {email_addr}")
        print(f"  Token: {access_token[:32] if access_token else '(无)'}")
        print(f"{'='*50}")


if __name__ == "__main__":
    main()
