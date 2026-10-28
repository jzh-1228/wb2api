"""轻量后台定时任务调度器（无第三方依赖）。

用守护线程每 15s 轮询 schedules 表，到点（next_run_at <= now）的任务就执行，
执行完更新 last_run_at / next_run_at / last_result。

支持的任务：
  - refresh_balances：遍历 active 账号刷新余额（统计平台总积分）
  - sync_models：从后端拉取最新模型列表并 upsert 倍率
"""
import json
import logging
import threading
import time
from datetime import datetime, timedelta

from admin.db import SessionLocal
from admin.models import Schedule
from admin.config import settings
from admin import maintenance
from admin.schedule_timing import timing_of, next_run_at
from collections import Counter

_logger = logging.getLogger(__name__)

#: last_result 是 TEXT，存得下完整 JSON；但仍给一个上限，
#: 防止某个任务的明细异常膨胀把这一行撑爆。
_RESULT_LIMIT = 20000


def _account_label(a) -> str:
    """给账号起个**人认得出**的短标签。

    后台只显示 `acc12` 的话，用户看到「acc12 没领成功」还得自己去号池里
    对 id，等于没说。优先用账号名（多数是手机号或昵称），没有才退回 id。
    手机号做脱敏：中间四位打码，既够辨认又不把完整号码写进结果里
    （结果会被截图、也会随日志流转）。
    """
    name = str(getattr(a, "name", "") or "").strip()
    if name:
        if len(name) == 11 and name.isdigit():
            return f"{name[:3]}****{name[-4:]}"
        return name[:20]
    return f"账号#{getattr(a, 'id', '?')}"


def _short_error(e) -> str:
    """把异常压成一句可读原因（去掉类名前缀与多余空白）。"""
    msg = str(e).strip() or e.__class__.__name__
    return msg[:160]


def _dump_result(result: dict) -> str:
    """序列化任务结果。

    这里**绝不能截断 JSON 字符串**：以前是 `json.dumps(...)[:500]`，
    一截断就成了非法 JSON，前端 `JSON.parse` 直接失败，用户看到的是
    一段被腰斩的原始文本 —— 正是「别只显示一堆 json 数据」的成因之一。
    超长时改为丢弃明细数组里的项，保住 JSON 结构完整。
    """
    try:
        text = json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps({"ok": False, "error": "结果无法序列化"},
                          ensure_ascii=False)
    if len(text) <= _RESULT_LIMIT:
        return text
    # 超长：先砍 detail（逐账号明细），保留计数与 summary
    slim = dict(result)
    detail = slim.get("detail")
    if isinstance(detail, list):
        for keep in (50, 20, 5, 0):
            slim["detail"] = detail[:keep]
            slim["detail_truncated"] = True
            text = json.dumps(slim, ensure_ascii=False)
            if len(text) <= _RESULT_LIMIT:
                return text
    slim.pop("detail", None)
    text = json.dumps(slim, ensure_ascii=False)
    return text if len(text) <= _RESULT_LIMIT else text[:_RESULT_LIMIT - 40] + '"}'



def run_task(task: str, db, schedule: "Schedule | None" = None) -> dict:
    """执行某个任务，返回结果摘要字典。"""
    from admin.pool import POOL
    if POOL.waf.active():
        return {"task": task, "ok": True, "deferred": True,
                "retry_after": max(1, POOL.waf.remaining()),
                "summary": "出口保护期间暂停后台上游任务"}
    if task == "refresh_balances":
        from admin.routers import accounts as acc_router
        from admin.models import Account
        ok = fail = 0
        skipped = Counter()
        failed_names: list[str] = []
        for a in db.query(Account).filter(Account.status == "active").all():
            with maintenance.account_slot(a, db) as reason:
                if reason:
                    skipped[reason] += 1
                    continue
                # with_expiry=True：顺带同步「积分包到期快照」。
                # 这个快照是选号策略（expiring / weighted）的依据 —— 之前
                # credits_expiring 长期是 0，导致「先用快过期的额度」这条规则
                # 实际上从未生效。
                if acc_router._refresh_balance(a, with_expiry=True,
                                               on_error=lambda error: maintenance.note_failure(db, a, error)):
                    ok += 1
                else:
                    fail += 1
                    failed_names.append(_account_label(a))
                db.commit()
        rows = db.query(Account).filter(Account.status == "active").all()
        expiring = sum(int(a.credits_expiring or 0) for a in rows)
        expiring_soon = sum(int(a.credits_expiring_soon or 0) for a in rows)
        return {"task": task, "ok": True, "refreshed": ok, "failed": fail,
                "skipped": sum(skipped.values()), "skip_reasons": dict(skipped),
                "credits_expiring": expiring,
                "credits_expiring_soon": expiring_soon,
                "failed_accounts": failed_names[:10],
                "summary": (f"刷新成功 {ok} 个账号"
                            + (f"，7 天内到期积分 {expiring_soon}" if expiring_soon
                               else (f"，快过期积分 {expiring}" if expiring else ""))
                            + (f"，{fail} 个失败：{'、'.join(failed_names[:3])}"
                               if fail else "")
                            + (f"，{sum(skipped.values())} 个因保护或占用跳过" if skipped else ""))}
    if task == "refresh_credits":
        from admin.routers import accounts as acc_router
        from admin.models import Account
        ok = fail = 0
        skipped = Counter()
        failed_names: list[str] = []
        # 逐号重采积分包明细 → 写回到期快照。选号的「先用快过期的」这条
        # 规则完全建立在这份快照上，所以它必须按自己的节奏刷新，
        # 而不是搭在余额刷新里「顺便做」—— 余额刷新频率高、开销敏感，
        # 一旦为了省钱把 with_expiry 关掉，调度依据就悄悄归零了
        # （这个 bug 真实发生过）。
        for a in db.query(Account).filter(Account.status == "active").all():
            with maintenance.account_slot(a, db) as reason:
                if reason:
                    skipped[reason] += 1
                    continue
                try:
                    sess = None
                    try:
                        from admin.backend import AccountSession
                        sess = AccountSession(a.auth_json)
                        packages = sess.fetch_credit_details()
                        a.auth_json = sess.updated_json()
                    finally:
                        if sess is not None:
                            try:
                                sess.close()
                            except Exception:
                                pass
                    acc_router._sync_credit_snapshot(a, packages)
                    collection = getattr(packages, "metadata", None)
                    if collection and not collection["complete"]:
                        fail += 1
                        failed_names.append(_account_label(a))
                    else:
                        ok += 1
                except Exception as error:
                    maintenance.note_failure(db, a, error)
                    fail += 1
                    failed_names.append(_account_label(a))
                db.commit()
        rows = db.query(Account).filter(Account.status == "active").all()
        expiring_soon = sum(int(a.credits_expiring_soon or 0) for a in rows)
        soonest = min((a.credits_soonest_expire_at for a in rows
                       if a.credits_soonest_expire_at), default=None)
        soonest_txt = ""
        if soonest is not None:
            import datetime as _dt
            left = (soonest - _dt.datetime.utcnow()).total_seconds() / 86400.0
            soonest_txt = (f"，最近一个 {int(left)} 天后到期" if left >= 1
                           else "，最近一个 24 小时内到期")
        return {"task": task, "ok": True, "refreshed": ok, "failed": fail,
                "skipped": sum(skipped.values()), "skip_reasons": dict(skipped),
                "credits_expiring_soon": expiring_soon,
                "failed_accounts": failed_names[:10],
                "summary": (f"已更新 {ok} 个账号的积分到期快照"
                            + (f"，7 天内到期 {expiring_soon} 积分" if expiring_soon else "")
                            + soonest_txt
                            + (f"，{fail} 个失败：{'、'.join(failed_names[:3])}"
                               if fail else "")
                            + (f"，{sum(skipped.values())} 个因保护或占用跳过" if skipped else ""))}
    if task == "sync_models":
        from admin.routers import models as models_router
        res = models_router._do_sync_models(db)
        if isinstance(res, dict) and "summary" not in res:
            res["summary"] = (f"新增 {res.get('added', 0)} 个、"
                              f"更新 {res.get('updated', 0)} 个，"
                              f"共 {res.get('total_in_db', 0)} 个模型")
        return res
    if task == "daily_checkin":
        return run_daily_checkin(db, schedule)
    if task == "refresh_growth_tasks":
        return run_refresh_growth_tasks()
    if task == "run_growth_tasks":
        return run_growth_tasks()
    if task == "run_growth_plan":
        from admin.growth_plan import run_growth_plan
        return run_growth_plan(db)
    if task == "keepalive_tokens":
        return run_keepalive_tokens(db)
    if task == "ban_recheck":
        from admin.ban_recheck import run_ban_recheck
        return run_ban_recheck(db)
    return {"task": task, "error": "未知任务类型"}


