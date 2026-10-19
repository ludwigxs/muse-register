# -*- coding: utf-8 -*-
"""
Muse 注册服务 - 任务队列

架构:
    WebUI submit_registration(count=N)
        → ThreadPoolExecutor 提交 N 个 _run_one_job
            → create job 记录 + 日志文件
            → 从邮箱池 acquire_email
            → 创建 MuseSession (代理池 pick_proxy)
            → MuseAuth.register (send_otp → wait → confirm)
            → 邀请码兑换
            → create_account
            → release_email / card

所有日志通过 _JobLogContext 重定向到任务专属 .log 文件。
"""
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from core import db

logger = logging.getLogger(__name__)

_JOB_EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)


# ============================================================
# 线程池管理 (复用 turb 模式)
# ============================================================

_DEFAULT_MAX_WORKERS = 4
_MIN_MAX_WORKERS = 1
_MAX_MAX_WORKERS = 16
_executor: ThreadPoolExecutor | None = None
_executor_workers = _DEFAULT_MAX_WORKERS
_executor_generation = 0
_retired_executors: list[ThreadPoolExecutor] = []
_executor_lock = threading.RLock()

_STOP_EVENTS: dict[int, threading.Event] = {}
_ACTIVE_JOBS: set[int] = set()
_STOP_LOCK = threading.Lock()
_THREAD_CTX = threading.local()


class StopRequested(RuntimeError):
    pass


def _activate_job(job_id: int) -> None:
    _THREAD_CTX.job_id = int(job_id)
    with _STOP_LOCK:
        _STOP_EVENTS.setdefault(int(job_id), threading.Event())
        _ACTIVE_JOBS.add(int(job_id))


def _deactivate_job(job_id: int) -> None:
    with _STOP_LOCK:
        _STOP_EVENTS.pop(int(job_id), None)
        _ACTIVE_JOBS.discard(int(job_id))


def _record_job_email(job_id: int, email: str) -> None:
    """邮箱被任务领取后立即落库，确保失败任务也能在 WebUI 追溯。"""
    email = str(email or "").strip()
    if email:
        db.update_job(job_id, email=email)


def _fetch_browser_account_invite_code(result: dict) -> str:
    """用浏览器注册流程留下的登录态读取账号自己的邀请码。"""
    cookies = result.get("cookies") or {}
    access_token = str(result.get("access_token") or "").strip()
    if not access_token and not cookies:
        return ""
    try:
        from core.muse_auth import MuseAuth
        from core.session import MuseSession

        with MuseSession(proxy=str(result.get("proxy") or "") or None) as session:
            if isinstance(cookies, dict):
                for name, value in cookies.items():
                    session.session.cookies.set(str(name), str(value), domain=".muse.ai", path="/")
            elif isinstance(cookies, list):
                for cookie in cookies:
                    if not isinstance(cookie, dict) or not cookie.get("name"):
                        continue
                    session.session.cookies.set(
                        str(cookie["name"]),
                        str(cookie.get("value") or ""),
                        domain=str(cookie.get("domain") or ".muse.ai"),
                        path=str(cookie.get("path") or "/"),
                    )
            return MuseAuth(session).fetch_own_invite_code(access_token=access_token)
    except Exception as exc:
        logger.warning("[Invite] 浏览器账号邀请码查询失败: %s", exc)
        return ""


def recover_job_email_from_log(job: dict) -> str:
    """为历史任务从日志补回已领取邮箱，不暴露日志中其它内容。"""
    if job.get("email"):
        return str(job["email"])
    log_file = str(job.get("log_file") or "")
    if not log_file or not Path(log_file).exists():
        return ""
    try:
        text = Path(log_file).read_text(encoding="utf-8", errors="replace")[-50_000:]
    except Exception:
        return ""
    markers = ("领取邮箱", "使用邮箱", "email=")
    for line in text.splitlines():
        if any(marker in line for marker in markers):
            match = _JOB_EMAIL_RE.search(line)
            if match:
                return match.group(0)
    return ""


