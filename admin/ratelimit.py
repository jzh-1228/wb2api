"""登录防爆破：IP+用户名 双维度失败计数 + 同 IP 全局上限 + 临时锁定。

- 优先用 Redis（多进程/多实例共享计数）；连不上则退回进程内内存计数。
- 支持反向代理：仅当直连对端在 `ADMIN_TRUSTED_PROXY_IPS` 内时才采信
  X-Real-IP / X-Forwarded-For（见 `client_ip_from_request`）。
- 超出阈值后锁定一段时间，期间直接返回 429（带 Retry-After）。

三个计数维度（2026-10 重构，修「全局锁」DoS 问题）
------------------------------------------------
此前键只有 IP 一个维度。部署在 nginx 后且信任链配置不完整时，所有请求
的「可信 IP」都是反代地址 —— 任何人打 3 次错误密码就能把**真正的管理员**
锁在门外 15 分钟；公开域名后这等于送出一个免费勒索开关。

现在同时记三本账，**任一维度到阈值即锁对应维度**：

1. ``ip|username`` —— 主维度。攻击者锁的是「他自己的 IP × 他猜的用户名」，
   管理员从自己的 IP 登录完全不受影响（这正是本次修复的目标）。
2. ``ip`` —— 同 IP 全局上限（阈值更高，默认 15）。防一个 IP 撒网式
   换用户名爆破：单维度键会随用户名无限摊薄，全局上限兜住总量。
3. ``fp:<指纹摘要>``（可选）—— 客户端上报设备/浏览器指纹时额外记一本。
   指纹可伪造，所以它**只收紧、不放松**：分布式换 IP 但共用同一指纹的
   爆破会被这本账拦住；不带头则完全不影响。

锁定判定按「最紧的先报」：任一维度在锁定期内即 429。登录成功清空
该 (ip, username) 的成对计数与该 IP 的全局计数。
"""
from __future__ import annotations

import hashlib
import time
from threading import Lock
from typing import Iterable, Optional

from admin.config import settings

try:
    if settings.GATEWAY_LIMIT_BACKEND == "memory":
        raise RuntimeError("Desktop uses memory rate limits")
    import redis as _redis_mod

    _redis = _redis_mod.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
    _redis.ping()
    _USE_REDIS = True
except Exception:  # pragma: no cover - 降级到内存
    _redis = None
    _USE_REDIS = False

_lock = Lock()
_memory_fails: dict[str, list[float]] = {}   # key -> 失败时间戳列表
_memory_locked: dict[str, float] = {}        # key -> 锁定到期时间戳


def get_client_ip(forwarded_for: Optional[str], direct_ip: Optional[str]) -> str:
    """取**展示用**客户端 IP：反向代理场景用 X-Forwarded-For 首段。

    仅用于审计展示（日志里的来源 IP）。安全判定请用
    `client_ip_from_request()` —— 首段是可以被客户端随便伪造的。
    """
    if forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first:
            return first
    return direct_ip or "unknown"


def get_trusted_client_ip(real_ip: Optional[str], forwarded_for: Optional[str],
                          direct_ip: Optional[str]) -> str:
    """取不可伪造的来源 IP（旧接口，保留给未带 Request 的调用方）。

    优先级：X-Real-IP（nginx 覆写）→ XFF 最后一段（nginx 追加的真实对端）
    → 直连地址。**注意**：这里无法校验「直连对端是否可信代理」，调用方
    必须自己保证该请求经由可信反代进入；带 Request 的新代码请改用
    `client_ip_from_request()`。
    """
    if real_ip and real_ip.strip():
        return real_ip.strip()
    if forwarded_for:
        parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return direct_ip or "unknown"


def client_ip_from_request(request) -> str:
    """登录/风控判定的权威 IP 来源（X-Forwarded-For 信任链）。

    委托 `admin.client_ip.request_ip`：**仅当**直连 socket 对端在
    `ADMIN_TRUSTED_PROXY_IPS`（默认 127.0.0.1,::1）内时，才采信反代
    覆写过的 X-Real-IP / X-Forwarded-For 末段；否则一律用直连地址。
    这堵住两个老问题：
      1. 客户端自带假 X-Forwarded-For 首段绕过/摊薄锁定（旧 get_client_ip）；
      2. 不校验对端就信 X-Real-IP —— 任何直连攻击者都能自报 IP。
    """
    from admin.client_ip import request_ip
    try:
        return request_ip(request)
    except Exception:
        return (request.client.host if request and request.client else None) or "unknown"