#: token 保活：连续多少次「session 已死」才把账号禁用。
#: 与 proxy 的 SESSION_DEAD_THRESHOLD 同源 —— 一次 12153 只说明这次刷新失败，
#: 可能是上游抖动；连续失败才说明这个号的登录态真的废了，需要人工重登。
_KEEPALIVE_DEAD_THRESHOLD = 3


#: 刷新/鉴权返回里代表「服务端会话已被删除」的标记 —— OAuth 标准语义，属**终态**。
#: 与文案式的 12153 抖动区分开：命中这些就没有重试价值，应立即禁用并提示重登。
_AUTH_DEAD_MARKERS = (
    "invalid_grant",
    "offline user session not found",
    "refresh token failed",
    "session not found",
)


class TrustedAuthFailure(RuntimeError):
    """Both trusted authenticated probes rejected the current credential."""

    trusted_auth_failure = True

    def __init__(self, errors):
        self.errors = list(errors)
        from admin.routers.proxy import _extract_http_status
        self.http_status = next(
            (_extract_http_status(str(error)) for error in self.errors
             if _extract_http_status(str(error)) in (401, 403)),
            0,
        )
        self.raw_body = str(self.errors[0])[:65536] if self.errors else ""
        super().__init__("trusted authentication probes both failed: " + self.raw_body)


def _access_token_of(sess) -> str:
    """从 AccountSession 里取当前 accessToken（用于判断是否真的刷新了）。"""
    try:
        return ((sess.cm._session().get("auth") or {}).get("accessToken")) or ""
    except Exception:
        return ""


def _access_token_alive(sess) -> bool:
    """Check trusted authenticated endpoints after refresh fails.

    The model-list route can reject a shared request shape while the bearer
    session remains valid.  Balance and account-profile routes are the
    authoritative probes for this decision; either one succeeding is enough.
    """
    try:
        sess.fetch_balance()
        return True
    except Exception:
        try:
            sess.fetch_account_profile()
            return True
        except Exception:
            return False


def _trusted_token_probe(sess) -> None:
    """Raise only when both trusted authenticated probes fail."""
    errors = []
    try:
        sess.fetch_balance()
        return
    except Exception as first:
        errors.append(first)
    try:
        sess.fetch_account_profile()
        return
    except Exception as second:
        errors.append(second)
    from admin.routers.proxy import _extract_http_status
    statuses = [_extract_http_status(str(error)) for error in errors]
    if statuses and all(status in (401, 403) for status in statuses):
        raise TrustedAuthFailure(errors)
    raise errors[0]