def is_stop_requested(job_id: int | None = None) -> bool:
    if job_id is None:
        job_id = getattr(_THREAD_CTX, "job_id", None)
    if not job_id:
        return False
    with _STOP_LOCK:
        ev = _STOP_EVENTS.get(int(job_id))
        if ev and ev.is_set():
            return True
    job = db.get_job(int(job_id))
    return bool(job and job.get("status") in ("stopping", "stopped", "cancelled"))


def check_stop() -> None:
    if is_stop_requested():
        raise StopRequested("任务已被用户停止")


def get_executor(max_workers: int | None = None) -> ThreadPoolExecutor:
    """返回当前批次的注册线程池。

    修改线程数时新建线程池承接后续任务，旧池不再接收新任务，但会完成已经
    提交的任务。这与 turb 的批量注册调度一致，不会丢失排队中的任务。
    """
    global _executor, _executor_workers, _executor_generation
    try:
        requested_workers = int(max_workers) if max_workers is not None else _DEFAULT_MAX_WORKERS
    except (TypeError, ValueError):
        requested_workers = _DEFAULT_MAX_WORKERS
    requested_workers = max(_MIN_MAX_WORKERS, min(_MAX_MAX_WORKERS, requested_workers))

    with _executor_lock:
        if _executor is None or requested_workers != _executor_workers:
            old_executor = _executor
            if old_executor is not None:
                # 不取消旧池已提交的任务；仅让新批次使用新线程数。
                old_executor.shutdown(wait=False, cancel_futures=False)
                _retired_executors.append(old_executor)
                logger.info(
                    "[Service] 注册线程从 %s 切换为 %s；旧池继续处理已提交任务",
                    _executor_workers,
                    requested_workers,
                )
            _executor_generation += 1
            _executor = ThreadPoolExecutor(
                max_workers=requested_workers,
                thread_name_prefix=f"muse-reg-{_executor_generation}",
            )
            _executor_workers = requested_workers
        return _executor


def shutdown_executor() -> None:
    global _executor
    with _executor_lock:
        executors = []
        if _executor is not None:
            executors.append(_executor)
            _executor = None
        executors.extend(_retired_executors)
        _retired_executors.clear()
    for executor in executors:
        executor.shutdown(wait=False, cancel_futures=False)


def recover_interrupted_jobs() -> int:
    """在服务启动时结束上一次进程遗留的活动任务。

    注册任务在内存线程中执行，服务重启后无法继续恢复。若保留 running/stopping
    状态，任务会永久显示进行中或停止中；因此统一收敛为 stopped 并保留原日志。
    """
    with _STOP_LOCK:
        active_job_ids = set(_ACTIVE_JOBS)

    recovered = 0
    for job in db.list_jobs(limit=10_000):
        if job.get("id") in active_job_ids:
            continue
        if job.get("status") not in ("pending", "running", "stopping"):
            continue
        db.update_job(
            job["id"],
            status="stopped",
            error="服务已重启，任务未完成",
            completed_at=datetime.now().isoformat(timespec="seconds"),
        )
        recovered += 1
    if recovered:
        logger.warning("[Service] 已结束 %s 个服务重启遗留任务", recovered)
    return recovered


# ============================================================
# 日志上下文
# ============================================================

class _JobLogContext:
    """让当前线程的 root logger 多一个 FileHandler，结束后移除。"""

    def __init__(self, log_path: str):
        self.log_path = log_path
        self.handler: logging.FileHandler | None = None

    def __enter__(self):
        Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
        self.handler = logging.FileHandler(self.log_path, encoding="utf-8")
        self.handler.setLevel(logging.INFO)
        self.handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        thread_name = threading.current_thread().name
        self.handler.addFilter(lambda r: r.threadName == thread_name)
        logging.getLogger().addHandler(self.handler)
        return self

    def __exit__(self, *exc):
        if self.handler is not None:
            self.handler.close()
            logging.getLogger().removeHandler(self.handler)


# ============================================================
# 单任务执行
# ============================================================

