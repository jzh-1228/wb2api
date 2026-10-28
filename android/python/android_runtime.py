"""Embedded API runtime. No subprocess, desktop installer, Node or device scanning."""
from __future__ import annotations

import json
import errno
import logging
import os
import secrets
import socket
import threading
import time
import urllib.request
from pathlib import Path
from logging.handlers import RotatingFileHandler

_server = None
_thread = None
_state = None
_lock = threading.Lock()


def load_config(path: Path) -> dict:
    """Repair launcher metadata only; never discard the account database."""
    from desktop.common import atomic_json
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("Invalid launcher configuration")
    except (OSError, ValueError):
        if path.exists():
            backup = path.with_name(f"config.invalid-{time.time_ns()}.json")
            path.replace(backup)
        config = {}
    defaults = {"username": "mobile", "password": secrets.token_urlsafe(32),
                "jwt_secret": secrets.token_urlsafe(48)}
    for key, value in defaults.items():
        if not isinstance(config.get(key), str) or not config[key].strip():
            config[key] = value
    try:
        port = int(config.get("port", 8790))
    except (TypeError, ValueError):
        port = 8790
    config["port"] = port if 1 <= port <= 65535 else 8790
    atomic_json(path, config)
    return config


def healthy() -> bool:
    # Called on a native worker, never on the Android UI thread.
    state = _state
    if not (_thread and _thread.is_alive() and _server and _server.started and state):
        return False
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(state["origin"] + "/api/app/version", timeout=3) as response:
            return response.status == 200 and bool(json.load(response).get("version"))
    except (OSError, ValueError):
        return False


def health_status() -> str:
    """A slow loopback response is not proof that a live server needs restarting."""
    alive = bool(_thread and _thread.is_alive() and _server and _server.started
                 and not _server.should_exit and _state)
    active = len(_server.server_state.tasks) if alive else 0
    return json.dumps({'alive': alive, 'responsive': healthy() if alive else False,
                       'active_requests': active})


def start(directory: str, progress=None) -> str:
    global _server, _thread, _state
    def stage(text):
        if progress is not None:
            progress.startupProgress(text)
    with _lock:
        # Opening the Activity must not stop an in-flight conversation merely
        # because a loopback health request took too long while backgrounded.
        if (_thread and _thread.is_alive() and _server and _server.started
                and not _server.should_exit and _state):
            _state["token"] = _issue_token()
            return json.dumps(_state)
        if _thread and _thread.is_alive():
            _server.should_exit = True
            _thread.join(10)
            if _thread.is_alive():
                raise RuntimeError("旧服务尚未停止，请稍后重试（无需清除应用数据）")
        _state = None
        stage("正在读取本地配置（保留账号数据）")
        from desktop.common import atomic_json
        base = Path(directory) / "WorkBuddy2API"
        base.mkdir(parents=True, exist_ok=True)
        path = base / "config.json"
        config = load_config(path)
        nonce = secrets.token_urlsafe(32)
        os.environ.update({"WORKBUDDY_ANDROID": "1", "WB_DESKTOP_BOOT": nonce,
                           "ADMIN_DATABASE_URL": "sqlite:///" + (base / "workbuddy.db").as_posix(),
                           "ADMIN_GATEWAY_LIMIT_BACKEND": "memory", "ADMIN_HOST": "0.0.0.0",
                           "ADMIN_USERNAME": config["username"], "ADMIN_PASSWORD": config["password"],
                           "ADMIN_JWT_SECRET": config["jwt_secret"], "ADMIN_DEV_TOOLS": "0"})
        logs = base / "logs"
        logs.mkdir(exist_ok=True)
        handler = RotatingFileHandler(logs / "mobile.log", maxBytes=4*1048576, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
        stage("正在加载 API 与数据库模块")
        import uvicorn
        from mobile_http import MobileServer, MobileH11Protocol
        from admin.config import settings
        # Python modules survive an in-app service restart. If launcher metadata
        # was repaired, align the shared auth settings before issuing its JWT.
        settings.ADMIN_USERNAME = config["username"]
        settings.ADMIN_PASSWORD = config["password"]
        settings.JWT_SECRET = config["jwt_secret"]
        from admin.server import app
        stage("正在绑定本地 API 端口")
        sock = socket.socket()
        try:
            # Reuse our listening port after accepted connections enter TIME_WAIT.
            # SO_REUSEADDR does not permit sharing an active TCP listener.
            if os.name == "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            preferred = int(config.get("port", 8790))
            if not 1 <= preferred <= 65535:
                preferred = 8790
            try:
                sock.bind(("0.0.0.0", preferred))
            except OSError as error:
                # Windows' exclusive listeners report WSAEACCES in host tests.
                if error.errno != errno.EADDRINUSE and not (
                        os.name == "nt" and getattr(error, "winerror", None) == 10013):
                    raise
                sock.bind(("0.0.0.0", 0))
            port = sock.getsockname()[1]
            sock.listen(128)
            server = MobileServer(uvicorn.Config(app, host="0.0.0.0", port=port, log_config=None,
                                    access_log=False, loop="asyncio", http=MobileH11Protocol, ws="none",
                                    proxy_headers=False, timeout_graceful_shutdown=5))
            server.config.mobile_runtime = progress
            _server = server
            startup_failure = []
            def serve():
                try:
                    if progress is not None:
                        progress.prepareApiThread()
                    server.run(sockets=[sock])
                except BaseException as error:
                    from admin.request_metrics import exception_text
                    startup_failure.append(exception_text(error))
                    logging.getLogger(__name__).exception("本地服务线程退出")
                finally:
                    sock.close()
            _thread = threading.Thread(target=serve, daemon=True, name="mobile-local-api")
            _thread.start()
            stage("正在初始化数据库并等待 API 就绪")
            origin = f"http://127.0.0.1:{port}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and _thread.is_alive():
                try:
                    request = urllib.request.Request(origin + "/api/app/desktop-ready", headers={"X-Desktop-Boot": nonce})
                    with opener.open(request, timeout=1) as response:
                        if json.load(response).get("ready"):
                            break
                except OSError:
                    time.sleep(.1)
            else:
                reason = startup_failure[-1] if startup_failure else "等待 API 就绪超过 90 秒" if _thread.is_alive() else "API 线程已退出，请查看本地服务日志"
                raise RuntimeError("本地服务启动失败：" + reason + "（无需清除账号数据）")
            if config.get("port") != port:
                config["port"] = port
                atomic_json(path, config)
            _state = {"origin": origin, "token": _issue_token()}
            return json.dumps(_state)
        except Exception:
            if _server:
                _server.should_exit = True
            if _thread and _thread.is_alive():
                _thread.join(10)
            sock.close()
            raise


def stop():
    global _state, _server, _thread
    with _lock:
        if _server:
            _server.should_exit = True
        if _thread:
            _thread.join(10)
            if _thread.is_alive():
                raise RuntimeError("本地服务仍在停止中，保留原端口等待退出")
        _state = None
        _server = None
        _thread = None


def session_token() -> str:
    with _lock:
        if not _state:
            return ""
        _state["token"] = _issue_token()
        return _state["token"]


def _issue_token() -> str:
    import jwt
    from admin.config import settings
    return jwt.encode({"sub": settings.ADMIN_USERNAME, "local_shell": True},
                      settings.JWT_SECRET, algorithm="HS256")
