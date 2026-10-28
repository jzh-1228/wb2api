from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from packaging.version import InvalidVersion, Version

from desktop.common import METADATA, REPOSITORY, RELEASES_URL, VERSION, atomic_json
from desktop.runtime import download


def newer(tag: str, current=VERSION) -> bool:
    try:
        candidate = Version(tag.lstrip("vV"))
        return not candidate.is_prerelease and candidate > Version(current)
    except InvalidVersion:
        return False


def release_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "github.com" or
            not parsed.path.startswith(f"/{REPOSITORY}/releases/")):
        raise ValueError("更新地址必须来自本项目 GitHub Releases")
    return url


class UpdateService:
    def __init__(self, base: Path, platform: str | None = None):
        self.base = base
        self.platform = platform or ("android" if METADATA["asset"].endswith(".apk") else "windows")
        if self.platform not in ("windows", "android"):
            raise ValueError("Unsupported update platform")
        suffix = "android-arm64.apk" if self.platform == "android" else "windows-x64.exe"
        self.asset_pattern = "wb2api-{version}-" + suffix
        self.cache = base / f"releases-cache-{self.platform}.json"
        self.lock = threading.Lock()
        self.releases = []
        self.etag = ""
        self.error = ""
        self.checked = False
        try:
            cached = json.loads(self.cache.read_text(encoding="utf-8"))
            self.releases = cached.get("releases", [])
            self.etag = cached.get("etag", "")
        except (OSError, ValueError):
            pass

    def snapshot(self) -> dict:
        candidates = [r for r in self.releases if newer(r["tag"]) and self.matches_asset(r)]
        latest = max(candidates, key=lambda r: Version(r["tag"].lstrip("vV")), default=None)
        return {"version": VERSION, "platform": self.platform, "releases": self.releases, "latest": latest,
                "available": latest is not None, "error": self.error,
                "checked": self.checked, "releases_url": RELEASES_URL}

    def matches_asset(self, release: dict) -> bool:
        expected = self.asset_pattern.format(version=release["tag"].lstrip("vV"))
        return (release.get("asset") or {}).get("name") == expected

    def check(self) -> dict:
        with self.lock:
            try:
                # Paginated history is bounded to 100 releases, newest first; no startup blocking.
                headers = {"User-Agent": "wb2api-Desktop", "Accept": "application/vnd.github+json"}
                if self.etag:
                    headers["If-None-Match"] = self.etag
                request = urllib.request.Request(
                    f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100", headers=headers)
                with urllib.request.urlopen(request, timeout=15) as response:
                    rows = json.load(response)
                    self.etag = response.headers.get("ETag", "")
                releases = []
                for row in rows:
                    if row.get("draft") or row.get("prerelease"):
                        continue
                    tag = str(row.get("tag_name", ""))
                    filename = self.asset_pattern.format(version=tag.lstrip("vV"))
                    assets = {a["name"]: a for a in row.get("assets", [])}
                    asset = assets.get(filename)
                    checksum = assets.get(filename + ".sha256")
                    digest = (asset or {}).get("digest", "") or ""
                    releases.append({"id": row["id"], "tag": tag, "name": row.get("name") or tag,
                                     "body": (row.get("body") or "暂无更新说明")[:100000],
                                     "date": row.get("published_at", ""),
                                     "url": release_url(row["html_url"]), "asset": asset,
                                     "checksum": checksum,
                                     "installable": bool(asset and (checksum or digest.startswith("sha256:")))})
                self.releases = releases
                atomic_json(self.cache, {"etag": self.etag, "releases": releases})
                self.error = ""
            except urllib.error.HTTPError as exc:
                if exc.code == 304:
                    self.error = ""
                else:
                    self.error = f"检查更新失败（HTTP {exc.code}），稍后可重试"
            except Exception as exc:
                self.error = f"检查更新失败：{exc}"
            self.checked = True
            return self.snapshot()

    def stage(self, release_id: int, progress=None) -> Path:
        # A caller passes a release ID, never a URL or executable path.
        release = next((r for r in self.releases if r["id"] == int(release_id)), None)
        if (self.platform != "windows" or not release or not newer(release["tag"])
                or not self.matches_asset(release) or not release["installable"]):
            raise ValueError("该版本没有可校验的 Windows 更新包")
        asset = release["asset"]
        expected = (asset.get("digest") or "").removeprefix("sha256:")
        if release["checksum"]:
            request = urllib.request.Request(release_url(release["checksum"]["browser_download_url"]),
                                             headers={"User-Agent": "wb2api-Desktop"})
            with urllib.request.urlopen(request, timeout=20) as response:
                expected = response.read(4096).decode("utf-8").strip().split()[0]
        if len(expected) != 64 or any(c not in "0123456789abcdefABCDEF" for c in expected):
            raise ValueError("更新包 SHA256 无效")
        directory = self.base / "updates"
        directory.mkdir(exist_ok=True)
        target = directory / f"{int(release_id)}.exe"
        partial = target.with_suffix(".part")
        try:
            actual = download(release_url(asset["browser_download_url"]), partial, progress)
            if actual.lower() != expected.lower():
                raise ValueError("更新文件校验失败，已保留当前版本")
            with partial.open("rb") as file:
                if file.read(2) != b"MZ":
                    raise ValueError("更新文件不是 Windows EXE")
            partial.replace(target)
            return target
        finally:
            partial.unlink(missing_ok=True)