def _run_one_job(job_id: int) -> None:
    log = logging.getLogger(__name__)
    _activate_job(job_id)

    current = db.get_job(job_id)
    if not current:
        _deactivate_job(job_id)
        return
    if current.get("status") == "cancelled":
        log.info(f"[Job {job_id}] 已取消，跳过")
        _deactivate_job(job_id)
        return

    db.update_job(job_id, status="running", started_at=datetime.now().isoformat(timespec="seconds"))

    with _JobLogContext(current.get("log_file") or ""):
        email = None
        email_consumed = False
        try:
            # ========== 1) 读取 driver_mode 分发 ==========
            try:
                from config.roxybrowser import REGISTRATION_DRIVER
                driver_mode = str(REGISTRATION_DRIVER or "protocol").strip().lower()
            except Exception:
                driver_mode = "protocol"

            log.info(f"[Job {job_id}] driver_mode={driver_mode}")

            # ========== 2) 浏览器路径 (local / roxy / cloak) ==========
            if driver_mode in ("local", "local_browser", "chrome", "default"):
                result = _run_job_local(job_id)
            elif driver_mode in ("roxy", "roxybrowser", "fingerprint", "browser"):
                result = _run_job_roxy(job_id)
            elif driver_mode in ("cloak", "cloakbrowser"):
                result = _run_job_cloak(job_id)
            elif driver_mode in ("browser_use", "browseruse", "bu"):
                result = _run_job_browser_use(job_id)
            else:
                # protocol / api / http — 纯 curl_cffi 协议
                result = _run_job_protocol(job_id)

            # 3) 落库
            email = result.get("email")
            email_consumed = bool(result.get("email_consumed"))
            if result.get("ok"):
                own_invite_code = str(result.get("own_invite_code") or "").strip()
                if not own_invite_code and driver_mode in ("local", "local_browser", "chrome", "default", "roxy", "roxybrowser", "fingerprint", "browser", "cloak", "cloakbrowser"):
                    own_invite_code = _fetch_browser_account_invite_code(result)

                payload = dict(result.get("payload") or {})
                redeemed_invite_code = str(result.get("invite_code") or "").strip()
                if redeemed_invite_code:
                    payload["redeemed_invite_code"] = redeemed_invite_code
                if own_invite_code:
                    payload["own_invite_code_fetched"] = True

                account_id = db.create_account(
                    email=email or "",
                    access_token=result.get("access_token", ""),
                    invite_code=own_invite_code,
                    email_source=current.get("email_source") or "",
                    proxy_used=result.get("proxy", ""),
                    payload=payload,
                )
                db.update_job(
                    job_id,
                    status="success",
                    email=email,
                    account_id=account_id,
                    completed_at=datetime.now().isoformat(timespec="seconds"),
                )
                # 注册成功 → 邮箱标记为已用 (不再参与注册)
                if email:
                    try:
                        from core.email_provider import release_email
                        release_email(email, status="used", note="注册成功")
                    except Exception:
                        pass
                log.info(f"[Job {job_id}] 成功: {email}, driver={driver_mode}, account_id={account_id}")
            else:
                raise RuntimeError(result.get("error") or "注册返回未成功")

        except StopRequested:
            log.warning(f"[Job {job_id}] 用户手动停止")
            db.update_job(
                job_id,
                status="stopped",
                error="用户停止",
                email=email or current.get("email") or "",
                completed_at=datetime.now().isoformat(timespec="seconds"),
            )
            # 停止时释放邮箱: OTP 已提交 (账号已创建) 则标 used, 否则回可用池
            if email:
                try:
                    from core.email_provider import release_email
                    release_email(email, status="used" if email_consumed else "available",
                                  note="任务被用户停止")
                except Exception:
                    pass

        except Exception as exc:
            log.exception(f"[Job {job_id}] 失败: {type(exc).__name__}: {exc}")
            db.update_job(
                job_id,
                status="failed",
                error=f"{type(exc).__name__}: {str(exc)[:500]}",
                email=email or current.get("email") or "",
                completed_at=datetime.now().isoformat(timespec="seconds"),
            )
            if email:
                try:
                    from core.email_provider import release_email
                    # OTP 已提交 = Muse 账号已创建, 邮箱必须标 used (回池会导致复用后收不到码)
                    release_email(email, status="used" if email_consumed else "available",
                                  note=f"注册失败: {str(exc)[:100]}")
                except Exception:
                    pass

        finally:
            _deactivate_job(job_id)


