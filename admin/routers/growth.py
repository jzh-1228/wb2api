"""成长计划任务：列表 / 参与 / 完成 / 领奖。

设计要点
--------
* **任务列表不绑账号**：任意一个可用登录态即可拉取任务定义，结果缓存在
  内存里供前端与定时任务复用（任务定义对所有账号一致，仓库里只存"定义"）。
* **完成操作按 task_code 走策略表**（admin/growth_plans.py）：只对已实测
  可行的任务发包，其余标记为需人工，避免盲目请求污染上游日志。
* **串行 + 限速**：逐个账号、逐个任务执行，中间 sleep，避免触发风控。
"""
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin import backend, growth_plans, jobrunner
from admin.config import settings
from admin.db import SessionLocal, get_db
from admin.models import Account
from admin.security import require_admin

# ⚠️ 必须整组挂 require_admin（安全审计「严重」项）。
# 历史实现的这个 router **一个鉴权依赖都没有**，于是：
#   * `/api/growth/accounts/{id}/tasks` 可以被匿名者从 id=1 递增枚举，
#     直接拿到全部账号的 uid 与**显示名**（实测泄露 180****7641 这类手机号）；
#   * `/api/growth/run-async`、`/run`、`/accept`、`/claim` 可以被匿名者直接调用，
#     在服务器上启动批量任务、真实消耗账号额度并写库 —— 不只是信息泄露，
#     是**有副作用**的未授权操作。
# 挂在 router 上而不是逐个接口，是为了杜绝「以后新增接口又忘了加」。
router = APIRouter(prefix="/api/growth", tags=["growth"],
                   dependencies=[Depends(require_admin)])

#: 任务定义缓存 {"tasks": [...], "synced_at": iso}
_task_cache: dict = {}
_task_cache_at = 0.0
_TASK_CACHE_TTL = 60.0

#: 单账号任务列表的短缓存（秒）。
#: 上游拉一次任务列表实测 1~3.3 秒，而前端「执行完 / 领奖完」都会立刻再拉一次；
#: 缓存一个很短的窗口能让连续刷新**秒回**，又不会让状态明显过时。
_ACCT_TASKS_TTL = 3.0
_acct_tasks_cache: dict = {}


def _invalidate_acct_tasks(account_ids) -> None:
    """清掉这些账号的任务缓存（做完任务 / 领完奖后必须调）。

    否则前端紧接着的刷新会拿到**旧的**状态，用户看到「刚做完却还显示未完成」。
    """
    for aid in (account_ids or []):
        _acct_tasks_cache.pop(aid, None)

#: 执行节流（秒）
_EVENT_GAP = 1.2          # 同一任务内两次触发之间
_ACCOUNT_GAP = 1.0        # 两个账号之间

#: 轮询参数：等上游落库时用「轮询到就绪」，而不是固定 sleep。
#: 超时只是兜底上限，正常情况远早于它返回。
_POLL_INTERVAL = 0.5      # 轮询间隔
_ACCEPT_TIMEOUT = 6.0     # 等 accept 参与状态落库
#: 等「任务变成可领取」的上限（秒）。
#: 上游把任务标成 completed 有延迟，跑完立刻查经常还是 in_progress ——
#: 这正是以前「做完了但没自动领、要手动再点」的根因。
#: 一旦查到可领取就立刻领（不等满这个时长）。
_CLAIM_WAIT = 8.0

_MAX_TIMES = 10           # 单任务最多触发次数上限

#: 单个账号的总时间预算（秒）。
#:
#: 实测（15 个可自动化任务、上游每次约 3s）跑完一个账号要约 310s：
#:   chat_5 / expert_5 / template_5 这类 MULTI 任务本身就要触发 5 次，
#:   每次「触发 + 间隔」约 4.2s，单个任务就 20s+。
#: 原来给 45s —— 结果必然在做完 2~3 个任务后就报「本账号超时，剩余任务留待下次」，
#: 用户得反复点好几次才能做完，这就是体感上的「老是超时」。
#:
#: 现在给 300s（5 分钟）：
#:   * 够跑完一个账号的全部可自动化任务；
#:   * 仍保留上限，异常账号不会无限拖住整个批量（这是当初加预算的目的）；
#:   * 定时任务/异步任务走后台线程，不受 HTTP 超时约束，长一点没问题。
#:     同步接口（后台「执行」按钮）如果账号多，仍建议用 run-async。
_ACCOUNT_BUDGET = 300.0