def run_keepalive_tokens(db) -> dict:
    """定时刷新所有活跃账号的 token，保持登录态存活（「保活」）。

    为什么需要：上游的登录态有绝对有效期。如果一个账号长期没有请求，它的
    refresh token 会在某天静默失效 —— 等到真有人来用，才在第一次请求时
    发现要重登。用户感知就是「号池里明明有余额的号，用的时候报错」。

    这也正是参考实现 internal/scheduler/scheduler.go 的 KeepaliveHours
    （默认 [22]，即每晚 22 点）在做的事：主动 refresh 一遍所有 token。

    实现要点（对齐参考实现，也贴合上游真实行为）：
      * **串行 + 节流**：账号之间有 KEEPALIVE_ACCOUNT_GAP 秒间隔。批量并发地
        刷新 token 是一个很明显的机器特征，节流后与真人逐个使用的节奏接近。
      * **只刷新不调用**：不发起对话、不消耗积分、不产生对话记录。
      * **必须真探测（本次修复的关键）**：原来只调 `get_headers()`，
        它只在**本地** expiresAt 临近时才刷新 token —— 而登录态被上游**吊销**时，
        本地 expiresAt 可能还有几千小时，于是保活一路报「存活 ✓」，
        真实请求却 401。这就是「xx 个号失效了但保活说全好」的根因。
        现在优先调余额或账号信息接口：这些接口必须接受登录态，
        能真实回答「这个登录态上游还认不认」。模型列表只用于路由，不作为
        自动禁用账号的唯一依据。
      * **区分 401 与网络抖动**：401/403 是登录态废了（终态）；
        HttpClient Stream error 之类只是抖动，**绝不能**计入失效计数，
        否则一次网络抖动会把好号累计到阈值然后误禁。
      * **连续计数后禁用**：失效达阈值才禁用，避免单次抖动误杀。
      * **成功清零**：探测成功即把 `session_dead_fails` 归零。

    返回结果摘要，写入 schedules.last_result 供后台查看。
    """
    from admin.backend import AccountSession
    from admin.models import Account
    from admin.models import SystemSetting

    # Persist only contested identities, so a one-minute retry never rotates
    # all already-refreshed accounts again and survives a process restart.
    pending_row = db.get(SystemSetting, "keepalive:pending")
    batch_row = db.get(SystemSetting, "keepalive:batch_started")
    try:
        pending_ids = json.loads(pending_row.value) if pending_row else []
    except (TypeError, ValueError):
        pending_ids = []
    try:
        batch_started = float(batch_row.value) if batch_row else 0
    except (TypeError, ValueError):
        batch_started = 0

    accounts = (db.query(Account).filter(Account.status == "active")
                .order_by(Account.id.asc()).all())
    if pending_ids and time.time() - batch_started < 20 * 3600:
        accounts = [a for a in accounts if a.id in pending_ids]
    else:
        batch_started = time.time()
    if not accounts:
        if pending_row:
            pending_row.value = "[]"
            db.commit()
        return {"task": "keepalive_tokens", "ok": True, "total": 0, "msg": "没有活跃账号"}

    total = len(accounts)
    ok = 0
    refreshed = 0
    dead_disabled = 0
    failed = 0
    dead_ids: list[int] = []        # 本次判定为「登录态失效」的账号
    errors: list[str] = []

    skipped = Counter()
    pending = []
    for a in accounts:
        with maintenance.token_slot(a, db) as reason:
            if reason:
                skipped[reason] += 1
                if reason in ("inflight", "background_busy"):
                    pending.append(a.id)
                continue
            sess = None
            try:
                sess = AccountSession(a.auth_json)
                before = _access_token_of(sess)
                # **强制刷新**，而不是 `get_headers()`（后者只在本地 expiresAt 临近时才刷）。
                #
                # 这是本事故里最容易被忽略的一层：这些号的 expiresAt 写着 2027 年，
                # 于是 `get_headers()` 一直认为「还早」，永远不刷新 ——
                # 保活每天都报「成功 N/N、refreshed=0」，**实际上一个 token 都没动过**。
                # 而上游的 offline session 会因为长期没有刷新活动而失效，
                # 最终变成 `invalid_grant: Offline user session not found`。
                #
                # 官方客户端正是**按 24h 固定节奏刷新**（见 10.8.1），并不看是否临近过期。
                # 我们的「每晚一次」只要真的去刷，节奏就与官方一致。
                sess.cm._refresh()
                after = _access_token_of(sess)
                if after and after != before:
                    refreshed += 1
                # **真探测**：本地 expiresAt 不可信（登录态被吊销时它仍可能是
                # 几千小时之后）。余额或账号信息接口才是登录态的权威判据。
                # Save a rotated token before any optional probe can fail.
                a.auth_json = sess.updated_json()
                db.commit()
                # Cooling/WAF protects business calls; credential renewal is
                # independent and must not clear or extend those protections.
                protected = maintenance._reason(a) in ("waf", "account_cooling")
                if not protected:
                    _trusted_token_probe(sess)
                # **回写刷新后的凭证**。原实现漏了这一步：刷新出来的新 token 只存在
                # 临时文件里，`close()` 一删就没了，库里仍是旧 token ——
                # 等于「刷新了但白刷」，下次请求还得从头再来（或直接失败）。
                a.auth_json = sess.updated_json()
                ok += 1
                # 探测成功 → 清零失效连续计数
                if a.session_dead_fails:
                    a.session_dead_fails = 0
                if not protected:
                    a.last_err_at = None
                    a.last_err_msg = ""
                    # 陈旧证据一并清掉：last_upstream_error 没有时间字段、
                    # 不会自过期，留着会让 _looks_login_dead 把健康号判死。
                    a.last_upstream_error = ""
                db.commit()
            except Exception as e:
                msg = str(e)
                from admin.routers.proxy import _extract_http_status
                # 从异常文本里抠出真实状态码再分类。
                # 传 0 会把 401 归成 transport（网络抖动），于是失效号永远不被发现
                # ——这正是「xx 个号失效、保活却报全好」的最后一环。
                http_status = getattr(e, "http_status", 0) or _extract_http_status(msg)
                trusted_failure = bool(getattr(e, "trusted_auth_failure", False))
                if trusted_failure:
                    # This exception is produced only after both trusted
                    # account/balance probes rejected the credential.  Do not
                    # send it through the shared-401 anomaly gate.
                    from admin.upstream_errors import error_info
                    a.last_upstream_error = json.dumps(
                        error_info(getattr(e, "raw_body", msg), http_status),
                        ensure_ascii=False)
                    kind = "session_dead"
                else:
                    kind = maintenance.note_failure(db, a, e)
                if kind == "session_dead" and not trusted_failure:
                    from admin.upstream_auth_guard import observe
                    if observe(http_status, msg, getattr(a, "uid", "")):
                        kind = "upstream_auth_anomaly"
                # Only explicit session failures count toward disabling. Account
                # restrictions and WAF use the same cooling policy as conversations.
                if kind == "session_dead":
                    # 权威判定：登录态被上游**明确拒绝**，重试多少次都一样。
                    #
                    # 两种都算权威：
                    #   * HTTP 401/403；
                    #   * 刷新接口返回的 `invalid_grant` /
                    #     `Offline user session not found` —— 这是 OAuth 标准里
                    #     「该会话已在服务端被删除」的语义，属于**终态**，
                    #     与「文案式 12153 抖动」完全不同。
                    authoritative = (http_status in (401, 403)
                                     or any(m in msg.lower() for m in _AUTH_DEAD_MARKERS))
                    # 但「续签失败」≠「号不可用」：上游吊销离线会话（refresh
                    # invalid_grant）**不必然**吊销已签发的 access token ——
                    # 线上实测 #18：refresh 死了 7 周，access token（60 天长效）
                    # 的真实对话照样 200。只有 access token 实测**也被拒**时，
                    # 这个号才真正无法出流量，才值得禁用；否则保留在池里服务，
                    # 直到 access token 自然过期，探测失败后自会禁用。
                    token_still_alive = (not trusted_failure and authoritative
                                         and _access_token_alive(sess))
                    if token_still_alive:
                        a.last_err_at = datetime.utcnow()
                        a.cool_kind = ""
                        a.session_dead_fails = 0
                        a.last_err_msg = (
                            f"保活续签失败（refresh token 已失效，无法再续签）"
                            f"但 access token 实测仍可用，暂保留出流量；"
                            f"请尽快重新登录：{msg[:120]}"
                        )[:255]
                        db.commit()
                        failed += 1
                        if len(errors) < 5:
                            errors.append(f"#{a.id}: 续签失败但 token 实测可用，保留（需重登）")
                        continue
                    dead_ids.append(a.id)
                    a.session_dead_fails = (a.session_dead_fails or 0) + 1
                    a.cool_kind = "session_dead"
                    a.last_err_at = datetime.utcnow()
                    if authoritative:
                        a.last_err_msg = (
                            f"登录态被上游拒绝（HTTP {http_status}），需重新登录：{msg[:150]}"
                        )[:255]
                    else:
                        a.last_err_msg = (msg or "keepalive 探测失败")[:255]
                    # Keepalive is a health hint, not the authority for
                    # deleting a credential.  Require repeated failures; a
                    # shared route/header fault must never empty the pool.
                    threshold = max(3, _KEEPALIVE_DEAD_THRESHOLD)
                    if a.session_dead_fails >= threshold:
                        a.status = "disabled"
                        a.cool_until = None
                        a.breaker_until = None
                        a.degrade_until = None
                        dead_disabled += 1
                    db.commit()
                    failed += 1
                    if len(errors) < 5:
                        errors.append(f"#{a.id}: 登录态失效（{kind}"
                                      f"{'，HTTP ' + str(http_status) if authoritative else ''}）")
                else:
                    # Transport failures do not disable credentials; WAF/rate
                    # restrictions and shared auth anomalies have already been
                    # recorded without changing account status.
                    failed += 1
                    if len(errors) < 5:
                        errors.append(f"#{a.id}: {kind} {msg[:90]}")
            finally:
                if sess is not None:
                    try:
                        sess.close()
                    except Exception:
                        pass

    if pending_row is None:
        pending_row = SystemSetting(key="keepalive:pending", value="[]")
        db.add(pending_row)
    pending_row.value = json.dumps(pending)
    if batch_row is None:
        batch_row = SystemSetting(key="keepalive:batch_started", value="0")
        db.add(batch_row)
    batch_row.value = str(batch_started)
    db.commit()
    # 只有「真失效」才值得告警；纯网络失败不该给人「号废了」的错觉
    summary = f"保活成功 {ok}/{total}"
    if refreshed:
        summary += f"，其中 {refreshed} 个刷新了 token"
    if dead_ids:
        summary += (f"，**{len(dead_ids)} 个登录态已失效**"
                    f"（{'、'.join('#' + str(i) for i in dead_ids[:5])}"
                    f"{' 等' if len(dead_ids) > 5 else ''}，"
                    f"{dead_disabled} 个已自动禁用，其余待人工重登）")
    if failed and not dead_ids:
        summary += f"，{failed} 个探测失败"
    if pending:
        summary += f"，{len(pending)} 个在途占用待重试（1 分钟后自动补跑）"
    if skipped.get("inactive"):
        summary += f"，{skipped['inactive']} 个账号已停用"
    return {"task": "keepalive_tokens", "ok": True, "total": total,
            "alive": ok, "refreshed": refreshed, "failed": failed,
            "skipped": sum(skipped.values()), "skip_reasons": dict(skipped),
            "pending_ids": pending, "retry_pending": bool(pending),
            "disabled": dead_disabled, "dead_ids": dead_ids, "errors": errors,
            "summary": summary}



