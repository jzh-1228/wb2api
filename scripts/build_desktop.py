"""Reproducible Windows x64 onefile packaging; never includes .env, logs or account data."""
from __future__ import annotations

import hashlib
import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", default="dist", help="Output folder; useful while an older EXE is running")
    args = parser.parse_args()
    destination = (ROOT / args.dist_dir).resolve()
    if sys.platform != "win32" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise SystemExit("Use Windows x64 with a 64-bit Python interpreter")
    if sys.maxsize <= 2**32:
        raise SystemExit("A 64-bit Python interpreter is required")
    subprocess.run([sys.executable, str(ROOT / 'scripts/check_runtime.py')], cwd=ROOT, check=True)
    import resvg_py
    from PIL import Image
    version = json.loads((ROOT / "desktop/version.json").read_text(encoding="utf-8"))["version"]
    parts = tuple(int(v) for v in version.split("."))
    if len(parts) != 3:
        raise SystemExit("desktop/version.json must contain a stable x.y.z version")
    assets = ROOT / "desktop/assets"
    png = resvg_py.svg_to_bytes(svg_string=(assets / "app.svg").read_text(encoding="utf-8"),
                                width=256, height=256)
    (assets / "app.png").write_bytes(png)
    Image.open(assets / "app.png").save(assets / "app.ico", format="ICO",
                                       sizes=[(16,16), (24,24), (32,32), (48,48), (64,64), (128,128), (256,256)])
    (ROOT / "build").mkdir(exist_ok=True)
    resource = f'''VSVersionInfo(
      ffi=FixedFileInfo(filevers={parts + (0,)}, prodvers={parts + (0,)},
          mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0,0)),
      kids=[StringFileInfo([StringTable('040904B0', [
        StringStruct('CompanyName', 'wb2api'),
        StringStruct('FileDescription', 'wb2api Desktop'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('ProductName', 'wb2api'),
        StringStruct('ProductVersion', '{version}')])]),
        VarFileInfo([VarStruct('Translation', [1033, 1200])])])'''
    (ROOT / "build/version-resource.txt").write_text(resource, encoding="utf-8")
    env = dict(os.environ, PYTHONUTF8="1")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(destination),
                    str(ROOT / "desktop/wb2api.spec")], cwd=ROOT, env=env, check=True)
    executable = destination / f"wb2api-{version}-windows-x64.exe"
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    executable.with_suffix(".exe.sha256").write_text(f"{digest}  {executable.name}\n", encoding="ascii")
    print(f"Built {executable} ({executable.stat().st_size / 1048576:.1f} MB)")


if __name__ == "__main__":
    main()
