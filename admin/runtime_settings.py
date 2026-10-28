"""运行配置：把常用 .env 项搬进后台「设置」面板，运行时可改。

桌面 exe / Android apk 打包后没有可编辑的 .env —— 这类部署只能靠
后台动态调整。所有覆盖值存 SystemSetting（key 前缀 ``runtime:``），
启动时与保存后立即应用到 settings 单例上。

全部即时生效：中间件与文档开关都在请求路径上实时读取（2026-10-24 起），
保存当下即套用，无需重启。

环境变量永远是默认值来源；面板保存的是覆盖值，清掉覆盖即回落 .env。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from admin.config import settings
from admin.models import SystemSetting

_logger = logging.getLogger(__name__)

PREFIX = "runtime:"

#: bool / int / float / str
@dataclass(frozen=True)
class Field:
    attr: str
    label: str
    group: str
    kind: str
    desc: str = ""
    minimum: float | None = None
    maximum: float | None = None
    step: float = 1
    restart: bool = False   # True = 需要重启才真正生效（值已持久化）

    def placeholder(self) -> str:
        return f"{self.minimum}~{self.maximum}" if self.minimum is not None else ""


G_GATE = "网关与超时"
G_POOL = "并发与账号池"
G_COOL = "冷却与退避"
G_AUTH = "登录与安全"
G_ANTH = "Anthropic /v1/messages"
G_KEEP = "保活与封号复核"
G_LOG = "记录与上游标识"

CATALOG: list[Field] = [
    # ---- 网关与超时 ----
    Field("GATEWAY_REQUEST_TIMEOUT", "网关总时限（秒）", G_GATE, "float", "单个请求的端到端硬上限", 1, None, 1),
    Field("GATEWAY_WAIT_TIMEOUT", "排队等待上限（秒）", G_GATE, "float", "进池前的最长排队等待", 0, None, 1),
    Field("MAX_ROTATE", "单请求最多换号次数", G_GATE, "int", "transport 类失败每个号要等满流静默超时，调大线性拉长最坏等待", 1, None),
    Field("MAX_TOTAL_ATTEMPTS", "单请求总尝试上限", G_GATE, "int", "含所有账号与重试的总预算", 1, None),
    Field("STREAM_CONNECT_TIMEOUT", "上游连接超时（秒）", G_GATE, "float", "连不上就快速换号", 1, None, 0.5),
    Field("STREAM_IDLE_TIMEOUT", "流式静默超时（秒）", G_GATE, "float", "两次数据之间的最大间隔", 10, None, 5),
    Field("STREAM_WRITE_TIMEOUT", "上传写入超时（秒）", G_GATE, "float", "发送 prompt 的写超时", 10, None, 5),
    Field("STREAM_POOL_TIMEOUT", "连接池等待（秒）", G_GATE, "float", "池饱和时快速失败交给退避", 5, None, 1),
    # ---- 并发与账号池 ----
    Field("MAX_IN_FLIGHT", "单账号并发上限", G_POOL, "int", "0 = 不限；把并发摊到整个池子", 0, None),
    Field("STICKY_ENABLED", "会话粘性路由", G_POOL, "bool", "同一会话固定同一账号：上游前缀缓存命中更高、多轮更省"),
    Field("STICKY_TTL_SECONDS", "会话绑定空闲 TTL（秒）", G_POOL, "int", "滚动续期，空闲即释放", 60, None, 60),
    Field("GATEWAY_MAX_CONCURRENT", "每 Key 并发上限", G_POOL, "int", "0 = 不限", 0, None),
    Field("GATEWAY_RATE_PER_MINUTE", "每 Key 每分钟请求数", G_POOL, "int", "0 = 不限", 0, None, 10),
    Field("UPSTREAM_START_INTERVAL_MS", "同账号请求起步间隔（ms）", G_POOL, "int", "账号级启动节流，拟人化", 0, None, 25),
    Field("JOIN_MAX_CONCURRENT", "/join 注册并发", G_POOL, "int", "公开注册页的并发上限", 1, None),
    # ---- 冷却与退避 ----
    Field("SOFT_RATE_SECONDS", "软限流冷却基数（秒）", G_COOL, "int", "429/6000-6008 类；连续触发指数退避", 30, None, 30),
    Field("REQUEST_RATE_SECONDS", "请求速率限流冷却（秒）", G_COOL, "int", "code 14003 专用：瞬时抖动别放大成停摆", 5, None, 5),
    Field("BREAKER_THRESHOLD", "连续失败熔断阈值（次）", G_COOL, "int", "", 2, None),
    Field("SESSION_DEAD_THRESHOLD", "12153 判死阈值（次）", G_COOL, "int", "达到该次数才永久禁用，防上游抖动误杀", 1, None),
    Field("MODEL_BLOCK_SECONDS", "模型不存在负缓存（秒）", G_COOL, "int", "code 11102：换模型/换号才有意义", 600, None, 60),
    # ---- 登录与安全 ----
    Field("LOGIN_MAX_ATTEMPTS", "登录失败锁定阈值（次）", G_AUTH, "int", "(IP,用户名) 维度", 3, None),
    Field("LOGIN_IP_MAX_ATTEMPTS", "同 IP 全局阈值（次）", G_AUTH, "int", "0 = 自动取主维度 ×3", 0, None),
    Field("LOGIN_WINDOW_SECONDS", "计数窗口（秒）", G_AUTH, "int", "", 60, None, 60),
    Field("LOGIN_LOCK_SECONDS", "锁定时长（秒）", G_AUTH, "int", "", 60, None, 60),
    Field("TRUSTED_PROXY_IPS", "可信代理 IP 列表", G_AUTH, "str", "逗号分隔；仅这些直连对端的 X-Real-IP/XFF 被采信"),
    Field("ENABLE_DOCS", "开放 API 文档", G_AUTH, "bool", "/docs /openapi.json；安全审计建议保持关闭"),
    # ---- Anthropic ----
    Field("ANTHROPIC_DESENSITIZE", "harness 脱敏", G_ANTH, "bool", "关闭后 Claude Code 的合规声明词大概率被上游整条拒绝"),
    Field("ANTHROPIC_NO_COMPACT", "只脱敏不压缩", G_ANTH, "bool", "保留 system 原文，误拦风险略高"),
    Field("ANTHROPIC_MODEL_OPUS", "opus 档映射模型", G_ANTH, "str"),
    Field("ANTHROPIC_MODEL_SONNET", "sonnet 档映射模型", G_ANTH, "str"),
    Field("ANTHROPIC_MODEL_HAIKU", "haiku 档映射模型", G_ANTH, "str"),
    # ---- 保活与复核 ----
    Field("KEEPALIVE_ENABLED", "token 保活", G_KEEP, "bool"),
    Field("KEEPALIVE_ACCOUNT_GAP", "保活账号间隔（秒）", G_KEEP, "float", "批量保活拟人化间隔", 0, None, 0.1),
    Field("BAN_RECHECK_ENABLED", "封号自动复核", G_KEEP, "bool", "定时真调模型探测，通过即自动解封"),
    Field("BAN_RECHECK_MIN_INTERVAL", "复核最小间隔（秒）", G_KEEP, "int", "退避基数", 300, None, 60),
    Field("BAN_RECHECK_MAX_INTERVAL", "复核退避封顶（秒）", G_KEEP, "int", "", 1800, None, 60),
    # ---- 记录与上游 ----
    Field("LOG_REQUEST_PAYLOAD", "记录请求载荷", G_LOG, "bool", "排障用；含敏感内容，平时建议关"),
    Field("PAYLOAD_MAX_MB", "载荷截断上限（MB）", G_LOG, "int", "", 1, None),
    Field("COST_PER_TOKEN", "无回传时的积分系数", G_LOG, "float", "每千 token 积分", 0, None, 0.001),
    Field("UPSTREAM_CLIENT_NAME", "上游 client 列产品名", G_LOG, "str", "通过 X-IDE-Name 识别客户端"),
    Field("UPSTREAM_CLIENT_HEADER", "自定义客户端 IP 头", G_LOG, "str", "留空不发送（官方客户端不带 IP 头；填写会暴露代理特征，仅排障用）"),
]

BY_ATTR = {f.attr: f for f in CATALOG}

# 模块导入即快照 .env 默认值（此时还没套任何覆盖）。
_ENV_DEFAULTS: dict[str, object] = {f.attr: getattr(settings, f.attr, None) for f in CATALOG}


def _coerce(field: Field, raw: str):
    """字符串覆盖值 → 目标类型；越界/类型错误抛 ValueError（中文消息）。"""
    text = (raw or "").strip()
    if field.kind == "bool":
        if text in ("1", "true", "True", "on", "yes", "开"):
            return True
        if text in ("0", "false", "False", "off", "no", "关"):
            return False
        raise ValueError("布尔值请填 1/0")
    if field.kind == "int":
        value = int(float(text)) if text else 0
    elif field.kind == "float":
        value = float(text) if text else 0.0
    else:
        return text
    if field.minimum is not None and value < field.minimum:
        raise ValueError(f"不能小于 {field.minimum:g}")
    if field.maximum is not None and value > field.maximum:
        raise ValueError(f"不能大于 {field.maximum:g}")
    return value


def apply_overrides(db: Session) -> int:
    """把 DB 里的覆盖值套到 settings 单例上；坏值跳过并记日志。返回套用数。"""
    rows = db.query(SystemSetting).filter(SystemSetting.key.like(PREFIX + "%")).all()
    applied = 0
    for row in rows:
        attr = row.key[len(PREFIX):]
        field = BY_ATTR.get(attr)
        if field is None:
            continue
        try:
            setattr(settings, attr, _coerce(field, row.value))
            applied += 1
        except (ValueError, TypeError) as exc:
            _logger.warning("运行配置 %s=%r 非法已忽略：%s", attr, row.value, exc)
    return applied


def snapshot() -> list[dict]:
    """当前目录 + 生效值 + 默认值 + 是否覆盖，供面板渲染。"""
    return [{
        "attr": f.attr, "label": f.label, "group": f.group, "kind": f.kind,
        "desc": f.desc, "min": f.minimum, "max": f.maximum, "step": f.step,
        "restart": f.restart,
        "value": getattr(settings, f.attr, None),
        "default": _ENV_DEFAULTS.get(f.attr),
        "overridden": getattr(settings, f.attr, None) != _ENV_DEFAULTS.get(f.attr),
    } for f in CATALOG]


def save(db: Session, updates: dict) -> dict:
    """保存一批覆盖值。value 为 None/空串 = 清除覆盖回落 .env 默认。"""
    changed, reverted, errors = [], [], {}
    for attr, raw in (updates or {}).items():
        field = BY_ATTR.get(attr)
        if field is None:
            errors[attr] = "未知配置项"
            continue
        key = PREFIX + attr
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if db.query(SystemSetting).filter(SystemSetting.key == key).first():
                db.query(SystemSetting).filter(SystemSetting.key == key).delete()
            setattr(settings, attr, _ENV_DEFAULTS[attr])
            reverted.append(attr)
            continue
        try:
            value = _coerce(field, str(raw))
        except (ValueError, TypeError) as exc:
            errors[attr] = str(exc)
            continue
        row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
        if row is None:
            db.add(SystemSetting(key=key, value=str(value)))
        else:
            row.value = str(value)
        setattr(settings, attr, value)
        changed.append(attr)
    db.commit()
    apply_overrides(db)
    return {"changed": changed, "reverted": reverted, "errors": errors}