#: 执行成长任务前，若列表刚刷新过不足这个秒数，则等待补足。
#: 上游任务定义与账号进度之间存在同步延迟，刷新列表后立刻执行会拿到
#: 旧的任务定义/进度，导致「新任务没被识别」或「重复触发」。定时任务里
#: 把本任务排在刷新之后几分钟，就是这个原因。
_GROWTH_MIN_AFTER_REFRESH = 180


def run_growth_tasks() -> dict:
    """自动完成号池内所有账号可自动化的成长任务，并领取奖励。

    与后台「批量做任务」按钮走的是同一套逻辑（growth 路由的 run_accounts），
    因此串行执行、节流规则与「执行后自动领取」的行为完全一致。

    过滤规则（与手动执行一致）：
      - 只处理策略表里 actionable 的任务（其余为需客户端完成，跳过）
      - 已 claimed 的跳过，保证幂等，重复跑无副作用
      - 单次失败只记录、不重试，避免对注定失败的任务反复发请求
    """
    from admin.models import Account
    from admin.routers import growth as growth_router

    started = datetime.utcnow()
    db = SessionLocal()
    try:
        # 1) 先刷新任务定义，保证本轮基于最新的任务列表与进度
        try:
            growth_router.growth_tasks(refresh=True, db=db)
        except Exception:
            pass  # 刷新失败不阻断执行（可能只是网络抖动，用旧缓存继续）

        accounts = (db.query(Account)
                    .filter(Account.status == "active")
                    .order_by(Account.id.asc()).all())
        ids = [a.id for a in accounts]
        if not ids:
            return {"task": "run_growth_tasks", "ok": True, "accounts": 0,
                    "msg": "没有可用账号"}

        run_res = growth_router.run_accounts(
            ids, None, db,
            # 整批预算：定时任务在**调度线程**里同步跑，跑多久占多久。
            # 没有这个上限时，xx 个账号最坏能占住调度线程一个多小时，
            # 期间整点刷新余额 / 签到 / token 保活全部停摆
            # （表现为「定时任务卡住了」）。超出预算的账号如实记录、留待下次。
            soft_budget=growth_router._SCHEDULE_SOFT_BUDGET)
    except Exception as e:
        return {"task": "run_growth_tasks", "ok": False, "error": str(e)}
    finally:
        db.close()

    # 汇总（run_accounts 内部已逐个账号执行 + 领取，无需再单独 claim 一遍）
    results = run_res.get("results") or []
    credit = sum((r.get("credit") or 0) for r in results)
    energy = sum((r.get("energy") or 0) for r in results)
    fired = sum(1 for a in results
                for t in (a.get("tasks") or []) if t.get("ok") and not t.get("skipped"))
    failed_acc = [a["account_id"] for a in results if not a.get("ok")]

    # 逐账号明细：失败原因同样要落到具体账号上，而不是只给一个数字
    labels = {a.id: _account_label(a) for a in accounts}
    detail = []
    for r in results:
        aid = r.get("account_id")
        label = labels.get(aid, f"账号#{aid}")
        credit_i = int(r.get("credit") or 0)
        n_done = sum(1 for t in (r.get("tasks") or [])
                     if t.get("ok") and not t.get("skipped"))
        if r.get("ok"):
            detail.append({"account_id": aid, "account": label,
                           "ok": True, "credit": credit_i,
                           "tasks_done": n_done,
                           "reason": f"完成 {n_done} 个任务、+{credit_i} 积分"
                           if n_done else "无待做任务"})
        else:
            failures = [t.get("last_error") or t.get("accept_message") or t.get("skipped")
                        for t in r.get("tasks", []) if not t.get("ok")]
            detail.append({"account_id": aid, "account": label,
                           "ok": False, "credit": credit_i, "tasks_done": n_done,
                           "reason": _short_error(r.get("msg") or r.get("error")
                                                  or next((f for f in failures if f), None)
                                                  or ("奖励领取失败" if r.get("claim_failed") else "未知原因"))})

    result = {
        "task": "run_growth_tasks",
        "ok": True,
        "accounts": len(ids),
        "tasks_done": fired,
        "credit": credit,
        "energy": energy,
        "failed_accounts": failed_acc[:10],
        "elapsed_s": round((datetime.utcnow() - started).total_seconds(), 1),
        "detail": detail,
    }
    result["summary"] = _growth_summary(result)
    return result


