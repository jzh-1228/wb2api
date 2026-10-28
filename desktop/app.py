from __future__ import annotations

import ctypes
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

import webbrowser

from desktop.common import ROOT, VERSION, atomic_json, data_dir
from desktop.runtime import configure_node, ensure_node, ensure_webview2
from desktop.tray import TrayController, available as tray_available
from desktop.updates import UpdateService


class _NullTray:
    """Stand-in used before the real tray exists and when tray mode is off."""

    def set_state(self, state):
        pass

    def notify(self, message):
        pass

    state = "stopped"


def select_port(preferred: int) -> int:
    """Keep the configured port unless another listener or reservation owns it."""
    preferred = int(preferred)
    if not 1 <= preferred <= 65535:
        raise ValueError("服务端口必须在 1 到 65535 之间")
    with socket.socket() as sock:
        try:
            with socket.socket() as probe:
                probe.settimeout(.2)
                if probe.connect_ex(("127.0.0.1", preferred)) == 0:
                    raise OSError("Port already in use")
            sock.bind(("0.0.0.0", preferred))
        except OSError:
            sock.bind(("0.0.0.0", 0))
        return sock.getsockname()[1]


def save_port(base: Path, config: dict, port: int) -> None:
    if config.get("port") != port:
        # Write only after our own backend confirms readiness, never on a failed start.
        config["port"] = port
        atomic_json(base / "config.json", config)


def configure_environment(base: Path) -> dict:
    file = base / "config.json"
    if file.is_file():
        config = json.loads(file.read_text(encoding="utf-8"))
    else:
        config = {"port": 8790, "username": "desktop", "password": secrets.token_urlsafe(32),
                  "jwt_secret": secrets.token_urlsafe(48)}
        atomic_json(file, config)
    if "converter_api_key" not in config:
        config["converter_api_key"] = "wb-" + secrets.token_urlsafe(32)
        atomic_json(file, config)
    os.environ.update({"WORKBUDDY_DESKTOP": "1", "WORKBUDDY_DESKTOP_DATA": str(base),
                       "ADMIN_DATABASE_URL": "sqlite:///" + (base / "workbuddy.db").as_posix(),
                       "ADMIN_GATEWAY_LIMIT_BACKEND": "memory", "ADMIN_HOST": "0.0.0.0",
                       "ADMIN_USERNAME": config["username"], "ADMIN_PASSWORD": config["password"],
                       "ADMIN_JWT_SECRET": config["jwt_secret"], "PYTHONUTF8": "1",
                       "CONVERTER_API_KEY": config["converter_api_key"],
                       "CONVERTER_LOG": str(base / "logs/converter.log")})
    configure_node(base)
    os.chdir(base)
    return config