#: 同步执行时的软预算：超过就**停止接新任务**（但不打断当前任务）。
#:
#: 与 `_ACCOUNT_BUDGET` 的区别：那个是「单账号硬上限」，这个是给
#: 同步接口的「别让整个请求超过 nginx 60s」用的。异步/定时任务不受它限制。
_SYNC_SOFT_BUDGET = 50.0

#: 定时任务整批的预算（秒）。
#:
#: 为什么必须有：定时任务是**在调度线程里同步跑**的，跑多久就占多久。
#: xx 个账号 × 单账号最长 300s，最坏能把调度线程占住 1 个多小时 ——
#: 期间整点刷新余额、签到、token 保活**全都不会执行**。
#: 这就是「定时任务会不会卡住」的答案：会，而且卡的是整个调度器。
#:
#: 25 分钟足够跑完「少数账号有新任务」的日常情况
#: （已完成的老号走快速通道，每个约 1 秒，不占时间）。
#: 跑不完的账号如实记录「留待下次」，第二天继续。
_SCHEDULE_SOFT_BUDGET = 1500.0

#: 触发事件时优先使用的免费模型（0 倍率），用完再退回低倍率
_PREFERRED_MODELS = ["hy3", "hunyuan-chat"]


def _pick_free_model(db: Session) -> str:
    """挑一个免费（0 倍率）模型，没有则退回传参默认值。

    任务记账不依赖模型输出质量，用免费模型可以把成本压到 0。
    """
    from admin.models import ModelConfig
    for mid in _PREFERRED_MODELS:
        row = (db.query(ModelConfig)
               .filter(ModelConfig.model_id == mid, ModelConfig.enabled == 1)
               .first())
        if row and (row.credit_multiplier or 0) == 0:
            return mid
    row = (db.query(ModelConfig)
           .filter(ModelConfig.enabled == 1, ModelConfig.credit_multiplier == 0)
           .order_by(ModelConfig.model_id.asc()).first())
    return row.model_id if row else "hy3"


def _pick_candidates(db: Session) -> list[Account]:
    """拉取任务定义的候选账号：健康号优先，按 id 稳定排序。

    原来 `_pick_account` 永远返回 id 最小的 active 号 —— 那个号一旦 401
    （登录态死了但还没被禁用），任务列表就永远拉不到，也不会换号
    （线上实测：#4 长期霸占首选位，面板一直报 502）。这里改成：
      1. 先过滤掉「已确证登录态失效 / 冷却中」的号；
      2. 全都不健康时退回全部 active 兜底（总比直接 400 好）；
    拉取侧（growth_tasks）再按顺序逐个尝试，一个失败自动换下一个。
    """
    from admin.routers.proxy import _looks_login_dead
    rows = (db.query(Account)
            .filter(Account.status == "active")
            .order_by(Account.id.asc()).all())
    now = datetime.utcnow()
    healthy = [a for a in rows if not _looks_login_dead(a, now)
               and not any(getattr(a, f) is not None and getattr(a, f) > now
                           for f in ("cool_until", "breaker_until", "degrade_until"))]
    return healthy or list(rows)


def _pick_account(db: Session) -> Account | None:
    """挑一个可用账号（仅用于拉取任务定义，不写任何东西）。"""
    candidates = _pick_candidates(db)
    return candidates[0] if candidates else None


def _classify_all(tasks: list[dict]) -> list[dict]:
    return [growth_plans.classify(t) for t in tasks]


class AcceptIn(BaseModel):
    account_ids: list[int]
    task_codes: list[str]


class RunIn(BaseModel):
    account_ids: list[int] = []           # 空 = 全部可用账号
    task_codes: list[str] | None = None   # 为空表示「所有可自动完成的任务」


class ClaimIn(BaseModel):
    account_ids: list[int]
    task_codes: list[str] | None = None   # 为空表示「所有已完成待领取的任务」


