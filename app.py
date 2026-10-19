# -*- coding: utf-8 -*-
"""
Flask 本地控制台 - Muse by Meta 注册机

所有接口返回 JSON；前端是单页 templates/index.html (原生 JS + fetch)。
默认绑定 127.0.0.1:8799。
"""
import logging

from flask import Flask, jsonify, render_template, request

from core import db
from core import registration_service as svc
from core import email_pool, card_pool
from config import proxy as proxy_cfg
from config import muse as muse_cfg
from config import email as email_cfg
from config import roxybrowser as roxy_cfg
from config import cloakbrowser as cloak_cfg

logger = logging.getLogger(__name__)


def _hot_update_attrs(mapping: dict, data: dict) -> None:
    for key_lower, module in mapping.items():
        val = data.get(key_lower)
        if val is None:
            continue
        attr = key_lower.upper()
        existing = getattr(module, attr, None)
        if isinstance(existing, bool):
            val = bool(val)
        setattr(module, attr, val)


def _update_env_file(updates: dict[str, str]) -> None:
    """把配置项写入 .env (空值=删除该键)。"""
    import pathlib
    env_path = pathlib.Path(__file__).resolve().parent.parent / ".env"
    lines = []
    existing = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()
            else:
                lines.append(line)
    for key, val in updates.items():
        val = str(val).strip()
        if val and val.lower() not in ("false", "0", ""):
            existing[key] = val
        elif key in existing:
            del existing[key]
    for key, val in existing.items():
        lines.append(f"{key}={val}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def create_app() -> Flask:
    app = Flask(__name__)

    # ==================== 首页 ====================

    @app.route("/")
    def index():
        return render_template("index.html")

    # ==================== 健康检查 ====================

    @app.get("/api/health")
    def api_health():
        return jsonify({"ok": True})

    # ==================== 配置读写 ====================

    @app.get("/api/config")
    def api_get_config():
        return jsonify({
            "invite_code": muse_cfg.INVITE_CODE,
            "proxy_pool": proxy_cfg.PROXY_POOL,
            "proxy_extract_api": proxy_cfg.PROXY_EXTRACT_API,
            "email_source": email_cfg.EMAIL_SOURCE,
            "use_email_service": email_cfg.USE_EMAIL_SERVICE,
            "otp_max_wait": email_cfg.OTP_MAX_WAIT,
            "otp_poll_interval": email_cfg.OTP_POLL_INTERVAL,
            "registration_driver": roxy_cfg.REGISTRATION_DRIVER,
            # Roxy
            "roxy_api_base": roxy_cfg.ROXY_API_BASE,
            "roxy_api_token": roxy_cfg.ROXY_API_TOKEN,
            "roxy_profile_id": roxy_cfg.ROXY_PROFILE_ID,
            "roxy_workspace_id": roxy_cfg.ROXY_WORKSPACE_ID,
            "roxy_open_headless": roxy_cfg.ROXY_OPEN_HEADLESS,
            "roxy_one_profile_per_account": roxy_cfg.ROXY_ONE_PROFILE_PER_ACCOUNT,
            "roxy_delete_profile_after_run": roxy_cfg.ROXY_DELETE_PROFILE_AFTER_RUN,
            "roxy_create_use_proxy_pool": roxy_cfg.ROXY_CREATE_USE_PROXY_POOL,
            # Cloak
            "cloak_headless": cloak_cfg.CLOAK_HEADLESS,
            "cloak_humanize": cloak_cfg.CLOAK_HUMANIZE,
            "cloak_geoip": cloak_cfg.CLOAK_GEOIP,
            "cloak_use_proxy": cloak_cfg.CLOAK_USE_PROXY,
            "cloak_license_key": cloak_cfg.CLOAK_LICENSE_KEY,
            "cloak_fingerprint_seed": cloak_cfg.CLOAK_FINGERPRINT_SEED,
        })

    @app.post("/api/config")
    def api_set_config():
        data = request.get_json(force=True, silent=True) or {}
        env_path = __import__("pathlib").Path(__file__).resolve().parent.parent / ".env"
        lines = []
        existing = {}
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    existing[k.strip()] = v.strip()
                else:
                    lines.append(line)

        env_keys = [
            "INVITE_CODE",
            "REGISTRATION_DRIVER",
            "ROXY_API_BASE", "ROXY_API_TOKEN", "ROXY_PROFILE_ID",
            "ROXY_WORKSPACE_ID", "ROXY_OPEN_HEADLESS",
            "ROXY_ONE_PROFILE_PER_ACCOUNT", "ROXY_DELETE_PROFILE_AFTER_RUN",
            "ROXY_CREATE_USE_PROXY_POOL",
            "CLOAK_HEADLESS", "CLOAK_HUMANIZE", "CLOAK_GEOIP",
            "CLOAK_USE_PROXY", "CLOAK_LICENSE_KEY", "CLOAK_FINGERPRINT_SEED",
        ]

        for key in env_keys:
            val = data.get(key.lower())
            if val is None:
                continue
            if isinstance(val, bool):
                val = "true" if val else "false"
            val = str(val).strip()
            if val and val not in ("false", "0", ""):
                existing[key] = val
            elif key in existing:
                del existing[key]

        for key, val in existing.items():
            lines.append(f"{key}={val}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        _hot_update_attrs({
            "invite_code": muse_cfg,
            "registration_driver": roxy_cfg,
            "roxy_api_base": roxy_cfg, "roxy_api_token": roxy_cfg,
            "roxy_profile_id": roxy_cfg, "roxy_workspace_id": roxy_cfg,
            "roxy_open_headless": roxy_cfg,
            "roxy_one_profile_per_account": roxy_cfg,
            "roxy_delete_profile_after_run": roxy_cfg,
            "roxy_create_use_proxy_pool": roxy_cfg,
            "cloak_headless": cloak_cfg, "cloak_humanize": cloak_cfg,
            "cloak_geoip": cloak_cfg, "cloak_use_proxy": cloak_cfg,
            "cloak_license_key": cloak_cfg, "cloak_fingerprint_seed": cloak_cfg,
        }, data)

        auto_workspace = bool(data.get("roxy_auto_workspace"))
        workspace_info = None
        if auto_workspace:
            try:
                from core.roxybrowser_client import RoxyBrowserClient

                discovered = RoxyBrowserClient().list_workspaces()
                items = discovered.get("items") or []
                if not discovered.get("ok") or not items:
                    raise RuntimeError("未能从当前 Roxy 账号读取工作区，请检查 API Base 和 API Token")
                selected = items[0]
                workspace_id = str(selected.get("id") or "").strip()
                if not workspace_id:
                    raise RuntimeError("Roxy 工作区返回缺少 ID")
                project_id = str(selected.get("projectId") or "").strip()
                roxy_cfg.ROXY_WORKSPACE_ID = workspace_id
                roxy_cfg.ROXY_PROJECT_ID = project_id
                _update_env_file({
                    "ROXY_WORKSPACE_ID": workspace_id,
                    "ROXY_PROJECT_ID": project_id,
                })
                workspace_info = {
                    "id": workspace_id,
                    "name": str(selected.get("name") or ""),
                    "project_id": project_id,
                }
                logger.info("[Roxy] 自动选择工作区 id=%s name=%s", workspace_id, workspace_info["name"])
            except Exception as exc:
                return jsonify({"ok": False, "error": f"Roxy 工作区自动获取失败: {exc}"}), 400

        return jsonify({"ok": True, "workspace": workspace_info})

    # ==================== 指纹浏览器健康检查 ====================

    @app.get("/api/fingerprint/health")
    def api_fingerprint_health():
        out = {}
        roxy_base = roxy_cfg.ROXY_API_BASE
        if roxy_base:
            try:
                import requests as _req
                headers = {}
                if roxy_cfg.ROXY_API_TOKEN:
                    headers["X-API-Token"] = roxy_cfg.ROXY_API_TOKEN
                r = _req.get(
                    roxy_base.rstrip("/") + roxy_cfg.ROXY_WORKSPACE_LIST_PATH,
                    headers=headers, timeout=3,
                )
                out["roxy"] = {"reachable": True, "status": r.status_code, "base": roxy_base}
            except Exception as e:
                out["roxy"] = {"reachable": False, "error": str(e)[:200], "base": roxy_base}
        else:
            out["roxy"] = {"reachable": False, "error": "未配置 ROXY_API_BASE"}

        try:
            import cloakbrowser
            out["cloak"] = {"available": True}
        except ImportError:
            out["cloak"] = {"available": False, "error": "未安装 cloakbrowser (pip install cloakbrowser)"}
        except Exception as e:
            out["cloak"] = {"available": True, "note": str(e)[:100]}

        out["driver"] = roxy_cfg.REGISTRATION_DRIVER
        return jsonify(out)

    # ==================== 账号 ====================

    @app.get("/api/accounts")
    def api_list_accounts():
        limit = int(request.args.get("limit", 100))
        offset = int(request.args.get("offset", 0))
        return jsonify({
            "ok": True,
            "items": db.list_accounts(limit=limit, offset=offset),
            "total": db.count_accounts(),
        })

    # ==================== 邮箱池 ====================

    @app.get("/api/emails")
    def api_list_emails():
        source = (request.args.get("source") or "").strip().lower()
        if source in ("", "all"):
            items = db.list_email_pool()
            stats = {}
            for it in items:
                stats[it["status"]] = stats.get(it["status"], 0) + 1
            return jsonify({"ok": True, "items": items, "stats": stats})
        return jsonify({
            "ok": True,
            "items": db.list_email_pool(source=source),
            "stats": db._pool_summary(source),
        })

    @app.post("/api/emails/<email>/status")
    def api_set_email_status(email):
        """手动切换邮箱状态: available(可用) / disabled(停用) / used(已用)。"""
        data = request.get_json(force=True, silent=True) or {}
        status = str(data.get("status") or "").strip().lower()
        source = (data.get("source") or "").strip() or None
        try:
            changed = db.set_email_status(email, status, source=source)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not changed:
            return jsonify({"ok": False, "error": f"未找到邮箱 {email}"}), 404
        return jsonify({"ok": True, "email": email, "status": status})

    @app.post("/api/emails/import")
    def api_import_emails():
        """
        统一邮箱导入接口 (复刻 turb)。

        支持三种模式:
            1. { source, text }       直接传多行文本 (WebUI 粘贴用)
            2. { source, file_path }  从文件读取
            3. { source, records }    已解析的对象数组

        支持分隔符: ---- 或 ====

        Outlook (source=outlook):
            4 段: email----password----clientId----refreshToken
            6 段: email----password----clientId----refreshToken----recoveryEmail----recoveryCode

        Generic API (source=generic_api):
            2 段: email----codeUrl

        IMAP (source=imap):
            2 段: email----imapPassword
            或 多行 imap_server / imap_port / imap_ssl 统一配置
        """
        data = request.get_json(force=True, silent=True) or {}
        source = str(data.get("source") or "outlook").strip().lower()
        if source not in ("outlook", "generic_api", "imap", "omnimail"):
            return jsonify({"ok": False, "error": f"未知 source={source}，可选 outlook / generic_api / imap / omnimail"}), 400

        # ---- OmniMail 特殊: 从 API 拉全量 mailbox ----
        if source == "omnimail":
            base = str(data.get("omnimail_base") or "").strip()
            email = str(data.get("omnimail_email") or "").strip()
            password = str(data.get("omnimail_password") or "").strip()
            device_name = str(data.get("omnimail_device") or "muse-register").strip()
            # 允许留空 → 从 config 默认值读取 (.env)
            if not (base and email and password):
                from config.email import OMNIMAIL_BASE as _def_base, OMNIMAIL_EMAIL as _def_email, OMNIMAIL_PASSWORD as _def_pass
                base = base or _def_base
                email = email or _def_email
                password = password or _def_pass
            if not (base and email and password):
                return jsonify({"ok": False, "error": "omnimail_base / omnimail_email / omnimail_password 未填写，且 .env 也没有默认值"}), 400
            try:
                added, skipped = db.import_omnimail_from_api(base, email, password, device_name)
                return jsonify({"ok": True, "source": "omnimail", "inserted": added, "skipped": skipped, "parsed": added + skipped})
            except Exception as exc:
                return jsonify({"ok": False, "error": f"OmniMail API 同步失败: {exc}"}), 500

        imap_server = str(data.get("imap_server") or "").strip()
        try:
            imap_port = int(data.get("imap_port") or 993)
        except (TypeError, ValueError):
            imap_port = 993
        imap_ssl = bool(data.get("imap_ssl", True))

        records = data.get("records")
        if records is None:
            text = str(data.get("text") or "").strip()
            file_path = str(data.get("file_path") or "").strip()
            if file_path:
                try:
                    from pathlib import Path
                    p = Path(file_path)
                    if not p.is_absolute():
                        p = Path(__file__).resolve().parent.parent / file_path
                    text = p.read_text(encoding="utf-8")
                except Exception as exc:
                    return jsonify({"ok": False, "error": f"读取文件失败: {exc}"}), 400
            if not text:
                return jsonify({"ok": False, "error": "records / text / file_path 至少传一个"}), 400

            records = []
            for raw in text.splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                sep = "----" if "----" in line else ("====" if "====" in line else None)
                if sep is None:
                    continue
                parts = [p.strip() for p in line.split(sep)]
                if source == "generic_api":
                    if len(parts) < 2 or not parts[0] or not parts[1]:
                        continue
                    records.append({"email": parts[0], "code_url": parts[1]})
                elif source == "imap":
                    if len(parts) < 2 or not parts[0] or not parts[1]:
                        continue
                    records.append({
                        "email": parts[0],
                        "imap_password": parts[1],
                        "imap_server": imap_server,
                        "imap_port": imap_port,
                        "imap_ssl": imap_ssl,
                    })
                else:  # outlook
                    if len(parts) < 4:
                        continue
                    records.append({
                        "email": parts[0], "password": parts[1],
                        "client_id": parts[2], "refresh_token": parts[3],
                        "recovery_email": parts[4] if len(parts) > 4 else "",
                        "recovery_code": parts[5] if len(parts) > 5 else "",
                    })

        if not records:
            need = ("email----codeUrl" if source == "generic_api"
                    else "email----imapPassword" if source == "imap"
                    else "email----password----clientId----refreshToken")
            return jsonify({"ok": False, "error": f"未解析到有效邮箱行。格式: {need}，分隔符 ---- 或 ===="}), 400

        if source == "generic_api":
            inserted, skipped = db.import_generic_api_emails(records)
        elif source == "imap":
            inserted, skipped = db.import_imap_emails(records)
        else:
            inserted, skipped = db.import_outlook_accounts(records)

        return jsonify({
            "ok": True,
            "source": source,
            "inserted": inserted,
            "skipped": skipped,
            "parsed": len(records),
        })

    @app.delete("/api/emails/<email>")
    def api_delete_email(email):
        src = request.args.get("source") or None
        db.delete_email_pool_entry(email, source=src)
        return jsonify({"ok": True})

    # ==================== 银行卡池 ====================

    @app.get("/api/cards")
    def api_list_cards():
        status = request.args.get("status") or None
        items = card_pool.list_cards(status=status, include_sensitive=True)
        return jsonify({"ok": True, "items": items, "stats": card_pool.card_stats()})

    @app.post("/api/cards")
    def api_add_card():
        data = request.get_json(force=True, silent=True) or {}
        try:
            cid = card_pool.add_card(
                card_number=data["card_number"], expiry=data["expiry"],
                cvv=data["cvv"], holder_name=data.get("holder_name", ""),
                billing_zip=data.get("billing_zip", ""),
                billing_country=data.get("billing_country", "US"),
                note=data.get("note", ""),
            )
            return jsonify({"ok": True, "id": cid})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/cards/import")
    def api_import_cards():
        data = request.get_json(force=True, silent=True) or {}
        text = str(data.get("text") or "")
        if not text.strip():
            return jsonify({"ok": False, "error": "请先粘贴银行卡信息"}), 400
        try:
            inserted, errors = card_pool.import_pasted_cards(text)
            return jsonify({
                "ok": True,
                "inserted": inserted,
                "failed": len(errors),
                "errors": errors[:10],
            })
        except Exception as exc:
            logger.exception("[CardPool] 批量导入失败")
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.delete("/api/cards/<int:card_id>")
    def api_delete_card(card_id):
        card_pool.remove_card(card_id)
        return jsonify({"ok": True})

    # ==================== 代理池 ====================

    @app.get("/api/proxies")
    def api_list_proxies():
        return jsonify({"ok": True, "proxy_pool": proxy_cfg.PROXY_POOL, "upstream": proxy_cfg.PROXY_POOL_UPSTREAM_PROXY, "extract_api": proxy_cfg.PROXY_EXTRACT_API})

    @app.post("/api/proxies")
    def api_set_proxies():
        data = request.get_json(force=True, silent=True) or {}
        pool = data.get("proxy_pool", [])
        if isinstance(pool, str):
            pool = [l for l in pool.splitlines() if l.strip()]
        proxy_cfg.PROXY_POOL = list(pool)
        if "upstream" in data:
            proxy_cfg.PROXY_POOL_UPSTREAM_PROXY = data["upstream"]
        if "extract_api" in data:
            proxy_cfg.PROXY_EXTRACT_API = str(data.get("extract_api") or "").strip()
        # 持久化到 .env (重启不丢, ; 分隔, env_loader 按 ; 拆分)
        _update_env_file({
            "PROXY_POOL": ";".join(pool),
            "PROXY_POOL_UPSTREAM_PROXY": str(data.get("upstream", "")),
            "PROXY_EXTRACT_API": str(data.get("extract_api") or ""),
        })
        return jsonify({"ok": True, "count": len(pool)})

    # ==================== 注册任务 ====================

    @app.post("/api/register")
    def api_submit_registration():
        data = request.get_json(force=True, silent=True) or {}
        try:
            count = int(data.get("count", 1))
            workers = int(data.get("workers", 3))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "注册数量或注册线程不合法"}), 400
        if count < 1:
            return jsonify({"ok": False, "error": "注册数量必须大于 0"}), 400
        workers = max(1, min(16, workers))
        email_source = data.get("email_source", "outlook")
        jobs = svc.submit_registration(count=count, email_source=email_source, workers=workers)
        return jsonify({"ok": True, "jobs": jobs})

    @app.get("/api/jobs")
    def api_list_jobs():
        limit = int(request.args.get("limit", 100))
        offset = int(request.args.get("offset", 0))
        items = db.list_jobs(limit=limit, offset=offset)
        # 兼容旧任务：此前邮箱只在注册成功时落库，失败任务从日志补回一次。
        for item in items:
            if item.get("email"):
                continue
            email = svc.recover_job_email_from_log(item)
            if email:
                db.update_job(item["id"], email=email)
                item["email"] = email
        return jsonify({"ok": True, "items": items})

    @app.post("/api/jobs/<int:job_id>/stop")
    def api_stop_job(job_id):
        return jsonify(svc.request_stop_job(job_id))

    @app.delete("/api/jobs/<int:job_id>")
    def api_delete_job(job_id):
        result = svc.delete_job(job_id)
        return jsonify(result), (200 if result.get("ok") else 400)

    @app.post("/api/jobs/batch-delete")
    def api_batch_delete_jobs():
        data = request.get_json(force=True, silent=True) or {}
        raw_ids = data.get("ids") or []
        if not isinstance(raw_ids, list):
            return jsonify({"ok": False, "error": "ids 必须是数组"}), 400
        ids = []
        for value in raw_ids:
            try:
                job_id = int(value)
                if job_id > 0 and job_id not in ids:
                    ids.append(job_id)
            except (TypeError, ValueError):
                continue
        if not ids:
            return jsonify({"ok": False, "error": "请至少选择一个任务"}), 400

        deleted, errors = [], []
        for job_id in ids:
            result = svc.delete_job(job_id)
            if result.get("ok"):
                deleted.append(job_id)
            else:
                errors.append({"id": job_id, "error": result.get("error") or "删除失败"})
        return jsonify({"ok": True, "deleted": deleted, "errors": errors})

    @app.get("/api/jobs/<int:job_id>/log")
    def api_job_log(job_id):
        max_bytes = int(request.args.get("max_bytes", 50_000))
        log_text = svc.read_job_log(job_id, max_bytes=max_bytes)
        return log_text, 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.post("/api/jobs/cancel-pending")
    def api_cancel_pending():
        n = svc.cancel_pending()
        return jsonify({"ok": True, "cancelled": n})

    return app


def run(host: str = "127.0.0.1", port: int = 8799, debug: bool = False):
    """启动 WebUI。优先 waitress (生产级, 稳定)；未安装则回退 Flask dev server。"""
    svc.recover_interrupted_jobs()
    app = create_app()
    logger.info(f"[WebUI] 启动 http://{host}:{port}")
    try:
        from waitress import serve
        logger.info("[WebUI] 使用 waitress 服务器 (threads=16)")
        serve(app, host=host, port=port, threads=16)
    except ImportError:
        logger.warning("[WebUI] waitress 未安装, 使用 Flask dev server (建议 pip install waitress)")
        app.run(host=host, port=port, debug=debug, threaded=True)
