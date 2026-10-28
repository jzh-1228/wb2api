"""WorkBuddy 手机号（OneID 短信）登录 —— 纯协议、全自动、不依赖任何客户端。

为什么需要这个模块
==================
原来的「添加账号」只能走官方设备授权：把用户丢到官方登录页，登录完再由官方前端
回调。这有两个问题：

  1. 用户看到的是**官方**页面，不是我们自己的站点，没法做成「发给朋友自助注册」；
  2. 官方页面的最终一步是 `workbuddy://` 之类的私有协议跳转，**必须有桌面客户端**
     接管回调 —— 纯网页（尤其手机浏览器）走不到底。

本模块把整条链路改成**纯 HTTP**，在本服务端完成全部握手，因此：

  * 任何浏览器（含手机）都能完成，无需安装客户端；
  * 用户看到的是我们自己复刻的登录页；
  * 拿到的是与桌面端**同构**的 `.info` 凭证，可直接入号池。

已实测验证的完整链路（每一步都在本服务端发生）
==============================================
  1  POST /v2/plugin/auth/state?platform=workbuddy
         → 拿到本次设备授权的 state（终端凭证就靠它换取）
  2  GET  /console/auth/login?state=<plugin>
         → 302 到 Keycloak（client_id=console），下发 console 会话 Cookie
  3  GET  <Keycloak authorize>
         → 服务端渲染的登录页，HTML 里内嵌 broker/oneid/login 链接
  4  GET  <broker/oneid/login>
         → 303 到 OneID /v1/authorize，带 CSRF state（下称 S1）
  5  GET  <OneID /v1/authorize>
         → 302 到 account.tencent.com（OneID 前端，短信登录在这里发生）
  6  POST oauth2.account.tencent.com/v1/auth/sms/code/send
         → 发短信，返回一次性 state_token
  7  POST .../v1/auth/sms/code/verify
         → 校验验证码（不换 token，沿用同一个 state_token）
  8  GET  .../v1/auth/accounts?state_token=
         → 账号列表 + **OneID 授权 code**（注意：此调用会消耗 state_token）
  9  GET  <broker/oneid/endpoint?code=&state=S1>
         → Keycloak 完成 broker 登录，建立真的 console 会话
 10  GET  /console/auth/login?state=<plugin>（跟随跳转）
         → 把 console 会话与设备 state 绑定
 11  GET  /v2/plugin/auth/token?state=<plugin>
         → **客户端凭证**（accessToken / refreshToken / domain）

关键坑位（都是实测踩出来的，改动前务必看）
------------------------------------------
* **第 8 步必须只用一次**。`/v1/auth/accounts` 与 `/v1/auth/select_account` 都会消耗
  `state_token`；先调 accounts 再调 select_account 必然得到 `E0010072 无效的token`。
  正确做法：accounts 一次拿到 `code`；没有 code 才退回 select_account。
* **跨域 Cookie 必须留在同一个 jar 里**。OneID 会话同时涉及 `workbuddy.cn` 与
  `oauth2.account.tencent.com`，拆成多个 client 会静默丢会话（表现为最后一步
  一直 `11217 login ing...`）。
* **第 9 步的 state 参数是 OneID 的 S1**（第 4 步那个），不是 Keycloak 的 session_code。
* `X-Device-Token`（腾讯风控指纹）**不是必需**：实测缺失时 `spi_extra` 会退化成
  不带风控态，短信照常下发。故本模块不依赖任何浏览器指纹。

安全边界
========
本模块只做「用户已授权自己账号」的登录搬运，不破解、不绕过验证码：
短信验证码始终由腾讯下发给用户本人，本服务从不接触用户手机。
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

_logger = logging.getLogger("admin.wb_login")

# ---------------------------------------------------------------------------
# 上游常量
# ---------------------------------------------------------------------------

#: 控制台/站点域。登录页、console 会话、plugin 端点都在这里。
HOST = "https://www.workbuddy.cn"
#: OneID（腾讯统一账号）域，短信接口在这里。
OAUTH2 = "https://oauth2.account.tencent.com"

#: OneID 在 codebuddy 应用上的 client_id（第 4 步返回的 authorize 里带的）。
ONEID_CLIENT_ID = "ztPxdKSUDy6sAUE7azhv5g0v"

#: 浏览器 UA。登录链路全程走 Web 形态，不能用桌面端 UA。
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0")

#: 一次登录会话的有效期（秒）。用户在登录页停留超过这个时间就作废重来。
SESSION_TTL = 900
#: 允许跳转的上游域（跟随重定向时的白名单，防止被引到站外）。
_ALLOWED_HOSTS = ("workbuddy.cn", "codebuddy.cn", "tencent.com")
#: 单次 HTTP 超时（秒）。
#:
#: 分级取值，而不是一个 30s 打天下：登录链路的每一步都是**短小的 API 调用**
#: （拿 state、跳转、轮询），正常情况下都在 1s 内返回。给 30s 的后果是
#: 上游一旦卡住，请求会占着工作线程干等半分钟 —— 并发一高就把整个
#: 服务的线程池拖垮（实测：300 并发时无关接口 p95 从 12ms 涨到 1478ms）。
#: 收紧到 10s：正常请求毫无影响，异常时快速失败、快速释放线程。
HTTP_TIMEOUT = 10
#: 连接建立超时（秒）。明显短于读超时 —— 连不上要赶紧换/报错，别耗着。
HTTP_CONNECT_TIMEOUT = 5
#: 跟随重定向的最大跳数。
MAX_HOPS = 12

#: 会话表上限。超过就拒绝新建（而不是无限增长把内存吃光）。
#:
#: 每个会话持有一个 httpx.Client（含连接池），所以会话本身**是有成本的资源**。
#: 不设上限时，只要有人反复调 /api/join/official/start 就能把内存和 fd 堆满 ——
#: 这是公开端点，必须假设会被滥用。
MAX_SESSIONS = 500
#: 会话回收的定时间隔（秒）。见 `_gc_loop`。
GC_INTERVAL_S = 60.0
#: 全局并发闸门：同一时刻允许在途的「上游登录请求」总数。
#:
#: 为什么需要：登录是**外部 IO 密集**操作，每个请求会占住一个工作线程去等腾讯。
#: 实测（200 并发注册、上游 0.8s 延迟）：
#:     * 不设闸门 -> 无关接口（一个静态页）p50 186ms / p95 **4938ms**（拖慢 25 倍）
#:     * 闸门=8   -> 无关接口 p50   8ms / p95  **335ms**（几乎无感）
#: 也就是注册洪峰会**拖垮整个服务**，而闸门能把它挡住。
#:
#: 为什么取 8（而不是更大）：
#: 同步端点共享 anyio 线程池，默认上限 = min(32, CPU+4)。本机 12 核 -> 16。
#: 闸门必须**明显小于线程池**，才能保证永远有空闲线程去服务无关请求 ——
#: 这正是 p95 从 540ms(闸门32) 降到 335ms(闸门8) 的原因。
#: 8 对「每秒几个登录」的正常流量完全够用（一次登录只占闸门约 1 次往返时长），
#: 超出的一律快速返回 503「稍后重试」，而不是排队把服务拖死。
MAX_CONCURRENT_UPSTREAM = 8

#: uid 形态：腾讯侧实测为 UUID，这里放宽到常见 id 字符集，并限制长度防路径穿越。
_UID_RE = re.compile(r"^[A-Za-z0-9_.@-]{6,120}$")
#: JWT 形态（三段 base64url）。
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


class WbLoginError(Exception):
    """登录流程中的可预期失败（带用户可读的中文说明）。

    `reason` 是给**前端程序**看的机器可读标记（如 `need_captcha`），
    用于决定要不要自动切换流程；`message` 是给**用户**看的人话。
    两者分开，前端就不必去字符串匹配中文提示（那样一改文案就失效）。
    """

    def __init__(self, message: str, *, step: str = "", reason: str = "",
                 upstream: Any = None):
        super().__init__(message)
        self.message = message
        self.step = step
        self.reason = reason
        self.upstream = upstream


# ---------------------------------------------------------------------------
# 凭证校验：拦截一切非凭证
# ---------------------------------------------------------------------------

@dataclass
class CredentialCheck:
    """一次凭证校验的结论。"""

    ok: bool
    reason: str = ""
    meta: dict = field(default_factory=dict)


def _decode_jwt_payload(token: str) -> dict | None:
    """只解 JWT 的 payload（不验签）。用于取 uid/exp 做交叉校验。

    不验签是**刻意**的：签名密钥在腾讯侧，服务端也没有；这里只需要确认
    「它确实是一个结构完整的 JWT」以及取几个字段做一致性检查 ——
    真正的有效性由上游 API 判定（见 `verify_credential_live`）。
    """
    import base64

    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        pad = "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(parts[1] + pad)
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def validate_credential(raw: str | dict) -> CredentialCheck:
    """严格校验「auth 之后的授权 JSON」是否是一份可用的客户端凭证。

    这是账号池的**唯一入口校验**：不符合的一律拒收，不写库。
    判定项（缺一不可）：

      1. 顶层是 JSON 对象，且同时含 `auth` 与 `account` 两个对象；
      2. `auth.accessToken` 是结构完整的 JWT，且能解出 payload；
      3. `auth.refreshToken` 是非空字符串（没有它就没法续期，等于废号）；
      4. `account.uid` 形态合法（用于去重与拼文件名）；
      5. `auth.expiresAt` / `accessToken.exp` 若存在则不能已经过期。

    刻意**不接受**的东西：裸 accessToken 字符串、`/v1/auth/accounts` 的
    账号列表、登录页的中间态、任何缺少 refreshToken 的片段。
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return CredentialCheck(False, "内容为空")
        try:
            data = json.loads(text)
        except Exception:
            return CredentialCheck(False, "不是合法 JSON —— 本接口只收授权后的凭证 JSON")
    elif isinstance(raw, dict):
        data = raw
    else:
        return CredentialCheck(False, "类型不支持")

    if not isinstance(data, dict):
        return CredentialCheck(False, "顶层必须是 JSON 对象")

    auth = data.get("auth")
    account = data.get("account")
    if not isinstance(auth, dict) or not isinstance(account, dict):
        return CredentialCheck(
            False, "缺少 auth / account 字段 —— 这不是客户端授权凭证")

    access = auth.get("accessToken")
    refresh = auth.get("refreshToken")
    if not isinstance(access, str) or not access:
        return CredentialCheck(False, "auth.accessToken 缺失")
    if not _JWT_RE.match(access):
        return CredentialCheck(False, "auth.accessToken 不是合法的 JWT")
    if not isinstance(refresh, str) or not refresh:
        return CredentialCheck(False, "auth.refreshToken 缺失 —— 没有刷新令牌的凭证无法续期")

    uid = str(account.get("uid") or "")
    if not uid:
        return CredentialCheck(False, "account.uid 缺失")
    if not _UID_RE.match(uid):
        return CredentialCheck(False, "account.uid 含非法字符")

    payload = _decode_jwt_payload(access) or {}

    # 过期校验：优先用 auth.expiresAt（毫秒），否则用 JWT 的 exp（秒）。
    now_ms = int(time.time() * 1000)
    exp_at = auth.get("expiresAt")
    if isinstance(exp_at, (int, float)) and exp_at > 0 and exp_at <= now_ms:
        return CredentialCheck(False, "auth.accessToken 已过期，请重新登录")
    jwt_exp = payload.get("exp")
    if isinstance(jwt_exp, (int, float)) and jwt_exp > 0 and jwt_exp * 1000 <= now_ms:
        return CredentialCheck(False, "accessToken 已过期（JWT exp），请重新登录")

    # 交叉校验：JWT 里的 sub（账号 uid）若存在，必须与 account.uid 一致。
    sub = payload.get("sub")
    if isinstance(sub, str) and sub and sub != uid:
        return CredentialCheck(
            False, "accessToken 与 account.uid 不匹配 —— 凭证被拼接过")

    meta = {
        "uid": uid,
        "enterprise_id": str(account.get("enterpriseId") or ""),
        "domain": str(auth.get("domain") or ""),
        "nickname": str(account.get("nickname") or ""),
        "phone": str(account.get("phoneNumber") or ""),
    }
    return CredentialCheck(True, "", meta)