@router.get("/tasks")
def growth_tasks(refresh: bool = False, db: Session = Depends(get_db)):
    """获取全量任务列表（带分级信息），不绑定具体账号。

    任务定义对所有账号一致，所以随便用一个可用登录态拉取即可。
    """
    global _task_cache_at
    if (_task_cache.get("tasks") and not refresh
            and time.monotonic() - _task_cache_at < _TASK_CACHE_TTL):
        cached = dict(_task_cache)
        cached["cached"] = True
        return cached

    acc = None
    raw = None
    last_error: Exception | None = None
    for candidate in _pick_candidates(db):
        acc = candidate
        try:
            with backend.AccountSession(acc.auth_json) as s:
                raw = s.growth_tasks()
            last_error = None
            break
        except Exception as e:
            # 这个号拉不动（典型是 401 登录态失效）→ 记录状态机后换下一个。
            # 不换号的话任务列表会永远卡在同一个死号上（线上实测过）。
            last_error = e
            try:
                from admin import maintenance
                maintenance.note_failure(db, acc, e)
                db.commit()
            except Exception:
                db.rollback()
    if not acc:
        raise HTTPException(400, "没有可用账号，无法拉取任务列表")
    if raw is None:
        # 所有候选都失败 → 退回旧缓存，避免面板整体不可用
        if _task_cache.get("tasks"):
            stale = dict(_task_cache)
            stale.update({"cached": True, "stale": True, "error": str(last_error)})
            return stale
        raise HTTPException(502, f"拉取任务列表失败: {last_error}")

    _task_cache.clear()
    _task_cache.update({
        "tasks": _classify_all(raw),
        "synced_at": datetime.utcnow().isoformat(timespec="seconds"),
        "source_account_id": acc.id,
        "cached": False,
    })
    _task_cache_at = time.monotonic()
    return dict(_task_cache)


def _run_accept(acc: Account, codes: list[str]) -> dict:
    """在一个账号上执行参与，并回写可能被刷新的凭据。"""
    with backend.AccountSession(acc.auth_json) as s:
        try:
            st = s.growth_accept(codes)
        finally:
            acc.auth_json = _updated(s, acc)
    return st


@router.post("/accept")
def growth_accept(payload: AcceptIn, db: Session = Depends(get_db)):
    """批量参与任务（未参与的任务不会累计进度）。"""
    results = []
    for aid in payload.account_ids:
        acc = db.get(Account, aid)
        if not acc:
            results.append({"account_id": aid, "ok": False, "msg": "账号不存在"})
            continue
        try:
            st = _run_accept(acc, payload.task_codes)
            results.append({"account_id": aid, "ok": True, "results": st})
        except Exception as e:
            results.append({"account_id": aid, "ok": False, "msg": str(e)})
        time.sleep(_ACCOUNT_GAP)
    db.commit()
    return {"results": results}


class _TaskState:
    """轮询用的任务状态缓存：在 min_interval 内复用上一次的列表结果。

    上游拉一次列表要 1~2 秒，如果轮询里每次都重新拉，既慢又浪费，
    还会让「8 秒超时」变成实际十几秒。这里缓存一个短窗口（默认 1 秒），
    既能看到状态变化，又不会把上游打爆。
    """

    def __init__(self, session, min_interval: float = 1.0):
        self._s = session
        self._min = min_interval
        self._ts = 0.0
        self._snapshot: list | None = None

    def _list(self, force: bool = False) -> list:
        now = time.monotonic()
        if force or self._snapshot is None or (now - self._ts) >= self._min:
            try:
                self._snapshot = self._s.growth_tasks()
                self._ts = time.monotonic()
            except Exception:
                if self._snapshot is None:
                    self._snapshot = []
                # 拉取失败时保留旧快照，等下次窗口再试
        return self._snapshot or []

    def seed(self, tasks: list) -> None:
        self._snapshot = tasks
        self._ts = time.monotonic()

    def get(self, task_code: str) -> dict | None:
        for t in self._list():
            if t.get("task_code") == task_code:
                return t
        return None

    def refresh(self) -> list:
        """强制拉一次最新列表（需要精确状态时用）。"""
        return self._list(force=True)


def _updated(session, acc: Account) -> str:
    """AccountSession 关闭前读回最新凭据（token 可能被刷新）。"""
    try:
        return session.updated_json()
    except Exception:
        return acc.auth_json


