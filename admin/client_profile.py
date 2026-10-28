#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端参数档案（UA / 版本号 / 可选头 / 桌面指纹）——探测、保存、生效、同步。

要解决什么问题
--------------
客户端业务可能涉及一批参数：UA、桌面端与 CLI 版本号、
已从官方源码确认的可选头（`X-CodeBuddy-Request` 等）、桌面事件指纹（ideName / os / arch /
cpuCores / commit / releaseDate …）。

这些值散落在代码与 `.env` 里，带来两个实际麻烦：

1. **本地探测到的值传不到线上**。线上服务器没装桌面端，`wb_install` 探测
   不到任何东西，只能退化成内置兜底值 —— 于是线上的 UA 报的是一个
   「谁也不认识的版本」。而本地机器明明装得好好的。
2. **改一个值要改代码或重启**。想在后台把 osVersion 调一下，得改 `.env`
   再重启进程。

所以这里把「客户端参数」收敛成**一份档案（profile）**，支持：

    探测(snapshot) → 保存(saved) → 生效(effective) → 同步(sync push)

取值优先级（`effective`）
------------------------
    1. 环境变量          显式运维覆盖，永远最高（如 WORKBUDDY_DESKTOP_VERSION）
    2. 现场探测 / 已保存   取决于 source 设置（见下）
    3. 内置兜底          保证任何情况下都有值，绝不抛异常

`source` 有两种模式，因为「本地」和「线上」的最优选择正好相反：

    auto  （默认）现场探测 > 已保存 > 兜底
          本机装了客户端时用真实值（版本升级后自动跟上）。
    saved 已保存 > 现场探测 > 兜底
          显式钉住后台里保存的值。线上实例必须用这个 —— 它探测不到
          客户端，只能靠同步过来的档案。

为什么读 DB 还要内存缓存
------------------------
`effective()` 在**每个请求**上被调用（构造上游头、拼桌面指纹）。
每次查库不可接受，所以进程内缓存 30 秒，保存时立即失效。
多进程部署下最坏有 30 秒延迟，对「改配置」这种低频操作完全够用。
"""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

#: 项目根加入 sys.path，便于导入根目录的 wb_install
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

#: SystemSetting 里的键名
PROFILE_KEY = "client_profile"          # 档案本体（JSON 字符串）
SOURCE_KEY = "client_profile_source"    # auto | saved

SOURCE_AUTO = "auto"
SOURCE_SAVED = "saved"
_VALID_SOURCES = (SOURCE_AUTO, SOURCE_SAVED)

#: DB 缓存时长（秒）。保存时会立即失效，所以这只是多进程下的收敛延迟。
_CACHE_TTL = 30.0

# ---------------------------------------------------------------------------
# 默认值：协议兼容参考值，不代表运行服务的真实设备环境。
#
# 只有现场探测或管理员显式保存的值才有对应来源；服务器没有客户端时，
# OS 与硬件字段仍是兼容默认值，不能当作服务器的真实信息。
# 值可由探测结果 / 后台保存值 / 环境变量覆盖。
# ---------------------------------------------------------------------------
DEFAULTS: dict[str, Any] = {
    # --- 版本（决定 UA 与 X-IDE-Version）---
    # 兜底参考值取自最近一次实测的安装包（2026-10：D:\WorkBuddy 5.9.0 / CLI 2.165.0）。
    # 本机装了客户端时探测值会自动盖过它们；线上实例应靠同步档案钉住。
    "desktop_version": "5.9.0",
    "cli_version": "2.165.0",
    # --- 风控 SDK ---
    "turing_product_name": "WorkBuddy",
    # --- 用量归属（X-IDE-Name / X-IDE-Type / X-Product）---
    "ide_name": "WorkBuddy",
    "ide_type": "WorkBuddy",
    #: 请求头 `X-Product` 的值。
    #:
    #: **不是产品名，是部署形态。** 官方客户端的全局
    #: `ProductEndpointHttpInterceptor` 写死为
    #:     headers["X-Product"] ||= configuration?.deploymentType ?? "SaaS"
    #: WorkBuddy 桌面端的 `product.json` 里 `deploymentType` 就是 "SaaS"。
    #: （唯一硬编码 "WorkBuddy" 的地方是 `stdio-mcp-inspector.js` 里对
    #: `/v2/activity/workbuddy/banner` 那个窄接口，模型请求不走那条路。）
    #: 原来这里填 "WorkBuddy" 是错的 —— 官方客户端不会这样发，
    #: 本身就是个可识别的差异。
    "product": "SaaS",
    #: UA 第一段的产品名（product.json 的 applicationName）。官方在
    #: app-instance.js 里用它覆写 electron 的 userAgentFallback，拼出
    #: `${applicationName}/${version} ...`。
    "application_name": "WorkBuddy",
    #: 桌面事件指纹里的 `product` 字段（遥测用，取 deploymentType）。
    #: **与请求头的 X-Product 不是一回事**：这里是事件里的产品形态。
    "fp_product": "SaaS",
    # --- 官方客户端请求中观察到的兼容头（按适用端点发送）---
    "headers": {
        "X-CodeBuddy-Request": "1",
        "X-Requested-With": "XMLHttpRequest",
        "Accept-Language": "zh-CN",
        # X-Private-Data **默认不发**（2026-10 实测抓包：官方 CLI 的模型请求
        # 不带它）。源码里它由 `resolveModelOptimizationEnabled` 拼出
        # （优化开启→"false"），随后被 `applyPrivateDataHeaderPolicy` 按账号
        # 类型/服务端策略剥离 —— 个人账号的线上真实流量里没有这个头。
        # 企业账号如需对齐「优化关闭」上报，可在后台把它加回（值 "true"）。
    },
    # --- 桌面事件指纹 ---
    "ext_name": "workbuddy-desktop",
    "os": "win32",
    "arch": "x64",
    "os_version": "10.0.26220",
    "cpu_cores": 20,
    "memory_size": 24,
    "timezone": "Asia/Shanghai",
    "report_delay": 2000,
    # --- 内嵌 OpenAI SDK（stainless JS 6.25.0）的可选指纹资料 ---
    # 仅在实际客户端业务确认使用该 SDK 时展示/传递；普通模型网关请求不自动带它。
    # 取值公式见 stainless_headers()；node_version 可由本机安装的官方客户端
    # 精确探测（ELECTRON_RUN_AS_NODE），兜底值取自 2026-10 实测（Electron
    # 37.10.3 内嵌 Node 22.21.1）。
    "node_version": "v22.21.1",
    "stainless_package_version": "6.25.0",
}

#: 值为非负整数的键（保存时按此校验/转换）
_INT_KEYS = ("cpu_cores", "memory_size", "report_delay", "turing_channel_id",
             "release_date_ms")
#: 值为字符串的键
_STR_KEYS = ("desktop_version", "cli_version", "turing_product_name",
             "ide_name", "ide_type", "product", "application_name", "fp_product",
             "ext_name", "os", "arch", "os_version", "timezone", "commit",
             "node_version", "stainless_package_version")
#: 字符串字段的最大长度（防止把整个文件塞进配置里）
_MAX_STR = 512
#: 风控头名/值的最大长度
_MAX_HEADER = 256


def _int_or_none(v) -> int | None:
    """把值转成非负整数；失败返回 None。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


