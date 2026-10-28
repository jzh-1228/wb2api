"""Build the current source tree as a paired Windows EXE and Android APK."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
METADATA = ROOT / "desktop/version.json"


def validate_version(value: str) -> str:
    value = value.strip().removeprefix("v")
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value):
        raise ValueError("版本号必须为 X.Y.Z，例如 1.0.1")
    major, minor, patch = map(int, value.split("."))
    code = major * 1000000 + minor * 1000 + patch
    if minor >= 1000 or patch >= 1000 or not 0 < code <= 2100000000 or major > 65535:
        raise ValueError("版本号超出 Android/Windows 范围：次版本和修订号须小于 1000")
    return value


def timed_version(current: str, timeout=10, console=None, now=time.monotonic, pause=time.sleep) -> str:
    """Only a submitted Enter changes the version; even partial input expires."""
    if console is None:
        if not sys.stdin.isatty():
            print(f"非交互运行，沿用版本 {current}（可用 --version 指定版本）")
            return current
        import msvcrt
        console = msvcrt
    print(f"当前版本 {current}。输入新版 X.Y.Z 后按回车；10 秒未提交则沿用当前版本：", end="", flush=True)
    deadline, value = now() + timeout, ""
    while now() < deadline:
        if console.kbhit():
            char = console.getwch()
            if char == "\x03":
                raise KeyboardInterrupt
            if char in ("\r", "\n"):
                print()
                return validate_version(value) if value.strip() else current
            if char == "\b":
                if value:
                    value = value[:-1]
                    print("\b \b", end="", flush=True)
            elif char in ("\x00", "\xe0"):
                console.getwch()
            elif char.isprintable():
                value += char
                print(char, end="", flush=True)
        pause(.05)
    print(f"\n未提交新版本，沿用 {current}")
    return current


def tools_for_android(args) -> tuple[Path, Path, Path]:
    def directory(option, env_names, candidates, marker):
        choices = ([Path(option)] if option else [])
        if not option:
            choices += [Path(os.environ[n]) for n in env_names if os.environ.get(n)] + list(candidates)
        for path in choices:
            if (path / marker).is_file():
                return path.resolve()
        raise ValueError(f"找不到 {marker}，请用 --{env_names[0].lower().replace('java_home', 'jdk').replace('android_home', 'sdk')} 指定目录")
    jdk = directory(args.jdk, ["JAVA_HOME"],
                    sorted((ROOT / "build/android-tools/microsoft-jdk17").glob("jdk-*"), reverse=True)
                    + sorted((ROOT / "build/android-tools/jdk").glob("jdk-*"), reverse=True), "bin/javac.exe")
    sdk = directory(args.sdk, ["ANDROID_HOME", "ANDROID_SDK_ROOT"],
                    [ROOT / "build/android-tools/sdk", Path(os.environ.get("LOCALAPPDATA", "")) / "Android/Sdk"],
                    "platforms/android-35/android.jar")
    gradle_candidates = [Path(args.gradle)] if args.gradle else []
    if not args.gradle:
        if shutil.which("gradle"):
            gradle_candidates.append(Path(shutil.which("gradle")))
        gradle_home = Path(os.environ.get("GRADLE_USER_HOME", str(Path.home() / ".gradle")))
        gradle_candidates += sorted(gradle_home.glob("wrapper/dists/gradle-8.11.1-bin/*/gradle-8.11.1/bin/gradle.bat"))
    gradle = next((p.resolve() for p in gradle_candidates if p.is_file()), None)
    if not gradle:
        raise ValueError("找不到 Gradle 8.11.1，请用 --gradle 指定 gradle.bat")
    # Reject an accidental JDK 21 selection before staging or changing the version.
    result = subprocess.run([str(jdk / "bin/java.exe"), "-version"], capture_output=True, text=True)
    if result.returncode or not re.search(r'version "17\.', result.stderr + result.stdout):
        raise ValueError("Android 构建需要 JDK 17，请用 --jdk 指定")
    return jdk, sdk, gradle


def run(command, log, env):
    print("\n> " + subprocess.list2cmdline([str(x) for x in command]), flush=True)
    with subprocess.Popen([str(x) for x in command], cwd=ROOT, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace") as process:
        for line in process.stdout:
            log.write(line)
            log.flush()
            print(line, end="", flush=True)
        code = process.wait()
    if code:
        raise RuntimeError(f"构建步骤失败（退出码 {code}），完整日志：{log.name}")


def build_commands(python: Path, jdk: Path, sdk: Path, gradle: Path, with_debug=False):
    return [
        [python, ROOT / "scripts/build_desktop.py"],
        [python, ROOT / "scripts/build_android.py", "--jdk", jdk, "--sdk", sdk, "--gradle", gradle]
        + (["--with-debug"] if with_debug else []),
    ]


def main():
    parser = argparse.ArgumentParser(description="一键打包当前代码：Windows EXE + Android APK；不会拉取 Git 或发布 Release")
    parser.add_argument("--version", help="指定 X.Y.Z 版本，不等待输入")
    parser.add_argument("--non-interactive", action="store_true", help="沿用当前版本，不等待输入")
    parser.add_argument("--dry-run", action="store_true", help="核对工具和显示构建计划，不改版本或构建")
    parser.add_argument("--jdk", help="JDK 17 目录")
    parser.add_argument("--sdk", help="Android SDK 35 目录")
    parser.add_argument("--gradle", help="Gradle 8.11.1 gradle.bat 路径")
    parser.add_argument("--with-debug", action="store_true", help="额外生成隔离的安卓调试包")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise ValueError("两端一起打包需要 Windows x64、Python 3.10")
    if sys.version_info[:2] != (3, 10) or sys.maxsize <= 2**32:
        raise ValueError("需要 64 位 Python 3.10，请运行 build.bat 或 py -3.10 build.py")
    metadata = json.loads(METADATA.read_text(encoding="utf-8"))
    current = validate_version(metadata["version"])
    version = validate_version(args.version) if args.version else current if args.non_interactive or args.dry_run else timed_version(current)
    if tuple(map(int, version.split('.'))) < tuple(map(int, current.split('.'))):
        raise ValueError("新版不能低于当前版本，否则 Android 无法覆盖升级")
    jdk, sdk, gradle = tools_for_android(args)
    python = ROOT / ".desktop-build/Scripts/python.exe"
    commands = build_commands(python, jdk, sdk, gradle, args.with_debug)
    print(f"\n两端版本：{version}。构建当前工作目录的最新源码（含未提交修改）。")
    if args.dry_run:
        for command in commands:
            print(subprocess.list2cmdline([str(x) for x in command]))
        return
    logs = ROOT / "build/package-logs"
    logs.mkdir(parents=True, exist_ok=True)
    # Prevent concurrent builders from overwriting version metadata and Android staging.
    import msvcrt
    with (ROOT / "build/packaging.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise ValueError("另一个打包任务正在运行，请等待完成")
        with (logs / (time.strftime("%Y%m%d-%H%M%S") + ".log")).open("w", encoding="utf-8") as log:
            env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
            if not python.is_file():
                run([sys.executable, "-m", "venv", ROOT / ".desktop-build"], log, env)
            # pip's resolver updates project requirements while keeping satisfied packages.
            run([python, "-m", "pip", "install", "--disable-pip-version-check",
                 "-r", ROOT / "requirements-desktop.txt", "--index-url", "https://pypi.tuna.tsinghua.edu.cn/simple"], log, env)
            # Windows JDK fallback for systems where Unix-domain loopback is unavailable.
            if "-Djdk.net.unixdomain.tmpdir=" not in env.get("JAVA_TOOL_OPTIONS", ""):
                fallback = ROOT / "build/jdk-uds-disabled/nonexistent"
                if fallback.exists():
                    raise ValueError(f"JDK 回退路径必须不存在：{fallback}")
                env["JAVA_TOOL_OPTIONS"] = (env.get("JAVA_TOOL_OPTIONS", "") + f' -Djdk.net.unixdomain.tmpdir="{fallback}"').strip()
            if version != current:
                metadata["version"] = version
                METADATA.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (ROOT / f"dist/build-{version}.json").unlink(missing_ok=True)
            for command in commands:
                run(command, log, env)
            artifacts = [ROOT / f"dist/wb2api-{version}-windows-x64.exe",
                         ROOT / f"dist/android/wb2api-{version}-android-arm64.apk"]
            manifest = {"version": version, "files": []}
            for path in artifacts:
                hasher = hashlib.sha256()
                with path.open("rb") as file:
                    for chunk in iter(lambda: file.read(1024 * 1024), b""):
                        hasher.update(chunk)
                digest = hasher.hexdigest()
                expected = path.with_suffix(path.suffix + ".sha256").read_text(encoding="ascii").split()[0]
                if digest != expected:
                    raise RuntimeError(f"产物校验失败：{path}")
                manifest["files"].append({"path": path.relative_to(ROOT).as_posix(), "bytes": path.stat().st_size, "sha256": digest})
            (ROOT / f"dist/build-{version}.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            print("\n两端构建及 SHA256 校验完成。Release 请上传以下两个包及各自的 .sha256：")
            for path in artifacts:
                print(path)
                print(path.with_suffix(path.suffix + ".sha256"))
            print("日志：" + log.name)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"\n打包失败：{exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n已取消打包", file=sys.stderr)
        sys.exit(130)