def _growth_summary(r: dict) -> str:
    """成长任务结果的一句话摘要。"""
    parts = []
    done = r.get("tasks_done") or 0
    if done:
        parts.append(f"完成 {done} 个任务")
    if r.get("credit"):
        parts.append(f"+{r['credit']} 积分")
    if r.get("energy"):
        parts.append(f"+{r['energy']} 能量")
    if not done:
        parts.append("本轮无待做任务")
    bad = [d for d in (r.get("detail") or []) if not d.get("ok")]
    if bad:
        who = "；".join(f"{d['account']}（{d['reason']}）" for d in bad[:3])
        more = f" 等 {len(bad)} 个" if len(bad) > 3 else ""
        parts.append(f"{len(bad)} 个账号失败：{who}{more}")
    return "，".join(parts)



def run_refresh_growth_tasks() -> dict:
    """刷新成长任务定义缓存。

    任务定义对所有账号一致，所以只需一个可用登录态即可（不遍历账号，
    避免无谓的上游请求）。失败时保留旧缓存，不影响面板使用。
    """
    from admin.routers import growth as growth_router

    try:
        db = SessionLocal()
        try:
            data = growth_router.growth_tasks(refresh=True, db=db)
        finally:
            db.close()
    except Exception as e:
        return {"task": "refresh_growth_tasks", "ok": False, "error": str(e)}

    tasks = data.get("tasks") or []
    actionable = sum(1 for t in tasks if t.get("actionable"))
    if data.get("stale"):
        return {"task": "refresh_growth_tasks", "ok": False, "stale": True,
                "total": len(tasks), "actionable": actionable,
                "synced_at": data.get("synced_at"),
                "error": data.get("error") or "上游任务刷新失败",
                "summary": "刷新失败，当前保留旧任务列表"}
    return {
        "task": "refresh_growth_tasks",
        "ok": True,
        "total": len(tasks),
        "actionable": actionable,
        "synced_at": data.get("synced_at"),
        "source_account_id": data.get("source_account_id"),
        "summary": f"共 {len(tasks)} 个任务，其中 {actionable} 个可自动完成",
    }


def run_daily_checkin(db, schedule: "Schedule | None" = None) -> dict:
    """遍历活跃账号执行每日签到领取积分。

    风控要点：
      - 请求只使用账号凭据和该接口实际需要的字段；不合成桌面设备令牌。
      - 若任务配置了 stop_after（下次停止领取时间），到达后直接跳过，不再发领取请求，
        避免活动下线后继续请求触发上游风控。
      - 若某账号领取返回 EventEnded(1003)，自动把 stop_after 设为今天，后续不再尝试。

    结果里除了计数，还逐账号记录「领到多少分 / 没领成功的原因」：
    只给一个 `failed: 3` 等于没说 —— 用户真正想知道的是**哪个号**失败了、**为什么**。
    """
    from admin.models import Account
    from admin.backend import AccountSession

    now = datetime.utcnow()

    # 停止领取时间：到达则跳过
    if schedule is not None and schedule.stop_after is not None:
        if now > schedule.stop_after:
            return {"task": "daily_checkin", "ok": True,
                    "skipped": "已超过停止领取时间，不再请求",
                    "stop_after": schedule.stop_after.isoformat()}

    accounts = (db.query(Account).filter(Account.status == "active")
                .order_by(Account.id.asc()).all())
    if not accounts:
        return {"task": "daily_checkin", "ok": True, "total": 0,
                "msg": "没有可用账号"}

    claimed = skipped_already = failed = 0
    total_credit = 0
    ended = False
    errors: list[str] = []          # 保留旧字段，兼容既有调用方
    detail: list[dict] = []         # 新增：逐账号明细（供后台展示）

    for a in accounts:
        label = _account_label(a)
        try:
            with AccountSession(a.auth_json) as sess:
                st = sess.get_checkin_status()
                if st.get("today_checked_in"):
                    skipped_already += 1
                    detail.append({"account_id": a.id, "account": label,
                                   "ok": True, "status": "already",
                                   "credit": 0, "reason": "今日已领过"})
                else:
                    res = sess.claim_daily_checkin()
                    if res.get("ok"):
                        credit = int(res.get("credit") or 0)
                        claimed += 1
                        total_credit += credit
                        from admin.growth_center import record_result
                        reward_record = record_result(db, a.id, {"ok": True, "credit": credit}, source="schedule", action="每日打卡")
                        try:
                            from admin.routers.accounts import _sync_credit_snapshot
                            balance = sess.fetch_balance()
                            a.balance_total = int(balance.get("total") or 0)
                            a.balance_remain = int(balance.get("remain") or 0)
                            from admin.credit_journal import attach_balance
                            attach_balance(db, [reward_record], a.balance_remain)
                            a.last_sync_at = datetime.utcnow()
                            _sync_credit_snapshot(a, sess.fetch_credit_details())
                        except Exception as error:
                            errors.append(f"{label}:奖励已领取，余额或到期快照刷新失败")
                            _logger.warning("签到后资源刷新失败 account_id=%s: %s", a.id, type(error).__name__)
                        detail.append({
                            "account_id": a.id, "account": label, "ok": True,
                            "status": "claimed", "credit": credit,
                            "streak_days": int(res.get("streak_days") or 0),
                            "reason": f"+{credit} 积分",
                        })
                    elif res.get("status") == "event_ended":
                        ended = True
                        failed += 1
                        reason = "活动已结束"
                        errors.append(f"{label}:{reason}")
                        detail.append({"account_id": a.id, "account": label,
                                       "ok": False, "status": "event_ended",
                                       "credit": 0, "reason": reason})
                    else:
                        failed += 1
                        reason = res.get("status") or res.get("msg") or "未知原因"
                        errors.append(f"{label}:{reason}")
                        detail.append({"account_id": a.id, "account": label,
                                       "ok": False, "status": "failed",
                                       "credit": 0, "reason": str(reason)})
                # 写回可能已刷新的 token（签到请求会触发鉴权头刷新）
                a.auth_json = sess.updated_json()
                db.commit()
        except Exception as e:
            failed += 1
            reason = _short_error(e)
            errors.append(f"{label}:{reason}")
            detail.append({"account_id": a.id, "account": label, "ok": False,
                           "status": "error", "credit": 0, "reason": reason})

    # 发现活动已结束：自动把停止时间设为今天，防止后续继续请求
    if ended and schedule is not None:
        schedule.stop_after = now
        db.commit()

    result = {
        "task": "daily_checkin",
        "ok": True,
        "total": len(accounts),
        "claimed": claimed,
        "skipped_already": skipped_already,
        "failed": failed,
        "credit": total_credit,
        "activity_ended": ended,
        "errors": errors[:10],
        "detail": detail,
    }
    result["summary"] = _checkin_summary(result)
    return result