# ---------------------------------------------------------------------------
# 现场探测
# ---------------------------------------------------------------------------
def _snapshot_package() -> dict:
    """探测本机客户端**安装包**，返回只包含真实读到的键的档案。

    探测不到任何东西时返回空 dict —— 调用方据此判断「这台机器上没有客户端」。
    线上服务器就是这种情况，它靠同步过来的 saved 档案工作。

    **关键**：只返回「确实从安装包里读到的」值，绝不返回 `wb_install` 的兜底
    默认值。否则线上（没装客户端）会「探测到」一堆兜底值，被当成现场探测，
    在 source=auto 下反而盖掉刚同步过来的真实参数 —— 那就完全违背了本模块
    的初衷。所以这里逐项对照 `wb_install` 的取值来源来判断。

    绝不抛异常：探测失败按「没有客户端」处理。
    """
    out: dict[str, Any] = {}
    try:
        from wb_install import WB
    except Exception as e:
        _logger.debug("wb_install 不可用，跳过客户端探测：%s", e)
        return out

    try:
        # sources 记录每个值的真实来源；带「兜底」字样的一律不算探测到
        data = WB._data()
        srcs = data.get("sources") or {}

        def real(key: str) -> bool:
            """该键是否来自真实安装包（而非兜底默认值）。"""
            s = srcs.get(key) or ""
            return bool(s)

        if WB.is_version_dynamic():
            out["desktop_version"] = WB.desktop_version()
            out["cli_version"] = WB.cli_version()
        # 下面这几项来自 product.json / 安装目录，与「版本是否动态」无关，
        # 所以单独按各自来源判断；读不到就不放进探测结果。
        if real("turing_channel_id"):
            out["turing_channel_id"] = WB.turing_channel_id()
        if real("commit"):
            out["commit"] = WB.commit()
        if real("release_date_ms"):
            out["release_date_ms"] = WB.release_date_ms()
        # 产品身份三件套：同样只在安装包里真读到才采纳。
        #  * deploymentType -> 请求头 X-Product（部署形态，官方 WorkBuddy 为 "SaaS"）
        #  * applicationName -> UA 第一段的产品名
        #  * authentication.id -> 桌面事件指纹的 extName
        if real("deployment_type"):
            out["product"] = WB.deployment_type()
            # 遥测事件里的 product 字段与请求头同源（都取 deploymentType）
            out["fp_product"] = WB.deployment_type()
        if real("application_name"):
            out["application_name"] = WB.application_name()
        if real("plugin_name"):
            out["ext_name"] = WB.plugin_name()
        # 产品名只有产品包里写死了才可信；当前安装包里没有这个字段，
        # 所以不进探测结果（保留兜底值即可，不必伪装成探测到的）。
    except Exception as e:
        _logger.debug("客户端探测部分失败：%s", e)
    return out


# ---------------------------------------------------------------------------
# 本机系统探测（桌面事件指纹的硬件/系统字段）
# ---------------------------------------------------------------------------
#: Node `os.release()` / Electron `process.platform` / `process.arch` 的命名对齐。
_OS_NAMES = {"windows": "win32", "darwin": "darwin", "linux": "linux"}
_ARCH_NAMES = {"amd64": "x64", "x86_64": "x64", "x64": "x64",
               "arm64": "arm64", "aarch64": "arm64",
               "i386": "ia32", "i486": "ia32", "i586": "ia32",
               "i686": "ia32", "x86": "ia32"}

#: Windows 时区键名（GetDynamicTimeZoneInformation 的 TimeZoneKeyName，英文且
#: 不随系统语言变）-> IANA 名。只收常见且无歧义的；查不到就放弃（该字段
#: 退回兜底/保存值），绝不猜 —— 指纹里的时区报错比报默认值更显眼。
_WIN_TZ_TO_IANA = {
    "China Standard Time": "Asia/Shanghai",
    "Taipei Standard Time": "Asia/Taipei",
    "Hong Kong S.A.R. Standard Time": "Asia/Hong_Kong",
    "Macau S.A.R. Standard Time": "Asia/Macau",
    "Singapore Standard Time": "Asia/Singapore",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "SE Asia Standard Time": "Asia/Bangkok",
    "India Standard Time": "Asia/Kolkata",
    "Arabian Standard Time": "Asia/Dubai",
    "W. Australia Standard Time": "Australia/Perth",
    "E. Australia Standard Time": "Australia/Brisbane",
    "UTC": "UTC",
    "GMT Standard Time": "Europe/London",
    "W. Europe Standard Time": "Europe/Berlin",
    "Romance Standard Time": "Europe/Paris",
    "Central Europe Standard Time": "Europe/Prague",
    "Central European Standard Time": "Europe/Warsaw",
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
}