def setup_logging(base: Path):
    logs = base / "logs"
    logs.mkdir(exist_ok=True)
    handler = RotatingFileHandler(logs / "desktop.log", maxBytes=4*1048576,
                                 backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def parent_watchdog():
    pid = int(os.environ.get("WB_DESKTOP_PARENT", "0"))
    if not pid or os.name != "nt":
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        os._exit(0)
    kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
    os._exit(0)


def backend(port: int):
    base = data_dir()
    configure_environment(base)
    setup_logging(base)
    threading.Thread(target=parent_watchdog, daemon=True).start()
    import uvicorn
    from admin.server import app, _CONVERTER_EMBEDDED
    if not _CONVERTER_EMBEDDED:
        raise RuntimeError("内嵌网关未能加载，请检查打包依赖")
    from admin.db import init_db
    if not init_db():
        raise RuntimeError("本地数据库初始化失败")
    def runtime_watch():
        from desktop.runtime import node_dir
        for _ in range(600):
            if (node_dir(base) / "node.exe").is_file():
                from admin.turing_token import clear_cache, warmup
                clear_cache()
                warmup()
                return
            time.sleep(1)
    threading.Thread(target=runtime_watch, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=port, log_config=None, access_log=False,
                loop="asyncio", http="h11", ws="none", proxy_headers=False, timeout_graceful_shutdown=5)


SPLASH = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#e6f6f0,#f5f7fa 55%,#e8effa);color:#172b29;font:15px 'Segoe UI','Microsoft YaHei',sans-serif;display:grid;place-items:center;min-height:100vh}
main{width:min(480px,90vw);padding:40px;background:#fffffff0;border:1px solid white;border-radius:28px;box-shadow:0 20px 70px #163c3020}.tag{color:#148665;font-size:12px;letter-spacing:2px}h1{font-size:28px;margin:10px 0 26px}p{color:#657670;line-height:1.8}.bar{height:5px;background:#e6f5ed;border-radius:6px;overflow:hidden}.bar:after{content:'';display:block;width:40%;height:100%;background:#148665;animation:load 1.3s infinite}@keyframes load{to{transform:translateX(260%)}}
button{padding:11px 16px;border:0;border-radius:10px;cursor:pointer;background:#e8f5ef;color:#146e55;margin:4px 6px 4px 0}.hint{font-size:12px;color:#8a9792}textarea{width:100%;height:190px;border:1px solid #d7e4dd;border-radius:10px;padding:12px;resize:vertical}
</style><div class="pywebview-drag-region" style="position:fixed;top:0;left:0;right:100px;height:36px"></div><div style="position:fixed;top:0;right:0"><button aria-label="最小化" onclick="pywebview.api.window_action('minimize')">−</button><button aria-label="关闭窗口" onclick="pywebview.api.window_action('close')">×</button></div><main><div class="tag">你的本地 AI 网关</div><h1>wb2api</h1><p class="hint">v__VERSION__</p><p id="status">正在启动本地服务…</p><div class="bar" id="bar"></div><p class="hint">就绪后自动进入工作台，无需登录管理台。<br>首次运行可能稍慢，请保留账号数据。</p>
<button onclick="pywebview.api.retry_start()">重试启动</button><button id="diagnosticButton" hidden onclick="showDiagnostic()">启动诊断</button><section id="diagnostic" hidden><p>服务尚未启动也可复制或导出诊断。</p><textarea id="report" readonly aria-label="启动诊断"></textarea><button onclick="copyReport()">复制诊断</button><button onclick="downloadReport()">导出诊断</button></section></main>
<script>
const began=Date.now();let reading=false;
async function showDiagnostic(){try{const r=await pywebview.api.startup_diagnostic();document.getElementById('report').value=r.report;document.getElementById('diagnostic').hidden=false}catch(e){document.getElementById('status').textContent='诊断暂不可用，请重试'}}
function copyReport(){const r=document.getElementById('report');r.select();document.execCommand('copy')}
function downloadReport(){const u=URL.createObjectURL(new Blob([document.getElementById('report').value],{type:'text/plain;charset=utf-8'}));const a=document.createElement('a');a.href=u;a.download='wb2api-startup-diagnostic.txt';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000)}
setInterval(async()=>{if(reading||!window.pywebview?.api)return;reading=true;try{const s=await pywebview.api.startup_status();document.getElementById('status').textContent=s.message;document.getElementById('diagnosticButton').hidden=!(s.failed||Date.now()-began>30000);document.getElementById('bar').hidden=s.failed}catch(e){}finally{reading=false}},1000);
</script></html>""".replace("__VERSION__", VERSION)


class Bridge:
    """Expose methods only: pywebview recursively serializes public object attributes."""
    def __init__(self, controller):
        self._controller = controller

    def startup_status(self):
        return self._controller.startup_status()

    def save_text(self, filename, text, mime="application/json"):
        return self._controller.save_text(filename, text, mime)

    def startup_diagnostic(self):
        return self._controller.startup_diagnostic()

    def desktop_login(self):
        return self._controller.desktop_login()

    def check_updates(self):
        return self._controller.check_updates()

    def update_status(self):
        return self._controller.update_status()

    def retry_runtime(self):
        return self._controller.retry_runtime()

    def retry_start(self):
        return self._controller.retry_start()

    def download_update(self, release_id):
        return self._controller.download_update(release_id)

    def apply_update(self):
        return self._controller.apply_update()

    def window_action(self, action):
        window = self._controller.window
        if action == "minimize":
            window.minimize()
        elif action == "maximize":
            if str(window.native.WindowState) == "Maximized":
                window.restore()
            else:
                window.maximize()
        elif action == "close":
            window.destroy()
        elif action.startswith("resize-") and os.name == "nt":
            edges = {"left": 1, "right": 2, "top": 3, "top-left": 4, "top-right": 5,
                     "bottom": 6, "bottom-left": 7, "bottom-right": 8}
            edge = edges.get(action.removeprefix("resize-"))
            if edge and str(window.native.WindowState) == "Normal":
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
                user32.PostMessageW(window.native.Handle.ToInt64(), 0x0112, 0xF000 + edge, 0)


class DesktopAPI:
    def __init__(self, base: Path, config: dict):
        self.base, self.config = base, config
        self.window = None
        self.process = None
        self.port = 0
        self.message = "正在启动本地服务…"
        self.failed = False
        self.runtime_message = "正在准备 Node.js…"
        self.runtime_busy = False
        self.update_state = {"busy": False, "message": "", "ready": False}
        self.update_file = None
        self.tray_startup_update_check = False
        self.updates = UpdateService(base)
        self.closing = threading.Event()
        self.start_lock = threading.Lock()
        self.runtime_lock = threading.Lock()
        self.update_lock = threading.Lock()
        # Assigned immediately after construction; a no-op stub keeps every call
        # site safe even if construction fails before the tray is attached.
        self.tray = _NullTray()

    def startup_status(self):
        return {"message": self.message, "failed": self.failed}

    def save_text(self, filename, text, mime="application/json"):
        """Save only after the owner chooses a destination in the native picker."""
        current = urllib.parse.urlsplit(self.window.get_current_url() or "")
        if (current.scheme != "http" or current.netloc != f"127.0.0.1:{self.port}"
                or current.path != "/admin"
                or urllib.parse.parse_qs(current.query).get("desktop") != ["1"]):
            return {"ok": False, "error": "只允许本地工作台导出文件"}
        if not isinstance(text, str) or len(text.encode("utf-8")) > 32 * 1024 * 1024:
            return {"ok": False, "error": "导出文件过大"}
        import re
        import webview
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(filename))[:120]
        if Path(name).suffix.lower() not in ('.info', '.json', '.jsonl', '.csv', '.txt'):
            return {"ok": False, "error": "不支持的导出文件类型"}
        try:
            selected = self.window.create_file_dialog(webview.FileDialog.SAVE, save_filename=name)
            if not selected:
                return {"ok": False, "cancelled": True}
            target = selected[0] if isinstance(selected, (list, tuple)) else selected
            Path(target).write_bytes(text.encode("utf-8"))
            return {"ok": True}
        except Exception:
            return {"ok": False, "error": "文件保存失败，请更换保存位置后重试"}

    def startup_diagnostic(self):
        from admin.request_metrics import safe_error_text
        import platform
        report = f"wb2api {VERSION} 启动诊断\n系统：{platform.platform()}\n阶段：{self.message}\n环境：{self.runtime_message}\n"
        process = self.process
        report += f"服务进程：{process.poll() if process else '未启动'}（None 表示运行中）\n"
        log = self.base / "logs/backend.log"
        if log.is_file():
            with log.open("rb") as source:
                source.seek(max(0, log.stat().st_size - 8192))
                report += "\n本地服务日志末尾（凭据脱敏）：\n" + source.read(8192).decode("utf-8", errors="replace")
        return {"report": safe_error_text(report)}

    def desktop_login(self):
        if self.window.get_current_url() != f"http://127.0.0.1:{self.port}/admin?desktop=1":
            raise RuntimeError("只允许本地桌面界面登录")
        from datetime import datetime, timedelta, timezone
        import jwt
        # Local ownership authenticates the shell; no password form or login request.
        token = jwt.encode({"sub": self.config["username"], "local_shell": True},
                           self.config["jwt_secret"], algorithm="HS256")
        return {"token": token}

    def check_updates(self):
        result = self.updates.check()
        result.update(runtime=self.runtime_message, update=self.update_state,
                      startup_prompt_handled=self.tray_startup_update_check)
        return result

    def _check_tray_startup_update(self):
        """A tray-only launch checks too, without starting the API service."""
        try:
            result = self.updates.check()
            latest = result.get("latest")
            if not result.get("available") or not latest or self.closing.is_set():
                return
            choice = ctypes.windll.user32.MessageBoxW(
                None, f"发现新版本 {latest['tag']}（当前 v{VERSION}）。\n\n"
                "是否立即更新？\n选择“是”将下载、校验并重启客户端；选择“否”暂不更新。",
                "wb2api · 版本更新", 0x00010024)
            if choice != 6 or self.closing.is_set():  # IDYES
                return
            response = self.download_update(latest["id"])
            if response.get("error"):
                self.tray.notify(response["error"])
                return
            while self.update_state.get("busy"):
                if self.closing.wait(.25):
                    return
            if self.update_state.get("ready") and not self.closing.is_set():
                response = self.apply_update()
                if response.get("error"):
                    self.tray.notify(response["error"])
            else:
                self.tray.notify(self.update_state.get("message") or "更新下载失败，请稍后重试")
        except Exception:
            logging.exception("Tray startup update check failed")

    def update_status(self):
        return dict(self.update_state, runtime=self.runtime_message)

    def retry_runtime(self):
        if self.runtime_lock.acquire(blocking=False):
            threading.Thread(target=self._runtime_worker, daemon=True).start()
        return {"ok": True}

    def _runtime_worker(self):
        self.runtime_busy = True
        try:
            ensure_node(self.base, lambda message: setattr(self, "runtime_message", message))
            self.runtime_message = "Node.js 已就绪 · Python 与项目依赖已内置"
        except Exception as exc:
            self.runtime_message = str(exc)
            logging.exception("Node setup failed")
        finally:
            self.runtime_busy = False
            self.runtime_lock.release()

    def download_update(self, release_id):
        if not getattr(sys, "frozen", False):
            return {"error": "源码模式请使用 Releases 中的 EXE"}
        if not self.update_lock.acquire(blocking=False):
            return {"error": "更新下载正在进行"}
        self.update_state = {"busy": True, "message": "正在下载更新…", "ready": False}
        threading.Thread(target=self._download_worker, args=(release_id,), daemon=True).start()
        return {"ok": True}

    def _download_worker(self, release_id):
        try:
            self.update_file = self.updates.stage(release_id, lambda n, total: self.update_state.update(
                message=f"下载更新：{n / 1048576:.1f} / {total / 1048576:.1f} MB"))
            self.update_state.update(busy=False, ready=True, message="校验通过，点击重启安装")
        except Exception as exc:
            self.update_state.update(busy=False, ready=False, message=str(exc))
            logging.exception("Update download failed")
        finally:
            self.update_lock.release()

    def apply_update(self):
        if not self.update_file or not self.update_state["ready"]:
            return {"error": "请先完成下载和校验"}
        target = Path(sys.executable).resolve()
        # Preflight write permission before shutting down the current working client.
        probe = target.parent / f".wb-update-{os.getpid()}.tmp"
        try:
            probe.touch()
            probe.unlink()
            script = self.base / "updates" / "apply-update.ps1"
            script.write_text((ROOT / "desktop/apply-update.ps1").read_text(encoding="utf-8"),
                              encoding="utf-8-sig")
            # PyInstaller onefile uses a parent bootloader which owns the executable handle.
            parent_pid = int(os.environ.get("WB_BOOTLOADER_PID", os.getpid()))
            env = dict(os.environ, WB_UPDATE_SOURCE=str(self.update_file), WB_UPDATE_TARGET=str(target),
                       WB_UPDATE_PID=str(parent_pid), WB_UPDATE_LOG=str(self.base / "logs/update.log"))
            subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                              "-File", str(script)], env=env, creationflags=subprocess.CREATE_NO_WINDOW,
                             close_fds=True)
        except Exception as exc:
            return {"error": f"无法安装更新：{exc}"}
        def close_for_update():
            import webview
            # The hidden tray host also owns the EXE process. Leaving it alive
            # prevents the updater from replacing the running binary.
            for window in list(webview.windows):
                window.destroy()
        threading.Thread(target=close_for_update, daemon=True).start()
        return {"ok": True}

    def retry_start(self):
        if self.closing.is_set():
            return {"error": "客户端正在关闭"}
        if self.start_lock.acquire(blocking=False):
            threading.Thread(target=self._start_worker, daemon=True).start()
        return {"ok": True}

    def _start_worker(self):
        try:
            if self.closing.is_set():
                return
            self.failed = False
            self.message = "正在启动本地服务…"
            self.tray.set_state("starting")
            if self.process and self.process.poll() is None:
                self.process.terminate()
                self.process.wait(timeout=10)
            self.port = select_port(self.config.get("port", 8790))
            command = [sys.executable]
            if not getattr(sys, "frozen", False):
                command.append(str(ROOT / "desktop_entry.py"))
            command += ["--backend", str(self.port)]
            nonce = secrets.token_urlsafe(32)
            env = dict(os.environ, WB_DESKTOP_PARENT=str(os.getpid()), WB_DESKTOP_BOOT=nonce)
            output = (self.base / "logs/backend.log").open("w", encoding="utf-8")
            try:
                self.process = subprocess.Popen(command, env=env, stdout=output, stderr=output,
                                                creationflags=subprocess.CREATE_NO_WINDOW)
            finally:
                output.close()
            # Closing during Popen must not leave a backend started after stop() ran.
            if self.closing.is_set():
                self.stop()
                return
            deadline = time.monotonic() + 90
            while not self.closing.is_set() and time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("本地服务启动失败，详细原因见用户目录 logs/backend.log")
                try:
                    request = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/app/desktop-ready",
                                                      headers={"X-Desktop-Boot": nonce})
                    with urllib.request.urlopen(request, timeout=1) as response:
                        if not json.load(response).get("ready"):
                            continue
                    save_port(self.base, self.config, self.port)
                    self.tray.set_state("running")
                    if not self.window:
                        # Tray-only session: starting succeeded, so nothing else to show.
                        self.tray.notify(f"服务已启动 · 控制台端口 {self.port}")
                        return
                    self.window.load_url(f"http://127.0.0.1:{self.port}/admin?desktop=1")
                    if os.getenv("WB_DESKTOP_SMOKE"):
                        threading.Thread(target=self._smoke_test, daemon=True).start()
                    return
                except OSError:
                    self.closing.wait(.15)
            if not self.closing.is_set():
                raise RuntimeError("本地服务启动超时，可以点击重试")
        except Exception as exc:
            self.message = str(exc)
            self.failed = True
            self.tray.set_state("stopped")
            self.tray.notify(f"启动失败：{exc}")
            logging.exception("Backend startup failed")
            if not self.window:
                return
            try:
                self.window.evaluate_js("document.getElementById('status').textContent=" + json.dumps(self.message) +
                                        ";document.getElementById('bar').style.display='none'")
            except Exception:
                pass
        finally:
            self.start_lock.release()

    # -- tray-driven actions --------------------------------------------

    def _window_alive(self, window) -> bool:
        """pywebview drops closed windows from its registry; that is the truth."""
        import webview
        return window is not None and window in webview.windows

    def open_window(self):
        """Show the embedded console window, creating it on demand.

        Closing the window destroys it (pywebview has no reliable hide-on-close
        hook), but the backend keeps running and the tray reopens a fresh one.
        Window creation runs on the tray thread, so failures surface as a tray
        notification instead of an unhandled traceback.
        """
        import webview
        if self._window_alive(self.window):
            try:
                self.window.show()
                self.window.restore()
                return {"ok": True}
            except Exception:
                logging.exception("Failed to restore window")
        try:
            self.window = webview.create_window(
                f"wb2api · v{VERSION}", html=SPLASH, js_api=Bridge(self),
                width=1280, height=860, min_size=(900, 600),
                background_color="#f5f6f8", text_select=True, frameless=True, easy_drag=False)
        except Exception as exc:
            logging.exception("Window creation failed from tray")
            self.window = None
            self.tray.notify("窗口创建失败，请改用「在浏览器中打开」")
            return {"error": str(exc)}
        self.window.events.closed += self._on_window_closed
        if self.running():
            self.window.load_url(f"http://127.0.0.1:{self.port}/admin?desktop=1")
        else:
            self.start()
        return {"ok": True}

    def open_console(self):
        """Open the admin console in the user's default browser.

        Starting is asynchronous and the port can shift when the configured one
        is taken by another deployment (a restart picks a new port), so resolve
        *this instance's* actual port and only open once it accepts TCP. A
        timeout notifies instead of handing the browser a dead URL.

        The URL carries a short-lived JWT (?token=) because a plain browser has
        no pywebview bridge to sign in with: ?desktop=1 hides the login form
        behind an overlay that only clears once that bridge reports ready, so
        without a token the browser would sit on a blank boot screen forever.
        """
        if not self.running():
            self.start()
            # Wait for _start_worker to commit a port before probing it.
            deadline = time.monotonic() + 90
            while not self.port and time.monotonic() < deadline and not self.closing.is_set():
                time.sleep(.25)
        port = self.port
        if not port:
            self.tray.notify("服务未能启动，无法打开控制台（见日志目录）")
            return {"error": "服务未启动"}
        deadline = time.monotonic() + 60
        ready = False
        while time.monotonic() < deadline and not self.closing.is_set():
            with socket.socket() as probe:
                probe.settimeout(.25)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    ready = True
                    break
            if not self.running():
                break  # backend died while we waited; don't spin for 60s
            time.sleep(.5)
        if not ready:
            self.tray.notify(f"端口 {port} 未就绪，请稍后重试")
            return {"error": "服务未就绪"}
        from datetime import datetime, timedelta, timezone
        import jwt
        token = jwt.encode({"sub": self.config["username"], "local_shell": True},
                           self.config["jwt_secret"], algorithm="HS256")
        url = f"http://127.0.0.1:{port}/admin?token={token}"
        webbrowser.open(url)
        # Restarts can move the port; make sure the user knows which one is live.
        self.tray.notify(f"控制台已在浏览器打开（端口 {port}，免登录）")
        return {"ok": True, "url": url}

    def _on_window_closed(self):
        """Close button must not stop services - the app lives in the tray."""
        self.window = None

    def _state_monitor(self):
        """Reconcile the tray badge when the backend dies on its own."""
        while not self.closing.wait(2):
            if self.tray.state == "running" and not self.running():
                self.tray.set_state("stopped")
                self.tray.notify("本地服务已退出，可从托盘菜单重新启动")

    def quit(self):
        """Exit the application entirely (tray → 退出)."""
        self.stop()
        try:
            import webview
            for window in list(webview.windows):  # emptying the registry ends the loop
                try:
                    window.destroy()
                except Exception:
                    logging.exception("Failed to destroy window during quit")
        except Exception:
            logging.exception("Quit failed")
        return {"ok": True}

    def _smoke_test(self):
        result = {"ok": False, "version": VERSION, "port": self.port}
        try:
            for _ in range(60):
                if self.closing.wait(.5):
                    break
                ready = self.window.evaluate_js("document.getElementById('app') && "
                    "!document.getElementById('app').classList.contains('hidden') && "
                    "document.getElementById('appVersion').textContent.includes('v" + VERSION + "')")
                if ready:
                    result.update(ok=True, title=self.window.evaluate_js("document.title"),
                                  footer=self.window.evaluate_js("document.getElementById('appVersion').textContent"))
                    break
        except Exception as exc:
            result["error"] = str(exc)
        finally:
            atomic_json(Path(os.environ["WB_DESKTOP_SMOKE"]), result)
            self.window.destroy()

    def running(self) -> bool:
        """True only while the backend child process is alive."""
        return bool(self.process and self.process.poll() is None)

    def teardown_backend(self):
        """Terminate the backend child, escalating to kill if it ignores us.

        The venv interpreter is a shim that spawns the real runtime as a child,
        so a bare terminate() can orphan the actual uvicorn process; kill the
        whole tree instead. The port is dropped so the next start re-selects it.
        """
        if self.process and self.process.poll() is None:
            pid = self.process.pid
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        self.process = None
        self.port = 0  # the next start re-selects; stale values would misdirect the console

    def start(self):
        """Start services and load the console. Used by both splash and tray."""
        if self.closing.is_set():
            return {"error": "客户端正在关闭"}
        if self.start_lock.acquire(blocking=False):
            threading.Thread(target=self._start_worker, daemon=True).start()
            return {"ok": True}
        # A start is already in flight; report where things stand without queueing another.
        return {"ok": True, "busy": True}

    def restart(self):
        """Tear down the backend and bring it back on the same port."""
        if self.closing.is_set():
            return {"error": "客户端正在关闭"}
        if self.start_lock.acquire(blocking=False):
            threading.Thread(target=self._restart_worker, daemon=True).start()
            return {"ok": True}
        return {"ok": True, "busy": True}

    def _restart_worker(self):
        try:
            self.tray.set_state("starting")
            self.teardown_backend()
            self.start_lock.release()
            self.start()
        except Exception as exc:
            self.start_lock.release()
            self.tray.set_state("stopped")
            logging.exception("Restart failed")
            self.tray.notify(f"重启失败：{exc}")

    def stop_services(self):
        """Stop the backend but keep the app resident so it can be restarted."""
        self.teardown_backend()
        self.tray.set_state("stopped")
        self.tray.notify("服务已停止")
        if self._window_alive(self.window):
            # Back to the splash page; its 重试启动 button is still wired to us.
            try:
                self.window.load_html(SPLASH)
            except Exception:
                logging.getLogger("desktop.tray").debug("无法刷新停止界面", exc_info=True)
        return {"ok": True}

    def stop(self):
        self.closing.set()
        self.teardown_backend()


def run(tray_mode: bool = False):
    """桌面向前端入口。

    tray_mode: 双击 EXE 或开机自启时的默认路径 —— 只放托盘图标，不起服务、不开窗口，
    由用户在右键菜单里决定何时启动。窗口模式（不开托盘）保持旧行为以兼容 --smoke-test。
    """
    if os.name != "nt":
        raise RuntimeError("桌面 EXE 支持 Windows 10/11 x64")
    import msvcrt
    import webview
    base = data_dir()
    # Use a per-user lock rather than a PID file (stale PID files cannot block startup).
    lock = (base / "desktop.lock").open("a+b")
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        ctypes.windll.user32.MessageBoxW(None, "wb2api 已在运行，请查看系统托盘图标。", "wb2api", 64)
        lock.close()
        return
    api = tray = None
    try:
        config = configure_environment(base)
        setup_logging(base)
        ensure_webview2(base)
        api = DesktopAPI(base, config)
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True

        if tray_mode:
            tray = TrayController(api, ROOT, VERSION)
            api.tray = tray
            if not tray.start():
                ctypes.windll.user32.MessageBoxW(
                    None, "托盘组件不可用，将以窗口模式启动。\n\n数据和日志：" + str(base),
                    "wb2api", 48)
                tray_mode = False

        if not tray_mode:
            # Original path: window owns everything, closing it exits the app.
            window = webview.create_window(f"wb2api · v{VERSION}", html=SPLASH, js_api=Bridge(api),
                                          width=1280, height=860, min_size=(900, 600),
                                          background_color="#f5f6f8", text_select=True, frameless=True, easy_drag=False)
            api.window = window
            window.events.closed += api.stop
            webview.start(lambda: (api.retry_runtime(), api.retry_start()), gui="edgechromium",
                          private_mode=False, storage_path=str(base / "webview"),
                          icon=str(ROOT / "desktop/assets/app.ico"))
            return

        # Tray mode: no window, no services - both start from the tray menu.
        # pywebview's GUI loop exits once every window is gone, so an invisible
        # 1x1 host window keeps the process resident between console sessions.
        threading.Thread(target=api._state_monitor, name="tray-state", daemon=True).start()
        webview.create_window("__tray_host__", html="<!doctype html><title></title>",
                              width=1, height=1, frameless=True, hidden=True)
        tray.notify("wb2api 已在托盘运行，右键图标启动服务。")
        api.tray_startup_update_check = True
        def start_tray_shell():
            api.retry_runtime()
            threading.Thread(target=api._check_tray_startup_update,
                             name="startup-update", daemon=True).start()
        webview.start(start_tray_shell, gui="edgechromium",
                      private_mode=False, storage_path=str(base / "webview"),
                      icon=str(ROOT / "desktop/assets/app.ico"))
    except Exception as exc:
        logging.exception("Desktop failed")
        ctypes.windll.user32.MessageBoxW(None, str(exc) + f"\n\n数据和日志：{base}", "wb2api", 16)
    finally:
        if api:
            api.stop()
        if tray:
            tray.stop()
        lock.close()