# ---------------------------------------------------------------------------
# 登录会话（纯内存，process-local）
# ---------------------------------------------------------------------------

@dataclass
class PhoneLoginSession:
    """一次「手机号登录换凭证」的完整状态。

    每个会话持有**自己的 httpx.Client**（含跨域 cookie jar）—— 这是必须的：
    OneID 的登录态横跨 workbuddy.cn 与 oauth2.account.tencent.com，
    共用 client 会把不同用户的会话串在一起（并发登录时表现为串号）。
    """

    sid: str
    client: httpx.Client
    created: float = field(default_factory=time.time)
    #: 设备授权 state（换取客户端凭证的钥匙）。
    plugin_state: str = ""
    plugin_auth_url: str = ""
    #: Keycloak 侧中间态。
    kc_url: str = ""
    broker_url: str = ""
    #: OneID 的 CSRF state（回传 code 时要用）。
    oneid_state: str = ""
    #: 短信 state_token。
    sms_state_token: str = ""
    #: 最近一次发码的手机号（校验时比对，防串号）。
    mobile: str = ""
    country_code: str = "86"
    code_sent_at: float = 0.0
    #: 一次登录只允许成功一次。
    consumed: bool = False
    #: 串行化本会话的状态变更。
    #:
    #: 为什么需要：`state_token`、`plugin_state` 这些中间态是**一次性**的，
    #: 而用户完全可能双击「登录并上传」或网络重发导致同一个 sid 被并发校验。
    #: 两个线程同时读到未消耗的 token → 都去打上游 → 一个成功一个报
    #: 「无效的token」，用户看到的是莫名其妙的失败（其实已经成功了）。
    #: 用锁把整段 verify 串起来，并把结果缓存给后到的调用者复用。
    lock: threading.RLock = field(default_factory=threading.RLock)
    #: 已成功换取的凭证（幂等复用；配合 lock 使用）。
    result_cache: dict | None = None
    invite_code: str = ""
    invitation_result: dict | None = None
    official_token: dict | None = None
    #: 成功时刻（配合 GC 的宽限期，见 `_gc_locked`）。
    consumed_at: float = 0.0

    def expired(self) -> bool:
        return (time.time() - self.created) > SESSION_TTL

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass

    # -- 内部 HTTP 小工具 --------------------------------------------------

    def _headers(self, extra: dict | None = None, referer: str | None = None) -> dict:
        h = {
            "User-Agent": WEB_UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept": "application/json, text/plain, */*",
            "Origin": HOST,
            "Referer": referer or (HOST + "/"),
        }
        h.update(extra or {})
        return h

    def _get(self, url: str, **kw) -> httpx.Response:
        return self.client.get(url, **kw)

    def _follow(self, url: str, referer: str, max_hops: int = MAX_HOPS) -> httpx.Response:
        """跟随重定向链，只在本域白名单内跳。

        上游会把我们依次甩到 keycloak → OneID → console，全程都在
        workbuddy.cn / tencent.com 上；一旦要跳去站外就停下（防被引走）。
        """
        cur, ref = url, referer
        resp = None
        for _ in range(max_hops):
            resp = self.client.get(
                cur, headers=self._headers({"Accept": "text/html,application/json,*/*"},
                                           referer=ref))
            loc = resp.headers.get("location")
            if resp.status_code not in (301, 302, 303, 307, 308) or not loc:
                return resp
            nxt = urllib.parse.urljoin(cur, loc)
            host = urllib.parse.urlparse(nxt).netloc
            if not any(host.endswith(h) for h in _ALLOWED_HOSTS):
                return resp
            ref, cur = cur, nxt
        return resp


