# Run through scripts/build_desktop.py; only explicitly listed product assets are included.
from pathlib import Path
import json
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH).parent
version = json.loads((root / 'desktop/version.json').read_text(encoding='utf-8'))['version']
name = f'wb2api-{version}-windows-x64'
datas = [(str(root / 'admin/static'), 'admin/static'),
         # The authenticated management console lives outside the public
         # static directory and must be present in the desktop bundle too.
         (str(root / 'admin/console'), 'admin/console'),
         # admin/data/ 是运行期用 read_text() 读的 JSON（上游错误文案表），
         # 漏打包会让 EXE/APK 启动即 FileNotFoundError：源码运行正常，只有产物才暴露。
         (str(root / 'admin/data'), 'admin/data'),
         (str(root / 'turing_helper.js'), '.'),
         (str(root / 'desktop/version.json'), 'desktop'),
         (str(root / 'desktop/apply-update.ps1'), 'desktop'),
         (str(root / 'desktop/assets/app.ico'), 'desktop/assets'),
         (str(root / 'desktop/assets/app.svg'), 'desktop/assets'),
         (str(root / 'LICENSE'), '.')]
datas += collect_data_files('webview')
datas += collect_data_files('tzdata')
hidden = collect_submodules('admin') + collect_submodules('desktop')
hidden += ['uvicorn.logging', 'uvicorn.loops.asyncio', 'uvicorn.protocols.http.h11_impl',
           'uvicorn.lifespan.on', 'sqlalchemy.dialects.sqlite', 'sqlalchemy.dialects.mysql.pymysql',
           'converter', 'anthropic_adapter', 'responses_adapter', 'responses_projection',
           'desensitize', 'upstream_compat', 'repository_instructions', 'model_metadata', 'wb_install', 'wb_asar', 'wb_envcheck',
           'webview.platforms.edgechromium', 'webview.platforms.winforms', 'clr', 'pythonnet',
           # Tray shell: pystray loads its backend lazily, so collect them explicitly.
           'pystray._win32', 'pystray._util', 'PIL.Image', 'PIL.ImageDraw', 'PIL._tkinter_finder']
a = Analysis([str(root / 'desktop_entry.py')], pathex=[str(root)], datas=datas, hiddenimports=hidden,
             excludes=['tkinter', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'cefpython3',
                       'cairosvg', 'resvg_py', 'numpy', 'scipy', 'matplotlib', 'pytest',
                       'IPython', 'watchfiles', 'websockets', 'httptools'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name=name, debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          icon=str(root / 'desktop/assets/app.ico'), version=str(root / 'build/version-resource.txt'))