def run_accounts(account_ids: list[int], task_codes: list[str] | None,
                 db: Session, on_done=None, on_beat=None,
                 soft_budget: float | None = None,
                 on_task=None, on_task_start=None) -> dict:
    """Batch participation and verify progress once per account, then claim rewards.

    Never equate a successful event request with a completed upstream task.
    Connections and task snapshots are shared within an account, not across accounts.
    """
    model = _pick_free_model(db)
    batch_start = time.monotonic()
    account_ids = _ordered_ids(list(account_ids), db)
    out = []
    for aid in account_ids:
        acc = db.get(Account, aid)
        acc_log = {"account_id": aid, "name": acc.name if acc else "",
                   "model": model, "ok": True, "tasks": []}
        reward_records = []
        def report(item):
            acc_log["tasks"].append(item)
            if on_task:
                on_task(acc_log["name"], item)
        if soft_budget is not None and time.monotonic() - batch_start >= soft_budget:
            acc_log.update(ok=False, msg="整批已用满本次时间预算，剩余账号留待下次")
        elif not acc:
            acc_log.update(ok=False, msg="账号不存在")
        else:
            if on_beat:
                on_beat(acc_log["name"])
            deadline = time.monotonic() + _ACCOUNT_BUDGET
            if soft_budget is not None:
                deadline = min(deadline, batch_start + soft_budget)
            try:
                with backend.AccountSession(acc.auth_json) as s:
                    tasks = s.growth_tasks()
                    by_code = {t.get("task_code"): t for t in tasks}
                    gate = by_code.get("first_buddy")
                    if gate and gate.get("accept_status") not in ("completed", "claimed"):
                        if on_task_start:
                            on_task_start(acc_log["name"], {"title": "解锁首次对话与宠物领养"})
                        # The prerequisite requires an actual buddy instance, not just
                        # the first_buddy task status. Adopt through the official flow.
                        pet = s.run_cat_travel()
                        acc_log["buddy_gate"] = {"ok": pet.get("ok", False),
                                                 "msg": pet.get("summary", "")}
                        tasks = s.growth_tasks()
                        by_code = {t.get("task_code"): t for t in tasks}
                    state = _TaskState(s)
                    state.seed(tasks)
                    wanted = list(dict.fromkeys(task_codes or [
                        t.get("task_code") for t in tasks
                        if growth_plans.classify(t).get("actionable")
                        and t.get("accept_status") not in ("claimed", "completed")
                    ]))
                    accept_codes = [c for c in wanted if growth_plans.classify(by_code.get(c, {})).get("actionable")
                                    and by_code.get(c, {}).get("accept_status") == "not_accepted"]
                    accepted = s.growth_accept(accept_codes) if accept_codes else {}
                    if accept_codes:
                        blocked_by_pet = [c for c in accept_codes
                                          if accepted.get(c) not in ("accepted", "already_accepted")
                                          and "first_buddy" in s.last_accept_message(c)]
                        if blocked_by_pet:
                            if on_task_start:
                                on_task_start(acc_log["name"], {"title": "完成宠物领养前置条件"})
                            pet = s.run_cat_travel()
                            acc_log["buddy_gate"] = {"ok": bool(pet.get("ok")), "msg": pet.get("summary", "")}
                            accepted.update(s.growth_accept(blocked_by_pet))
                        state.refresh()
                        def ready():
                            return all((state.get(c) or {}).get("accept_status") not in
                                       (None, "", "not_accepted") for c in accept_codes
                                       if accepted.get(c) in ("accepted", "already_accepted"))
                        jobrunner.wait_for(ready, _ACCEPT_TIMEOUT, _POLL_INTERVAL)
                        by_code.update({t.get("task_code"): t for t in state._list()})
                    attempted = []
                    for code in wanted:
                        info = by_code.get(code) or {}
                        plan = growth_plans.plan_for(code, info)
                        item = {"task_code": code, "title": info.get("title") or code,
                                "level": plan.level}
                        if info.get("accept_status") == "claimed":
                            item.update(ok=True, skipped="已领取")
                            report(item)
                            continue
                        if info.get("accept_status") == "completed":
                            item.update(ok=True, status="completed", skipped="已完成，等待领奖")
                            report(item)
                            continue
                        if not growth_plans.classify(info).get("actionable") or not info:
                            item.update(ok=True, skipped=plan.reason or "任务不可自动执行")
                            report(item)
                            continue
                        if code in accept_codes and accepted.get(code) not in ("accepted", "already_accepted"):
                            msg = s.last_accept_message(code) or "参与任务失败"
                            item.update(ok=False, accept_failed=accepted.get(code) or "error",
                                        accept_message=msg, skipped=msg)
                            report(item)
                            acc_log["ok"] = False
                            continue
                        if info.get("accept_status") == "not_accepted":
                            item.update(ok=False, skipped="参与状态尚未落库，留待下次")
                            report(item)
                            acc_log["ok"] = False
                            continue
                        if time.monotonic() >= deadline:
                            item.update(ok=False, skipped="本账号超时，剩余任务留待下次")
                            report(item)
                            acc_log.update(ok=False, budget_exceeded=True)
                            continue
                        if on_task_start:
                            on_task_start(acc_log["name"], item)
                        prog = info.get("progress") or {}
                        target, current = prog.get("target"), prog.get("current") or 0
                        times = plan.times or (target - current if isinstance(target, int) and target > current else 1)
                        times = max(1, min(times, _MAX_TIMES))
                        fired = 0
                        try:
                            if plan.firer == "fire_design_canvas" and not s.ensure_ardot_connected():
                                raise RuntimeError("账号未能绑定 Ardot，需要先完成授权")
                            for i in range(times):
                                if time.monotonic() >= deadline:
                                    acc_log["budget_exceeded"] = True
                                    item["last_error"] = "本账号超时，留待下次"
                                    break
                                fn = getattr(s, plan.firer, None) if plan.firer else None
                                if plan.firer and fn is None:
                                    raise RuntimeError(f"缺少触发方法 {plan.firer}")
                                r = fn(**plan.firer_kwargs) if fn else s.growth_fire_event(plan.event_codes, model=plan.model or model)
                                if r.get("expert_id"):
                                    item.update(expert_id=r["expert_id"], expert_name=r.get("expert_name", ""))
                                if not r.get("ok"):
                                    item["last_error"] = r.get("msg") or "触发失败"
                                    break  # Do not repeat a timed-out event ten times.
                                fired += 1
                                if i < times - 1:
                                    time.sleep(_EVENT_GAP)
                        except Exception as e:
                            item["last_error"] = str(e)[:160]
                        item.update(fired=fired, times=times, status="verifying", ok=False)
                        if plan.model:
                            item["model"] = plan.model
                        attempted.append(item)
                        report(item)
                    # One final reconciliation replaces accept/verify/claim polling
                    # separately for every task. Refresh only after all event writes.
                    if on_task_start:
                        on_task_start(acc_log["name"], {"title": "核对任务进度与领取奖励"})
                    state.refresh()
                    pending = {item["task_code"] for item in attempted if item.get("fired")}
                    def completed():
                        return all((state.get(c) or {}).get("accept_status") in
                                   ("completed", "claimed") for c in pending)
                    if pending:
                        jobrunner.wait_for(completed, min(_CLAIM_WAIT, max(0, deadline-time.monotonic())), _POLL_INTERVAL)
                    latest = {t.get("task_code"): t for t in state._list()}
                    for item in attempted:
                        t = latest.get(item["task_code"]) or {}
                        prog = t.get("progress") or {}
                        item["progress"] = f"{prog.get('current')}/{prog.get('target')}"
                        item["status"] = t.get("accept_status")
                        item["ok"] = item["status"] in ("completed", "claimed")
                        if not item["ok"]:
                            item["skipped"] = item.get("last_error") or "上游进度尚未完成，留待下次"
                            acc_log["ok"] = False
                    claimable = [c for c, t in latest.items() if c and t.get("accept_status") == "completed"]
                    claimed = []
                    for i, code in enumerate(claimable):
                        if on_task_start:
                            on_task_start(acc_log["name"], {"title": f"领取奖励 {i+1}/{len(claimable)}"})
                        try:
                            r = s.growth_claim(code)
                            d = r.get("data") or {}
                            if r.get("ok") and not d.get("already_claimed") and int(d.get("credit") or 0) > 0:
                                from admin.growth_center import record_result
                                reward_records.append(record_result(db, acc.id, {"ok": True, "credit": int(d["credit"])}, source="task", action="任务奖励 " + str(code)[:30]))
                            claimed.append({"task_code": code, "ok": bool(r.get("ok")),
                                            "credit": d.get("credit") or 0, "energy": d.get("energy") or 0})
                        except Exception as e:
                            claimed.append({"task_code": code, "ok": False, "msg": str(e)[:120]})
                        if i < len(claimable) - 1:
                            time.sleep(_EVENT_GAP)
                    if claimed:
                        acc_log["claimed"] = claimed
                        acc_log["credit"] = sum(c["credit"] for c in claimed if c["ok"])
                        acc_log["energy"] = sum(c["energy"] for c in claimed if c["ok"])
                        failed = [c for c in claimed if not c["ok"]]
                        if failed:
                            acc_log.update(ok=False, claim_failed=failed)
                    if not wanted and not claimable:
                        acc_log.update(noop=True, noop_reason="无待完成或待领取的自动任务")
                    try:
                        bal = s.fetch_balance()
                        acc.balance_total = int(bal.get("total") or 0)
                        acc.balance_remain = int(bal.get("remain") or 0)
                        from admin.credit_journal import attach_balance
                        attach_balance(db, reward_records, acc.balance_remain)
                        acc.last_sync_at = datetime.utcnow()
                        acc_log.update(balance_total=acc.balance_total, balance_remain=acc.balance_remain)
                        from admin.routers.accounts import _sync_credit_snapshot
                        _sync_credit_snapshot(acc, s.fetch_credit_details())
                    except Exception as e:
                        acc_log["balance_error"] = str(e)[:120]
                    acc.auth_json = _updated(s, acc)
                db.commit()
            except Exception as e:
                db.rollback()
                acc_log.update(ok=False, msg=str(e)[:160])
        out.append(acc_log)
        if on_done:
            on_done(acc_log)
        if aid != account_ids[-1]:
            time.sleep(_ACCOUNT_GAP)
    _invalidate_acct_tasks(account_ids)
    return {"results": out}