def _checkin_summary(r: dict) -> str:
    """把签到结果写成一句人话，后台直接显示这句。"""
    if r.get("skipped"):
        return f"已跳过：{r['skipped']}"
    if r.get("msg"):
        return r["msg"]
    parts = []
    claimed = r.get("claimed") or 0
    credit = r.get("credit") or 0
    if claimed:
        parts.append(f"今日新领 {claimed} 个账号，共 +{credit} 积分")
    already = r.get("skipped_already") or 0
    if already:
        parts.append(f"{already} 个今日已领过")
    failed = r.get("failed") or 0
    if failed:
        # 带上具体是哪个号、什么原因 —— 这才是用户要的信息
        bad = [d for d in (r.get("detail") or []) if not d.get("ok")]
        who = "；".join(f"{d['account']}（{d['reason']}）" for d in bad[:3])
        more = f" 等 {len(bad)} 个" if len(bad) > 3 else ""
        parts.append(f"{failed} 个未领成功：{who}{more}" if who
                     else f"{failed} 个未领成功")
    if r.get("activity_ended"):
        parts.append("活动已结束，已自动停止")
    return "，".join(parts) if parts else "无可领取账号"



def _run_one(s: Schedule, db, now: datetime):
    from admin.pool import POOL
    if POOL.waf.active() and s.task != "keepalive_tokens":
        s.next_run_at = now + timedelta(seconds=max(15, POOL.waf.remaining()))
        s.last_result = _dump_result({"task": s.task, "ok": True, "deferred": True,
                                     "summary": "出口保护期间顺延任务，保护解除后重试"})
        db.commit()
        return
    if s.task == "run_growth_plan":
        start_growth_plan(s, db)
        return
    # 保活是「每天在指定本地整点执行」而非「每 N 分钟执行」。
    #
    # 原实现（有 bug，已修）：`if not _is_keepalive_hour(now): 顺延到下一天; return`
    # —— 只要那一小时里服务不在线（重启 / 部署 / 夜里没人用），
    # 下次轮询就把 next_run_at 顺延到**第二天**，而 last_run_at 不动。
    # 结果是「每天 22 点刚好错过 → 永远错过」：实测 09-20 之后连续 18 天
    # 一次都没跑过，而界面上每天都显示「下次 22:00」，看起来完全正常。
    # 这直接导致 xx 个账号登录态失效后无人发现。
    #
    # 现在的语义是**到期窗口**：只要现在距上次成功执行已超过一个周期
    # （或超过了今天的计划时刻），就补跑一次。这样错过整点只会「晚跑」，
    # 不会「永远不跑」。
    if s.task == "keepalive_tokens" and timing_of(s).mode == "preset":
        from admin.models import SystemSetting
        pending = db.get(SystemSetting, "keepalive:pending")
        if not (pending and pending.value not in ("", "[]")) and not _keepalive_due(s, now):
            # 还没到今天的计划时刻 → 顺延到下一个计划时刻（正常等待）
            s.next_run_at = _next_keepalive_at(now)
            db.commit()
            return
        # 到期（可能已经迟到）→ 照常执行；执行后把 next 排到下一个计划时刻
        result = run_task(s.task, db, s)
        s.last_result = _dump_result(result)
        s.last_run_at = now
        s.next_run_at = now + timedelta(minutes=1) if result.get("retry_pending") else _next_keepalive_at(now)
        db.commit()
        return
    result = {}
    try:
        result = run_task(s.task, db, s)
        s.last_result = _dump_result(result)
    except Exception as e:  # 单个任务失败不影响调度循环
        s.last_result = json.dumps(
            {"ok": False, "error": _short_error(e)}, ensure_ascii=False)[:2000]
    s.last_run_at = now
    s.next_run_at = (now + timedelta(minutes=1) if s.task == "keepalive_tokens"
                     and result.get("retry_pending") else next_run_at(s, now))
    db.commit()


def _loop():
    while True:
        db = None
        try:
            db = SessionLocal()
            now = datetime.utcnow()
            schedules = db.query(Schedule).filter(Schedule.enabled == 1).all()
            # 先启动独立的成长巡检线程，再执行原有同步任务。旧的一次性
            # 成长任务可能很长，不能让它拖延已经到期的每日活跃巡检。
            schedules.sort(key=lambda s: s.task != "run_growth_plan")
            for s in schedules:
                if s.next_run_at is None or s.next_run_at <= now:
                    _run_one(s, db, now)
        except Exception:
            # 数据库短暂不可用（如 MySQL 重启）时静默跳过本轮，下一轮再试。
            # 原先的写法在 `SessionLocal()` 本身抛异常时会引用未赋值的 db，
            # 让异常从 except 块里再次抛出并终止调度线程。
            pass
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass
        time.sleep(15)


def seed_defaults(db):
    """首次启动若无任何任务则写入默认任务（含每日签到）。"""
    if db.query(Schedule).count() == 0:
        now = datetime.utcnow()
        db.add(Schedule(name="整点刷新平台总积分", task="refresh_balances",
                        interval_minutes=60, enabled=1, next_run_at=now))
        # 积分到期快照：选号「先用快过期的」完全依赖它，单独成一个任务，
        # 免得被人为了省开销把 with_expiry 关掉时连调度依据一起搞丢。
        db.add(Schedule(name="每 2 小时更新积分到期快照", task="refresh_credits",
                        interval_minutes=120, enabled=1,
                        next_run_at=now + timedelta(minutes=3)))
        db.add(Schedule(name="每日同步模型列表", task="sync_models",
                        interval_minutes=1440, enabled=1, next_run_at=now))
        db.add(Schedule(name="每日签到领取积分", task="daily_checkin",
                        interval_minutes=1440, enabled=1, next_run_at=now))
        db.add(Schedule(name="每日更新成长任务列表", task="refresh_growth_tasks",
                        interval_minutes=1440, enabled=1, next_run_at=now))
        db.add(Schedule(name="每日自动做成长任务", task="run_growth_tasks",
                        interval_minutes=1440, enabled=1,
                        next_run_at=now + timedelta(seconds=_GROWTH_MIN_AFTER_REFRESH)))
        db.commit()


def ensure_credit_schedule(db):
    """补齐「积分到期快照」定时任务（幂等）。

    为什么必须有独立任务：选号策略里的「先用快过期的」读的是
    `accounts.credits_expiring_soon` / `credits_soonest_expire_at`。
    这两个值只在积分快照刷新时更新 —— 如果没人刷，它们会一直停在旧值，
    表现就是「明明有积分快到期了，调度却一直不动」，而且**没有任何报错**，
    属于最难发现的一类故障。默认每 2 小时一次：官方积分按自然日推进，
    2 小时的粒度足够让「7 天内到期」提前被看见，又不会把上游打得太密。
    """
    row = db.query(Schedule).filter(Schedule.task == "refresh_credits").first()
    if row is None:
        now = datetime.utcnow()
        db.add(Schedule(name="每 2 小时更新积分到期快照", task="refresh_credits",
                        interval_minutes=120, enabled=1,
                        next_run_at=now + timedelta(minutes=3)))
        db.commit()


