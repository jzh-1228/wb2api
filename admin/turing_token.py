"""X-Device-Token 提供器（Python 侧）。

复用本机 WorkBuddy 桌面端自带的 Turing Shield SDK（原生模块）取得设备风控 Token，
仅供本机诊断使用。普通网关请求不会把服务器进程生成的 Token 注入上游，避免把
服务器环境误报成桌面客户端。

实现：调用项目根目录的 `turing_helper.js`（Node 脚本），该脚本 require 桌面端的
TuringShieldSDK 原生桥接并返回 token。token 带进程内缓存（默认 10 分钟），避免每次
请求都 fork 一个 Node 进程。

失败（桌面端未安装 / SDK 不支持 / 超时）时返回 None，调用方应优雅降级（不注入该头），
绝不影响主流程。

性能约定（重要）
----------------
诊断页可能调用 `get_device_token()`；它不在模型请求路径上。每次调用仍必须严格限制：

  * 成功结果缓存 600s（`_CACHE_TTL`）；
  * **失败结果也做负缓存**（`_FAIL_TTL`）—— 这是曾经的线上问题：Linux 服务器上没有
    桌面端，`node turing_helper.js` 每次都失败，但失败不写缓存，导致**每一个请求**都
    同步 fork 一次 node，事件循环被反复阻塞，nginx 侧表现为大量 upstream 超时；
  * 单次执行超时收紧到 `_RUN_TIMEOUT`（SDK 可用时是毫秒级返回，8s 足够；
    25s 的超时在异常情况下足以拖垮事件循环）；
  * 另提供 `warmup()`，供启动时在后台线程里预热，避免第一个真实请求承担这份阻塞。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

# 项目根目录（admin/ 的上一级）
_ROOT = Path(__file__).resolve().parent.parent
_HELPER = _ROOT / "turing_helper.js"

#: 成功结果的缓存时长（秒）。Turing SDK 自身也有缓存，这里再兜一层避免频繁 fork node。
_CACHE_TTL = 600
#: 失败结果的负缓存时长（秒）。必须有：否则每个请求都会同步 fork 一次 node。
_FAIL_TTL = 300
#: 单次 node 调用的硬超时（秒）。
_RUN_TIMEOUT = 8

_cache: dict = {"token": None, "ts": 0.0, "fail_ts": 0.0}
_lock = threading.Lock()
_warm_lock = threading.Lock()


def _node_bin() -> str | None:
    if os.getenv("WORKBUDDY_DESKTOP") == "1":
        from desktop.common import data_dir
        from desktop.runtime import node_dir
        private = node_dir(data_dir()) / "node.exe"
        if private.is_file():
            return str(private)
    return shutil.which("node") or shutil.which("node.exe")


def _mark_failure(now: float) -> None:
    with _lock:
        _cache["fail_ts"] = now
        _cache["token"] = None
        _cache["ts"] = 0.0


def get_device_token(force: bool = False) -> str | None:
    """返回本机设备风控 Token；取不到返回 None。

    force=True 时忽略缓存（含负缓存），重新向 SDK 索取（用于排查 / 测试）。
    """
    if os.getenv("WORKBUDDY_ANDROID") == "1":
        return None
    now = time.time()
    if not force:
        with _lock:
            if _cache["token"] and now - _cache["ts"] < _CACHE_TTL:
                return _cache["token"]
            # 负缓存命中：直接返回，避免在请求路径上同步 fork node 阻塞事件循环。
            if _cache["fail_ts"] and now - _cache["fail_ts"] < _FAIL_TTL:
                return None

        # A cache miss must never fork Node synchronously inside the ASGI event loop.
        # Serve the last token while a single background worker refreshes it.
        warmup()
        with _lock:
            return _cache["token"]

    node = _node_bin()
    if not node or not _HELPER.is_file():
        _mark_failure(now)
        return None

    env = dict(os.environ)
    # 不写死 SDK 目录：若用户显式设置了 WORKBUDDY_TURING_SDK_DIR 则下发，
    # 否则交给 turing_helper.js 按本机安装位置自动发现（不同用户安装目录不同）。
    # 注意：不要在此处兜底写死某个绝对路径，否则会覆盖 helper 的自动发现逻辑。
    #
    # 这里额外做一件事：把我们**已经**发现的安装目录 / 版本号 / channelId
    # 下发给 helper，避免 Node 侧再扫一遍盘符；同时让 helper 报的版本号
    # 与本机真实客户端一致（而不是 helper 里的兜底默认值）。
    try:
        from wb_install import WB
        inst = WB.install_dir()
        if inst and not env.get("WORKBUDDY_INSTALL_DIR"):
            env["WORKBUDDY_INSTALL_DIR"] = str(inst)
        if not env.get("WORKBUDDY_TURING_CHANNEL_ID"):
            env["WORKBUDDY_TURING_CHANNEL_ID"] = str(WB.turing_channel_id())
        if not env.get("WORKBUDDY_TURING_PRODUCT_NAME"):
            env["WORKBUDDY_TURING_PRODUCT_NAME"] = WB.turing_product_name()
        if not env.get("WORKBUDDY_TURING_VERSION"):
            env["WORKBUDDY_TURING_VERSION"] = WB.desktop_version()
    except Exception:
        # 发现失败不影响主流程：helper 自己还会再找一次。
        pass

    try:
        out = subprocess.run(
            [node, str(_HELPER)],
            capture_output=True, text=True, timeout=_RUN_TIMEOUT, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception:
        _mark_failure(now)
        return None

    if out.returncode != 0:
        _mark_failure(now)
        return None

    token: str | None = None
    try:
        data = json.loads(out.stdout)
        token = (data.get("token") or "").strip() or None
    except Exception:
        token = None

    if not token:
        _mark_failure(now)
        return None

    with _lock:
        _cache["token"] = token
        _cache["ts"] = time.time()
        _cache["fail_ts"] = 0.0
    return token


def warmup() -> None:
    """在后台线程里预热一次 token。

    启动时调用。这样「第一次请求」不必在事件循环里承担 fork node 的阻塞；
    服务器上没有桌面端时，也会顺手把负缓存填上，让后续请求直接走快速失败分支。
    幂等：重复调用只会真正执行一次（由 `_warm_lock` 保证）。
    """
    def _run() -> None:
        try:
            get_device_token(force=True)
        except Exception:
            pass
        finally:
            _warm_lock.release()

    if _warm_lock.acquire(blocking=False):
        t = threading.Thread(target=_run, daemon=True, name="wb-turing-warmup")
        try:
            t.start()
        except Exception:
            _warm_lock.release()
            raise


def clear_cache() -> None:
    """清除缓存（进程内）。调试用。"""
    with _lock:
        _cache["token"] = None
        _cache["ts"] = 0.0
        _cache["fail_ts"] = 0.0


def status() -> dict:
    """Return non-secret availability information for diagnostics.

    The token itself is deliberately never returned.  This lets the admin
    console distinguish a real native SDK token from a server-side fallback
    without making the credential or its cache contents editable.
    """
    now = time.time()
    with _lock:
        token_ts = float(_cache.get("ts") or 0.0)
        fail_ts = float(_cache.get("fail_ts") or 0.0)
        has_token = bool(_cache.get("token")) and now - token_ts < _CACHE_TTL
        failure_cached = bool(fail_ts) and now - fail_ts < _FAIL_TTL
    node = _node_bin()
    return {
        "available": has_token,
        "source": "native_sdk" if has_token else "unavailable",
        "checked": bool(token_ts or fail_ts),
        "node_available": bool(node),
        "helper_available": _HELPER.is_file(),
        "cached_failure": failure_cached,
    }