def claim_accounts(account_ids: list[int], task_codes: list[str] | None,
                   db: Session) -> dict:
    """对一批账号领取奖励。供接口与定时任务共用。"""
    out = []
    # 与做任务同序（新 → 老），保证界面上的顺序稳定、可预期
    account_ids = _ordered_ids(list(account_ids), db)
    for aid in account_ids:
        acc = db.query(Account).get(aid)
        if not acc:
            out.append({"account_id": aid, "ok": False, "msg": "账号不存在"})
            continue

        acc_log = {"account_id": aid, "name": acc.name, "ok": True,
                   "claimed": [], "total_credit": 0, "total_energy": 0}
        reward_records = []
        try:
            with backend.AccountSession(acc.auth_json) as s:
                if task_codes:
                    codes = list(task_codes)
                else:
                    tasks = s.growth_tasks()
                    codes = [t.get("task_code") for t in tasks
                             if t.get("accept_status") == "completed"]

                for i, code in enumerate(codes):
                    try:
                        r = s.growth_claim(code)
                        d = r.get("data") or {}
                        credit = d.get("credit") or 0
                        energy = d.get("energy") or 0
                        if r.get("ok") and not d.get("already_claimed") and int(credit) > 0:
                            from admin.growth_center import record_result
                            reward_records.append(record_result(db, acc.id, {"ok": True, "credit": int(credit)}, source="task", action="任务奖励 " + str(code)[:30]))
                        acc_log["claimed"].append({
                            "task_code": code,
                            "ok": bool(r.get("ok")),
                            "already": bool(d.get("already_claimed")),
                            "credit": credit,
                            "energy": energy,
                            "msg": r.get("msg") or "",
                        })
                        acc_log["total_credit"] += credit
                        acc_log["total_energy"] += energy
                    except Exception as e:
                        acc_log["claimed"].append(
                            {"task_code": code, "ok": False, "msg": str(e)})
                    # 只在**还有下一个**时才等：原来每次（含最后一次）都 sleep，
                    # 领 10 个任务就白等 12 秒 —— 这是「领奖卡卡的」主因之一。
                    if i + 1 < len(codes):
                        time.sleep(_EVENT_GAP)
                acc.auth_json = _updated(s, acc)

                # 领完**顺手刷一次真实余额**并落库。
                # 不刷的话前端拿到的还是旧余额，用户会以为「领了但没到账」
                # （实际到账了，只是库里没更新）。
                # 放在同一个 AccountSession 里，省掉重新建连的开销。
                try:
                    bal = s.fetch_balance()
                    acc.balance_total = int(bal.get("total", 0) or 0)
                    acc.balance_remain = int(bal.get("remain", 0) or 0)
                    from admin.credit_journal import attach_balance
                    attach_balance(db, reward_records, acc.balance_remain)
                    acc_log["balance_total"] = acc.balance_total
                    acc_log["balance_remain"] = acc.balance_remain
                    acc.last_sync_at = datetime.utcnow()
                    from admin.routers.accounts import _sync_credit_snapshot
                    _sync_credit_snapshot(acc, s.fetch_credit_details())
                except Exception as e:
                    acc_log["balance_error"] = str(e)[:120]
        except Exception as e:
            acc_log.update({"ok": False, "msg": str(e)})
        out.append(acc_log)
        # 账号之间保留间隔（并发打上游不礼貌），但单账号场景不该等：
        # 只有确实还有下一个账号时才睡。
        if len(account_ids) > 1:
            time.sleep(_ACCOUNT_GAP)

    _invalidate_acct_tasks(account_ids)
    db.commit()
    return {"results": out}


