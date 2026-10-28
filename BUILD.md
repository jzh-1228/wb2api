# 一键构建 Windows EXE 和 Android APK

在 Windows x64 上双击根目录 `build.bat`，或在终端运行：

```powershell
.\build.bat
```

输入版本号（例如 `1.0.4`）并按回车，两端统一使用这个版本。**10 秒内未提交输入，或直接按回车，都沿用当前版本，不自动递增。** 输入后没有按回车也不会修改版本。新版本不能低于当前版本。

构建使用当前工作目录中的源码，包含尚未提交的修改；不会自动拉取/合并 Git、提交、推送或发布 Release。请在构建前完成所需的 Git 合并，不要在构建过程中修改源码。同一目录不允许同时运行两次一键构建。

```powershell
# 明确指定新版，跳过倒计时
.\build.bat --version 1.0.4

# CI 或重复构建：沿用当前版本，不等待输入
.\build.bat --non-interactive

# 只检查工具和显示命令，不修改版本、安装依赖或构建
.\build.bat --dry-run

# 可选：同时构建独立数据目录的安卓调试包
.\build.bat --version 1.0.4 --with-debug
```

版本唯一来源是 `desktop/version.json`，EXE 文件属性、APK 的 versionName/versionCode、应用左下角版本号及安装包名称保持一致。仅支持稳定的三段数字 `X.Y.Z`，次版本号和修订号须小于 1000；实际发布更新需要输入比已发布版本更高的版本号。

## 首次构建环境

构建机需要 64 位 Python 3.10、JDK 17、Gradle 8.11.1、Android SDK 35（含 Build Tools 35.0.0，并已接受 SDK 许可）。这是开发机工具，用户安装 EXE/APK 不需要这些构建工具。

脚本优先使用项目的 `.desktop-build` Python；不存在时，通过 Windows `py -3.10` 启动并自动创建虚拟环境，再从清华 PyPI 镜像安装项目和桌面打包依赖。Android Python 依赖由 Chaquopy 在构建时内置，使用现有清华/Chaquopy 源；Gradle/Maven 使用项目配置的镜像。

自动查找本项目 `build/android-tools/` 中的 JDK/SDK，以及用户 Gradle 缓存中的 8.11.1。也支持 `JAVA_HOME`、`ANDROID_HOME`、`ANDROID_SDK_ROOT`、`GRADLE_USER_HOME` 和 PATH 中的 Gradle。工具安装在其他目录时运行：

```powershell
.\build.bat --jdk C:\Tools\jdk-17 --sdk C:\Tools\android-sdk --gradle C:\Tools\gradle-8.11.1\bin\gradle.bat
```

缺失工具时会在修改版本前停止，并提示需要指定的路径。首轮下载依赖需要联网，后续构建复用缓存。

## 产物和发布

一次成功构建产出：

```text
dist/WorkBuddy2API-1.0.4-windows-x64.exe
dist/WorkBuddy2API-1.0.4-windows-x64.exe.sha256
dist/android/WorkBuddy2API-1.0.4-android-arm64.apk
dist/android/WorkBuddy2API-1.0.4-android-arm64.apk.sha256
dist/build-1.0.4.json
```

完整日志位于 `build/package-logs/`。只有两端构建成功且 SHA256 校验通过，才生成成功清单 `build-版本号.json`。构建失败会返回非零退出码，保留日志；版本已修改后失败，可以用 `--non-interactive` 重试。

打包前会在临时空数据库上检查应用启动及批量接口参数校验，不读取本机账号。Windows 使用当前打包依赖；Android 自动在 `build/android-runtime-check/` 创建独立 Python 环境，安装 `android/requirements.txt` 的固定版本后验证暂存源码。首次检查需要下载对应的主机依赖，后续复用环境；此检查验证 Python 服务兼容性，不替代安卓设备测试。

Android 覆盖升级必须使用同一个签名。请安全备份 `build/android-signing/release.jks` 和 `signing.json`，迁移构建机时恢复它们；不要上传到 Git 或 Releases。一键脚本沿用已有签名，不会每次生成新签名。

在 [本项目 Releases](https://github.com/xiaofan6ya/wb2api/releases) 创建正式版本，Tag 用 `1.0.4` 或 `v1.0.4`，上传上述 **两个安装包和两个 `.sha256` 文件**，保留生成的文件名。不要发布成 Draft/Prerelease，否则稳定版更新检查会忽略它。GitHub 提供有效 SHA256 digest 时也支持直接校验，但建议同时上传校验文件。

桌面只匹配 `windows-x64.exe`，安卓只匹配 `android-arm64.apk`；缺少本平台包的版本仍可查看公告，但不会提示可升级。各平台独立缓存，按版本号选取最高的可用正式版本。每次启动自动检查，点击左下角版本号可再次检查和查看历史公告；更新由用户决定。安卓下载 APK 后按系统安装流程覆盖升级。

公告直接写 Markdown，支持标题、粗体、列表、任务列表、链接、表格、引用和代码块。旧公告把全文包在 `markdown`/`md` 代码块里时也会正确解析；正文里的正常代码块保留。解析器随包内置，无需 CDN，过滤可执行 HTML 和危险链接。

仓库中的版本公告统一保存在 `docs/RELEASE_NOTES_版本号.md`，发布时可复制 [1.0.4 更新公告](docs/RELEASE_NOTES_1.0.4.md) 的内容作为 Release 正文。

示例公告（复制内容即可，不需要在公告外再加代码块）：

```markdown
## 更新内容
- 新增一键构建 EXE/APK，支持输入版本号。
- 更新检查按平台匹配安装包。
- 更新说明和历史公告支持 Markdown。

## 安装
Windows 下载 EXE；安卓下载 APK，覆盖安装保留账号和配置。
```

## 验证

```powershell
.\build.bat --dry-run
Get-FileHash dist\WorkBuddy2API-1.0.4-windows-x64.exe -Algorithm SHA256
Get-Content dist\WorkBuddy2API-1.0.4-windows-x64.exe.sha256
Get-FileHash dist\android\WorkBuddy2API-1.0.4-android-arm64.apk -Algorithm SHA256
Get-Content dist\android\WorkBuddy2API-1.0.4-android-arm64.apk.sha256
```

核对每个安装包的计算结果与对应校验文件一致，并查看 `dist/build-1.0.4.json` 确认两端版本、文件大小及 SHA256。回归测试保留在维护者本地，不随源码发布。