def ensure_growth_plan_schedule(db):
    """逐账号成长巡检：每小时检查到站礼物，每天只补一次成功对话。"""
    if db.query(Schedule).filter(Schedule.task == "run_growth_plan").first() is None:
        db.add(Schedule(name="WorkBuddy 成长计划巡检", task="run_growth_plan",
                        interval_minutes=60, enabled=1,
                        next_run_at=datetime.utcnow() + timedelta(minutes=5)))
        db.commit()


def start_growth_plan(schedule, db):
    """独立后台线程执行，立即运行按钮和调度循环均无需等待整批账号。"""
    from admin.growth_plan import _Lease, run_growth_plan
    lease = _Lease(db)
    if not lease.acquire():
        # 不覆盖正在执行的进度；略延后其它同类型任务，避免每15秒重复调度。
        if schedule.next_run_at is None or schedule.next_run_at <= datetime.utcnow():
            schedule.next_run_at = datetime.utcnow() + timedelta(minutes=1)
            db.commit()
        return {"ok": True, "running": True, "summary": "成长计划正在执行"}
    try:
        sid = schedule.id
        lease_value = lease.value
        started = datetime.utcnow()
        initial = {"task": "run_growth_plan", "ok": True, "running": True,
                   "summary": "成长计划已开始，正在逐账号检查"}
        schedule.last_result = _dump_result(initial)
        schedule.last_run_at = started
        # 进程意外退出后租约十分钟过期，调度器可自动恢复。
        schedule.next_run_at = started + timedelta(minutes=10)
        db.commit()
    except Exception:
        db.rollback()
        lease.release()
        raise

    def worker():
        with SessionLocal() as worker_db:
            worker_lease = _Lease(worker_db)
            worker_lease.value = lease_value

            def progress(result):
                row = worker_db.get(Schedule, sid)
                if row:
                    row.last_result = _dump_result(result)
                    worker_db.commit()

            try:
                result = run_growth_plan(worker_db, progress=progress, lease=worker_lease)
            except Exception as exc:
                worker_db.rollback()
                worker_lease.release()
                result = {"ok": False, "error": f"成长巡检失败（{type(exc).__name__}）"}
            row = worker_db.get(Schedule, sid)
            if row:
                # 刷新以保留执行期间用户修改的间隔/启停配置。
                worker_db.refresh(row)
                row.last_result = _dump_result(result)
                finished = datetime.utcnow()
                row.next_run_at = (finished + timedelta(minutes=1) if result.get("pending")
                                   else next_run_at(row, finished))
                worker_db.commit()

    thread = threading.Thread(target=worker, daemon=True, name="wb-growth-plan")
    try:
        thread.start()
    except Exception:
        lease.release()
        schedule.last_result = _dump_result({"ok": False, "error": "成长巡检线程启动失败"})
        db.commit()
        raise
    return initial


def ensure_daily_checkin(db):
    """已存在其它任务但缺每日签到时，补一个默认签到任务（幂等）。

    保证「定期自动签到」在任意已运行实例上都有配置：今天已领的账号会被跳过，
    活动结束（EventEnded）时调度器自动把 stop_after 置为今天，不会误发请求触发风控。
    """
    if db.query(Schedule).filter(Schedule.task == "daily_checkin").count() == 0:
        now = datetime.utcnow()
        db.add(Schedule(name="每日签到领取积分", task="daily_checkin",
                        interval_minutes=1440, enabled=1, next_run_at=now))
        db.commit()


def ensure_growth_schedules(db):
    """补齐成长任务相关定时任务（幂等）。

    两个任务必须成对存在且有先后顺序：
      - refresh_growth_tasks：先刷新任务列表
      - run_growth_tasks：    几分钟后再执行
    执行任务排在后面是硬性要求，见 _GROWTH_MIN_AFTER_REFRESH 的说明。

    这里用 next_run_at 保证先后：刷新任务排在 now，执行任务排在 now+延迟；
    若执行任务已存在但时间早于刷新任务，直接顺延。
    """
    now = datetime.utcnow()
    changed = False

    refresh = (db.query(Schedule)
               .filter(Schedule.task == "refresh_growth_tasks").first())
    if refresh is None:
        refresh = Schedule(name="每日更新成长任务列表", task="refresh_growth_tasks",
                           interval_minutes=1440, enabled=1, next_run_at=now)
        db.add(refresh)
        db.flush()  # 拿到 id / 默认值
        changed = True

    run = db.query(Schedule).filter(Schedule.task == "run_growth_tasks").first()
    if run is None:
        base = refresh.next_run_at or now
        db.add(Schedule(name="每日自动做成长任务", task="run_growth_tasks",
                        interval_minutes=1440, enabled=1,
                        next_run_at=base + timedelta(seconds=_GROWTH_MIN_AFTER_REFRESH)))
        changed = True
    else:
        # 已存在则校正先后：执行必须晚于刷新
        base = refresh.next_run_at or now
        want = base + timedelta(seconds=_GROWTH_MIN_AFTER_REFRESH)
        if timing_of(run).mode == "preset" and (run.next_run_at is None or run.next_run_at < want):
            run.next_run_at = want
            changed = True

    if changed:
        db.commit()


def ensure_keepalive_schedule(db):
    """补齐 token 保活定时任务（幂等）。

    为什么用「每天的整点」而不是固定 interval：上游登录态的失效是按自然时间
    推进的，保活要在**没人用号的时候**做（夜里），这样既不影响白天请求，
    又保证第二天早上所有号都是热的。

    参考实现（internal/scheduler/scheduler.go）默认 KeepaliveHours=[22]，
    即每晚 22 点。我们沿用这个思路：默认 22 点，可用
    ADMIN_KEEPALIVE_HOURS 配置多个小时（逗号分隔），或设
    ADMIN_KEEPALIVE_ENABLED=0 关闭。

    这里把 interval_minutes 设为 1440（每天一次），并且只在到期时才真正执行
    —— 见 _keepalive_due。
    """
    if not settings.KEEPALIVE_ENABLED:
        # 显式关闭：若存在则禁用（不删除，保留后台可见与手动触发能力）
        row = db.query(Schedule).filter(Schedule.task == "keepalive_tokens").first()
        if row is not None and row.enabled:
            row.enabled = 0
            db.commit()
        return

    row = db.query(Schedule).filter(Schedule.task == "keepalive_tokens").first()
    if row is None:
        now = datetime.utcnow()
        target = _next_keepalive_at(now)
        db.add(Schedule(name="每日 token 保活刷新", task="keepalive_tokens",
                        interval_minutes=1440, enabled=1, next_run_at=target))
        db.commit()
        return

    # ---- 存量记录自愈 ----
    if timing_of(row).mode != "preset":
        return  # 用户指定的周期不被启动时的系统预设覆盖。
    # 旧版本用 **UTC** 的小时来计算 next_run_at，与现在的「本地时区」语义不符
    # （22 点被算成 UTC 22:00 = 北京次日 06:00）。部署新版后，库里那条旧记录
    # 会把下次执行排到「按新语义看还早得很」的时刻 —— 逻辑修好了却仍然不跑。
    # 更糟的是它可能已经逾期很久（本次故障就是 18 天）。
    #
    # 这里只做「**向前**对齐」：
    #   * 已经到期（含逾期很久）→ 把 next_run_at 设为现在，立即补跑；
    #   * 尚未到期但存量时间明显偏晚 → 拉回按新语义算出的计划时刻；
    #   * 绝不往后推 —— 推迟本来就该执行的保活是危险的。
    now = datetime.utcnow()
    want = now if _keepalive_due(row, now) else _next_keepalive_at(now)
    if row.next_run_at is None or row.next_run_at > want:
        if row.next_run_at is None or row.next_run_at - want > timedelta(minutes=1):
            row.next_run_at = want
            db.commit()