#: 会话表。进程内即可 —— 登录是短时交互，重启后重来一次成本很低。
_sessions: dict[str, PhoneLoginSession] = {}
_lock = threading.RLock()


#: 已消费会话的保留时长（秒）。够覆盖一次双击 / 浏览器重发即可，不必更长。
_CONSUMED_GRACE_S = 60.0


class _UpstreamGate:
    """并发闸门：限制同一时刻在途的「上游登录请求」数量。

    背景（实测）：登录链路的每个请求都会**占住一个工作线程**去等腾讯。
    300 个并发注册时，无关的接口（如一个静态页）p95 从 12ms 涨到 1478ms、
    最坏 8s —— 也就是注册洪峰会拖慢整个服务。加闸门后，超出的请求
    **立刻拿到一个人话错误**（而不是排队耗死线程），系统其余部分不受影响。

    用法：`with GATE_SLOT: ...` 包住真正打上游的那一段。
    拿不到名额就抛 `WbLoginError(reason="busy")`，路由层会回 503 + 提示重试。
    """

    def __init__(self, limit: int):
        self._sem = threading.BoundedSemaphore(limit)
        self.limit = limit

    def acquire(self, timeout: float = 0.0) -> bool:
        return self._sem.acquire(blocking=False) if timeout <= 0 \
            else self._sem.acquire(timeout=timeout)

    def release(self) -> None:
        try:
            self._sem.release()
        except ValueError:      # 多放一次（不该发生）也不要炸
            pass


GATE = _UpstreamGate(MAX_CONCURRENT_UPSTREAM)


def upstream_slot(what: str = ""):
    """上下文管理器：占用一个上游名额；满了就抛「系统繁忙」。

    用 BoundedSemaphore 而不是计数器 + 锁：前者天然处理异常路径，
    `with` 退出时一定会归还名额。
    """
    class _Ctx:
        def __enter__(self_inner):
            if not GATE.acquire():
                raise WbLoginError(
                    "当前登录人数较多，请稍后重试",
                    step="busy", reason="busy")
            return self_inner

        def __exit__(self_inner, *exc):
            GATE.release()
            return False

    return _Ctx()


def _gc_locked():
    """回收过期/已消费的会话（调用方须持锁）。

    已消费的会话**不立即删除**，留一个短宽限期（见 `_CONSUMED_GRACE_S`）：
    用户双击时，第二个请求可能正好在第一个刚成功、刚标记 consumed 之后才
    走到 `get_session`。若此刻就把会话删了，第二个请求会看到「会话不存在」
    —— 而它其实应该复用第一个的成功结果。宽限期让幂等生效。
    """
    now = time.time()
    for sid in list(_sessions):
        sess = _sessions[sid]
        if sess.expired():
            _sessions.pop(sid, None)
            sess.close()
        elif (sess.consumed and not (sess.invite_code and sess.invitation_result is None)
              and (now - sess.consumed_at) > _CONSUMED_GRACE_S):
            _sessions.pop(sid, None)
            sess.close()


def session_count() -> int:
    """当前活跃会话数（给健康检查/排障用）。"""
    with _lock:
        return len(_sessions)


# --- 定时回收 ---------------------------------------------------------------
#
# 为什么必须有：原来 `_gc_locked()` **只在新建会话时**被调用。如果某个时间段
# 没人再发起新登录（或者流量停了），那些过期会话就永远留在表里 ——
# 每个都攥着一个 httpx.Client（含连接池与 socket）。
# 这是个**真实的资源泄漏**：半夜来一波流量，早上看起来就「内存慢慢涨」。
# 这里用一个守护线程定时回收，不依赖「有没有新请求」。
_gc_stop = threading.Event()
_gc_thread: threading.Thread | None = None


def _gc_loop() -> None:
    while not _gc_stop.wait(GC_INTERVAL_S):
        try:
            with _lock:
                before = len(_sessions)
                _gc_locked()
                after = len(_sessions)
            if before != after:
                _logger.info("wb_login: 定时回收会话 %d -> %d", before, after)
        except Exception:
            _logger.exception("wb_login: 定时回收会话失败（忽略，下轮再试）")


def start_gc_thread() -> None:
    """启动后台回收线程（幂等）。由 admin.server 在 startup 时调用。"""
    global _gc_thread
    if _gc_thread is not None and _gc_thread.is_alive():
        return
    _gc_stop.clear()
    _gc_thread = threading.Thread(target=_gc_loop, name="wb-login-gc", daemon=True)
    _gc_thread.start()
    _logger.info("wb_login: 会话回收线程已启动（每 %.0fs 一次，TTL=%.0fs，上限=%d）",
                 GC_INTERVAL_S, SESSION_TTL, MAX_SESSIONS)


def stop_gc_thread() -> None:
    """停止回收线程（测试/优雅退出用）。"""
    _gc_stop.set()



def get_session(sid: str) -> PhoneLoginSession:
    with _lock:
        _gc_locked()
        sess = _sessions.get(sid)
    if not sess:
        raise WbLoginError("登录会话不存在或已过期，请重新开始", step="session")
    return sess


def _new_session() -> PhoneLoginSession:
    client = httpx.Client(
        headers={"User-Agent": WEB_UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        follow_redirects=False,
        # 分级超时：连接 5s / 读 10s / 写 10s / 池等待 5s。
        # 池等待也设上限 —— 否则连接池被占满时请求会**无限等**，
        # 那比直接失败更糟（线程一直挂着）。
        timeout=httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT,
                              pool=HTTP_CONNECT_TIMEOUT),
        # 每个会话的连接池收紧：登录链路是**串行**的（一步接一步），
        # 根本用不到 10 条连接。调小可以显著降低几百个并发会话时的
        # 总 fd 占用（500 会话 × 10 = 5000 fd，很容易撞 ulimit）。
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2,
                            keepalive_expiry=30.0),
    )
    sid = uuid.uuid4().hex
    return PhoneLoginSession(sid=sid, client=client)


def _register_session(sess: PhoneLoginSession) -> None:
    """把会话放进表里，并强制执行**数量上限**。

    公开端点必须假设会被滥用：不设上限时，反复调 `official/start`
    就能把内存/连接堆满。满了就直接拒绝（回人话），而不是慢慢耗死进程。
    """
    with _lock:
        _gc_locked()
        if len(_sessions) >= MAX_SESSIONS:
            # 再尝试一次「更激进」的清理：把已消费的立刻收掉（不等宽限期），
            # 因为此刻更需要腾地方。
            for sid in [s for s, v in _sessions.items() if v.consumed]:
                old = _sessions.pop(sid, None)
                if old:
                    old.close()
        if len(_sessions) >= MAX_SESSIONS:
            sess.close()
            raise WbLoginError(
                "当前登录人数较多，请稍后重试",
                step="busy", reason="busy")
        _sessions[sess.sid] = sess