# ============================================================
# 三条路径的具体实现
# ============================================================

def _run_job_protocol(job_id: int) -> dict:
    """纯 curl_cffi 协议注册 (MuseAuth API)。"""
    from core.email_provider import acquire_email, acquire_email_from_source, wait_for_otp
    from core.session import MuseSession
    from core.muse_auth import MuseAuth, OtpInvalidError
    from config import muse as muse_cfg

    current = db.get_job(job_id) or {}
    email_source = current.get("email_source") or "imap"
    email = acquire_email_from_source(email_source) if email_source else acquire_email()
    _record_job_email(job_id, email)
    check_stop()

    session_sticky = f"job-{job_id}"
    with MuseSession(session_id=session_sticky) as session:
        auth = MuseAuth(session)
        auth.restart()
        auth.report_caa_events()
        auth.report_falco()
        auth.send_otp(email)

        check_stop()

        import time as _t
        otp_deadline = _t.time() + 180
        otp_code = None
        while _t.time() < otp_deadline:
            check_stop()
            try:
                otp_code = wait_for_otp(email, after_ts=_t.time() - 30, stop_check=check_stop)
                break
            except TimeoutError:
                continue
        if not otp_code:
            raise TimeoutError("OTP 等待超时")

        reg_result = auth.confirm_otp(otp_code)
        check_stop()

        access_token = reg_result.get("access_token") or reg_result.get("accessToken") or ""
        own_invite_code = auth.fetch_own_invite_code(access_token=access_token)

        invite_code = muse_cfg.INVITE_CODE or ""
        invite_result = None
        if invite_code:
            try:
                invite_result = auth.redeem_invite_code(invite_code)
            except Exception as e:
                logging.getLogger(__name__).warning(f"邀请码兑换失败: {e}")

        return {
            "ok": True,
            "email": email,
            "access_token": access_token,
            "invite_code": invite_code,
            "own_invite_code": own_invite_code,
            "proxy": session.proxy or "",
            "driver": "protocol",
            "payload": {"invite_result": invite_result},
        }


def _run_job_roxy(job_id: int) -> dict:
    """RoxyBrowser 指纹浏览器注册: 先领邮箱 → 页面流程 (外层统一标记邮箱状态)。"""
    from core import muse_browser_registration as mbr
    from core.email_provider import acquire_email, acquire_email_from_source

    current = db.get_job(job_id) or {}
    email_source = current.get("email_source") or ""

    email = acquire_email_from_source(email_source) if email_source else acquire_email()
    _record_job_email(job_id, email)
    logging.getLogger(__name__).info(f"[Job {job_id}] [Roxy] 领取邮箱: {email}")

    result = mbr.run_roxy_registration(email=email)
    result.setdefault("email", email)
    result.setdefault("driver", "roxy")
    return result


def _run_job_local(job_id: int) -> dict:
    """使用本机普通浏览器注册，不注入项目代理池或指纹浏览器。"""
    from core import muse_browser_registration as mbr
    from core.email_provider import acquire_email, acquire_email_from_source

    current = db.get_job(job_id) or {}
    email_source = current.get("email_source") or ""
    email = acquire_email_from_source(email_source) if email_source else acquire_email()
    _record_job_email(job_id, email)
    logging.getLogger(__name__).info(f"[Job {job_id}] [Local] 领取邮箱: {email}")

    result = mbr.run_local_registration(email=email)
    result.setdefault("email", email)
    result.setdefault("driver", "local")
    result["proxy"] = ""
    return result


def _run_job_cloak(job_id: int) -> dict:
    """CloakBrowser 指纹浏览器注册。"""
    from core import muse_browser_registration as mbr

    current = db.get_job(job_id) or {}
    email_source = current.get("email_source") or "outlook"

    result = mbr.run_cloak_registration()
    if not result.get("email"):
        from core.email_provider import acquire_email_from_source
        result["email"] = acquire_email_from_source(email_source)

    result.setdefault("driver", "cloak")
    return result