def _probe_timezone() -> str:
    """IANA 时区名，对齐官方取值 `Intl.DateTimeFormat().resolvedOptions().timeZone`。

    只做**确定**的读取（环境变量 / Windows 时区键名 / Linux-mac 的系统配置），
    读不到就返回空串 —— 不做 UTC 偏移反猜（同偏移的时区太多，猜了就是错的）。
    """
    tz = (os.environ.get("TZ") or "").strip()
    if tz and (tz == "UTC" or re.fullmatch(r"[A-Za-z0-9_+\-]+/[A-Za-z0-9_+\-]+", tz)):
        return tz
    if sys.platform == "win32":
        try:
            import ctypes

            class _SYSTEMTIME(ctypes.Structure):
                _fields_ = [("wYear", ctypes.c_ushort), ("wMonth", ctypes.c_ushort),
                            ("wDayOfWeek", ctypes.c_ushort), ("wDay", ctypes.c_ushort),
                            ("wHour", ctypes.c_ushort), ("wMinute", ctypes.c_ushort),
                            ("wSecond", ctypes.c_ushort), ("wMilliseconds", ctypes.c_ushort)]

            class _DTZI(ctypes.Structure):
                _fields_ = [("Bias", ctypes.c_long),
                            ("StandardName", ctypes.c_wchar * 32),
                            ("StandardDate", _SYSTEMTIME),
                            ("StandardBias", ctypes.c_long),
                            ("DaylightName", ctypes.c_wchar * 32),
                            ("DaylightDate", _SYSTEMTIME),
                            ("DaylightBias", ctypes.c_long),
                            ("TimeZoneKeyName", ctypes.c_wchar * 128),
                            ("DynamicDaylightTimeDisabled", ctypes.c_ubyte)]

            dtzi = _DTZI()
            kernel32 = ctypes.windll.kernel32
            kernel32.GetDynamicTimeZoneInformation.restype = ctypes.c_uint
            if kernel32.GetDynamicTimeZoneInformation(ctypes.byref(dtzi)) != 0xFFFFFFFF:
                return _WIN_TZ_TO_IANA.get(dtzi.TimeZoneKeyName, "")
        except Exception as e:
            _logger.debug("读取 Windows 时区失败：%s", e)
        return ""
    # POSIX：/etc/timezone（Debian 系）或 /etc/localtime 符号链接目标
    try:
        p = Path("/etc/timezone")
        if p.is_file():
            v = p.read_text(encoding="utf-8", errors="ignore").strip().splitlines()
            if v and v[0].strip():
                return v[0].strip()
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo" in link:
            return link.split("zoneinfo" + os.sep, 1)[-1].replace(os.sep, "/")
    except Exception as e:
        _logger.debug("读取系统时区失败：%s", e)
    return ""


def _probe_memory_gb() -> int:
    """物理内存 GiB（Math.round(totalmem / 1024**3)，与官方客户端同式）。

    官方源码（main/main-bootstrap.js）：
        memorySize: Math.round(node_os.totalmem() / 1024 / 1024 / 1024)
    JS Math.round 是四舍五入（half-up），Python round 是银行家舍入，
    所以这里显式 `+0.5` 取整对齐。
    """
    try:
        if sys.platform == "win32":
            import ctypes

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = _MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(st)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return int(st.ullTotalPhys / 1024 ** 3 + 0.5)
            return 0
        if sys.platform == "darwin":
            import subprocess
            r = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                               text=True, timeout=3)
            return int(int(r.stdout.strip()) / 1024 ** 3 + 0.5) if r.returncode == 0 else 0
        # Linux / *BSD：/proc/meminfo 的 MemTotal（kB）
        p = Path("/proc/meminfo")
        if p.is_file():
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return int(kb * 1024 / 1024 ** 3 + 0.5)
    except Exception as e:
        _logger.debug("读取物理内存失败：%s", e)
    return 0


def _probe_system() -> dict:
    """本机系统只读探测：桌面事件指纹里的 os / arch / osVersion / cpuCores /
    memorySize / timezone。

    官方客户端（Electron）取值方式（已对照解包源码逐一核实）：
        os        = process.platform                     -> win32 / darwin / linux
        arch      = process.arch                         -> x64 / arm64 / ia32
        osVersion = node_os.release()                    -> Windows 上为 10.0.xxxxx 内核版本
        cpuCores  = node_os.cpus().length                -> 逻辑核数
        memorySize= Math.round(os.totalmem()/1024**3)    -> 物理内存 GiB
        timezone  = Intl...resolvedOptions().timeZone    -> IANA 名

    全部是 Python 标准库可稳定读到的，读不到的键直接缺席、绝不含糊其辞。
    """
    out: dict[str, Any] = {}
    try:
        sysname = platform.system().lower()
        if sysname in _OS_NAMES:
            out["os"] = _OS_NAMES[sysname]
        machine = platform.machine().lower()
        if machine in _ARCH_NAMES:
            out["arch"] = _ARCH_NAMES[machine]
        # Windows 上 platform.version() 与 Node os.release() 同为内核版本
        # （10.0.xxxxx）；其它平台两者都是 os.release() 的内核版本号。
        rel = platform.version() if sysname == "windows" else platform.release()
        rel = str(rel).strip()
        if re.fullmatch(r"\d+(\.\d+){0,3}", rel):
            out["os_version"] = rel
        ncpu = os.cpu_count()
        if ncpu and ncpu > 0:
            out["cpu_cores"] = int(ncpu)
        mem = _probe_memory_gb()
        if mem > 0:
            out["memory_size"] = mem
        tz = _probe_timezone()
        if tz:
            out["timezone"] = tz
        # 内嵌 Node 版本：来自后台一次性探测（_schedule_node_probe），尚未
        # 就绪时缺省 —— 探测完成会失效缓存，下一次合成自然带上精确值。
        nv = _node_version_memo.get("value")
        if nv:
            out["node_version"] = nv
    except Exception as e:
        _logger.debug("系统探测部分失败：%s", e)
    return out


