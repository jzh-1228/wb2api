# wb2api Windows 桌面版

名称与路径中的 `***` 表示已脱敏，实际参数、文件名和存储位置以现有源码及配置为准；兼容参数保留原格式。

支持 Windows 10（1809 及以后）/ Windows 11 x64。双击 `wb2api-版本-windows-x64.exe`，无需安装 Python、pip、MySQL 或 Redis，也无需填写后台用户名和密码。

Python 运行时、项目全部 Python 依赖、网页、字体及图标都内置在单文件 EXE 中。桌面版自动使用 SQLite（WAL 模式）与内存限流，默认监听 `0.0.0.0:8790`，支持本机与局域网访问。后续优先使用 `config.json` 保存的端口；只有该端口被占用时才选择空闲端口，并在服务成功启动后保存，重启继续复用。API Keys 页可选择并复制检测到的局域网地址；本地应用自动进入工作台，远程管理页仍需要登录。

Node.js 22.14.0 第一次在后台自动准备：依次尝试 npmmirror、华为云镜像、Node 官方源，并校验固定 SHA256；只保留 Node 执行器与许可证，无需 npm 或安装管理员权限。下载不阻塞窗口与后台使用，失败可在版本窗口重试。设备风控 SDK 仍来自用户已安装的 wb 客户端，项目不包含第三方账号或 SDK；使用网关需要添加自己的有效账号。

使用系统 WebView2，避免随 EXE 携带完整 Chromium。系统缺少 WebView2 时，自动从 Microsoft 官方地址下载签名验证过的安装引导程序并静默安装。首次启动需要联网，准备完后复用已安装环境，普通启动不下载运行时。上游 API 本身需要联网。

## 数据与日志

手机号登录弹窗左下角的“授权页面”会在系统浏览器打开本机 `/join`，支持短信登录与官方授权页面接力。授权成功后自动入库，回到客户端刷新账号列表。授权期间保持客户端运行；本机地址只对当前电脑有效。

新版 wb 的 `.info` 文件使用 `$wbEncrypted` 加密敏感字段。扫描本机、文件上传和手动添加会在 Windows 上调用已安装 wb 的内置能力，在内存中解密后执行原有凭证校验；不修改客户端原文件、不弹出客户端窗口，也不在项目中携带密钥。入库后可从号池导出凭证，再导入手机或服务器。其它设备上的原始加密 `.info` 若没有匹配的客户端解密能力，会提示使用手机号/浏览器授权，不能把加密对象当作明文 token 使用。

默认目录：`%LOCALAPPDATA%\***`，与 EXE 存放位置独立。

- `***.db`：账号、API Key、调度与用量数据，升级保留。
- `config.json`：端口、随机本机鉴权信息、可选独立 `/gw/v1` 网关密钥 `converter_api_key`。桌面界面直接打开，不要求密码；后台接口仍有鉴权。
- `runtime/`：缓存 Node，后续启动复用。
- `logs/desktop.log`：轮转日志；`logs/backend.log`：本次后台启动日志；`logs/update.log`：升级记录。
- `webview/`：本地 WebView2 缓存。

原部署 `.env`、数据库备份、账号 token、日志和本机虚拟环境不会进入 EXE。桌面模式不读取项目 `.env`，避免误连部署数据库。

## 更新

每次打开窗口检查 [新仓库 Releases](https://github.com/xiaofan6ya/wb2api/releases)，发现新版本时弹出更新公告，可选择“稍后再说”或“暂不更新此版本”。跳过某版本不影响后续新版本提醒。左下角版本号或顶部更新按钮随时可查看公告并下载最新版。网络失败仍能使用已缓存的历史记录，不强制更新。

点击“下载最新版”，校验后再点击“重启并安装更新”。下载失败、文件校验不通过或输出目录没有写入权限时保留旧版本。替换 EXE 前保存 `原文件名.exe.previous`；需要恢复时关闭客户端，将备份重命名为 `.exe`。更新只替换 EXE，不清除数据。无管理员权限使用时请把 EXE 放在有写权限的文件夹。

自动更新资产约定（大小写一致）：

文件后缀为 `-windows-x64.exe` 与 `.exe.sha256`，完整前缀以现有构建代码为准。

也支持 GitHub 返回的 `sha256:` asset digest。Release 没有 Windows EXE 或校验信息时仅展示说明和链接。草稿与预发布不会触发自动升级。Release 正文按纯文本展示，不执行其中 HTML。

## 开发与发布

在 Windows x64 使用 Python 3.10（与当前验证版本一致）：

```powershell
python -m venv .desktop-build
.\.desktop-build\Scripts\python.exe -m pip install -r requirements-desktop-lock.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
.\.desktop-build\Scripts\python.exe -m unittest discover -s tests -p test_desktop.py -v
.\.desktop-build\Scripts\python.exe scripts/build_desktop.py
```

输出 EXE 与 SHA256 到 `dist/`；若旧 EXE 正在运行，可用 `--dist-dir dist/final` 构建到不同目录。依赖使用锁定版本，可重现当前构建；`requirements-desktop.txt` 用于更新依赖后重新生成锁文件。

版本唯一来源：`desktop/version.json`，图标源：`desktop/assets/app.svg`。构建脚本由 SVG 生成七种尺寸 ICO，写入 EXE 图标及文件版本信息，不携带 SVG 转换工具。

`.github/workflows/desktop-release.yml` 仅支持手动构建 Windows 产物。正式发布需先部署验证，再构建 EXE/APK 并完成产物冒烟，随后写公告、提交源码并推送裸版本标签（例如 `1.14.0`），最后上传两个包及各自的 SHA256。1.13.0 及更早版本需手动安装新版一次。

可验证真实 PyWebView 窗口、自动进入后台与版本入口，执行后自动退出：

使用隔离空数据目录（环境参数按 `desktop/common.py` 设置），对实际生成的 EXE 执行 `--smoke-test` 并指定结果 JSON 路径。

隔离测试参数不需要普通用户配置。
