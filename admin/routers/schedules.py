"""定时任务管理：列表 / 新增 / 修改 / 删除 / 启停 / 立即运行。"""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from admin.db import get_db
from admin.models import Schedule
from admin.scheduler import run_task, _dump_result, start_growth_plan
from admin.security import require_admin
from admin.schedule_timing import ScheduleTiming, timing_of, next_run_at, cycle_label, display_name

router = APIRouter(prefix="/api/schedules", tags=["schedules"])

TASK_CHOICES = ["refresh_balances", "refresh_credits", "sync_models", "daily_checkin",
                "refresh_growth_tasks", "run_growth_tasks", "run_growth_plan", "keepalive_tokens",
                "ban_recheck"]
TASK_LABELS = {
    "refresh_balances": "刷新平台总积分",
    "refresh_credits": "更新积分到期快照",
    "sync_models": "同步模型列表",
    "daily_checkin": "签到领取积分",
    "refresh_growth_tasks": "更新成长任务列表",
    "run_growth_tasks": "自动做成长任务",
    "run_growth_plan": "WorkBuddy 成长计划巡检",
    "keepalive_tokens": "token 保活刷新",
    "ban_recheck": "风控封号复核与自动解封",
}


def _parse_stop_after(value: str | None):
    """把 ISO 字符串解析成 datetime；空 / 非法返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


class ScheduleIn(BaseModel):
    name: str = ""
    task: str = "refresh_balances"
    interval_minutes: int = Field(default=60, ge=1, le=5256000)
    timing: Optional[ScheduleTiming] = None
    enabled: int = 1
    stop_after: Optional[str] = None  # ISO 时间字符串（daily_checkin 用）


class SchedulePatch(BaseModel):
    name: Optional[str] = None
    task: Optional[str] = None
    interval_minutes: Optional[int] = Field(default=None, ge=1, le=5256000)
    timing: Optional[ScheduleTiming] = None
    enabled: Optional[int] = None
    stop_after: Optional[str] = None  # ISO 时间字符串（daily_checkin 用）


@router.get("")
def list_schedules(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.query(Schedule).order_by(Schedule.id.asc()).all()
    now = datetime.utcnow()
    return {"items": [
        {
            "id": s.id,
            "name": s.name,
            "display_name": display_name(s),
            "timing": timing_of(s).dict(),
            "cycle_label": cycle_label(s),
            "task": s.task,
            "task_label": TASK_LABELS.get(s.task, s.task),
            "interval_minutes": s.interval_minutes,
            "enabled": bool(s.enabled),
            "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
            "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
            "last_result": s.last_result or "",
            "stop_after": s.stop_after.isoformat() if s.stop_after else None,
            "due_soon": bool(s.enabled and (s.next_run_at is None or s.next_run_at <= now)),
        }
        for s in rows
    ]}


@router.post("")
def create_schedule(body: ScheduleIn, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    if body.task not in TASK_CHOICES:
        raise HTTPException(status_code=400, detail="未知任务类型")
    s = Schedule(
        name=body.name or TASK_LABELS.get(body.task, body.task),
        task=body.task,
        interval_minutes=max(1, body.interval_minutes),
        enabled=body.enabled,
        stop_after=_parse_stop_after(body.stop_after),
        next_run_at=datetime.utcnow(),
    )
    if body.timing is not None:
        _set_timing(s, body.timing)
    if timing_of(s).mode != "preset":
        s.next_run_at = next_run_at(s, datetime.utcnow())
    db.add(s)
    db.commit()
    db.refresh(s)
    return {"id": s.id, "ok": True}


def _set_timing(schedule, timing):
    schedule.timing_config = timing.json()


@router.patch("/{sid}")
def patch_schedule(sid: int, body: SchedulePatch, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    s = db.query(Schedule).filter(Schedule.id == sid).first()
    if not s:
        raise HTTPException(status_code=404, detail="任务不存在")
    was_enabled = bool(s.enabled)
    if body.name is not None:
        s.name = body.name
    if body.task is not None:
        if body.task not in TASK_CHOICES:
            raise HTTPException(status_code=400, detail="未知任务类型")
        s.task = body.task
    if body.interval_minutes is not None:
        s.interval_minutes = max(1, body.interval_minutes)
    if body.timing is not None:
        _set_timing(s, body.timing)
    if body.enabled is not None:
        s.enabled = body.enabled
    if body.timing is not None or body.interval_minutes is not None or body.task is not None or (body.enabled and not was_enabled):
        s.next_run_at = next_run_at(s, datetime.utcnow())
    if "stop_after" in body.__fields_set__:
        s.stop_after = _parse_stop_after(body.stop_after)
    db.commit()
    return {"id": s.id, "ok": True}


@router.post("/{sid}/toggle")
def toggle_schedule(sid: int, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    s = db.query(Schedule).filter(Schedule.id == sid).first()
    if not s:
        raise HTTPException(status_code=404, detail="任务不存在")
    s.enabled = 0 if s.enabled else 1
    if s.enabled:
        s.next_run_at = next_run_at(s, datetime.utcnow())
    db.commit()
    return {"id": s.id, "enabled": bool(s.enabled)}


@router.post("/{sid}/run")
def run_now(sid: int, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """立即执行一次（后台「执行」按钮）。

    与调度器写的是同一份 last_result —— 所以必须复用 `_dump_result`，
    不能在这里再写一次 `json.dumps(...)[:500]`：截断会产出非法 JSON，
    前端 `JSON.parse` 失败，用户就看到「一堆 json 数据」（其实是半个）。
    """
    s = db.query(Schedule).filter(Schedule.id == sid).first()
    if not s:
        raise HTTPException(status_code=404, detail="任务不存在")
    now = datetime.utcnow()
    from admin.pool import POOL
    if POOL.waf.active() and s.task != "keepalive_tokens":
        result = {"task": s.task, "ok": True, "deferred": True,
                  "summary": "出口保护期间顺延任务，保护解除后重试"}
        s.last_result = _dump_result(result)
        s.next_run_at = now + timedelta(seconds=max(15, POOL.waf.remaining()))
        db.commit()
        return {"id": s.id, "result": result}
    if s.task == "run_growth_plan":
        return {"id": s.id, "result": start_growth_plan(s, db)}
    try:
        _run = run_task(s.task, db, s)
        s.last_result = _dump_result(_run)
    except Exception as e:
        # 手动执行失败同样要落库，否则界面还显示上一次的成功结果，
        # 用户会以为这次也成功了。
        _run = {"ok": False, "error": str(e)}
        s.last_result = _dump_result(_run)
    s.last_run_at = now
    s.next_run_at = (now + timedelta(minutes=1) if s.task == "keepalive_tokens"
                     and _run.get("retry_pending") else next_run_at(s, now))
    db.commit()
    return {"id": s.id, "result": _run}


@router.delete("/{sid}")
def delete_schedule(sid: int, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    s = db.query(Schedule).filter(Schedule.id == sid).first()
    if not s:
        raise HTTPException(status_code=404, detail="任务不存在")
    db.delete(s)
    db.commit()
    return {"id": sid, "ok": True}