def _local_now() -> datetime:
    """当前**本地**时间。

    为什么需要：调度器其它地方统一用 `datetime.utcnow()`（库里存的也是 UTC），
    但 `ADMIN_KEEPALIVE_HOURS=22` 在配置注释里明确写的是「本地时区」，
    用户理解就是「晚上 10 点」。原实现直接拿 UTC 的 hour 去比，
    于是 22 点实际打到了 **UTC 22:00 = 北京次日 06:00** —— 用户设的夜间保活
    变成了清晨，与他「在没人用号的时候做」的意图不符。
    """
    return datetime.now()


def _tz_offset() -> timedelta:
    """本地时间与 UTC 的偏移（`本地 - UTC`）。

    **必须用同一瞬间的两个时钟来求**：`datetime.now()`（本地）与
    `datetime.utcnow()`（UTC）。早先的写法是
    `_local_now() - now`（把「真实当前时间」减去**调用方传入的时间**），
    两者不是同一瞬间 —— 于是偏移量变成了「真实时刻 − 参数」这个毫无意义的差值，
    判断自然全错（测试里表现为「已过计划时刻却算成没到」）。
    """
    return datetime.now() - datetime.utcnow()


def _next_keepalive_at(now: datetime) -> datetime:
    """返回下一个保活整点时刻（**UTC naive**，与 Schedule.next_run_at 同口径）。

    `now` 传入的是 UTC naive（调度循环统一用 utcnow）。
    这里把 now 换成本地时间判断「今天该跑的小时过没过」，再换回 UTC 存储。
    """
    tz = _tz_offset()
    now_local = now + tz
    hours = sorted(set(h for h in settings.KEEPALIVE_HOURS if 0 <= h <= 23)) or [22]
    for h in hours:
        cand_local = now_local.replace(hour=h, minute=0, second=0, microsecond=0)
        if cand_local > now_local:
            return cand_local - tz
    nxt = (now_local + timedelta(days=1)).replace(
        hour=hours[0], minute=0, second=0, microsecond=0)
    return nxt - tz


def _keepalive_due(s: "Schedule", now: datetime) -> bool:
    """保活现在是否该跑。

    判据（任一成立即到期）：

    1. 已到/已过**今天**的计划时刻，且今天还没成功跑过
       —— 覆盖「22:00 那一刻服务不在线，23:30 才起来」的情况；
    2. 距上次成功执行已超过 `周期 + 宽限`（24h + 6h）
       —— 兜底「计划时刻总是错过」的极端情况，保证至少一天跑一次。

    这样错过整点只会**晚跑**，不会像原实现那样无限顺延。
    """
    tz = _tz_offset()
    now_local = now + tz
    hours = sorted(set(h for h in settings.KEEPALIVE_HOURS if 0 <= h <= 23)) or [22]
    today_plan_local = now_local.replace(
        hour=hours[0], minute=0, second=0, microsecond=0)
    today_plan = today_plan_local - tz          # 换回 UTC 比较

    last = s.last_run_at
    if last is not None:
        # 今天已经跑过（且是在今天的计划时刻之后跑的）→ 不重复
        if last >= today_plan:
            return False
        # 兜底：距上次超过 周期+宽限，即使计划时刻算错也要补一次
        grace = timedelta(hours=6)
        if now - last >= timedelta(days=1) + grace:
            return True
    # 计划时刻已到（今天还没跑）→ 到期
    return now >= today_plan


def _is_keepalive_hour(now: datetime) -> bool:
    """当前（本地）小时是否命中保活整点。

    .. deprecated:: 判断「该不该跑」请用 `_keepalive_due`。
       保留本函数只因它出现在既有调用方与文档里；语义已改为**本地时区**。
    """
    hours = set(settings.KEEPALIVE_HOURS)
    return _local_now().hour in hours


_scheduler_lock = threading.Lock()
_scheduler_started = False


def ensure_ban_recheck_schedule(db):
    """补齐「风控封号复核」定时任务（幂等）。

    每 30 分钟一轮；真正的探测频率由 admin/ban_recheck.py 的退避控制
    （每号 ≥ BAN_RECHECK_MIN_INTERVAL，持续仍封则指数退避）。
    ADMIN_BAN_RECHECK_ENABLED=0 时禁用任务（保留后台手动触发能力）。
    """
    row = db.query(Schedule).filter(Schedule.task == "ban_recheck").first()
    if not settings.BAN_RECHECK_ENABLED:
        if row is not None and row.enabled:
            row.enabled = 0
            db.commit()
        return
    if row is None:
        db.add(Schedule(name="风控封号复核与自动解封", task="ban_recheck",
                        interval_minutes=30, enabled=1,
                        next_run_at=datetime.utcnow() + timedelta(minutes=2)))
        db.commit()


def start_scheduler():
    """在 FastAPI 启动时调用：播种默认任务并拉起守护线程。

    幂等：重复调用只会有一个调度线程。uvicorn --reload 或多 worker 场景下
    会多次触发 startup，没有这个守卫就会起多个线程、把定时任务重复执行。
    """
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True
    try:
        db = SessionLocal()
        seed_defaults(db)
        ensure_daily_checkin(db)
        ensure_growth_schedules(db)
        ensure_keepalive_schedule(db)
        ensure_credit_schedule(db)
        ensure_ban_recheck_schedule(db)
        ensure_growth_plan_schedule(db)
        # DeepSeek's time-limited compensation endpoint expired; retire old
        # schedules so a legacy database never keeps polling it.
        db.query(Schedule).filter(Schedule.task.in_(("claim_benefits", "claim_compensation"))).delete(
            synchronize_session=False)
        db.commit()
        from admin.growth_center import import_last_run
        import_last_run(db)
        db.close()
    except Exception:
        pass
    t = threading.Thread(target=_loop, daemon=True, name="wb-scheduler")
    t.start()