def snapshot() -> dict:
    """现场探测结果：**只包含真实读到的键**（安装包 + 本机系统）。

    本机系统字段（os / arch / 内存 / 时区…）只有在**安装包探测成功**时才采纳
    —— 指纹描述的应该是「真实跑着官方客户端的那台机器」。线上服务器没有
    客户端，探测结果为空 dict，继续靠同步过来的 saved 档案工作；否则一台
    Linux 服务器会把自己的 os/arch 报成指纹，反而引入新的可识别差异。

    绝不抛异常。
    """
    pkg = _snapshot_package()
    if not pkg:
        return {}
    # 安装包与系统字段互不重叠；包值放后面只是防御（真撞车时包值更权威）。
    return {**_probe_system(), **pkg}


def snapshot_detail() -> dict:
    """探测明细：values（等价 snapshot()）+ 按 origin 拆分的来源。

    origins: 每个键取自「安装包」（product.json / install-manifest.json 等）
    还是「本机系统」（OS / 硬件 / 时区）。供后台把每个字段的来源标清楚，
    不把兜底默认值混在「探测到」里展示。
    """
    pkg = _snapshot_package()
    if not pkg:
        return {"values": {}, "package": {}, "system": _probe_system(), "origins": {}}
    sysd = _probe_system()
    values = {**sysd, **pkg}
    origins = {k: "本机系统" for k in sysd}
    origins.update({k: "安装包" for k in pkg})
    return {"values": values, "package": pkg, "system": sysd, "origins": origins}


# ---------------------------------------------------------------------------
# 校验 / 归一化
# ---------------------------------------------------------------------------
def normalize(raw: Any, base: dict | None = None) -> tuple[dict, list[str]]:
    """校验并归一化一份档案。返回 (干净档案, 错误列表)。

    只接受已知的键，未知键被忽略并记一条错误 —— 这样前端多传字段不会写脏库，
    同时用户能看清是哪个字段没被接受。
    """
    errors: list[str] = []
    clean: dict[str, Any] = {}

    if not isinstance(raw, dict):
        return {}, ["档案必须是 JSON 对象"]

    for k in _STR_KEYS:
        if k in raw:
            v = raw[k]
            if v is None or v == "":
                errors.append(f"{k} 不能为空（如不需要请删除该字段）")
                continue
            if not isinstance(v, (str, int, float)):
                errors.append(f"{k} 必须是字符串")
                continue
            s = str(v).strip()
            if len(s) > _MAX_STR:
                errors.append(f"{k} 过长（>{_MAX_STR} 字符）")
                continue
            clean[k] = s

    for k in _INT_KEYS:
        if k in raw:
            n = _int_or_none(raw[k])
            if n is None:
                errors.append(f"{k} 必须是非负整数")
                continue
            clean[k] = n

    if "headers" in raw:
        h = raw["headers"]
        if not isinstance(h, dict):
            errors.append("headers 必须是对象（header 名 -> 值）")
        else:
            hh: dict[str, str] = {}
            for hk, hv in h.items():
                hk = str(hk).strip()
                hv = "" if hv is None else str(hv).strip()
                if not hk or len(hk) > _MAX_HEADER or len(hv) > _MAX_HEADER:
                    errors.append(f"header 名或值非法/过长：{hk[:40]!r}")
                    continue
                hh[hk] = hv
            clean["headers"] = hh

    # 未知键
    known = set(_STR_KEYS) | set(_INT_KEYS) | {"headers", "user_agent"}
    unknown = [k for k in raw if k not in known]
    if unknown:
        errors.append("忽略未知字段：" + ", ".join(sorted(unknown)[:10]))

    # user_agent 不在 _STR_KEYS 里：它由版本号派生，存下来没有意义
    # （下次版本号一变就成了陈旧值）。这里明确拒绝并给出正确做法，
    # 比「静静存下来但永远不生效」好 —— 后者会让人以为改了却看不到效果。
    if raw.get("user_agent"):
        errors.append(
            "user_agent 不可保存（它由 desktop_version + cli_version 自动派生，"
            "保存会变成陈旧值）。要强制指定整条 UA，请设置环境变量 "
            "WORKBUDDY_USER_AGENT")

    return clean, errors