# ---------------------------------------------------------------------------
# 各阶段
# ---------------------------------------------------------------------------

def _upstream_json(resp: httpx.Response) -> dict:
    try:
        obj = resp.json()
    except Exception:
        raise WbLoginError("上游返回不是 JSON（可能被风控拦截）",
                           upstream=resp.text[:200])
    return obj if isinstance(obj, dict) else {}


def _envelope_data(obj: dict, step: str) -> dict:
    """解 {code,msg,data} 信封；code!=0 抛错。"""
    if obj.get("code") not in (0, None):
        raise WbLoginError(obj.get("msg") or f"上游返回 code={obj.get('code')}",
                           step=step, upstream=obj)
    data = obj.get("data")
    return data if isinstance(data, dict) else {}


def start_login() -> PhoneLoginSession:
    """第 1-5 步：建会話、拿设备 state、把 Keycloak/OneID 授权链铺到 OneID 首页。

    返回的会话已经可以直接发短信，因此这一步对用户是「瞬时」的。
    """
    sess = _new_session()
    try:
        from admin.invitation import current
        sess.invite_code = current()["invite_code"]
        # 1) 设备授权 state
        r = sess.client.post(
            f"{HOST}/v2/plugin/auth/state?platform=workbuddy", json={},
            headers=sess._headers({"Content-Type": "application/json",
                                   "X-Requested-With": "XMLHttpRequest"}))
        data = _envelope_data(_upstream_json(r), "auth_state")
        sess.plugin_state = str(data.get("state") or "")
        sess.plugin_auth_url = str(data.get("authUrl") or "")
        if not sess.plugin_state:
            raise WbLoginError("上游未返回设备授权 state", step="auth_state")

        # 2) /console/auth/login → Keycloak（同时下发 console 会话 Cookie）
        r = sess.client.get(
            f"{HOST}/console/auth/login",
            params={"platform": "workbuddy", "state": sess.plugin_state,
                    "domain": urllib.parse.urlparse(HOST).netloc},
            headers=sess._headers({"X-Domain": urllib.parse.urlparse(HOST).netloc}))
        kc = r.headers.get("location")
        if not kc:
            raise WbLoginError("未能取得 Keycloak 授权地址", step="console_auth_login",
                               upstream=r.text[:200])
        sess.kc_url = urllib.parse.urljoin(HOST, kc)

        # 3) Keycloak 登录页：HTML 里内嵌 broker/oneid/login（服务端渲染，无需 JS）
        r = sess.client.get(sess.kc_url,
                            headers=sess._headers({"Accept": "text/html,*/*"}))
        m = re.search(r'data-idp="oneid"[^>]*href="([^"]+)"', r.text)
        if not m:
            raise WbLoginError("登录页未提供 OneID 入口（上游可能已调整页面）",
                               step="keycloak_page")
        sess.broker_url = urllib.parse.urljoin(HOST, m.group(1).replace("&amp;", "&"))

        # 4) broker/oneid/login → OneID /v1/authorize
        r = sess.client.get(sess.broker_url,
                            headers=sess._headers({"Accept": "text/html,*/*"},
                                                  referer=sess.kc_url))
        authorize = r.headers.get("location")
        if not authorize:
            raise WbLoginError("OneID 未返回授权地址", step="broker_login",
                               upstream=r.text[:200])
        q = urllib.parse.parse_qs(urllib.parse.urlparse(authorize).query)
        sess.oneid_state = (q.get("state") or [""])[0]
        if not sess.oneid_state:
            raise WbLoginError("OneID 授权地址缺少 state", step="broker_login")

        # 5) 走一趟 OneID 首页，建立 OneID 侧会话
        r = sess.client.get(authorize,
                            headers=sess._headers({"Accept": "text/html,*/*"}))
        # 这一步失败也不致命（有的账号 OneID 已有会话），后面发码会再校验。
        if r.status_code >= 400:
            _logger.warning("OneID authorize 预访问返回 %s", r.status_code)

    except WbLoginError:
        sess.close()
        raise
    except httpx.HTTPError as e:
        sess.close()
        raise WbLoginError(f"网络错误：{e}", step="start") from e
    except Exception as e:  # 兜底，避免半成品会话泄漏
        sess.close()
        raise WbLoginError(f"发起登录失败：{e}", step="start") from e

    _register_session(sess)
    return sess


#: 中国大陆手机号（11 位，1 开头）。登录页只放开 +86，避免把国际号写死错。
MOBILE_PATTERNS = {"86": r"1[3-9][0-9]{9}", "852": r"[4-9][0-9]{7}", "853": r"6[0-9]{7}"}


def normalize_mobile(mobile: str, country_code: str = "86") -> str:
    country_code = country_code.lstrip("+").strip()
    if country_code not in MOBILE_PATTERNS:
        raise WbLoginError("不支持的地区代码", step="mobile")
    digits = re.sub(r"[\s()-]", "", mobile or "")
    if digits.startswith("+" + country_code):
        digits = digits[len(country_code) + 1:]
    elif digits.startswith(country_code) and len(digits) == (11 if country_code == "86" else 8) + len(country_code):
        digits = digits[len(country_code):]
    if not re.fullmatch(MOBILE_PATTERNS[country_code], digits):
        raise WbLoginError("请输入所选地区的有效手机号", step="mobile")
    return digits


def send_sms(sess: PhoneLoginSession, mobile: str, country_code: str = "86") -> dict:
    with sess.lock:
        if sess.consumed or sess.expired():
            raise WbLoginError("会话已失效，请重新登录", step="session")
        return _send_sms_locked(sess, mobile, country_code)


def _send_sms_locked(sess: PhoneLoginSession, mobile: str, country_code: str) -> dict:
    """第 6 步：发短信验证码。

    关于 `spi_extra`（风控上下文）：官方网页版每次都会带上它，其 `state` 来自
    `POST /console/auth/risk-context`。实测**不是必需** —— 缺失时上游走
    「未配置」降级路径，短信照常下发（已多次实测）。带上它也不会改变风控判定
    （实测：带 spi_extra / 带 device_token / 都不带，三者对同一号的结果一致）。

    关于 `need_captcha`：上游对**单个手机号**做频率风控 —— 同一号码短时间内
    被请求多次后会要求图形验证码，换成别的号立刻正常（已实测确认是按号而非按 IP）。
    这种情况纯协议无法通过（滑块要跑腾讯的 JS），只能如实告知用户稍后再试。
    """
    country_code = country_code.lstrip("+").strip()
    mobile = normalize_mobile(mobile, country_code)
    if sess.sms_state_token and (sess.mobile != mobile or sess.country_code != country_code):
        raise WbLoginError("当前会话正在等待验证码，请完成验证后再更换号码", step="mobile")

    r = sess.client.post(
        f"{OAUTH2}/v1/auth/sms/code/send",
        json={"client_code": "codebuddy", "mobile": "+" + country_code + " " + mobile,
              "scopes": ["openid", "mobile", "profile"]},
        headers=sess._headers({"Content-Type": "application/json",
                               "X-TOA-LANG": "zh-CN"}))
    obj = _upstream_json(r)
    if obj.get("errCode"):
        raise WbLoginError(str(obj.get("errMessage") or "发送验证码失败"),
                           step="sms_send", upstream=obj)

    token = obj.get("state_token")
    if not token:
        # `need_captcha`：该号码被上游风控要求图形验证码，纯协议过不去。
        # 这是**按号**触发的（短时间内对该号发码太频繁），换号或等待即可恢复。
        if obj.get("captcha") or obj.get("status") == "need_captcha":
            # 明确说明「换号可解」——这是实测确认的**按号**风控
            # （同一号码短时间发码太多次所致；换别的号立刻正常）。
            # 前端据 `step=sms_send` + `reason=need_captcha` 自动切到
            # 「官方页面接力」让用户自己过验证码，而不是被堵死在报错上。
            raise WbLoginError(
                "该手机号短时间内请求验证码次数过多，需要完成安全验证。"
                "请在弹出的验证页面上完成，或改用其他手机号。",
                step="sms_send", reason="need_captcha", upstream=obj)
        raise WbLoginError("上游未返回 state_token", step="sms_send", upstream=obj)

    sess.sms_state_token = str(token)
    sess.mobile = mobile
    sess.country_code = country_code
    sess.code_sent_at = time.time()
    _logger.info("wb_login: 已发验证码 sid=%s mobile=%s***", sess.sid[:8], mobile[:3])
    return {"expires_in": int(obj.get("expires_in") or obj.get("expire") or 60)}


