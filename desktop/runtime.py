"""Private, cached Node runtime. Python and all application dependencies are frozen."""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

NODE_VERSION = "22.14.0"
NODE_ARCHIVE = f"node-v{NODE_VERSION}-win-x64.zip"
# Pinned from nodejs.org/dist/v22.14.0/SHASUMS256.txt; mirrors cannot change the binary.
NODE_SHA256 = "55b639295920b219bb2acbcfa00f90393a2789095b7323f79475c9f34795f217"
NODE_SOURCES = [f"https://npmmirror.com/mirrors/node/v{NODE_VERSION}/{NODE_ARCHIVE}",
                f"https://mirrors.huaweicloud.com/nodejs/v{NODE_VERSION}/{NODE_ARCHIVE}",
                f"https://nodejs.org/dist/v{NODE_VERSION}/{NODE_ARCHIVE}"]


def download(url: str, target: Path, progress=None, max_bytes=300 * 1024 * 1024) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "wb2api-Desktop"})
    digest = hashlib.sha256()
    done = 0
    with urllib.request.urlopen(request, timeout=30) as response, target.open("wb") as out:
        total = int(response.headers.get("Content-Length", 0))
        if total > max_bytes:
            raise ValueError("下载文件超出大小限制")
        while chunk := response.read(256 * 1024):
            done += len(chunk)
            if done > max_bytes:
                raise ValueError("下载文件超出大小限制")
            out.write(chunk)
            digest.update(chunk)
            if progress:
                progress(done, total)
    return digest.hexdigest()


def node_dir(base: Path) -> Path:
    return base / "runtime" / f"node-{NODE_VERSION}"


def configure_node(base: Path) -> bool:
    directory = node_dir(base)
    if (directory / "node.exe").is_file():
        os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
        return True
    return False


def ensure_node(base: Path, status) -> Path:
    destination = node_dir(base)
    if (destination / "node.exe").is_file():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    errors = []
    with tempfile.TemporaryDirectory(dir=destination.parent) as work:
        archive = Path(work) / "node.zip"
        for url in NODE_SOURCES:
            try:
                status("正在从镜像准备 Node.js（首次启动）")
                actual = download(url, archive, lambda n, total: status(
                    f"准备 Node.js：{n / 1048576:.1f} / {total / 1048576:.1f} MB"))
                if actual != NODE_SHA256:
                    raise ValueError("Node.js SHA256 校验失败")
                # The helper uses only node:* built-ins. npm and its dependency tree are unnecessary.
                with zipfile.ZipFile(archive) as z:
                    executable = z.read(f"node-v{NODE_VERSION}-win-x64/node.exe")
                    staged = Path(work) / "ready"
                    staged.mkdir()
                    (staged / "node.exe").write_bytes(executable)
                    for name in ("LICENSE", "README.md"):
                        (staged / name).write_bytes(z.read(f"node-v{NODE_VERSION}-win-x64/{name}"))
                subprocess.run([str(staged / "node.exe"), "--version"], check=True,
                               capture_output=True, timeout=10,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                staged.replace(destination)
                return destination
            except Exception as exc:
                errors.append(str(exc))
    raise RuntimeError("Node.js 自动准备失败，可在版本窗口重试：" + "; ".join(errors))


def ensure_webview2(base: Path) -> None:
    if has_webview2():
        return
    # Microsoft distributes the evergreen bootstrapper from this canonical link.
    # It installs per-user silently; never replace it with an unsigned third-party mirror.
    installer = base / "runtime" / "MicrosoftEdgeWebview2Setup.exe"
    installer.parent.mkdir(parents=True, exist_ok=True)
    download("https://go.microsoft.com/fwlink/p/?LinkId=2124703", installer, max_bytes=10*1048576)
    os.environ["WB_WEBVIEW_INSTALLER"] = str(installer)
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
               "$s=Get-AuthenticodeSignature -LiteralPath $env:WB_WEBVIEW_INSTALLER; "
               "if($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch 'Microsoft Corporation'){exit 1}"]
    subprocess.run(command, check=True, capture_output=True, timeout=30,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    subprocess.run([str(installer), "/silent", "/install"], check=True, timeout=300,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    if not has_webview2():
        raise RuntimeError("WebView2 安装未完成，请检查网络后再次双击 EXE")


def has_webview2() -> bool:
    import winreg
    key = r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as handle:
                    version, _ = winreg.QueryValueEx(handle, "pv")
                    if int(version.split(".")[0]) >= 86:
                        return True
            except (OSError, ValueError):
                pass
    return False
