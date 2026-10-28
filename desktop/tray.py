"""System tray shell for the desktop client.

The stock client owns a single WebView2 window whose close button exits the
whole process, so it behaves like a browser tab you must keep on your desktop.
This module keeps the application resident in the notification area instead:

  * double-clicking the EXE only creates the tray icon - no window, no backend;
  * the tray menu owns the service lifecycle and can open the UI or console;
  * closing the window hides it instead of tearing down the backend;
  * everything here is optional: without pystray/Pillow the caller falls back
    to the original "close means quit" behaviour rather than failing to start.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import webbrowser
import winreg
from pathlib import Path

logger = logging.getLogger("desktop.tray")

AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "WorkBuddy2API"

STATE_LABELS = {"running": "运行中", "starting": "启动中", "stopped": "已停止"}
# The badge colour tracks service health so the icon reports state at a glance.
STATE_COLORS = {"running": (66, 192, 142), "starting": (222, 168, 62), "stopped": (110, 118, 129)}

try:  # import guard mirrors runtime availability check
    import pystray
    from PIL import Image, ImageDraw
except Exception:  # dependency is optional
    pystray = None
    Image = ImageDraw = None


def available() -> bool:
    """True when the tray can run in this interpreter."""
    return pystray is not None and Image is not None


# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------

def load_base_image(root: Path):
    """Prefer the packaged icon drawn at build time; fall back to drawing it."""
    for candidate in (root / "desktop/assets/app.ico", root / "desktop/assets/app.png"):
        try:
            if candidate.is_file():
                image = Image.open(candidate)
                image.load()
                return image.convert("RGBA")
        except Exception:
            logger.debug("Tray icon asset unusable: %s", candidate, exc_info=True)
    return draw_fallback()


def draw_fallback(size: int = 256):
    """Rounded gradient tile with the transfer glyph, echoing assets/app.svg.

    Implemented with vertical gradient lines (cheaper than per-pixel blending)
    because this runs on the startup path when no built icon exists.
    """
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    s = size / 256.0

    for y in range(size):
        t = y / max(size - 1, 1)
        r = int(66 + (20 - 66) * t)
        g = int(192 + (134 - 192) * t)
        b = int(142 + (101 - 142) * t)
        draw.line([(0, y), (size, y)], fill=(r, g, b, 255))
    draw.line([66 * s, 89 * s, 89 * s, 168 * s, 128 * s, 112 * s, 167 * s, 168 * s, 190 * s, 89 * s],
              fill=(255, 255, 255, 255), width=max(1, int(17 * s)), joint="curve")
    radius = 9 * s
    cx, cy = 128 * s, 73 * s
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=(214, 255, 240, 255))
    return image


def compose(base, state: str, box: int = 64):
    """Downscale the app mark and badge a corner dot reporting service state."""
    image = base.resize((box, box), Image.LANCZOS).convert("RGBA")
    draw = ImageDraw.Draw(image)
    color = STATE_COLORS.get(state, STATE_COLORS["stopped"])
    radius = max(4, box // 6)
    pad = max(1, box // 16)
    x0 = y0 = box - 2 * radius - pad
    # White ring keeps the dot legible on both light and dark tray themes.
    draw.ellipse([x0 - 2, y0 - 2, x0 + 2 * radius + 2, y0 + 2 * radius + 2], fill=(255, 255, 255, 235))
    draw.ellipse([x0, y0, x0 + 2 * radius, y0 + 2 * radius], fill=color + (255,))
    return image


# ---------------------------------------------------------------------------
# Autostart (HKCU Run)
# ---------------------------------------------------------------------------

def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as handle:
            value, _ = winreg.QueryValueEx(handle, AUTOSTART_NAME)
            return bool(value)
    except OSError:
        return False


def set_autostart(enabled: bool) -> bool:
    """Register or clear this EXE under HKCU Run. Requires a frozen build."""
    if not getattr(sys, "frozen", False):
        return False  # a source checkout has no stable entry point to register
    command = f'"{sys.executable}" --tray'
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE) as handle:
            if enabled:
                winreg.SetValueEx(handle, AUTOSTART_NAME, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(handle, AUTOSTART_NAME)
                except OSError:
                    pass
        return True
    except OSError:
        logger.warning("开机自启设置失败", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class TrayController:
    """Owns the tray icon; translates menu clicks into controller actions."""

    def __init__(self, api, root: Path, version: str):
        self.api = api
        self.root = root
        self.version = version
        self.icon = None
        self.state = "stopped"
        self._base_image = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> bool:
        if not available() or self.icon is not None:
            return False
        try:
            self._base_image = load_base_image(self.root)
            self.icon = pystray.Icon(
                "workbuddy2api",
                compose(self._base_image, self.state),
                self._title(),
                menu=pystray.Menu(
                    pystray.MenuItem("打开控制台", self._on_open_ui, default=True),
                    pystray.MenuItem("在浏览器中打开", self._on_open_browser),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("启动服务", self._on_start, enabled=lambda _: self.state == "stopped"),
                    pystray.MenuItem("停止服务", self._on_stop, enabled=lambda _: self.state != "stopped"),
                    pystray.MenuItem("重启服务", self._on_restart, enabled=lambda _: self.state == "running"),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("打开日志目录", self._on_open_logs),
                    pystray.MenuItem("开机自启", self._on_autostart, checked=lambda _: autostart_enabled()),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("退出", self._on_quit),
                ),
            )
            # pystray pumps its own message loop; daemon keeps shutdown unblocked.
            threading.Thread(target=self.icon.run, name="tray", daemon=True).start()
            return True
        except Exception:
            logger.warning("托盘启动失败，回退到普通窗口模式", exc_info=True)
            self.icon = None
            return False

    def stop(self) -> None:
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                logger.debug("Tray stop failed", exc_info=True)
            self.icon = None

    # -- actions ----------------------------------------------------------

    def _on_open_ui(self, *_):
        self._run(self.api.open_window)

    def _on_open_browser(self, *_):
        self._run(self.api.open_console)

    def _on_start(self, *_):
        self._run(self.api.start)

    def _on_stop(self, *_):
        self._run(self.api.stop_services)

    def _on_restart(self, *_):
        self._run(self.api.restart)

    def _on_open_logs(self, *_):
        target = Path(self.api.base) / "logs"
        target.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(target))
        except Exception:
            logger.warning("打开日志目录失败: %s", target, exc_info=True)

    def _on_autostart(self, *_):
        was = autostart_enabled()
        set_autostart(not was)

    def _on_quit(self, *_):
        # Return False from pywebview's closing handler and stop the loop, so the
        # backend is torn down by api.stop() in the caller's finally block.
        self._run(self.api.quit)

    def _run(self, action):
        """Menu callbacks must not block the tray loop."""
        def wrapper():
            try:
                action()
            except Exception:
                logger.warning("托盘操作失败", exc_info=True)
        threading.Thread(target=wrapper, daemon=True).start()

    # -- state ------------------------------------------------------------

    def _title(self) -> str:
        return f"wb2api · v{self.version} · {STATE_LABELS.get(self.state, self.state)}"

    def set_state(self, state: str) -> None:
        self.state = state
        if self.icon is None or self._base_image is None:
            return
        try:
            self.icon.icon = compose(self._base_image, state)
            self.icon.title = self._title()
        except Exception:
            logger.debug("Tray icon refresh failed", exc_info=True)

    def notify(self, message: str) -> None:
        if self.icon is None:
            return
        try:
            self.icon.notify(message, "wb2api")
        except Exception:
            logger.debug("Tray notify failed", exc_info=True)