@router.post("/run")
def growth_run(payload: RunIn, db: Session = Depends(get_db)):
    """执行任务：自动参与 + 触发完成事件。

    只处理策略表里标记为可自动的任务；其它任务跳过并在返回里说明原因。

    ⚠️ 这是**同步**接口：账号多/任务多时会跑很久，容易撞 nginx 的 60s 超时。
    所以：
      * 单账号硬上限仍是 `_ACCOUNT_BUDGET`；
      * 这里额外传 `soft_budget=_SYNC_SOFT_BUDGET`，到点就**停止接新任务**
        （不打断当前任务），保证请求本身能及时返回；
      * 要一次跑完多个账号请用 `/run-async`（后台线程，不受此限）。

    `account_ids` 为空 = 全部可用账号（与 `/run-async` 同口径）。
    之前这里直接把空列表透传给 `run_accounts`，于是「不传账号」= 什么都不做，
    而 `/run-async` 的同一个参数却是「全部账号」—— 同一个字段两种语义，
    前端稍不注意就会「点了没反应」。
    """
    ids = _resolve_ids(payload.account_ids or None)
    return run_accounts(ids, payload.task_codes, db,
                        soft_budget=_SYNC_SOFT_BUDGET)


#: 后台任务 key：同 key 同时只允许一个在跑
JOB_KEY = "growth_run"