# ---------------------------------------------------------------------------
# 档案仓库
# ---------------------------------------------------------------------------
class ClientProfileStore:
    """档案的读取 / 保存 / 生效值计算（进程内缓存 + 线程安全）。

    缓存三层快照：`saved`（DB）、`source`（DB）、`detected`（现场探测）。
    三者一起刷新（TTL 30 秒，或保存时立即失效）。

    `effective()` 由这三层算出，**结果也缓存**：它在每个请求上被调用
    （拼上游头、拼桌面指纹），现场探测 + 环境变量扫描合计约 40µs，
    对高并发网关来说不该每个请求都付。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._saved: dict = {}
        self._source: str = SOURCE_AUTO
        self._detected: dict = {}
        self._origins: dict = {}
        self._computed: dict | None = None
        self._loaded_at: float = 0.0
        self._loaded: bool = False

    # -- DB 读写 ---------------------------------------------------------
    @staticmethod
    def _load_db(db=None):
        """从 DB 读 (saved_dict, source)。db=None 时自建会话。"""
        from admin.db import SessionLocal
        from admin.models import SystemSetting

        own = db is None
        if own:
            db = SessionLocal()
        try:
            rows = {r.key: r.value for r in db.query(SystemSetting).filter(
                SystemSetting.key.in_([PROFILE_KEY, SOURCE_KEY])).all()}
            raw = rows.get(PROFILE_KEY) or ""
            src = (rows.get(SOURCE_KEY) or SOURCE_AUTO).strip() or SOURCE_AUTO
            if src not in _VALID_SOURCES:
                src = SOURCE_AUTO
            data: dict = {}
            if raw:
                try:
                    obj = json.loads(raw)
                    if isinstance(obj, dict):
                        data = obj
                except Exception as e:
                    _logger.warning("client_profile 解析失败，按空档案处理：%s", e)
            return data, src
        except Exception as e:
            _logger.debug("读取 client_profile 失败：%s", e)
            return {}, SOURCE_AUTO
        finally:
            if own:
                db.close()

    def refresh(self, db=None) -> None:
        """强制重新加载（DB + 现场探测），并重算生效值。"""
        data, src = self._load_db(db)
        detail = snapshot_detail()
        computed = _compose(data, src, detail["values"])
        with self._lock:
            self._saved = data
            self._source = src
            self._detected = detail["values"]
            self._origins = dict(detail.get("origins") or {})
            self._computed = computed
            self._loaded_at = time.time()
            self._loaded = True

    def invalidate(self) -> None:
        """让缓存过期（保存后调用，使新值立即生效）。"""
        with self._lock:
            self._loaded = False
            self._loaded_at = 0.0
            self._computed = None

    def _ensure_loaded(self) -> None:
        with self._lock:
            fresh = (self._loaded and self._computed is not None
                     and (time.time() - self._loaded_at) < _CACHE_TTL)
        if not fresh:
            self.refresh()

    def detected(self) -> dict:
        """本机现场探测结果（只含真实探测到的键）。"""
        self._ensure_loaded()
        with self._lock:
            return dict(self._detected)

    # -- 查询 ------------------------------------------------------------
    def saved(self) -> dict:
        """后台保存的档案（可能为空）。"""
        self._ensure_loaded()
        with self._lock:
            out = dict(self._saved)
            if isinstance(out.get("headers"), dict):
                out["headers"] = dict(out["headers"])
            return out

    def source(self) -> str:
        """当前模式：auto | saved。"""
        self._ensure_loaded()
        with self._lock:
            return self._source

    def effective(self, db=None) -> dict:
        """实际生效的档案：兜底 <- 探测/保存 <- 环境变量。

        这是所有调用方（展示或明确支持该字段的客户端业务）唯一该用的入口。
        结果被缓存，保存或 TTL 到期后重算。
        """
        if db is not None:
            self.refresh(db)
        else:
            self._ensure_loaded()
        with self._lock:
            if self._computed is not None:
                # headers 是嵌套 dict，必须单独复制：否则调用方改动返回值
                # 会直接污染缓存里的档案（下一个请求就带着脏值出去）。
                out = dict(self._computed)
                out["headers"] = dict(self._computed.get("headers") or {})
                return out
        # 兜底：理论上 refresh() 一定填了 _computed，这里防御性重算一次
        return _compose(self.saved(), self.source(), snapshot())

    def sources(self, db=None) -> dict:
        """每个键当前取自哪一层（排障用）。

        现场探测的键会进一步标明来自「安装包」（product.json 等安装包文件）
        还是「本机系统」（OS / 硬件 / 时区探测），让后台能把
        「官方客户端实测值」与「协议兼容参考默认值」分清楚。
        """
        if db is not None:
            self.refresh(db)
        else:
            self._ensure_loaded()
        with self._lock:
            saved = dict(self._saved)
            detected = dict(self._detected)
            origins = dict(self._origins)
            src = self._source
        out: dict[str, str] = {}
        for k in set(list(DEFAULTS) + list(saved) + list(detected)):
            if k == "headers":
                out[k] = "合并（兜底+保存+探测）"
                continue
            env = _ENV_FOR.get(k)
            if env and (os.getenv(env) or "").strip():
                out[k] = f"环境变量 {env}"
                continue
            def _probe_label() -> str:
                origin = origins.get(k)
                if origin == "本机系统":
                    return "现场探测（本机系统）"
                if origin == "安装包":
                    return "现场探测（安装包）"
                return "现场探测"
            if src == SOURCE_SAVED:
                out[k] = "已保存" if k in saved else (
                    _probe_label() if k in detected else "内置兜底")
            else:
                out[k] = _probe_label() if k in detected else (
                    "已保存" if k in saved else "内置兜底")
        out["source_mode"] = src
        return out


def _compose(saved: dict, src: str, detected: dict) -> dict:
    """把三层（兜底 / 保存或探测 / 环境变量）合成最终生效档案。

    抽成纯函数是为了能被缓存：`effective()` 在每个请求上被调用，
    现场探测 + 环境变量扫描约 40µs，不该每个请求都重算一遍。

    关于 `layers` 的顺序（**曾经写反过，是个真 bug**）：
    下面用 `out[k] = v` 逐层覆盖，所以**越靠后的层优先级越高**。
    于是「谁优先级高」就要放在**列表末尾**：

        saved 模式：已保存 > 现场探测  ->  [探测, 保存]   保存最后写入
        auto  模式：现场探测 > 已保存  ->  [保存, 探测]   探测最后写入

    历史实现把这两个分支写反了（saved 模式让探测胜出、auto 模式让保存胜出），
    后果是「auto 模式下版本升级后自动跟上」这条承诺根本不成立 —— 只要后台
    保存过一次，真实探测值就永远被那份可能已经过期的档案压住。
    `sources()` 一直是按正确优先级写的，所以会出现「排障视图说取自现场探测、
    实际生效的却是已保存值」的自相矛盾，正是这个 bug 的显性症状。
    """
    out = dict(DEFAULTS)
    # headers 要按「兜底 -> 保存 -> 探测」逐层覆盖，不能整体替换：
    # 否则后台只改 Accept-Language 时，其余默认风控头会消失。
    out["headers"] = dict(DEFAULTS.get("headers") or {})

    layers = ([detected, saved] if src == SOURCE_SAVED else [saved, detected])
    for layer in layers:
        for k, v in layer.items():
            if k == "headers" and isinstance(v, dict):
                out["headers"].update(v)
            elif k == "user_agent":
                # 故意忽略：UA 始终由版本号现拼（见下方注释）。
                # 历史保存过的旧 UA 不能覆盖版本号变化。
                continue
            elif v is not None:
                # turing_channel_id/release_date_ms 为 0 视为「没探测到」
                if k in ("turing_channel_id", "release_date_ms") and not v:
                    continue
                out[k] = v

    # 环境变量最后覆盖（运维逃生门，永远最高优先级）
    out = _apply_env(out)

    # 首次使用档案后安排一次客户端 Node 版本探测（后台、一次性、不阻塞）
    _schedule_node_probe()

    # 派生 UA：**始终**按 desktop_version / cli_version 现拼。
    #
    # 为什么不把 UA 当成一个可独立保存的字段：它 100% 由两个版本号决定
    # （官方三段式 `WorkBuddy/<v> WorkBuddy/<v> CLI/<cli>`）。若把它一起
    # 存下来，就会出现「同步过一次 UA 之后，版本号再变而 UA 不变」的
    # 陈旧陷阱 —— 实测踩过：env 覆盖了 desktop_version，UA 却还在报旧版本。
    #
    # 想彻底自定义 UA 的，用 WORKBUDDY_USER_AGENT 环境变量（见 _apply_env）。
    if not out.get("user_agent"):
        out["user_agent"] = _build_ua(out.get("desktop_version"),
                                      out.get("cli_version"),
                                      out.get("application_name"))
    return out


#: 键 -> 环境变量名（用于优先级判定与覆盖）
_ENV_FOR = {
    "desktop_version": "WORKBUDDY_DESKTOP_VERSION",
    "cli_version": "WORKBUDDY_CLI_VERSION",
    "turing_channel_id": "WORKBUDDY_TURING_CHANNEL_ID",
    "turing_product_name": "WORKBUDDY_TURING_PRODUCT_NAME",
    "commit": "WORKBUDDY_COMMIT",
    "release_date_ms": "WORKBUDDY_RELEASE_DATE_MS",
    "os": "WORKBUDDY_OS",
    "arch": "WORKBUDDY_ARCH",
    "os_version": "WORKBUDDY_OS_VERSION",
    "cpu_cores": "WORKBUDDY_CPU_CORES",
    "memory_size": "WORKBUDDY_MEMORY_SIZE",
    "ide_name": "WORKBUDDY_IDE_NAME",
    "ext_name": "WORKBUDDY_EXT_NAME",
    "user_agent": "WORKBUDDY_USER_AGENT",
    "node_version": "WORKBUDDY_NODE_VERSION",
    "stainless_package_version": "WORKBUDDY_STAINLESS_PACKAGE_VERSION",
}


def _apply_env(profile: dict) -> dict:
    """用环境变量覆盖档案里对应的键（只覆盖显式设置了非空值的）。

    `WORKBUDDY_USER_AGENT` 是唯一的「整条 UA 覆盖」入口；其余情况 UA 都由
    版本号现拼，避免出现「UA 报旧版本、X-IDE-Version 报新版本」的错位。
    """
    for key, env in _ENV_FOR.items():
        raw = os.getenv(env)
        if raw is None or not raw.strip():
            continue
        raw = raw.strip()
        if key in _INT_KEYS:
            n = _int_or_none(raw)
            if n is not None:
                profile[key] = n
            continue
        profile[key] = raw
    return profile


def _build_ua(desktop_version: str | None, cli_version: str | None,
              application_name: str | None = None) -> str:
    """按官方三段式拼 UA：`WorkBuddy/<v> WorkBuddy/<v> CLI/<cli>`。

    第一段/第二段是产品名（官方取 `applicationName`，见 app-instance.js 里对
    `electron.app.userAgentFallback` 的覆写），第三段是内嵌 CLI 版本。
    """
    v = (desktop_version or DEFAULTS["desktop_version"]).strip()
    c = (cli_version or DEFAULTS["cli_version"]).strip()
    brand = (application_name or DEFAULTS["application_name"]).strip()
    return f"{brand}/{v} {brand}/{v} CLI/{c}"


# ---------------------------------------------------------------------------
# 官方内嵌 SDK（stainless JS 6.x）指纹头
# ---------------------------------------------------------------------------
#: Node `process.platform` -> SDK `X-Stainless-OS`（官方 normalizePlatform 映射）
_STAINLESS_OS = {"win32": "Windows", "darwin": "MacOS", "linux": "Linux",
                 "android": "Android", "freebsd": "FreeBSD", "openbsd": "OpenBSD"}
#: `process.arch` -> `X-Stainless-Arch`（官方 normalizeArch 映射）
_STAINLESS_ARCH = {"x64": "x64", "arm64": "arm64", "x32": "x32", "arm": "arm"}

_node_probe_lock = threading.Lock()
_node_probe_done = threading.Event()
#: 后台探测到的内嵌 Node 版本（"" = 探测过但没有；None = 尚未探测）
_node_version_memo: dict = {"value": None}


def stainless_headers() -> dict:
    """构造已探测的 Stainless 资料，供明确使用该 SDK 的业务查看。

    普通模型网关请求不会自动调用此函数。

    取值公式对照官方解包源码（cli/dist/lazy-lite-wb/3043.*.mjs，SDK 6.25.0）::

        X-Stainless-Lang            恒 "js"
        X-Stainless-Package-Version SDK 版本常量（"6.25.0"）
        X-Stainless-OS              normalizePlatform(process.platform) -> Windows/MacOS/Linux
        X-Stainless-Arch            normalizeArch(process.arch)         -> x64/arm64/…
        X-Stainless-Runtime         恒 "node"
        X-Stainless-Runtime-Version process.version（带 v 前缀，如 "v22.21.1"）
        X-Stainless-Retry-Count     String(retryCount)，本网关每次上游请求都是
                                    全新 SDK 请求，恒 "0"

    os/arch 复用档案里探测到的值（与桌面指纹同源）；node_version 优先用本机
    官方客户端探测的精确值（见 _probe_node_version_once）。
    """
    p = effective()
    os_name = _STAINLESS_OS.get(str(p.get("os") or "").lower(), "Unknown")
    arch = _STAINLESS_ARCH.get(str(p.get("arch") or "").lower(), "unknown")
    ver = str(p.get("node_version") or DEFAULTS["node_version"]).strip()
    pkg = str(p.get("stainless_package_version")
              or DEFAULTS["stainless_package_version"]).strip()
    return {
        "X-Stainless-Lang": "js",
        "X-Stainless-Package-Version": pkg,
        "X-Stainless-OS": os_name,
        "X-Stainless-Arch": arch,
        "X-Stainless-Runtime": "node",
        "X-Stainless-Runtime-Version": ver,
        "X-Stainless-Retry-Count": "0",
    }


def _probe_node_version_once() -> str:
    """从本机官方客户端读内嵌 Node 版本（X-Stainless-Runtime-Version 用）。

    `ELECTRON_RUN_AS_NODE=1 WorkBuddy.exe -e …` 只是把官方 exe 当 node 跑一行
    打印，只读、无副作用，约 1 秒。结果进程内记忆（含失败），绝不重试。
    """
    if sys.platform != "win32" or os.getenv("WORKBUDDY_SKIP_NODE_PROBE"):
        return ""
    try:
        from wb_install import WB
        exe = WB.install_dir()
        exe = exe / "WorkBuddy.exe" if exe else None
        if not exe or not exe.is_file():
            return ""
        import subprocess
        env = dict(os.environ, ELECTRON_RUN_AS_NODE="1")
        r = subprocess.run([str(exe), "-e", "process.stdout.write(process.versions.node)"],
                           capture_output=True, text=True, timeout=10, env=env)
        v = (r.stdout or "").strip()
        return f"v{v}" if re.fullmatch(r"\d+\.\d+\.\d+", v) else ""
    except Exception as e:
        _logger.debug("探测客户端 Node 版本失败：%s", e)
        return ""


def _schedule_node_probe() -> None:
    """首次使用档案后，延迟 3 秒后台探测一次 Node 版本并失效缓存。

    绝不在导入期/请求线程里同步跑（约 1 秒的子进程不可接受）；探测完成后
    `STORE.invalidate()`，下一次合成档案即带上精确值。进程内只跑一次。
    """
    with _node_probe_lock:
        if _node_probe_done.is_set():
            return
        _node_probe_done.set()

    def _run() -> None:
        v = _probe_node_version_once()
        _node_version_memo["value"] = v
        if v and v != DEFAULTS["node_version"]:
            _logger.info("已探测客户端内嵌 Node 版本：%s（X-Stainless-Runtime-Version）", v)
            STORE.invalidate()

    threading.Timer(3.0, _run).start()


# ---------------------------------------------------------------------------
# 单例 + 便捷函数
# ---------------------------------------------------------------------------
STORE = ClientProfileStore()


def effective(db=None) -> dict:
    return STORE.effective(db)


def effective_local() -> dict:
    """**不含 DB** 的生效档案：兜底 + 现场探测 + 环境变量。

    专给**模块导入期**用（如 `converter.py` / `admin/backend.py` 里的
    模块级常量）。原因很实在：standalone `converter.py` 可能根本没配 MySQL，
    不该为了算一个 UA 去连库 —— 连不上会白等几秒，还打一行没意义的报错。

    发请求请用 `effective()`（含后台保存值），本函数只是「没有 DB 时的等价物」。
    """
    return _compose({}, SOURCE_AUTO, snapshot())


def saved() -> dict:
    return STORE.saved()


def source() -> str:
    return STORE.source()


def sources(db=None) -> dict:
    return STORE.sources(db)


def snapshot_live() -> dict:
    """公开的现场探测（供后台「探测」按钮用，总是实时扫）。"""
    return snapshot()


def detected() -> dict:
    """缓存过的现场探测结果（供后台展示；想强制实时用 snapshot_live）。"""
    return STORE.detected()


def invalidate() -> None:
    STORE.invalidate()


def ua() -> str:
    """出站 User-Agent（实时，带缓存）。"""
    return effective().get("user_agent") or _build_ua(None, None)


def desktop_version() -> str:
    return str(effective().get("desktop_version") or DEFAULTS["desktop_version"])


def cli_version() -> str:
    return str(effective().get("cli_version") or DEFAULTS["cli_version"])


def turing_channel_id() -> int:
    v = effective().get("turing_channel_id")
    n = _int_or_none(v)
    return n if n else 109144


def turing_product_name() -> str:
    return str(effective().get("turing_product_name")
               or DEFAULTS["turing_product_name"])


def risk_headers() -> dict:
    """官方客户端的风控/审计头（不含 IP、不含账号级与会话级头）。"""
    p = effective()
    name = str(p.get("ide_name") or DEFAULTS["ide_name"])
    h = {
        "X-IDE-Name": name,
        "X-IDE-Type": str(p.get("ide_type") or name),
        # X-Product 是部署形态（SaaS/…），**不能**回落到 ide_name（那是产品名）。
        # 回落到产品名正是修复前的老 bug，会让这个头报成官方从不发的值。
        "X-Product": str(p.get("product") or DEFAULTS["product"]),
        "X-IDE-Version": str(p.get("desktop_version") or DEFAULTS["desktop_version"]),
    }
    extra = p.get("headers") or {}
    if isinstance(extra, dict):
        for k, v in extra.items():
            # 后加入的不会覆盖上面已定的四个归属头（避免把 IDE 名改成风控头）
            h.setdefault(str(k), str(v))
    return h


def runtime_diagnostics() -> dict:
    """Return safe runtime/request-contract diagnostics for the admin UI.

    This reports whether the local native device-token provider is available
    and which generated header groups are present.  Secrets, bearer tokens and
    raw device identifiers are intentionally excluded.
    """
    try:
        from admin.turing_token import status as turing_status
        device_token = turing_status()
    except Exception as exc:
        device_token = {
            "available": False,
            "source": "unavailable",
            "checked": False,
            "node_available": False,
            "helper_available": False,
            "cached_failure": False,
            "error": type(exc).__name__,
        }
    generated = risk_headers()
    return {
        "platform": sys.platform,
        "device_token": device_token,
        "header_groups": {
            "client_profile": sorted(generated),
            "sdk_stainless": sorted(stainless_headers()),
            "account_session": [
                "Authorization", "Accept", "Content-Type", "User-Agent",
                "X-User-Id", "X-Enterprise-Id", "X-Tenant-Id", "X-Domain",
            ],
            "request_trace": [
                "X-Request-ID", "X-Conversation-Request-ID",
                "X-Conversation-Message-ID", "X-Root-Request-ID",
                "traceparent", "b3", "X-B3-TraceId", "X-B3-SpanId",
                "X-B3-Sampled", "X-Trace-ID",
            ],
        },
    }


def fingerprint(uid: str, machine_id: str = "", session_id: str = "") -> dict:
    """桌面事件指纹（`report_desktop_events` 每个事件都要带）。

    machineId / sessionId 由调用方按 uid 稳定派生后传入；不传则此处派生。
    """
    p = effective()
    dv = str(p.get("desktop_version") or DEFAULTS["desktop_version"])
    if not machine_id or not session_id:
        try:
            from admin.backend import stable_device_id
            machine_id = machine_id or stable_device_id(uid, "machine")
            session_id = session_id or stable_device_id(uid, "session")
        except Exception:
            machine_id = machine_id or ""
            session_id = session_id or ""
    rdm = _int_or_none(p.get("release_date_ms")) or 0
    out = {
        "timezone": str(p.get("timezone") or DEFAULTS["timezone"]),
        "reportDelay": _int_or_none(p.get("report_delay"))
                       or DEFAULTS["report_delay"],
        "product": str(p.get("fp_product") or DEFAULTS["fp_product"]),
        "ideName": str(p.get("ide_name") or DEFAULTS["ide_name"]),
        "ideType": str(p.get("ide_type") or DEFAULTS["ide_type"]),
        "ideVersion": dv,
        "machineId": machine_id,
        "sessionId": session_id,
        "extName": str(p.get("ext_name") or DEFAULTS["ext_name"]),
        "extVersion": dv,
        "os": str(p.get("os") or DEFAULTS["os"]),
        "arch": str(p.get("arch") or DEFAULTS["arch"]),
        "osVersion": str(p.get("os_version") or DEFAULTS["os_version"]),
        "cpuCores": _int_or_none(p.get("cpu_cores")) or DEFAULTS["cpu_cores"],
        "memorySize": _int_or_none(p.get("memory_size")) or DEFAULTS["memory_size"],
    }
    if rdm:
        out["releaseDate"] = rdm
    commit = p.get("commit")
    if commit:
        out["commit"] = str(commit)
    return out


# ---------------------------------------------------------------------------
# 保存 / 重置
# ---------------------------------------------------------------------------
def save(profile: Any, new_source: str | None = None, db=None,
         merge: bool = True) -> dict:
    """保存档案到 DB，并让新值立即生效。

    merge=True 时与已有保存值合并（后台「只改一个字段」的常见用法）；
    False 时整体替换。
    """
    from admin.db import SessionLocal
    from admin.models import SystemSetting

    clean, errors = normalize(profile)
    if errors and not clean:
        return {"ok": False, "errors": errors}

    own = db is None
    if own:
        db = SessionLocal()
    try:
        current, cur_src = STORE._load_db(db)
        merged = dict(current) if merge else {}
        for k, v in clean.items():
            if k == "headers" and isinstance(v, dict) and merge:
                base = dict(merged.get("headers") or {})
                base.update(v)
                merged["headers"] = base
            else:
                merged[k] = v

        src = (new_source or cur_src or SOURCE_AUTO).strip()
        if src not in _VALID_SOURCES:
            errors.append(f"source 只能是 {_VALID_SOURCES}，已按 {SOURCE_AUTO} 处理")
            src = SOURCE_AUTO

        _upsert(db, SystemSetting, PROFILE_KEY,
                json.dumps(merged, ensure_ascii=False))
        _upsert(db, SystemSetting, SOURCE_KEY, src)
        db.commit()
        STORE.refresh(db)
    finally:
        if own:
            db.close()

    return {"ok": True, "errors": errors, "saved": merged, "source": src}


def reset(db=None) -> dict:
    """清空保存的档案，回到「纯探测 + 兜底」。"""
    from admin.db import SessionLocal
    from admin.models import SystemSetting

    own = db is None
    if own:
        db = SessionLocal()
    try:
        _upsert(db, SystemSetting, PROFILE_KEY, "")
        _upsert(db, SystemSetting, SOURCE_KEY, SOURCE_AUTO)
        db.commit()
        STORE.refresh(db)
    finally:
        if own:
            db.close()
    return {"ok": True}


def _upsert(db, model, key: str, value: str) -> None:
    row = db.query(model).filter(model.key == key).first()
    if row:
        row.value = value
    else:
        db.add(model(key=key, value=value))


def diff(a: dict, b: dict) -> dict:
    """比较两份档案，返回 {key: {"old":…, "new":…}}（供后台展示改动）。"""
    out: dict[str, dict] = {}
    for k in sorted(set(list(a) + list(b))):
        if k == "headers":
            ha, hb = a.get("headers") or {}, b.get("headers") or {}
            if ha != hb:
                out["headers"] = {"old": ha, "new": hb}
            continue
        if a.get(k) != b.get(k):
            out[k] = {"old": a.get(k), "new": b.get(k)}
    return out


__all__ = [
    "STORE", "DEFAULTS", "PROFILE_KEY", "SOURCE_KEY",
    "SOURCE_AUTO", "SOURCE_SAVED",
    "effective", "effective_local", "saved", "source", "sources",
    "snapshot_live", "detected", "snapshot_detail",
    "invalidate", "ua", "desktop_version", "cli_version",
    "turing_channel_id", "turing_product_name",
    "risk_headers", "stainless_headers", "fingerprint", "save", "reset",
    "normalize", "diff",
    "snapshot",
]