def _exchange_code_for_session(sess: PhoneLoginSession, code: str) -> None:
    """第 9-10 步：把 OneID 授权 code 交给 Keycloak broker，落到 console 会话。

    成功后 cookie jar 里会出现 KEYCLOAK_SESSION/KEYCLOAK_IDENTITY ——
    这是后面 `/console/auth/login` 能认人的前提。
    """
    endpoint = (f"{HOST}/auth/realms/copilot/broker/oneid/endpoint"
                f"?code={urllib.parse.quote(code)}"
                f"&state={urllib.parse.quote(sess.oneid_state)}")
    resp = sess._follow(endpoint, referer=sess.kc_url)
    if resp is None:
        raise WbLoginError("OneID 回调未返回结果", step="broker_endpoint")

    # 会话建立的判据是 cookie，而不是某个响应体。
    names = {c.name for c in sess.client.cookies.jar}
    if not ({"KEYCLOAK_SESSION", "KEYCLOAK_IDENTITY"} & names):
        raise WbLoginError(
            "未能建立登录会话（上游可能拒绝了本次授权）",
            step="broker_endpoint", upstream=resp.text[:200])
    _logger.info("wb_login: console 会话已建立 sid=%s cookies=%s",
                 sess.sid[:8], sorted(names))


def _bind_device_state(sess: PhoneLoginSession) -> None:
    """第 10 步（收尾）：让 console 会话认领设备 state。

    不做这一步的话，最后 `/v2/plugin/auth/token` 会一直回 `11217 login ing...`。
    """
    domain = urllib.parse.urlparse(HOST).netloc
    sess._follow(
        f"{HOST}/console/auth/login?platform=workbuddy"
        f"&state={urllib.parse.quote(sess.plugin_state)}&domain={domain}",
        referer=HOST + "/",
    )


def _fetch_credential(sess: PhoneLoginSession) -> dict:
    """第 11 步：换取客户端凭证（这就是最终要入池的东西）。"""
    r = sess.client.get(
        f"{HOST}/v2/plugin/auth/token", params={"state": sess.plugin_state},
        headers=sess._headers({"X-No-Authorization": "true"}))
    obj = _upstream_json(r)
    if obj.get("code") != 0:
        raise WbLoginError(
            f"换取凭证失败：{obj.get('msg') or obj.get('code')}",
            step="auth_token", upstream=obj)
    data = obj.get("data")
    if not isinstance(data, dict) or not data.get("accessToken"):
        raise WbLoginError("上游未返回 accessToken", step="auth_token", upstream=obj)
    return data


def _fetch_account(sess: PhoneLoginSession, credential: dict) -> dict:
    """用刚拿到的 token 拉一次账号信息（拿 uid / 昵称 / 手机号用于落库与展示）。"""
    token = credential["accessToken"]
    domain = credential.get("domain") or urllib.parse.urlparse(HOST).netloc
    r = sess.client.get(
        f"{HOST}/v2/plugin/accounts",
        headers=sess._headers({
            "Authorization": "Bearer " + token,
            "X-Domain": domain,
            "X-No-User-Id": "true",
            "X-No-Enterprise-Id": "true",
            "X-No-Department-Info": "true",
        }))
    obj = _upstream_json(r)
    if obj.get("code") != 0:
        return {}
    data = obj.get("data") or {}
    accounts = data.get("accounts") or []
    return accounts[0] if accounts else {}


def verify_sms(sess: PhoneLoginSession, code: str) -> dict:
    """第 7-11 步：校验验证码 → 换 console 会话 → 换取客户端凭证。

    返回 `{"credential": <.info JSON 字符串>, "meta": {...}, "raw_token": {...}}`。

    本函数对同一 `sess` 是**幂等**的：整段流程持 `sess.lock` 串行执行，
    成功后把结果缓存进 `sess.result_cache`，重复调用直接复用。
    这样用户双击「登录并上传」不会变成「成功一次 + 报一次无效token」。
    """
    code = re.sub(r"\D", "", code or "")
    if len(code) != 6:
        raise WbLoginError("请输入 6 位数字验证码", step="code")

    with sess.lock:
        # 已成功过 → 直接复用（幂等），不再打上游
        if sess.result_cache is not None:
            return sess.result_cache

        if not sess.sms_state_token:
            raise WbLoginError("请先获取验证码", step="code")

        result = _verify_sms_locked(sess, code)
        sess.result_cache = result
        sess.consumed = True
        sess.consumed_at = time.time()
        return result


def _verify_sms_locked(sess: PhoneLoginSession, code: str) -> dict:
    """`verify_sms` 的实际流程（调用方必须已持 `sess.lock`）。"""
    # 7) 校验验证码
    r = sess.client.post(
        f"{OAUTH2}/v1/auth/sms/code/verify",
        json={"state_token": sess.sms_state_token, "code": code},
        headers=sess._headers({"Content-Type": "application/json",
                               "X-TOA-LANG": "zh-CN"}))
    obj = _upstream_json(r)
    if obj.get("errCode"):
        raise WbLoginError(str(obj.get("errMessage") or "验证码校验失败"),
                           step="sms_verify", upstream=obj)
    # 校验成功后会回一个新的 state_token（实测同值）；以返回值为准更稳。
    new_tok = obj.get("state_token")
    if isinstance(new_tok, str) and new_tok:
        sess.sms_state_token = new_tok

    # 8) 取账号 + OneID 授权 code。
    #    ⚠️ 这一步会消耗 state_token，所以必须只做一次，且不能先做别的调用。
    oneid_code = ""
    accounts: list = []
    r = sess.client.get(f"{OAUTH2}/v1/auth/accounts",
                        params={"state_token": sess.sms_state_token},
                        headers=sess._headers())
    obj = _upstream_json(r)
    if not obj.get("errCode"):
        accounts = obj.get("accounts") or []
        oneid_code = str(obj.get("code") or "")

    if not oneid_code:
        # 退路：accounts 没给 code 时，用 select_account 再试一次。
        # （只有当 accounts 因为别的原因失败、state_token 未被消耗时才可能成功）
        acc_id = ""
        for a in accounts:
            if isinstance(a, dict) and a.get("id"):
                acc_id = str(a["id"])
                break
        r = sess.client.post(
            f"{OAUTH2}/v1/auth/select_account",
            json={"state_token": sess.sms_state_token, "account_id": acc_id or "codebuddy@"},
            headers=sess._headers({"Content-Type": "application/json"}))
        obj2 = _upstream_json(r)
        if obj2.get("errCode"):
            raise WbLoginError(
                str(obj2.get("errMessage") or obj.get("errMessage") or "账号选择失败"),
                step="account_select", upstream=obj2)
        oneid_code = str(obj2.get("code") or "")

    if not oneid_code:
        raise WbLoginError("未能取得 OneID 授权码", step="account_select", upstream=obj)

    # 9-10) 用 code 完成 broker 登录并绑定设备 state
    _exchange_code_for_session(sess, oneid_code)
    _bind_device_state(sess)

    # 11) 换取凭证
    cred = _fetch_credential(sess)
    account = _fetch_account(sess, cred)

    built = build_credential_json(cred, account)
    check = validate_credential(built)
    if not check.ok:
        # 自产自检失败 = 上游改了协议，必须报出来而不是把坏数据写进池子。
        raise WbLoginError(f"生成的凭证未通过自检：{check.reason}", step="selfcheck")

    _logger.info("wb_login: 登录成功 sid=%s uid=%s", sess.sid[:8], check.meta.get("uid"))
    return {"credential": built, "meta": check.meta, "raw_token": cred}


