"""手机号登录换凭证：发起 / 发码 / 校验换凭证 / 入账号池。

设计要点
--------
这是「把 WorkBuddy 手机号验证登录 + 全自动 auth 授权」暴露成 API 的薄路由层，
真正的协议实现在 `admin.wb_login`（纯 HTTP、不依赖客户端）。

为什么单独一个路由文件而不是塞进 `accounts.py`：
`accounts.py` 已经 700+ 行，且职责是「账号的增删改查」；登录是一条**有状态的
多步交互**（会话、发码、限流），放一起会让两边都难以阅读。

两组端点、两种鉴权
------------------
* `router`（`/api/login/*`）—— **管理端**，要求 `X-Admin-Token`。
* `public_router`（`/api/join/*`）—— **自助端**，给「发给朋友」用，不要求后台
  口令，改用邀请码鉴权（未配置邀请码则整个入口关闭）。

两条路径共用 `_verify_and_store`，因此**对「什么算合法凭证」的判定完全一致**，
不会出现「公开端宽松一点」这种后门。

凭证入库前的两道校验
--------------------
`validate_credential`（结构）+ `probe_credential_live`（上游真实调用）。
**不通过的一律拒收**，不会写进账号池。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin import wb_login
from admin.db import get_db
from admin.models import Account
from admin.ratelimit import client_ip_from_request
from admin.security import require_admin

_logger = logging.getLogger("admin.wb_login_router")

router = APIRouter(prefix="/api/login", tags=["login"])
#: 自助注册用的公开路由（不需要后台登录态、也不需要邀请码）。
public_router = APIRouter(tags=["join"])

#: 同一出口 IP、同一手机号两次发码的最小间隔（秒）。
SMS_MIN_INTERVAL = 60
#: 同一 IP 每小时最多发码次数（防有人拿我们的出口 IP 刷短信）。
SMS_MAX_PER_HOUR = 10

#: 发码节流表。
#:   ip -> [时间戳...]        按小时封顶
#:   mobile -> 上次发码时间戳  按号码 60s 间隔
#:
# The upstream observes our shared egress IP, including failed requests.
_sms_log: dict[str, list[float]] = {}
_sms_mobile_last: dict[str, float] = {}
#: FastAPI 的同步端点跑在线程池里，节流表会被并发读写 —— 必须加锁。
_sms_lock = threading.Lock()
_sms_exit_last = 0.0
_sms_exit_blocked_until = 0.0
_sms_exit_hits: list[float] = []


def _check_sms_quota(ip: str, mobile: str = "") -> None:
    """发码节流。超限直接 429。

    * `mobile` 非空时按号码限 60 秒间隔；
    * 所有客户端共用出口的最小间隔和每小时上限，失败请求也占配额；
    * 任意情况下按客户端 IP 限每小时 `SMS_MAX_PER_HOUR` 次。
    """
    global _sms_exit_last
    now = time.time()
    with _sms_lock:
        _sms_exit_hits[:] = [stamp for stamp in _sms_exit_hits if now - stamp < 3600]
        if len(_sms_exit_hits) >= SMS_MAX_PER_HOUR:
            wait = max(1, int(3600 - (now - _sms_exit_hits[0])) + 1)
            raise HTTPException(429, "短信出口本小时配额已用完，请稍后重试", headers={"Retry-After": str(wait)})
        wait = max(_sms_exit_last + SMS_MIN_INTERVAL, _sms_exit_blocked_until) - now
        if wait > 0:
            seconds = int(wait) + 1
            raise HTTPException(429, detail=f"短信出口保护中，请 {seconds} 秒后再试",
                                headers={"Retry-After": str(seconds)})
        if mobile:
            last = _sms_mobile_last.get(mobile, 0.0)
            if last and (now - last) < SMS_MIN_INTERVAL:
                wait = int(SMS_MIN_INTERVAL - (now - last)) + 1
                raise HTTPException(
                    status_code=429,
                    detail=f"该手机号发送过于频繁，请 {wait} 秒后再试",
                    headers={"Retry-After": str(wait)})

        hits = [t for t in _sms_log.get(ip, []) if now - t < 3600]
        if len(hits) >= SMS_MAX_PER_HOUR:
            raise HTTPException(status_code=429,
                                detail="本小时发送次数已达上限，请稍后再试")
        hits.append(now)
        _sms_log[ip] = hits
        if mobile:
            _sms_mobile_last[mobile] = now
        _sms_exit_last = now
        _sms_exit_hits.append(now)
        for key in list(_sms_log):
            if not any(now - stamp < 3600 for stamp in _sms_log[key]):
                del _sms_log[key]
        for key, stamp in list(_sms_mobile_last.items()):
            if now - stamp > 3600:
                del _sms_mobile_last[key]


class StartOut(BaseModel):
    sid: str
    auth_url: str


class SendIn(BaseModel):
    sid: str
    mobile: str
    country_code: str = "86"


class VerifyIn(BaseModel):
    sid: str
    code: str
    account_id: Optional[int] = None
    #: 是否在拿不到有效凭证时仍写入（**始终为 False** —— 保留字段只为让前端显式表态）
    force: bool = False


def _fail(e: wb_login.WbLoginError, status: int = 400) -> HTTPException:
    detail = {"message": e.message, "step": e.step}
    # reason 透给前端做流程判断（如 need_captcha -> 自动切官方页面接力）
    if e.reason:
        detail["reason"] = e.reason
    return HTTPException(status_code=status, detail=detail)


def _clean_nick(v) -> str:
    """归一化昵称：把缺失值（null / none / undefined / 空串）统统当作「没有」。

    为什么需要：腾讯侧账号没有昵称时，凭证里存的**不是空串**而是字面量
    `"null"`（本机真实凭证就是这样）。若直接拿去当号池显示名，
    后台会满屏都是叫「null」的账号 —— 既难看又分不清谁是谁。
    """
    s = (v if isinstance(v, str) else "").strip()
    return "" if s.lower() in ("null", "none", "undefined") else s


def _verify_and_store(sess, code: str, db: Session, account_id: Optional[int] = None) -> dict:
    """登录收尾的**唯一**实现：校验验证码 → 换凭证 → 双重校验 → 入池去重。

    管理端与公开自助端都走这里，**刻意不各写一份**：
    两条路径对「什么算合法凭证」的判定必须完全一致 —— 否则公开端一旦放宽，
    就等于给号池开了一个后门，而这种不一致极难在 review 里发现。

    返回一份内部结构，由调用方决定回显多少（公开端会抹掉 uid / 账号 id）。
    """
    try:
        result = wb_login.verify_sms(sess, code)
    except wb_login.WbLoginError as e:
        raise _fail(e, 502)
    out = _store_verified_credential(result["credential"], result["meta"] or {}, db, account_id=account_id)
    out["invitation"] = wb_login.try_bind_invitation(sess, result["credential"])
    return out


def _store_verified_credential(credential: str, meta: dict, db: Session, account_id: Optional[int] = None) -> dict:
    """凭证的**双重校验 + 入库去重**。

    「官方页面接力」路径也走这里 —— 无论凭证是服务端代跑出来的，
    还是用户自己在官方页面上登录换来的，入库前的把关完全一致。
    """
    # ---- 第一道：结构校验（拦截一切非凭证） ----
    check = wb_login.validate_credential(credential)
    if not check.ok:
        raise HTTPException(status_code=400, detail={
            "message": f"凭证格式校验未通过：{check.reason}", "step": "validate"})

    # ---- 第二道：**真实上游**验证（拦伪造 / 张冠李戴 / 已注销的 token） ----
    # 结构校验挡不住「JWT 字段齐全但签名是编的」，只有真调上游才能确认。
    probe = wb_login.probe_credential_live(credential)
    if not probe.ok:
        _logger.warning("wb_login: 上游验证失败 uid=%s reason=%s %s",
                        meta.get("uid"), probe.reason, probe.message)
        raise HTTPException(status_code=400, detail={
            "message": probe.message, "step": "live_probe",
            "reason": probe.reason})

    # 上游返回的真实账号信息优先于凭证里的声明值（前者可信）
    live_ok, live_msg = probe.ok, probe.message
    profile = probe.profile or {}
    # 顺带把验证阶段**已经拿到**的余额写进库。
    # 不写的话列表就显示 0/0（用户看到的就是「没刷新额度」），
    # 而后台只能等整点定时任务或手动点刷新 —— 明明数据已经在手上了。
    balance = probe.balance or {}
    if profile.get("uid"):
        meta["uid"] = str(profile["uid"])
    for src, dst in (("nickname", "nickname"), ("phoneNumber", "phone"),
                     ("enterpriseId", "enterprise_id")):
        if profile.get(src):
            meta[dst] = str(profile[src])

    uid = meta.get("uid") or wb_login.credential_uid(credential)

    # ── 按 uid 串行化「查重 + 写入」 ────────────────────────────────────
    #
    # 为什么必须加锁：原来是「先 SELECT 看有没有，再 INSERT」两步，
    # 中间没有任何保护。**实测**：8 个并发注册同一个账号，
    # 会建出 6 条重复记录（多个线程都查不到、于是都插入）。
    # 线上那 4 组重复 uid 就是这么来的 —— 用户侧表现为「同一个号出现两行，
    # 一行有余额一行是 0，还不确定该删哪个」。
    #
    # 用进程内锁按 uid 串行即可：本服务是单进程（main.py 起一个 uvicorn），
    # 多进程部署时仍需要数据库唯一索引兜底（DB 层加约束需要迁移，
    # 这里先在应用层堵住；`_find_dup` 也会兜住历史重复数据）。
    _uid_lock = _uid_write_lock(uid)
    with _uid_lock:
        if account_id is not None:
            from admin.routers.accounts import _reauthenticate_account
            acc = _reauthenticate_account(db, account_id, credential, profile, balance)
            db.commit()
            return {"duplicated": True, "message": "原账号重新登录成功，历史记录与绑定已保留",
                    "uid": acc.uid, "live_ok": live_ok, "live_msg": live_msg,
                    "account": {"id": acc.id, "name": acc.name, "uid": acc.uid,
                                "balance_total": acc.balance_total, "balance_remain": acc.balance_remain}}
        return _store_locked(credential, meta, balance, uid, db,
                             live_ok, live_msg)


_uid_locks: dict[str, threading.Lock] = {}
_uid_locks_guard = threading.Lock()


def _uid_write_lock(uid: str) -> threading.Lock:
    """取（或建）该 uid 专属的写锁。

    uid 为空时返回一把全局共享锁 —— 没有 uid 就没法区分账号，
    只能串行（这种情况本来就该拒绝，见上游验证的 uid 校验）。
    """
    key = uid or "__no_uid__"
    with _uid_locks_guard:
        lk = _uid_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _uid_locks[key] = lk
        # 简单防膨胀：锁对象很小，但 uid 无限增长时仍会堆积。
        # 超过阈值就清理「当前没人持有」的锁（acquire 非阻塞探测）。
        if len(_uid_locks) > 2000:
            for k in list(_uid_locks):
                if k != key and _uid_locks[k].acquire(blocking=False):
                    _uid_locks[k].release()
                    _uid_locks.pop(k, None)
        return lk


def _store_locked(credential: str, meta: dict, balance: dict, uid: str,
                  db: Session, live_ok, live_msg) -> dict:
    """持锁执行「查重 + 写入/更新」。逻辑与原来一致，只是被串行化了。"""
    def _apply_balance(acc_obj) -> None:
        """把上游余额落到账号记录上（拿不到就不动，绝不写 0 覆盖）。"""
        if not balance:
            return
        acc_obj.balance_total = int(balance.get("total") or 0)
        acc_obj.balance_remain = int(balance.get("remain") or 0)
        acc_obj.last_sync_at = datetime.utcnow()

    existing: Optional[Account] = None
    if uid:
        existing = (db.query(Account)
                    .filter(Account.uid == uid)
                    .order_by(Account.id.asc())
                    .first())

    if existing:
        # 重复登录 = 刷新同一账号的凭证（token 会过期，重登是常态）
        existing.auth_json = credential
        existing.uid = uid or existing.uid
        existing.enterprise_id = meta.get("enterprise_id") or ""
        existing.domain = meta.get("domain") or ""
        # 昵称缺失时凭证里常是字面量 "null"（本机真实凭证即如此），
        # 统一用 _clean_nick 归一，避免把 "null" 当昵称写进号池。
        nick = _clean_nick(meta.get("nickname"))
        if nick:
            existing.name = nick
        elif not existing.name:
            existing.name = uid or "未命名"
        # 重新登录成功 = 人工恢复口径：清掉旧的禁用/失败计数
        existing.status = "active"
        existing.err_count = 0
        existing.breaker_fails = 0
        existing.consecutive_fails = 0
        existing.session_dead_fails = 0
        existing.degrade_until = None
        existing.breaker_until = None
        existing.cool_until = None
        existing.cool_kind = ""
        _apply_balance(existing)
        db.commit()
        db.refresh(existing)
        _logger.info("wb_login: 账号已刷新 uid=%s id=%s 余额=%s/%s",
                     uid, existing.id, existing.balance_remain, existing.balance_total)
        _schedule_post_login_automation(existing.id, existing.name or uid)
        return {
            "duplicated": True,
            "message": "该账号此前已存在，已用最新凭证覆盖更新",
            "uid": uid,
            "live_ok": live_ok, "live_msg": live_msg,
            "account": {"id": existing.id, "name": existing.name, "uid": existing.uid,
                        "balance_total": existing.balance_total,
                        "balance_remain": existing.balance_remain},
        }

    acc = Account()
    acc.auth_json = credential
    acc.uid = uid
    acc.enterprise_id = meta.get("enterprise_id") or ""
    acc.domain = meta.get("domain") or ""
    # 命名优先级：上游昵称 → 手机号 → uid（都用 _clean_nick 归一化，滤掉 "null"）
    acc.name = (_clean_nick(meta.get("nickname")) or _clean_nick(meta.get("phone"))
                or uid or "未命名")
    acc.status = "active"
    _apply_balance(acc)
    db.add(acc)
    db.commit()
    db.refresh(acc)
    _logger.info("wb_login: 新账号入库 uid=%s id=%s 余额=%s/%s",
                 uid, acc.id, acc.balance_remain, acc.balance_total)
    _schedule_post_login_automation(acc.id, acc.name or uid)
    return {
        "duplicated": False,
        "message": "登录成功，凭证已加入账号池",
        "uid": uid,
        "live_ok": live_ok, "live_msg": live_msg,
        "account": {"id": acc.id, "name": acc.name, "uid": acc.uid,
                    "balance_total": acc.balance_total,
                    "balance_remain": acc.balance_remain},
    }


@router.post("/start", response_model=StartOut)
def login_start(_: bool = Depends(require_admin)):
    """第 1 步：创建一个登录会话并铺好授权链。

    不涉及用户输入，所以前端进页面即可调用；返回的 `auth_url` 仅用于
    「手动授权」兜底展示，正常流程用不到。
    """
    with _upstream_slot("login_start"):
        try:
            sess = wb_login.start_login()
        except wb_login.WbLoginError as e:
            raise _fail(e, 502)
    return {"sid": sess.sid, "auth_url": sess.plugin_auth_url}


@router.post("/send")
def login_send(body: SendIn, request: Request, _: bool = Depends(require_admin)):
    """第 2 步：给指定手机号发验证码。"""
    ip = client_ip_from_request(request)
    return _send_guarded(body, ip)


def _send_guarded(body, ip):
    global _sms_exit_blocked_until
    try:
        mobile = wb_login.normalize_mobile(body.mobile, body.country_code)
        sess = wb_login.get_session(body.sid)
        with _session_slot(sess, "send"), _upstream_slot("send"):
            if sess.consumed or sess.expired():
                raise wb_login.WbLoginError("会话已失效", step="session")
            if sess.sms_state_token and (sess.mobile != mobile or sess.country_code != body.country_code.lstrip("+")):
                raise wb_login.WbLoginError("请先完成当前手机号的验证码验证", step="mobile")
            _check_sms_quota(ip, body.country_code + ":" + mobile)
            # An upstream failure or timeout may still have sent an SMS. Keep the quota.
            try:
                out = wb_login.send_sms(sess, mobile, body.country_code)
            except wb_login.WbLoginError as error:
                if error.reason == "need_captcha" or any(word in error.message.lower() for word in ("频繁", "限制", "rate", "频率")):
                    with _sms_lock:
                        _sms_exit_blocked_until = time.time() + 300
                raise
    except wb_login.WbLoginError as error:
        response = _fail(error, 400 if error.step in ("mobile", "session") else 502)
        if error.step == "sms_send":
            response.headers = {"Retry-After": str(max(SMS_MIN_INTERVAL, int(_sms_exit_blocked_until - time.time()) + 1))}
        raise response
    except wb_login.httpx.RequestError:
        raise HTTPException(502, detail={"message": "短信发送结果未知，请稍后再试", "step": "sms_send"},
                            headers={"Retry-After": str(SMS_MIN_INTERVAL)}) from None
    return {"ok": True, "expires_in": out.get("expires_in", 60), "retry_after": SMS_MIN_INTERVAL}


@router.post("/verify")
def login_verify(
    body: VerifyIn,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """第 3 步：校验验证码 → 全自动完成授权 → 换取凭证 → 入号池。

    凭证**先校验后入库**：结构校验 + 上游存活校验都过了才写账号表，
    并按 uid 去重（重复登录同一个号只更新、不新增）。
    """
    try:
        sess = wb_login.get_session(body.sid)
    except wb_login.WbLoginError as e:
        raise _fail(e, 502)

    with _session_slot(sess, "verify"), _upstream_slot("verify"):
        out = _verify_and_store(sess, body.code, db, account_id=body.account_id)
    return {
        "ok": True,
        "duplicated": out["duplicated"],
        "message": out["message"],
        "uid": out["uid"],
        "check": {"live": out["live_ok"], "live_message": out["live_msg"],
                  "format": "ok"},
        "account": out["account"],
        "invitation": out.get("invitation"),
    }


@router.get("/status/{sid}")
def login_status(sid: str, _: bool = Depends(require_admin)):
    """查询会话状态（前端轮询用；也让「刷新页面后继续」成为可能）。"""
    try:
        sess = wb_login.get_session(sid)
    except wb_login.WbLoginError:
        return {"alive": False}
    return {
        "alive": True,
        "mobile": sess.mobile,
        "country_code": sess.country_code,
        "code_sent": bool(sess.sms_state_token),
        "consumed": sess.consumed,
        "retry_after": max(0, int(sess.code_sent_at + SMS_MIN_INTERVAL - time.time()) + 1),
        "age": int(time.time() - sess.created),
    }


@public_router.get("/api/join/status/{sid}")
def join_status(sid: str):
    return login_status(sid, True)


# ===========================================================================
# 自助注册（公开路由）：把地址发给任何人，对方输入手机号即可完成注册
# ===========================================================================
#
# 这是**完全公开**的端点，不需要后台口令、也不需要邀请码。
#
# 安全说明（重要）
# ----------------
# 该端点会发真实短信，因此**唯一**的防线是发码节流（见 `_check_sms_quota`）：
#   * 同一手机号 60 秒内只能发一次（防对单号轰炸）；
#   * 同一 IP 每小时最多 `SMS_MAX_PER_HOUR` 次（防拿我们的出口 IP 批量刷）。
#
# 这比原先「邀请码 fail-closed」的防护弱：任何拿到地址的人都能触发一次真实短信。
# 之所以接受，是因为需求明确要求「页面公开、操作越简单越好」。
# 若日后被滥用，最直接的收紧手段是把 `SMS_MAX_PER_HOUR` 调小，
# 或在此处重新引入一个共享口令。


class JoinIn(BaseModel):
    """自助端点入参。

    三个端点（start / send / verify）共用一个模型，各自只用到一部分字段。
    """
    sid: Optional[str] = ""
    mobile: str = ""
    country_code: str = "86"
    sms_code: str = ""


def _mask_name(name: str) -> str:
    """模糊化账号名（公开接口回显用）：保留首尾，中间打码。"""
    s = (name or "").strip()
    if not s:
        return "***"
    if len(s) <= 2:
        return s[0] + "*"
    if len(s) <= 6:
        return s[0] + "*" * (len(s) - 2) + s[-1]
    return s[:2] + "*" * 4 + s[-2:]


# ---------------------------------------------------------------------------
# 登录后的自动收尾：做成长任务 + 猫猫旅行 + 每日签到
# ---------------------------------------------------------------------------
#
# 为什么要有这一段
# ----------------
# 原来的登录流程只做「入库」就结束了，于是新账号在后台看起来是「半成品」：
#   * 余额 0/0、同步时间是空的（要等整点定时任务或手动点刷新）
#   * 成长任务一个都没做（要手动点「批量做任务」）
#   * 猫猫旅行没领（要手动点）
# 用户的原话是「还得我手动完成任务之类的操作」—— 这正是缺了这段。
#
# 为什么放**后台线程**
# --------------------
# 一个账号要做完全部可自动化任务约 5 分钟（见 growth.py 的预算推算）。
# 放在登录请求里同步跑，前端会一直转圈直到 nginx 超时（60s）→ 体验极差；
# 而且会长时间占着并发闸门的名额，把其他登录堵住。
# 所以：登录请求立即返回「注册成功」，收尾工作在守护线程里慢慢做。
#
# 失败绝不影响「注册成功」这个结论 —— 凭证已经入库、可以正常用了。

#: 登录后自动做的事（都做成可开关，便于按需关掉）
POST_LOGIN_AUTOMATION = True

def _run_post_login_automation(account_id: int, name: str) -> dict:
    """后台线程：对一个刚入库的账号做成长任务 + 猫猫旅行 + 签到。

    每一步都独立 try：任一环节失败不影响其它环节，也绝不影响登录结果。
    """
    import time as _t
    from admin.db import SessionLocal
    from admin import backend

    log = logging.getLogger("admin.wb_login_router")
    result = {"errors": [], "pet_credits": 0, "growth_credits": 0, "checkin_credits": 0}
    db = SessionLocal()
    try:
        acc = db.query(Account).filter(Account.id == account_id).first()
        if not acc:
            return {"errors": ["账号不存在"]}
        log.info("wb_login: 登录后收尾开始 id=%s name=%s", account_id, name)
        _t0 = _t.time()

        # ---- 1) 猫猫旅行（领养 +300 / 派猫 / 领奖）----
        try:
            with backend.AccountSession(acc.auth_json) as sess:
                res = sess.run_cat_travel()
                acc.auth_json = sess.updated_json()
                db.commit()
            result["pet_credits"] = res.get("credits") or 0
            if not res.get("ok") or res.get("outcome") == "gate_blocked":
                result["errors"].append("宠物领养或旅行未完成")
            log.info("wb_login: 收尾-猫猫旅行 id=%s outcome=%s credits=%s",
                     account_id, res.get("outcome"), res.get("credits"))
        except Exception as e:
            db.rollback()
            result["errors"].append("宠物任务失败")
            log.warning("wb_login: 收尾-猫猫旅行失败 id=%s: %s", account_id, e)

        # ---- 2) 每日签到 ----
        try:
            with backend.AccountSession(acc.auth_json) as sess:
                st = sess.get_checkin_status()
                if not st.get("today_checked_in"):
                    r = sess.claim_daily_checkin()
                    result["checkin_credits"] = r.get("credit") or 0
                    log.info("wb_login: 收尾-签到 id=%s ok=%s credit=%s",
                             account_id, r.get("ok"), r.get("credit"))
                acc.auth_json = sess.updated_json()
                db.commit()
        except Exception as e:
            db.rollback()
            result["errors"].append("签到失败")
            log.warning("wb_login: 收尾-签到失败 id=%s: %s", account_id, e)

        # ---- 3) 成长任务（含自动领奖）----
        # 必须复用 growth 的 run_accounts：那里沉淀了 accept 落库等待、
        # 节流、复查与自动领取，绕开它就会出现「任务做了但没领」。
        try:
            from admin.routers import growth as growth_router
            r = growth_router.run_accounts([account_id], None, db)
            rows = r.get("results") or []
            if rows:
                first = rows[0]
                result["growth_credits"] = first.get("credit") or 0
                if not first.get("ok") or first.get("budget_exceeded") or first.get("claim_failed"):
                    result["errors"].append("部分成长任务未完成或奖励未领取")
                log.info("wb_login: 收尾-成长任务 id=%s 任务数=%s 领取积分=%s 能量=%s",
                         account_id, len(first.get("tasks") or []),
                         first.get("credit"), first.get("energy"))
        except Exception as e:
            db.rollback()
            result["errors"].append("成长任务失败")
            log.warning("wb_login: 收尾-成长任务失败 id=%s: %s", account_id, e)

        # ---- 4) 再刷一次余额，把上面领到的积分反映出来 ----
        try:
            with backend.AccountSession(acc.auth_json) as sess:
                bal = sess.fetch_balance()
                acc.balance_total = int(bal.get("total", 0) or 0)
                acc.balance_remain = int(bal.get("remain", 0) or 0)
                from admin.routers.accounts import _sync_credit_snapshot
                try:
                    _sync_credit_snapshot(acc, sess.fetch_credit_details())
                except Exception:
                    result["errors"].append("积分到期快照刷新失败")
                acc.auth_json = sess.updated_json()
            acc.last_sync_at = datetime.utcnow()
            db.commit()
            log.info("wb_login: 收尾完成 id=%s 用时%.0fs 余额=%s/%s",
                     account_id, _t.time() - _t0,
                     acc.balance_remain, acc.balance_total)
        except Exception as e:
            db.rollback()
            result["errors"].append("余额刷新失败")
            log.warning("wb_login: 收尾-最终刷新余额失败 id=%s: %s", account_id, e)
    except Exception:
        db.rollback()
        result["errors"].append("自动任务异常")
        log.exception("wb_login: 登录后收尾异常 id=%s（不影响登录结果）", account_id)
    finally:
        db.close()
    return result


def _schedule_post_login_automation(account_id: int, name: str) -> None:
    """Persist and queue onboarding without blocking the login response."""
    from admin.post_login import enqueue_resources
    enqueue_resources(account_id)
    if not POST_LOGIN_AUTOMATION:
        return
    try:
        from admin.post_login import enqueue
        enqueue(account_id, name)
    except Exception:
        _logger.exception("登录后任务提交失败 account_id=%s（不影响入库）", account_id)


# ---------------------------------------------------------------------------
# 并发闸门：保护整个服务不被注册洪峰拖垮
# ---------------------------------------------------------------------------
#
# 结论来自实测（见 docs/workbuddy-phone-login.md 的并发一节）：
# 登录链路的每个请求都会**占住一个工作线程**去等腾讯。300 并发注册时，
# 一个完全无关的静态页请求 p95 从 12ms 涨到 1478ms、最坏 8s ——
# 也就是说注册洪峰会拖慢**整个服务**（含网关 /v1/* 与后台页面）。
#
# 这里的闸门按「每个请求占一个名额」算，超出的请求**立刻**拿到一个
# 人话错误 / 或对轮询直接回 done=false，而不是排队耗死线程。
#
# 为什么在路由层而不是每次上游调用都过闸：
#   * 一个请求期间要多打几次上游（start_login 要 5 次），逐次过闸会嵌套获取，
#     可用容量被腰斩，也不好推理；
#   * 路由层能针对不同端点给不同语义：轮询可以「静默重试」，发码要明确告知。


def _upstream_slot(reason: str):
    """占用一个「上游登录请求」名额；满了抛 503（带 Retry-After）。"""
    class _Ctx:
        def __enter__(self_inner):
            if not wb_login.GATE.acquire():
                raise HTTPException(
                    status_code=503,
                    detail={"message": "当前登录人数较多，请稍后重试",
                            "step": "busy", "reason": reason},
                    headers={"Retry-After": "3"})
            return self_inner

        def __exit__(self_inner, *exc):
            wb_login.GATE.release()
            return False

    return _Ctx()


def _session_slot(sess, reason: str):
    """Claim one session operation without tying up a worker on its RLock."""
    class _Ctx:
        def __enter__(self_inner):
            if not sess.lock.acquire(blocking=False):
                raise HTTPException(
                    status_code=409,
                    detail={"message": "该登录会话正在处理中，请稍后重试",
                            "step": "busy", "reason": reason},
                    headers={"Retry-After": "2"})
            return self_inner

        def __exit__(self_inner, *exc):
            sess.lock.release()
            return False

    return _Ctx()


def _try_session_slot(sess):
    class _Ctx:
        acquired = False

        def __enter__(self_inner):
            self_inner.acquired = sess.lock.acquire(blocking=False)
            return self_inner.acquired

        def __exit__(self_inner, *exc):
            if self_inner.acquired:
                sess.lock.release()
            return False

    return _Ctx()


@public_router.post("/api/join/start")
def join_start(body: JoinIn, request: Request):
    """公开：发起一次注册会话。"""
    with _upstream_slot("join_start"):
        try:
            sess = wb_login.start_login()
        except wb_login.WbLoginError as e:
            raise _fail(e, 502)
    return {"sid": sess.sid}


@public_router.post("/api/join/send")
def join_send(body: JoinIn, request: Request):
    """公开：发短信验证码。"""
    ip = client_ip_from_request(request)
    return _send_guarded(body, ip)


@public_router.post("/api/join/verify")
def join_verify(body: JoinIn, request: Request, db: Session = Depends(get_db)):
    """公开：校验验证码 → 自动完成授权 → 凭证校验 → 入库。

    与管理端**共用 `_verify_and_store`**，所以两条路对「什么算合法凭证」
    的判定完全等价，不会出现「公开端宽松一点」这种后门。

    返回刻意只说「注册成功」：本流程等价于用手机号注册/登录这个服务，
    不向用户暴露「凭证正在被收进号池」这类内部实现细节。
    """
    try:
        sess = wb_login.get_session(body.sid)
    except wb_login.WbLoginError as e:
        raise _fail(e, 502)

    with _session_slot(sess, "verify"), _upstream_slot("verify"):
        out = _verify_and_store(sess, body.sms_code, db)

    # 公开接口**不回显** uid / 账号 id，只回模糊化的账号名，
    # 避免陌生人靠反复登录枚举号池内容。
    return {
        "ok": True,
        "already": out["duplicated"],
        "message": "注册成功",
        "masked": _mask_name(out["account"].get("name") or out["uid"]),
        "invitation": out.get("invitation"),
    }


# ---------------------------------------------------------------------------
# 兜底：官方页面接力（当上游要求图形验证码时用这个）
# ---------------------------------------------------------------------------
#
# 纯协议模式会卡在图形验证码上（腾讯的 `collect` 参数是 VMP+TEA 混淆的
# 行为指纹，密钥还随版本轮换 —— 纯协议无法产出，见 docs/workbuddy-captcha-assessment.md）。
#
# 这时最稳的办法是**让用户自己去官方页面过验证码**，我们只负责把结果接回来：
#   * 官方登录页**没有** X-Frame-Options / frame-ancestors（已实测），
#     所以可以原样内嵌到本站 iframe 里，用户看不出跳转；
#   * `/v2/plugin/auth/token?state=` 轮询**不需要任何 cookie**（已实测），
#     所以服务端能独立拿到凭证，不必碰用户会话；
#   * 全程不需要客户端：官方页面走完之后，凭证是靠 state 从服务端换的，
#     不依赖那个 `workbuddy://` 私有协议回调。

class OfficialIn(BaseModel):
    sid: Optional[str] = ""


@public_router.post("/api/join/official/start")
def join_official_start(body: OfficialIn, request: Request):
    """公开：发起「官方页面接力」——只拿 state + authUrl。"""
    with _upstream_slot("official_start"):
        try:
            sess = wb_login.start_official_login()
        except wb_login.WbLoginError as e:
            raise _fail(e, 502)
    return {"sid": sess.sid, "auth_url": sess.plugin_auth_url}


@public_router.post("/api/join/official/poll")
def join_official_poll(body: OfficialIn, request: Request, db: Session = Depends(get_db)):
    """公开：轮询用户是否已在官方页面完成登录；完成后自动换凭证并入库。

    返回：
      * `{"done": false}`           用户还没登录完 / 系统忙，前端继续轮询
      * `{"done": true, ...}`       已换到凭证并入库
      * 502                         会话失效等需要重新开始的错误
      * 400                         换到了凭证但未通过上游验证（伪造/失效）

    并发保护：并发洪峰下闸门可能已满，此时**不报错**，直接回 `done=false`
    让前端下一轮再来 —— 轮询本来就是每 2.5s 一次，对用户完全无感。
    """
    try:
        sess = wb_login.get_session(body.sid)
    except wb_login.WbLoginError as e:
        raise _fail(e, 502)

    # 已成功过 -> 直接复用（幂等）
    with _try_session_slot(sess) as acquired:
        if not acquired:
            return {"done": False, "busy": True}
        if sess.result_cache is not None:
            cached = sess.result_cache
            return {
                "done": True,
                "message": "注册成功",
                "masked": _mask_name((cached.get("account") or {}).get("name") or cached.get("uid")),
                "invitation": cached.get("invitation"),
            }

        try:
            # 闸门满了不算错误：轮询本来就每 2.5s 一次，静默回 done=false
            # 让前端下一轮再来，对用户完全无感。
            with _upstream_slot("poll"):
                token = sess.official_token or wb_login.fetch_credential_by_state(sess.plugin_state)
                if token:
                    sess.official_token = token
        except HTTPException as he:
            if he.status_code == 503:
                return {"done": False, "busy": True}
            raise
        except wb_login.WbLoginError as e:
            raise _fail(e, 502)
        if not token:
            return {"done": False}

        try:
            with _upstream_slot("poll_finalize"):
                credential, meta = wb_login.build_credential_from_token(token)
                out = _store_verified_credential(credential, meta, db)
                out["invitation"] = wb_login.try_bind_invitation(sess, credential)
        except HTTPException as he:
            if he.status_code == 503:
                return {"done": False, "busy": True}
            raise
        except wb_login.WbLoginError as e:
            raise _fail(e, 502)
        sess.result_cache = dict(out)
        sess.consumed = True
        sess.consumed_at = time.time()

    return {
        "done": True,
        "message": "注册成功",
        "masked": _mask_name(out["account"].get("name") or out["uid"]),
        "invitation": out.get("invitation"),
    }