def _run_job_browser_use(job_id: int) -> dict:
    """Browser Use Cloud 指纹浏览器 (占位)。"""
    raise RuntimeError("browser_use 驱动尚未集成 Muse 注册流程，请使用 roxy / cloak / protocol")


# ============================================================
# 公共接口
# ============================================================

def submit_registration(
    count: int = 1,
    email_source: str = "imap",
    workers: int | None = None,
) -> list[dict]:
    """创建 N 个注册任务并提交到线程池。"""
    if count < 1:
        return []

    # 每次点击“开始注册”都是独立一轮；窗口布局按本轮数量从第一个槽位开始，
    # 不把上一轮已完成任务的窗口数量累加进去。
    try:
        from core.roxybrowser_client import reset_window_layout
        reset_window_layout()
    except Exception as exc:
        logging.getLogger(__name__).debug("[Service] 重置 Roxy 窗口槽位失败: %s", exc)

    # 创建、切换线程池、提交任务必须整体串行化。否则并发请求中一个请求切换
    # 线程池时，另一个请求可能向已经 shutdown 的旧池提交任务。
    with _executor_lock:
        pool = get_executor(max_workers=workers)
        effective_workers = _executor_workers
        jobs = []
        for _ in range(count):
            # create_job 默认是 pending；只有工作线程实际开始时 _run_one_job 才会
            # 标记为 running，从而准确反映线程池的等待状态。
            job = db.create_job(email_source=email_source)
            try:
                pool.submit(_run_one_job, job["id"])
            except Exception as exc:
                db.update_job(
                    job["id"],
                    status="failed",
                    error=f"队列提交失败: {exc}",
                    completed_at=datetime.now().isoformat(timespec="seconds"),
                )
            jobs.append(db.get_job(job["id"]) or job)

    logger.info(f"[Service] 已提交 {count} 个注册任务，注册线程={effective_workers}")
    return jobs


def request_stop_job(job_id: int) -> dict:
    """停止单个任务。pending → cancelled, running → stopped。"""
    job = db.get_job(job_id)
    if not job:
        return {"ok": False, "error": "任务不存在"}

    status = job.get("status")
    if status == "pending":
        db.update_job(job_id, status="cancelled", completed_at=datetime.now().isoformat(timespec="seconds"))
        return {"ok": True, "state": "cancelled"}
    if status == "running":
        with _STOP_LOCK:
            ev = _STOP_EVENTS.setdefault(job_id, threading.Event())
            ev.set()
        db.update_job(job_id, status="stopping")
        return {"ok": True, "state": "stopping"}
    return {"ok": True, "state": status}


def cancel_pending() -> int:
    """把所有 pending 任务改成 cancelled。"""
    jobs = db.list_jobs(limit=1000)
    n = 0
    for j in jobs:
        if j.get("status") == "pending":
            db.update_job(j["id"], status="cancelled", completed_at=datetime.now().isoformat(timespec="seconds"))
            n += 1
    return n


def delete_job(job_id: int) -> dict:
    """删除已结束任务及其日志；活动任务必须先停止，避免并发写日志。"""
    job = db.get_job(job_id)
    if not job:
        return {"ok": False, "error": "任务不存在"}
    if job.get("status") in ("pending", "running", "stopping"):
        return {"ok": False, "error": "任务仍在运行，请先停止并等待结束"}

    deleted = db.delete_job(job_id)
    if not deleted:
        return {"ok": False, "error": "任务不存在"}

    log_file = str(deleted.get("log_file") or "").strip()
    if log_file:
        try:
            path = Path(log_file).resolve()
            logs_dir = (Path(__file__).resolve().parent.parent / "data" / "logs").resolve()
            if path.is_relative_to(logs_dir) and path.exists():
                path.unlink()
        except Exception as exc:
            logger.warning("[Job %s] 删除日志失败: %s", job_id, exc)
    return {"ok": True}


def read_job_log(job_id: int, max_bytes: int = 50_000) -> str:
    """读取任务日志尾部。"""
    job = db.get_job(job_id)
    if not job:
        return ""
    log_file = job.get("log_file")
    if not log_file or not Path(log_file).exists():
        return ""
    try:
        with open(log_file, "rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