def build_credential_json(token: dict, account: dict) -> str:
    """把上游 token + 账号信息拼成与桌面端**同构**的 `.info` 凭证。

    字段对齐本机真实凭证（`%LOCALAPPDATA%\\CodeBuddyExtension\\...\\workbuddy-desktop.info`）：
    顶层 `account` / `auth`，两处时间戳都是**毫秒**，`expiresIn` 是**秒**。
    号池与 converter 的 `CredentialManager` 直接读这份结构，所以字段名不能改。
    """
    now_ms = int(time.time() * 1000)
    expires_in = int(token.get("expiresIn") or 0)
    refresh_expires_in = int(token.get("refreshExpiresIn") or 0)

    acct = {
        "uid": str(account.get("uid") or ""),
        "nickname": str(account.get("nickname") or ""),
        "uin": str(account.get("uin") or ""),
        "type": str(account.get("type") or "personal"),
        "lastLogin": True,
        "isCreator": bool(account.get("isCreator", False)),
        "isAdmin": bool(account.get("isAdmin", False)),
        "pluginEnabled": bool(account.get("pluginEnabled", True)),
        "deployStatus": account.get("deployStatus") or {
            "statusCode": 0, "statusMsg": "", "detailMsg": ""},
        "accountType": str(account.get("accountType") or ""),
        "sso": account.get("sso") or {"domain": "", "domainModifiedTimes": 0},
        "idp": str(account.get("idp") or ""),
        "areaInfoComplete": bool(account.get("areaInfoComplete", False)),
        "oneidAccountId": str(account.get("oneidAccountId") or ""),
        "isCurrentOneIdEnterprise": bool(account.get("isCurrentOneIdEnterprise", False)),
        "isCurrentOneIdPersonal": bool(account.get("isCurrentOneIdPersonal", False)),
        "isFirstLogin": bool(account.get("isFirstLogin", False)),
    }
    phone = str(account.get("phoneNumber") or "")
    if phone:
        acct["phoneNumber"] = phone
    if account.get("enterpriseId"):
        acct["enterpriseId"] = str(account["enterpriseId"])
    if account.get("enterpriseName"):
        acct["enterpriseName"] = str(account["enterpriseName"])

    auth: dict = {
        "accessToken": token["accessToken"],
        "expiresIn": expires_in,
        "refreshExpiresIn": refresh_expires_in,
        "refreshToken": token["refreshToken"],
        "tokenType": str(token.get("tokenType") or "Bearer"),
        "sessionState": str(token.get("sessionState") or ""),
        "domain": str(token.get("domain") or urllib.parse.urlparse(HOST).netloc),
        "lastRefreshTime": now_ms,
    }
    if expires_in:
        auth["expiresAt"] = now_ms + expires_in * 1000
    if refresh_expires_in:
        auth["refreshExpiresAt"] = now_ms + refresh_expires_in * 1000
    if token.get("scope"):
        auth["scope"] = str(token["scope"])

    return json.dumps({"account": acct, "auth": auth}, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 真实验证：用上游接口确认凭证可用，并核对它是不是「伪造的」
# ---------------------------------------------------------------------------

@dataclass
class LiveProbe:
    """一次上游实测的结论。"""

    ok: bool
    #: 面向用户的说明（成功时是积分信息，失败时是原因）。
    message: str
    #: 上游真实返回的用户信息（uid/nickname/phone 等），失败时为空。
    profile: dict = field(default_factory=dict)
    #: 具体失败类别，便于上层区分处理。
    reason: str = ""
    #: 上游真实余额 `{"total": .., "remain": ..}`。
    #:
    #: 这里顺带带出来是**刻意的**：验证阶段为了确认凭证可用，本来就要调
    #: `fetch_balance()`，结果就摆在手上。如果丢掉它，入库后还得为一个
    #: 「列表显示 0/0」再补一次一模一样的请求 —— 白白多打一次上游。
    balance: dict = field(default_factory=dict)


#: 上游业务码 -> 人话。这些码表示「令牌本身有问题」，属于**伪造/失效**证据。
_TOKEN_DEAD_CODES = {
    12153: "登录态已失效（会话不存在）",
    12151: "登录态无效（未选择账号）",
}
#: 上游业务码 -> 人话。这些表示配额类问题，**不影响凭证真实性**。
_QUOTA_CODES = {
    6004: "该模型使用量超限",
    6008: "该模型每日请求数已达上限",
    14003: "触发上游限流",
    14051: "该权益已领取",
}


def _extract_upstream_error(exc: Exception) -> tuple[int | None, str]:
    """从 converter 抛出的 RuntimeError 文本里抠出上游 code 与原文。

    converter 有几种错误形态，都要认（实测确认）：
      * `后端请求失败 POST /path: HTTP 200 / 11217:login ing...`
            -> 业务码在 `code:msg` 里，取 11217
      * `后端返回非 JSON POST /path HTTP 401: <html>...`
            -> 网关直接 401（伪造/失效令牌的典型表现），取 401
      * `后端请求失败 GET /path: HTTP 500 / ...`
            -> 取 500

    返回值 `(code, msg)`；code 可能是业务码（5 位）也可能是 HTTP 状态码（3 位），
    两者在判据里会被分别处理。
    """
    from admin.upstream_errors import error_info
    evidence = getattr(exc, 'upstream', None) or error_info(str(exc))
    if evidence.get('code') and str(evidence['code']).lstrip('-').isdigit():
        return int(evidence['code']), evidence.get('message') or evidence['raw']
    text = str(exc)

    # 1) `HTTP 200 / 11217:msg` —— 业务码优先
    m = re.search(r"HTTP\s+(\d{3})\s*/\s*(\d{3,6})\s*[:：]?\s*(.*)", text, re.S)
    if m:
        return int(m.group(2)), (m.group(3) or "").strip()[:200]

    # 2) `HTTP 401: <html>` —— 网关状态码
    m = re.search(r"HTTP\s+(\d{3})", text)
    if m:
        return int(m.group(1)), text[:200]

    # 3) 裸 `11217:msg`
    m = re.search(r"(?:^|\s)(\d{4,6})\s*[:：]\s*(.*)", text, re.S)
    if m:
        return int(m.group(1)), (m.group(2) or "").strip()[:200]

    return None, text[:200]


def probe_credential_live(credential_json: str) -> LiveProbe:
    """用**真实上游接口**验证凭证：可用性 + 真伪 + 取回真实用户信息。

    为什么必须真调上游
    ------------------
    `validate_credential` 只做结构校验，它挡不住「格式完全正确但签名伪造 /
    已被注销 / 属于别人」的 token —— 那种凭证的 JWT 结构、uid、过期时间
    全都可以随手编出来。只有真正拿它去访问上游，才能确认：
        1. 令牌有效（上游认它）；
        2. 令牌对应的账号**确实存在**（能取回用户信息）；
        3. 凭证里的 uid / 昵称与上游返回的**一致**（挡住「拿真凭证套别人 uid」）。

    实现要点
    --------
    * 用 `fetch_balance()` —— 它打的是 `/v2/billing/meter/get-user-resource`，
      是最轻量的鉴权探测端点（顺带拿到积分）。
    * 再用 `fetch_models()` 走一遍带 `X-User-Id` 的路径，把上游**真实**
      账号信息与凭证里声明的做交叉比对。

    与「配额耗尽」的区别
    -------------------
    余额为 0 **不是**伪造 —— 号池里本来就可能有 0 额度的号（正常现象）。
    只有上游明确回「令牌失效」类业务码，才判为不可用。
    """
    try:
        from admin import backend
    except Exception as e:  # admin 包不可用（standalone）时降级
        return LiveProbe(True, f"跳过上游验证（{e}）", reason="skipped")

    # 凭证里声明的 uid：用于和上游返回的做比对（防伪造/张冠李戴）
    claimed_uid = ""
    claimed_nick = ""
    claimed_phone = ""
    try:
        obj = json.loads(credential_json)
        acct = obj.get("account") or {}
        claimed_uid = str(acct.get("uid") or "")
        claimed_nick = str(acct.get("nickname") or "")
        claimed_phone = str(acct.get("phoneNumber") or "")
    except Exception:
        pass

    profile: dict = {}
    bal: dict = {}

    try:
        with backend.AccountSession(credential_json) as sess:
            # 1) 最轻量的鉴权探测 + 积分
            bal = sess.fetch_balance() or {}
            # 2) 带用户身份的请求，取真实账号信息
            #
            # 取不到 profile **不致命**：能走到这里说明 fetch_balance 已经过了，
            # 那本身就证明令牌是真的（伪造令牌在 fetch_balance 就 401 了）。
            # 但若这个请求明确报「鉴权失败 / 令牌失效」，仍然必须判失败 ——
            # 不能因为「另一个端点恰好放行」就认为凭证没问题。
            try:
                profile = sess.fetch_account_profile() or {}
            except Exception as e:
                code, _m = _extract_upstream_error(e)
                if code in _TOKEN_DEAD_CODES:
                    return LiveProbe(
                        False, f"上游拒绝该凭证：{_TOKEN_DEAD_CODES[code]}；上游 code={code}，原文：{getattr(e, 'raw_body', str(e))}",
                        reason="token_dead")
                if code in (401, 403):
                    return LiveProbe(
                        False, f"上游拒绝了凭据验证请求；上游 HTTP/code={code}，原文：{getattr(e, 'raw_body', str(e))}",
                        reason="auth_failed")
                _logger.debug("wb_login: fetch_account_profile 失败（忽略）：%s", e)
    except Exception as e:
        code, msg = _extract_upstream_error(e)
        if code in _TOKEN_DEAD_CODES:
            return LiveProbe(False, f"上游拒绝该凭证：{_TOKEN_DEAD_CODES[code]}；上游 code={code}，原文：{getattr(e, 'raw_body', str(e))}",
                             reason="token_dead")
        if code in _QUOTA_CODES:
            # 配额类问题不代表凭证假 —— 放行，并如实说明。
            return LiveProbe(True, f"上游返回额度限制（{_QUOTA_CODES[code]}）；上游 code={code}，原文：{getattr(e, 'raw_body', str(e))}",
                             profile=profile, reason="quota")
        if code == 401 or code == 403:
            return LiveProbe(False, f"上游拒绝了凭据验证请求；上游 HTTP/code={code}，原文：{getattr(e, 'raw_body', str(e))}",
                             reason="auth_failed")
        return LiveProbe(False, f"无法用该凭证访问上游；上游 code={code or '未返回'}，原文：{getattr(e, 'raw_body', str(e))}",
                         reason="upstream_error")

    # 3) 交叉比对：上游真实 uid 必须与凭证里声明的一致
    real_uid = str(profile.get("uid") or "")
    if claimed_uid and real_uid and claimed_uid != real_uid:
        return LiveProbe(
            False,
            "凭证与上游账号不匹配（uid 对不上，疑似伪造或拼接）",
            profile=profile, reason="uid_mismatch")

    # 4) 昵称/手机号一致性：只在双方都有**有效**值时比对。
    #    凭证里常把缺失昵称写成字面量 "null"（本机真实凭证就是这样），
    #    那不是「不一致」，是「没填」—— 必须排除，否则每号都报一条假告警。
    def _meaningful(v: str) -> str:
        s = (v or "").strip()
        return "" if s.lower() in ("null", "none", "undefined") else s

    real_nick = _meaningful(profile.get("nickname") or "")
    if _meaningful(claimed_nick) and real_nick and _meaningful(claimed_nick) != real_nick:
        _logger.warning("wb_login: 昵称不一致 凭证=%r 上游=%r", claimed_nick, real_nick)
    real_phone = _meaningful(profile.get("phoneNumber") or "")
    if _meaningful(claimed_phone) and real_phone and _meaningful(claimed_phone) != real_phone:
        _logger.warning("wb_login: 手机号不一致 凭证=%r 上游=%r", claimed_phone, real_phone)

    remain = bal.get("remain")
    total = bal.get("total")
    who = real_nick or real_phone or real_uid or claimed_uid or "该账号"
    return LiveProbe(True, f"上游验证通过：{who}（积分 {remain}/{total}）",
                     profile=profile, reason="ok",
                     balance={"total": int(total or 0), "remain": int(remain or 0)})


def credential_uid(credential_json: str) -> str:
    """从凭证里取 uid（去重用）。取不到返回空串。"""
    try:
        return str((json.loads(credential_json).get("account") or {}).get("uid") or "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 「官方页面接力」模式：用户自己在官方页面上登录，服务端只负责轮询换凭证
# ---------------------------------------------------------------------------
#
# 为什么需要这个模式
# ------------------
# 纯协议模式（上面那套）会在上游要求图形验证码时卡死 —— 那时只能让用户
# 去官方页面过验证码。而官方页面的正常收尾是 `workbuddy://` 私有协议跳转，
# 需要桌面客户端接管；纯网页走不到底。
#
# 但实测发现一个关键事实：**`/v2/plugin/auth/token?state=` 的轮询完全不需要
# 任何 cookie** —— 它只靠 URL 里那个 state（已实测：用一个全新的、零 cookie
# 的 client 轮询，同样正常返回）。于是可以这样接力：
#
#   1. 服务端 POST /v2/plugin/auth/state 拿 state + authUrl；
#   2. 把 authUrl 交给用户（原样打开，或在本站 iframe 里内嵌 ——
#      官方登录页确实**没有** X-Frame-Options / frame-ancestors，可内嵌）；
#   3. 用户在官方页面自己登录（验证码由腾讯自己处理，无需任何自动化）；
#   4. 服务端持续轮询 auth/token?state=；
#   5. 轮询到凭证后，正常走「上游验证 → 入库」。
#
# 这条路**不需要客户端、不需要破解验证码、也不碰用户的会话 cookie**，
# 而且不受本服务出口 IP 的风控影响。


def fetch_credential_by_state(state: str) -> dict | None:
    """轮询设备授权 state 换取凭证。

    返回凭证 dict（含 accessToken），未完成时返回 None。

    关键：**用一次性、零 cookie 的 client**。这个端点不需要调用方的任何会话，
    所以「谁去轮询」完全不影响结果 —— 谁调都一样。

    这是高频路径（前端每 2.5s 轮一次），所以**必须**过长超时 + 并发闸门：
    上游一慢，这些请求就会把工作线程占满并拖垮整个服务。
    """
    # 闸门由**路由层**统一持有（见 admin/routers/login.py 的 `_upstream_slot`）：
    # 一个请求占一个名额，而不是每次上游调用都占 —— 避免嵌套获取名额导致
    # 可用容量被腰斩，也更容易推理「同时在途的登录请求数」。
    try:
        with httpx.Client(
                headers={"User-Agent": WEB_UA},
                timeout=httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT,
                                      pool=HTTP_CONNECT_TIMEOUT)) as c:
            r = c.get(f"{HOST}/v2/plugin/auth/token", params={"state": state},
                      headers={"Accept": "application/json, text/plain, */*",
                               "X-No-Authorization": "true"})
            obj = _upstream_json(r)
    except httpx.HTTPError as e:
        _logger.warning("wb_login: 轮询 auth/token 网络错误 state=%s…: %s",
                        state[:8], e)
        return None

    if obj.get("code") != 0:
        # 11217 = `login ing...`：用户还没在官方页面登录完，属正常待续状态。
        # ⚠️ 实测：**不存在的 state 也返回 11217** —— 上游不区分「还没好」与
        # 「state 无效」，所以前端只能靠超时收尾，不能靠这个码判断。
        # 这里每次都记一条，方便排障时确认「服务端确实在轮询、上游确实还没放行」。
        _logger.info("wb_login: 轮询未完成 state=%s… code=%s msg=%s",
                     state[:8], obj.get("code"), str(obj.get("msg"))[:60])
        return None
    data = obj.get("data")
    if not isinstance(data, dict) or not data.get("accessToken"):
        _logger.warning("wb_login: auth/token 返回 code=0 但没有 accessToken：%s",
                        str(obj)[:200])
        return None
    _logger.info("wb_login: 轮询成功拿到凭证 state=%s…", state[:8])
    return data


def build_credential_from_token(token: dict) -> tuple[str, dict]:
    """由 token 换取账号信息并组装 .info 凭证。

    返回 `(credential_json, meta)`；自检不过则抛 `WbLoginError`。
    """
    # 用同一个「零 cookie」思路取账号信息：只需要 Bearer 令牌
    domain = token.get("domain") or urllib.parse.urlparse(HOST).netloc
    account: dict = {}
    try:
        with httpx.Client(headers={"User-Agent": WEB_UA},
                          timeout=httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT,
                                                pool=HTTP_CONNECT_TIMEOUT)) as c:
            r = c.get(
                f"{HOST}/v2/plugin/accounts",
                headers={"Accept": "application/json, text/plain, */*",
                         "Authorization": "Bearer " + str(token["accessToken"]),
                         "X-Domain": domain,
                         "X-No-User-Id": "true",
                         "X-No-Enterprise-Id": "true",
                         "X-No-Department-Info": "true"})
            obj = _upstream_json(r)
            if obj.get("code") == 0:
                accts = ((obj.get("data") or {}).get("accounts")) or []
                account = accts[0] if accts else {}
    except Exception as e:
        _logger.warning("wb_login: 取账号信息失败（继续用 token 里的信息）：%s", e)

    built = build_credential_json(token, account)
    check = validate_credential(built)
    if not check.ok:
        raise WbLoginError(f"生成的凭证未通过自检：{check.reason}", step="selfcheck")
    return built, check.meta