# ---------------------------------------------------------------------------
# 键的构造：三个维度共用同一套 Redis/内存读写
# ---------------------------------------------------------------------------

def _pair_key(ip: str, username: str) -> str:
    return f"{ip}|{(username or '').strip().lower()}"


def _fingerprint_key(fingerprints: Iterable[str]) -> Optional[str]:
    """把若干指纹串合成一个限流键；没有任何指纹时返回 None（不启用维度）。"""
    parts = sorted(p for p in fingerprints or [] if p)
    if not parts:
        return None
    digest = hashlib.sha256("\n".join(parts).encode("utf-8", "replace")).hexdigest()[:24]
    return f"fp:{digest}"


def _keys_for(ip: str, username: str, fingerprint: Optional[str]) -> list[str]:
    keys = [_pair_key(ip, username), ip]
    if fingerprint:
        keys.append(fingerprint)
    return keys


def _ip_max_attempts() -> int:
    """同 IP 全局上限：默认是单维度阈值的 3 倍，可用环境变量显式覆盖。"""
    raw = getattr(settings, "LOGIN_IP_MAX_ATTEMPTS", 0)
    return max(settings.LOGIN_MAX_ATTEMPTS, int(raw or settings.LOGIN_MAX_ATTEMPTS * 3))


def _threshold(key: str) -> int:
    return _ip_max_attempts() if _is_ip_global_key(key) else settings.LOGIN_MAX_ATTEMPTS


def _is_ip_global_key(key: str) -> bool:
    # 全局键就是裸 IP（不含 "|" 也不以 "fp:" 开头）。
    return key and "|" not in key and not key.startswith("fp:")


def _is_locked_key(key: str) -> tuple[bool, int]:
    if _USE_REDIS:
        ttl = _redis.ttl(f"wb:login:lock:{key}")
        if ttl and ttl > 0:
            return True, int(ttl)
        return False, 0
    now = time.time()
    until = _memory_locked.get(key, 0.0)
    if until > now:
        return True, int(until - now)
    return False, 0


def is_locked(ip: str, username: str = "", fingerprint: Optional[str] = None) -> tuple[bool, int]:
    """返回 (是否处于锁定中, 还需等待的秒数)。

    任一维度在锁定期内即视为锁定，报出**最短**的等待秒数
    （对用户最宽松、信息泄露最少的选择）。
    """
    wait = 0
    for key in _keys_for(ip, username, fingerprint):
        locked, seconds = _is_locked_key(key)
        if locked:
            wait = seconds if wait == 0 else min(wait, seconds)
    return (wait > 0), wait


def record_failure(ip: str, username: str = "", fingerprint: Optional[str] = None) -> None:
    """记录一次失败登录；任一维度超阈值则锁该维度。"""
    global _memory_fails, _memory_locked
    for key in _keys_for(ip, username, fingerprint):
        threshold = _threshold(key)
        if _USE_REDIS:
            fkey = f"wb:login:fail:{key}"
            cnt = _redis.incr(fkey)
            if cnt == 1:
                _redis.expire(fkey, settings.LOGIN_WINDOW_SECONDS)
            if cnt >= threshold:
                _redis.set(f"wb:login:lock:{key}", "1", ex=settings.LOGIN_LOCK_SECONDS)
            continue
        with _lock:
            now = time.time()
            fails = [t for t in _memory_fails.get(key, []) if now - t < settings.LOGIN_WINDOW_SECONDS]
            fails.append(now)
            _memory_fails[key] = fails
            if len(fails) >= threshold:
                _memory_locked[key] = now + settings.LOGIN_LOCK_SECONDS
            # 过期条目顺手清理，防止键空间随攻击维度无限增长。
            if len(_memory_fails) > 10000 or len(_memory_locked) > 10000:
                _memory_fails = {k: v for k, v in _memory_fails.items()
                                 if v and now - v[-1] < settings.LOGIN_WINDOW_SECONDS}
                _memory_locked = {k: t for k, t in _memory_locked.items() if t > now}


def clear_failures(ip: str, username: str = "", fingerprint: Optional[str] = None) -> None:
    """登录成功后清空对应维度的失败计数与锁定。"""
    for key in _keys_for(ip, username, fingerprint):
        if _USE_REDIS:
            _redis.delete(f"wb:login:fail:{key}", f"wb:login:lock:{key}")
            continue
        with _lock:
            _memory_fails.pop(key, None)
            _memory_locked.pop(key, None)