def _ordered_ids(ids: list[int], db: Session) -> list[int]:
    """把账号 id 按**创建时间倒序**（新 → 老）排好。

    为什么按创建时间而不是 id 大小：批量做任务时用户希望**先看到新号**的结果
    —— 新录入的号最可能需要处理，老号大多已经做完了。原先按 `id.asc()`
    排，结果是「新号排在最后，前面一堆老号在快速通道里空转」，
    用户盯着进度条等半天看不到自己刚加的号。

    用 id 作为次级键：id 是自增的，与创建顺序一致，
    且能保证同秒创建的账号也有稳定顺序（不会每次跑顺序都变）。
    """
    if not ids:
        return []
    rows = (db.query(Account.id, Account.created_at)
            .filter(Account.id.in_(ids)).all())
    created = {r.id: r.created_at for r in rows}
    # created_at 为空的排最后（理论上不该有，兜底不让它插队）
    return sorted(ids,
                  key=lambda i: (created.get(i) is None,
                                 -(created.get(i).timestamp() if created.get(i)
                                   else 0),
                                 -i))


def _resolve_ids(payload_ids: list[int] | None) -> list[int]:
    """把请求里的 ids 解析成实际要处理的账号 id 列表（空 = 全部 active）。

    返回顺序统一为**创建时间倒序（新 → 老）**，见 `_ordered_ids`。
    """
    db = SessionLocal()
    try:
        if payload_ids:
            return _ordered_ids(list(payload_ids), db)
        rows = (db.query(Account)
                .filter(Account.status == "active")
                .order_by(Account.created_at.desc(), Account.id.desc()).all())
        return [a.id for a in rows]
    finally:
        db.close()


