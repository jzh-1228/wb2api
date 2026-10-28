"""Calendar cycles use Beijing time; database timestamps remain naive UTC."""
import calendar
import json
import re
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, validator


class ScheduleTiming(BaseModel):
    mode: Literal["preset", "interval", "daily", "weekly", "monthly"] = "preset"
    time: str = "09:00"
    weekdays: list[int] = Field(default_factory=lambda: [0])
    monthday: int = Field(default=1, ge=1, le=31)

    # Monday=0, matching datetime.weekday().
    @validator("time")
    def valid_time(cls, value):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("执行时间格式必须为 HH:MM")
        return value

    # Use the validator API shared by desktop Pydantic 2 and Android Pydantic 1.
    @validator("weekdays")
    def valid_weekdays(cls, value):
        if not 1 <= len(value) <= 7 or any(day < 0 or day > 6 for day in value):
            raise ValueError("星期必须在周一至周日之间")
        return sorted(set(value))


def timing_of(schedule) -> ScheduleTiming:
    raw = getattr(schedule, "timing_config", "") or ""
    return ScheduleTiming(**json.loads(raw)) if raw else ScheduleTiming()


def next_run_at(schedule, now: datetime) -> datetime:
    timing = timing_of(schedule)
    if timing.mode == "preset" and schedule.task == "keepalive_tokens":
        from admin.scheduler import _next_keepalive_at
        return _next_keepalive_at(now)
    if timing.mode in ("preset", "interval"):
        return now + timedelta(minutes=schedule.interval_minutes or 60)
    local = now + timedelta(hours=8)
    hour, minute = map(int, timing.time.split(":"))
    if timing.mode == "monthly":
        year, month = local.year, local.month
        for _ in range(2):
            day = min(timing.monthday, calendar.monthrange(year, month)[1])
            target = datetime(year, month, day, hour, minute)
            if target > local:
                return target - timedelta(hours=8)
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    for days in range(8):
        target = (local + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target > local and (timing.mode == "daily" or target.weekday() in timing.weekdays):
            return target - timedelta(hours=8)
    raise ValueError("无法计算下次执行时间")


def cycle_label(schedule) -> str:
    timing = timing_of(schedule)
    if timing.mode == "preset" and schedule.task == "keepalive_tokens":
        from admin.config import settings
        hours = sorted(set(h for h in settings.KEEPALIVE_HOURS if 0 <= h <= 23)) or [22]
        return "每天 " + "、".join(f"{h:02d}:00" for h in hours) + "（系统时区）"
    if timing.mode in ("preset", "interval"):
        minutes = schedule.interval_minutes or 60
        for size, unit in ((10080, "周"), (1440, "天"), (60, "小时")):
            if minutes % size == 0:
                return f"每 {minutes // size} {unit}"
        return f"每 {minutes} 分钟"
    if timing.mode == "daily":
        return f"每天 {timing.time}"
    if timing.mode == "weekly":
        days = "、".join("周" + "一二三四五六日"[day] for day in sorted(set(timing.weekdays)))
        return f"每{days} {timing.time}"
    return f"每月 {timing.monthday} 日 {timing.time}"


DEFAULT_NAMES = {
    "refresh_balances": ("整点刷新平台总积分", "刷新平台总积分"),
    "refresh_credits": ("每 2 小时更新积分到期快照", "更新积分到期快照"),
    "sync_models": ("每日同步模型列表", "同步模型列表"),
    "daily_checkin": ("每日签到领取积分", "签到领取积分"),
    "refresh_growth_tasks": ("每日更新成长任务列表", "更新成长任务列表"),
    "run_growth_tasks": ("每日自动做成长任务", "自动做成长任务"),
    "run_growth_plan": ("WorkBuddy 成长计划巡检", "WorkBuddy 成长计划巡检"),
    "keepalive_tokens": ("每日 token 保活刷新", "token 保活刷新"),
}


def display_name(schedule) -> str:
    original, base = DEFAULT_NAMES.get(schedule.task, ("", schedule.task))
    if not schedule.name or schedule.name in (original, base, "token 保活（按整点）"):
        return f"{cycle_label(schedule)} · {base}"
    return schedule.name