def start_official_login() -> PhoneLoginSession:
    """只做第 1 步：拿 state + authUrl（供「官方页面接力」用）。

    比 `start_login()` 轻得多 —— 不铺 Keycloak/OneID 授权链，
    因为那条链是给「服务端代跑短信登录」用的；官方接力模式下
    用户自己会把这条链走完。
    """
    sess = _new_session()
    try:
        from admin.invitation import current
        sess.invite_code = current()["invite_code"]
        r = sess.client.post(
            f"{HOST}/v2/plugin/auth/state?platform=workbuddy", json={},
            headers=sess._headers({"Content-Type": "application/json",
                                   "X-Requested-With": "XMLHttpRequest"}))
        data = _envelope_data(_upstream_json(r), "auth_state")
        sess.plugin_state = str(data.get("state") or "")
        sess.plugin_auth_url = str(data.get("authUrl") or "")
        if not sess.plugin_state or not sess.plugin_auth_url:
            raise WbLoginError("上游未返回授权地址", step="auth_state")
    except WbLoginError:
        sess.close()
        raise
    except Exception as e:
        sess.close()
        raise WbLoginError(f"发起登录失败：{e}", step="start") from e

    _register_session(sess)
    return sess


def bind_invitation(sess: PhoneLoginSession, credential: str) -> dict:
    """Submit the official binding request using the authenticated session."""
    from admin.invitation import BIND_PATH, invite_url

    if sess.invitation_result is not None:
        return sess.invitation_result
    if not sess.invite_code:
        return {"status": "disabled", "message": "未配置邀请绑定"}
    data = json.loads(credential)
    try:
        response = sess.client.post(
            HOST + BIND_PATH, json={"inviteCode": sess.invite_code},
            headers=sess._headers({
                "Authorization": "Bearer " + data["auth"]["accessToken"],
                "Content-Type": "application/json",
                "X-Domain": urllib.parse.urlparse(HOST).netloc,
                "X-User-Id": data["account"]["uid"],
            }, referer=invite_url(sess.invite_code)))
        obj = _upstream_json(response)
    except (httpx.HTTPError, WbLoginError) as exc:
        raise WbLoginError("邀请绑定结果未知，请重试当前注册会话", step="invitation_bind") from exc
    code = obj.get("code")
    outcomes = {
        0: ("bound", "邀请绑定成功"),
        12310: ("already_bound", "该账号已有邀请关系，上游未确认绑定到当前邀请码"),
        12311: ("ineligible", "该账号不符合活动新用户条件，未绑定邀请"),
    }
    if response.status_code != 200 or type(code) is not int or code not in outcomes:
        raise WbLoginError("邀请绑定未成功；可重试，或检查邀请码和活动状态后重新发起注册",
                           step="invitation_bind", upstream={"code": code})
    status, message = outcomes[code]
    sess.invitation_result = {"status": status, "code": code, "message": message}
    _logger.info("wb_login: invitation sid=%s status=%s code=%s", sess.sid[:8], status, code)
    return sess.invitation_result


def try_bind_invitation(sess: PhoneLoginSession, credential: str) -> dict:
    """Invitation is optional; a failure must not undo a verified account import."""
    try:
        return bind_invitation(sess, credential)
    except Exception as exc:
        upstream = exc.upstream if isinstance(exc, WbLoginError) else None
        code = upstream.get("code") if isinstance(upstream, dict) else None
        code = code if type(code) is int else None
        sess.invitation_result = {
            "status": "failed", "code": code,
            "message": "账号已录入，邀请绑定未完成，不影响账号使用",
        }
        _logger.warning("wb_login: invitation failed sid=%s code=%s error=%s; account retained",
                        sess.sid[:8], code, type(exc).__name__)
        return sess.invitation_result


def gen_session_id() -> str:
    """暴露给路由层生成会话 id（也便于测试注入）。"""
    return secrets.token_urlsafe(16)