@router.post("/run-async")
def growth_run_async(payload: RunIn):
    """异步批量做任务：立即返回 job_id，前端轮询进度。

    为什么异步：一个账号约 27 秒（串行 + 限速），xx 个账号要 6 分钟以上，
    同步等会让 nginx 先超时（默认 60s）→ 前端吃 504、体感「点了没反应」。

    重复点击不会叠起并发批量：同 key 已有任务在跑时直接返回现有 job。
    """
    running = jobrunner.RUNNER.get(JOB_KEY)
    if running and running.status == "running":
        return {"reused": True, **running.snapshot()}

    # 统一按创建时间倒序（新 → 老），与 /run 的 id 解析口径一致
    ids = _resolve_ids(payload.account_ids or None)
    tasks = payload.task_codes

    def worker(job: jobrunner.Job) -> dict:
        db = SessionLocal()
        try:
            job.set_phase("执行中")
            res = run_accounts(ids, tasks, db, on_done=job.add_item,
                               on_beat=job.beat,
                               on_task=job.task_beat, on_task_start=job.task_start)
        finally:
            db.close()
        results = res.get("results") or []
        credit = sum((r.get("credit") or 0) for r in results)
        energy = sum((r.get("energy") or 0) for r in results)
        done = sum(1 for r in results for t in (r.get("tasks") or [])
                   if t.get("ok") and not t.get("skipped"))
        return {"accounts": len(ids), "tasks_done": done,
                "credit": credit, "energy": energy,
                "failed": [r["account_id"] for r in results if not r.get("ok")]}

    job = jobrunner.RUNNER.start(JOB_KEY, len(ids), worker, title="批量做任务")
    return job.snapshot()


@router.get("/job/{job_id}")
def growth_job(job_id: str):
    """查询后台任务进度。"""
    job = jobrunner.RUNNER.by_id(job_id)
    if not job:
        raise HTTPException(404, "任务不存在或已过期")
    return job.snapshot()


@router.post("/claim")
def growth_claim(payload: ClaimIn, db: Session = Depends(get_db)):
    """批量领取奖励。

    task_codes 为空时，自动领取所有「已完成但未领取」（completed）的任务。
    """
    return claim_accounts(payload.account_ids, payload.task_codes, db)


@router.get("/accounts/{account_id}/tasks")
def account_tasks(account_id: int, refresh: bool = False,
                  db: Session = Depends(get_db)):
    """单个账号的任务完成情况（面板弹窗用）。

    短缓存：上游拉一次任务列表实测要 **1~3.3 秒**，而前端在「执行完 → 刷新」
    「领奖完 → 刷新」时会立刻再拉一次，用户体感就是「卡卡的」。
    这里缓存一个很短的窗口（默认 3 秒），既能让连续刷新秒回，
    又不至于让状态显示过时（执行完刷新时传 refresh=True 可强制绕过）。
    """
    if not refresh:
        hit = _acct_tasks_cache.get(account_id)
        if hit and (time.time() - hit[0]) < _ACCT_TASKS_TTL:
            data = dict(hit[1])
            data["cached"] = True
            return data

    acc = db.query(Account).get(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    try:
        with backend.AccountSession(acc.auth_json) as s:
            tasks = s.growth_tasks()
            profile = s.growth_profile()
    except Exception as e:
        # 拉取失败时退回旧缓存（哪怕过期），别让弹窗直接白屏
        hit = _acct_tasks_cache.get(account_id)
        if hit:
            data = dict(hit[1])
            data.update({"cached": True, "stale": True, "error": str(e)[:160]})
            return data
        raise HTTPException(502, f"查询失败: {e}")

    items = _classify_all(tasks)
    done = sum(1 for t in items if t.get("accept_status") in ("completed", "claimed"))
    #: 可领取奖励（completed 但未 claim）
    claimable = [t for t in items if t.get("accept_status") == "completed"]
    out = {
        "account_id": account_id,
        "name": acc.name,
        "tasks": items,
        "profile": profile,
        "summary": {
            "total": len(items),
            "done": done,
            "claimable": len(claimable),
            "claimable_credit": sum(t.get("reward_credit") or 0 for t in claimable),
            "actionable": sum(1 for t in items if t.get("actionable")
                              and t.get("accept_status") != "claimed"),
        },
    }
    _acct_tasks_cache[account_id] = (time.time(), out)
    # 简单防膨胀：条目远多于账号数时清掉最旧的
    if len(_acct_tasks_cache) > 500:
        for k in sorted(_acct_tasks_cache, key=lambda k: _acct_tasks_cache[k][0])[:200]:
            _acct_tasks_cache.pop(k, None)
    return out


@router.get("/plans")
def growth_plans_view():
    """任务策略表（分级依据），便于管理员了解哪些能自动完成。"""
    return {
        "levels": growth_plans.LEVEL_LABEL,
        "plans": [
            {"task_code": p.code, "level": p.level,
             "level_label": growth_plans.LEVEL_LABEL.get(p.level, ""),
             "event_codes": p.event_codes, "reason": p.reason,
             "actionable": p.actionable}
            for p in growth_plans.TASK_PLANS.values()
        ],
    }
