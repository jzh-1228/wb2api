# wb2api 通用工具脚本

名称与路径中的 `***` 表示已脱敏，实际参数、文件名和存储位置以现有源码及配置为准；兼容参数保留原格式。

在项目根目录执行。Python 脚本可先使用 `--help` 查看参数。

| 脚本 | 用途 |
| --- | --- |
| `build_desktop.py` | 构建 Windows EXE；建议通过根目录 `build.bat` 统一构建 |
| `build_android.py` | 暂存安卓端源码并构建 APK；可用 `--prepare-only` 仅生成暂存目录 |
| `check_runtime.py` | 使用临时空数据库检查服务启动及批量参数校验；安卓构建自动在固定依赖环境中运行 |
| `daemon.ps1` | Windows 服务启动、状态检查、停止与守护 |
| `watchdog.sh` | Linux HTTP 探活与异常重启，支持通过 `WB_*` 环境变量配置路径、用户和端口 |
| `export_credential_key.py` | 从本机官方客户端生成私有凭证加解密配置，供服务器配置使用 |
| `asar_grep.py` | 在本机 `app.asar` 数据区搜索文本或正则，输出上下文 |
| `asar_slice.py` | 列出、读取或抽取本机 ASAR 文件及字节区间 |

## 构建与守护

```powershell
.\build.bat --version 1.0.4
powershell -File scripts\daemon.ps1 -Status
powershell -File scripts\daemon.ps1 -Detach
```

Linux 守护脚本的安装方式与环境变量见 `watchdog.sh` 文件头。Docker 部署优先使用 Compose 中已有的容器重启策略。

## 凭证配置

在已安装官方 Windows 客户端的电脑上执行：

```powershell
python scripts/export_credential_key.py --output ..\wb2api-private\credential-key.json
```

脚本拒绝覆盖已有文件。将生成文件放到服务器 Web 根目录之外，仅允许服务用户读取，并按源码中的兼容参数指定文件路径。参数原名与格式以 `admin/credential_codec.py` 和脚本帮助为准。该文件包含私有密钥材料，不能提交到 Git、放入镜像或随安装包发布。

## ASAR 工具

```powershell
python scripts/asar_grep.py "/v2/report" --max 3
python scripts/asar_slice.py --list --grep product.json
python scripts/asar_slice.py --read /cli/product.json
```

默认自动发现本机 wb 安装位置，也可用 `--asar` 指定文件。读取到的内容可能含有客户端配置，请自行妥善保管。

本机部署快捷脚本、线上探针与一次性排障脚本不纳入仓库；`tests/` 保留在本地用于回归验证。
