# wb2api

公开文档统一使用 `wb2api`；上游客户端统称 `wb`。上游域名、协议字段、现有环境变量和运行数据路径继续按源码保留。文档中的 `***` 是脱敏占位符，不能作为实际配置值。项目仓库：[xiaofan6ya/wb2api](https://github.com/xiaofan6ya/wb2api)。

把 wb 登录态接入 OpenAI、Responses 和 Anthropic 兼容 API，提供多账号池、API Key 和自动化管理界面。

项目包含两部分：**API 反代网关**负责协议转换、账号轮换和用量记账；**多账号管理与自动化平台**负责手机号登录、凭证导入、积分到期管理、签到、成长任务和宠物旅行。可在服务器部署，也可使用 Windows EXE 或 Android APK 独立运行。

## 模块说明

| 模块 | 功能 |
| --- | --- |
| 账号池 | 手机号登录、扫描本机、凭证批量导入/导出、启停账号、余额刷新、模型额度与冷却 |
| API Keys | 创建/停用密钥、积分限额、独占账号绑定、模型分组、选择并复制本机或局域网 API 地址 |
| 模型配置与分组 | 同步模型、批量启停、分组授权、配置模型列表名称中的倍率和繁忙程度、检测延迟与抖动 |
| 积分到期 | 逐账号积分包快照、临近到期排行、到期风险与消耗构成 |
| 成长任务 | 查看任务、参与、执行可自动化任务、领取奖励、查看逐账号执行结果 |
| 成长计划 | 连续登录、能量、活跃地图、年度热力图、宠物图鉴、盲盒与领取记录 |
| 定时任务 | 签到、刷新余额/到期快照、同步模型、成长任务、成长计划巡检与 token 保活 |
| 使用记录与日志 | 按 Key、账号、模型、端点查询用量、token、耗时及错误，查看趋势和汇总 |
| 客户端参数与同步 | 管理客户端版本/请求参数档案，将本机配置与账号同步到其他部署 |
| 本地客户端 | Windows EXE、Android APK 自动进入工作台，后台运行、局域网接入、更新公告和可选升级 |


## 阅读导航

- [项目架构与运行模式](#项目架构与运行模式)
- [项目结构](#项目结构)
- [Windows 与 Android](#windows-与-android)
- [Linux Docker 部署](#linux-docker-部署)
- [源码运行](#源码运行)
- [账号管理与日常使用](#账号管理与日常使用)
- [模型配置与模型分组](#模型配置与模型分组)
- [自动化与定时任务](#自动化与定时任务)
- [API 接入与客户端配置](#api-接入与客户端配置)
- [管理接口总览](#管理接口总览)
- [环境变量](#环境变量)
- [稳定性与流量治理](#稳定性与流量治理)
- [日志与常见问题](#日志与常见问题)
- [技术栈与已知限制](#技术栈与已知限制)
- [文档与构建](#文档与构建)
- [协议与致谢](#协议与致谢)

## 项目架构与运行模式

```text
OpenAI / Responses / Anthropic 兼容客户端
                    │ API Key
                    ▼
     admin/server.py（默认 8790 单端口）
       ├── /admin、/join      管理界面 / 手机号授权入库
       ├── /api/*            账号、Key、模型、统计与自动化
       └── /v1/*             共享账号池网关
                    │
          Key 配额与分组 → 选号 / 租约 / 冷却 → 协议适配
                    │
                    ▼
              wb 上游 API

可选：converter.py（独立 8787，或挂载至 8790/gw）
      使用本机 .info 登录态，独立鉴权，不经过共享账号池配额
```

| 运行模式 | 入口 | 账号来源与用途 |
| --- | --- | --- |
| 服务器 / Docker | `main.py` 或 `admin.server:app` | 数据库账号池，面向共享调用、后台管理与自动化 |
| Windows EXE | `desktop_entry.py` → `desktop/` | 内置后端、SQLite 与 WebView2 工作台，本机与局域网接入 |
| Android APK | `android/` | 手机内独立 Python 后端、SQLite、WebView 和前台服务 |
| 独立 converter | `converter.py` | 读取本机官方客户端登录文件，适合单账号协议转换 |

共享网关的 Chat、Responses 和 Anthropic 三个入口共用 API Key、账号池和记账规则。`/gw` 仅在配置 `CONVERTER_API_KEY` 后挂载，使用独立密钥和本机凭证；普通多账号接入直接使用 `/v1/*`。

## 项目结构

```text
wb2api/
├── README.md                    # 功能、结构、部署与使用说明
├── BUILD.md                     # EXE / APK 构建与发布指南
├── main.py                      # 单端口启动、进程与端口管理、日志转发
├── converter.py                 # 独立单账号兼容网关
├── anthropic_adapter.py         # Anthropic Messages ↔ Chat 协议转换
├── responses_adapter.py         # OpenAI Responses ↔ Chat 协议转换
├── responses_projection.py      # Responses / agent 消息投影与压缩
├── upstream_compat.py           # 上游请求字段兼容处理
├── desensitize.py               # 请求文本兼容处理
├── wb_install.py                # 官方客户端安装位置、版本与资源自动发现
├── wb_envcheck.py               # Python、依赖、Node、客户端及数据库环境自检
├── wb_asar.py                   # ASAR 读取、检索与抽取工具
├── turing_helper.js             # 本机客户端设备 SDK 调用入口
├── .env.example                 # 环境配置模板，不含真实部署凭证
├── requirements.txt             # 后端运行依赖
├── requirements-desktop*.txt    # Windows 构建依赖及锁定清单
├── Dockerfile
├── docker-compose.yml           # 默认 admin；converter 为可选 profile
├── build.bat / build.py          # 统一版本的一键 EXE + APK 构建
├── desktop_entry.py             # Windows 打包入口
├── admin/
│   ├── server.py                # FastAPI 入口、鉴权、路由与静态页
│   ├── config.py / db.py        # 环境配置、数据库会话与表结构初始化
│   ├── models.py                # 账号、Key、模型、调度、日志等 ORM
│   ├── security.py              # 后台 JWT、Key 校验及配额检查
│   ├── account_binding.py       # Key 独占账号与账号范围控制
│   ├── backend.py               # 单账号上游业务调用
│   ├── credential_codec.py      # 官方加密 .info 的读取与导出
│   ├── wb_login.py              # 手机号登录与上游授权会话
│   ├── post_login.py            # 入库后的余额、到期快照与任务处理
│   ├── network.py               # 本机 / 局域网地址发现
│   ├── pool.py                  # 选号、租约、粘性会话、冷却与出口保护
│   ├── gateway_limits.py        # Key 请求速率与并发准入
│   ├── maintenance.py           # 后台维护任务节流与账号资源协调
│   ├── credits.py / billing.py  # 到期口径与用量记账
│   ├── model_*.py               # 模型目录、名称、繁忙程度与额度状态
│   ├── growth_plans.py          # 成长任务完成策略
│   ├── growth_plan.py           # 成长计划日常巡检
│   ├── growth_center.py         # 成长中心快照与操作记录
│   ├── scheduler.py             # 定时任务与 token 保活
│   ├── jobrunner.py             # 批量后台任务与进度
│   ├── client_profile.py       # 客户端参数档案
│   ├── turing_token.py          # 设备 token 提供器
│   ├── routers/                 # 账号、Key、分组、模型、代理、任务等 HTTP 路由
│   └── static/                  # 管理界面、/join、主题与本地前端资源
│       ├── index.html / join.html
│       ├── console.css / theme.js
│       ├── filter-combobox.js   # 用量日志的自定义搜索下拉
│       ├── desktop.js          # 桌面桥接与更新公告交互
│       └── vendor/             # 本地样式、字体及第三方前端资源
├── desktop/                     # Windows 窗口、托盘、运行时、更新与应用图标
│   ├── app.py / tray.py
│   ├── runtime.py / updates.py
│   ├── version.json             # 两端应用版本的唯一来源
│   └── README.md
├── android/
│   ├── app/src/main/            # 原生 WebView、前台服务、通知与授权设置
│   ├── python/                  # 手机后端启动与 HTTP 运行时
│   ├── web/                     # 手机布局、设置与原生桥接
│   └── README.md
├── scripts/                     # 用户可用脚本，保留在仓库
│   ├── build_desktop.py / build_android.py
│   ├── daemon.ps1 / watchdog.sh # Windows / Linux 服务守护
│   ├── export_credential_key.py # 生成私有的官方凭证加密配置
│   ├── asar_grep.py / asar_slice.py
│   └── README.md
└── docs/
    ├── PUBLICATION.md           # 文档公开范围与迁移约定
    └── RELEASE_NOTES_*.md       # 各版本公开更新公告
```

本地的 `.env`、数据库、账号凭证、SSH 部署配置、签名文件、`build/`、`dist/` 和 `tests/` 不纳入 Git。`scripts/` 中的通用脚本保留，维护者本机的一次性部署脚本与探针不公开。内部分析、协议研究、取证资料和旧截图保存在仓库外，不随迁移源码公开。

## Windows 与 Android

从 [Releases](https://github.com/xiaofan6ya/wb2api/releases) 下载对应平台安装包及 SHA256 校验文件。1.13.0 及更早版本需要手动安装 1.14.0 一次，此后软件从新仓库检查更新。覆盖安装保留原数据目录，Android 必须使用同签名安装包。

| 平台 | 使用方式 | 详细说明 |
| --- | --- | --- |
| Windows x64 | 下载 EXE 后直接运行，内置 Python 与项目依赖 | [桌面版说明](desktop/README.md) |
| Android ARM64 | 安装 APK，在手机上独立运行后端和 SQLite | [安卓版说明](android/README.md) |

本地应用无需输入后台账号密码。服务监听 `0.0.0.0`，同机使用 `127.0.0.1`，其他设备使用局域网地址；在 **API Keys → 连接地址** 选择检测到的 IP，即可复制相应 API 地址。端口冲突时客户端会选择可用端口并保存。远程浏览器访问管理页仍需要后台登录，调用 API 仍需要 Key。

发现新版本时弹出更新公告，可选择“稍后再说”或“暂不更新此版本”。右上角更新入口和左下角版本号随时可以重新打开公告、下载最新版。Windows 校验下载文件后由用户确认重启安装；Android 下载 APK 后通过系统安装器覆盖安装，同签名升级保留数据。

Android 后台稳定运行需要允许通知、后台运行及取消电池优化。设置页可检查服务、授权、电池和后台限制状态；系统强行停止应用后仍需重新打开。

新版 Windows 官方客户端导出的加密原始 `.info` 无法直接在未配置解密密钥的安卓端解密。迁移时请在电脑账号管理页选择导出“系统明文备份”，或在手机使用手机号登录和 `/join` 授权。明文备份包含账号凭证，请妥善保管。

## Linux Docker 部署

安装 Docker Engine 和 Compose 插件后执行：

```bash
git clone https://github.com/xiaofan6ya/wb2api.git
cd wb2api
umask 077
printf 'ADMIN_USERNAME=admin\nADMIN_PASSWORD=%s\nADMIN_JWT_SECRET=%s\n' "$(openssl rand -hex 16)" "$(openssl rand -hex 32)" > .env.docker
cat .env.docker  # 查看并保存自动生成的后台密码
docker compose --env-file .env.docker up -d --build
docker compose --env-file .env.docker ps
docker compose --env-file .env.docker logs --tail=100 admin
```

访问 `http://服务器IP:8790/admin`，使用 `.env.docker` 的用户名和密码登录。放行 TCP 8790；公网部署建议配置 HTTPS 反向代理。

默认只运行 **一个 admin 容器**，包含 Web UI、多账号网关与定时任务。SQLite 保存在 `admin_data` 命名卷，限流使用进程内存，无需另装 MySQL 或 Redis。保持单 worker，使账号租约、短信出口限流与调度任务共享状态。

Compose 中的 `converter` 是可选的单账号转换器，已放入 `standalone` profile，admin 不依赖它。只有需要独立读取宿主机 `credentials/` 凭证目录时，才在 `.env.docker` 中配置独立转换器的访问密钥（参数原名以 Compose 文件为准），并执行：

```bash
docker compose --env-file .env.docker --profile standalone up -d --build
```

升级执行 `git pull`，再执行 `up -d --build`。`docker compose down` 保留数据，**不要添加 `-v`**。旧版 MySQL 数据不会自动迁入 SQLite；迁移前先备份。沿用 MySQL 时用 Compose override 覆盖 `ADMIN_DATABASE_URL`，地址须从容器内可达。

管理台的「更新与备份」可以检查 GitHub 正式版本、查看 Docker/Linux 升级命令，并下载经过完整性校验的 SQLite 在线备份。MySQL 部署会提供备份命令说明，需先用数据库工具完成备份。当前采用版本检查和手动执行升级，不会从 Web 服务控制宿主机或 Docker。

也可以通过命令行备份 SQLite：

```bash
docker compose --env-file .env.docker stop admin
docker compose --env-file .env.docker cp admin:/data/. ./backup-data/
docker compose --env-file .env.docker start admin
```

容器内检测到的 IP 可能属于 Docker 网桥。远程接入优先使用浏览器当前访问的服务器地址及映射端口；需要局域网访问时填写宿主机 IP。

管理台「积分流水」分别展示上游确认的奖励和余额变化观察，支持账号、来源、类型、时间范围筛选及分页。签到、成长奖励和补偿都会记录来源；无法确认来源的余额变化标记为未知，不与奖励记录相加。升级前已保存的成长记录可分批导入，更早未保存的来源及奖励后余额无法恢复。

账号池操作中的「账号并发上限」支持留空跟随全局、`0` 不限、正整数限制在途请求数。全局默认仍为 `ADMIN_POOL_MAX_IN_FLIGHT=3`；这是网关本地的并发保护，不代表上游提供的配额。维护中的账号始终暂停接入。

## 源码运行

### 前置依赖

| 依赖 | 何时需要 |
| --- | --- |
| Python 3.10+ | 从源码运行后端；Windows / Android 打包用户无需自行安装 |
| SQLite 或 MySQL | 保存账号、Key、日志及调度；单机可用 SQLite，已有 MySQL 部署可继续使用 |
| Redis | 可选，用于 Redis 限流后端；单进程也可使用内存限流 |
| Node.js | 调用本机官方客户端设备 SDK 时需要；Windows 版自动准备 |
| 官方 wb 客户端 | 扫描本机登录态、自动读取加密凭证能力或使用其设备 SDK 时需要；手机号入库本身不要求安装 |

JDK、Android SDK 和 Gradle 只在构建 APK 时需要，版本和安装路径见 [构建说明](BUILD.md)。

### 安装与配置

先克隆仓库并进入项目目录，再创建虚拟环境：

```bash
git clone https://github.com/xiaofan6ya/wb2api.git
cd wb2api
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell 则执行 .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`，设置 `ADMIN_USERNAME`、`ADMIN_PASSWORD` 和随机的 `ADMIN_JWT_SECRET`。模板保留 MySQL/Redis 配置；单机 SQLite 可改为：

```dotenv
ADMIN_DATABASE_URL=sqlite:///./wb2api.db
ADMIN_REDIS_URL=
ADMIN_GATEWAY_LIMIT_BACKEND=memory
```

启动时自动初始化所需表结构。升级前备份数据库与配置，已有 MySQL 数据不会因改用 SQLite 自动迁移。

### 方式一：单端口一体化启动

```bash
python main.py
python main.py --host 127.0.0.1 --port 8790
```

默认监听 `0.0.0.0:8790`，管理页、管理 API 和共享网关共用该端口。`main.py` 将服务输出同时写入控制台与 `logs/`，关闭终端会停止前台服务。端口被本项目旧实例占用时会尝试接管；被其他程序占用时先检查占用者，确认后才使用 `--restart` 强制接管。

### 方式二：直接运行管理后台

```bash
python -m uvicorn admin.server:app --host 0.0.0.0 --port 8790
```

同样包含账号池网关和调度任务。使用单 worker，避免账号租约、短信出口限制和后台调度被拆成多份状态。直接运行 uvicorn 时日志由当前终端或进程管理器收集。

打开 `http://127.0.0.1:8790/admin`。Web 部署需要管理账号登录。添加账号后自动刷新余额与到期快照；手机号支持中国大陆 `+86`、中国香港 `+852`、中国澳门 `+853`。等待验证码时保留会话，遵守发送倒计时。

### 方式三：独立单账号 converter

在已准备好本机登录凭证的电脑运行：

```bash
python converter.py --host 127.0.0.1 --port 8787 --api-key "<自设访问密钥>" --desensitize --log logs/converter.log
```

独立 converter 读取本机 `.info`，不使用数据库账号池、Key 配额或模型分组。常用参数：

| 参数 | 用途 |
| --- | --- |
| `--host` / `--port` | 默认 `127.0.0.1:8787`，按需修改 |
| `--api-key` | 要求客户端携带指定密钥 |
| `--log PATH` | 将请求处理日志写入指定文件 |
| `--desensitize` | 启用请求文本兼容处理 |
| `--no-compact` | 配合上项保留更多原始文本 |
| `--skip-check` | 跳过启动预检 |

独立 converter 未设置密钥时不校验调用方，因此对其他设备开放监听前应先设置密钥。可用 `python converter.py --help` 查看完整参数。

### 服务守护与备份

- Windows：`powershell -File scripts/daemon.ps1 -Detach` 启动守护，`-Status` 查看状态，`-Stop` 停止。脚本默认使用项目 `.venv`。
- Linux：使用 systemd、现有进程管理器或 `scripts/watchdog.sh`；脚本支持通过环境变量指定项目、Python、运行用户和端口，配置方法见文件头。
- Docker：使用 Compose 已配置的 `restart: unless-stopped`；容器数据备份方法见前面的 Docker 章节。
- SQLite 源码部署：停止服务后备份 `.env` 与实际数据库文件，再启动；MySQL 使用对应数据库备份工具。不要把数据库备份提交到仓库。

## 账号管理与日常使用

### 添加账号

1. 首次打开工作台，进入账号池，选择手机号登录、上传凭证或扫描本机已有账号。
2. 手机号登录选择 `+86` / `+852` / `+853` 后输入号码和验证码。登录弹窗保留等待中的手机号与倒计时，只能点击关闭按钮关闭。
3. 也可打开 `/join` 完成手机号授权入库，无需将后台密码交给授权人。该页会把授权凭证添加到当前服务的账号池。
4. 批量导入支持系统导出的凭证文件；导入后检查余额、到期快照与账号状态。同 UID 再次登录会更新已有记录。

短信发送受共享出口和上游 IP 频率限制。按钮冷却或接口提示稍后重试时，等待倒计时结束，避免连续点击或频繁切换手机号发送。

### 账号状态、余额和到期

- 在账号池中启停账号、刷新余额、查看模型额度与冷却。总积分、可用积分和逐包到期快照分别反映不同口径，以页面采集时间为准。
- 新账号入库及奖励领取后会刷新余额和到期快照；后台任务因账号忙碌、出口保护或上游错误而顺延时，查看任务结果和刷新时间。
- 积分到期页展示临近到期账号、积分包和消耗构成。调度会先考虑到期紧迫度，再执行配置的选号策略。
- 冷却结束表示可再次尝试，不保证上游已经恢复；模型状态未知也不等同于账号失效。

### 导出与跨设备迁移

界面默认导出官方新版加密凭证：单账号为 `.info`，批量为 `.jsonl`；批量文件用于系统恢复，写入官方客户端时使用单账号 `.info`。Windows 可读取已安装 wb 的加密能力；Linux / Docker 需配置匹配的私有密钥文件，具体步骤见 [凭证配置脚本](scripts/README.md#凭证配置)。

“系统明文备份”用于未配置解密能力的设备迁移，包含可直接使用的账号令牌。加密导出不会延长令牌有效期，不同官方客户端版本的密钥也未必兼容。请将导出文件保存在私有位置。

### API Key 配额与独占账号

在 API Keys 页创建密钥并保存创建时显示的明文，后续列表仅显示脱敏信息。为 Key 设置积分上限、模型分组和启用状态；调用会计入该 Key 的用量。

可将 Key 绑定为独占使用某个账号：公共池和公共测速会避开该账号及同 UID 的重复记录。绑定 Key 只使用该账号，同时受全局模型规则、分组和 Key 配额限制；账号不可用时不会自动借用公共池。停用 Key 保留独占关系，解绑或删除 Key 才释放；删除绑定账号前需先解绑。

### 界面与查询

左下角可切换浅色/深色主题，手机在导航抽屉内切换。表格、输入框、下拉框、滚动条及 API 示例会跟随主题。Tooltip 可移入、滚动、选中和复制。

使用记录与用量日志支持按 Key、账号、模型及端点查询，日志中的模型和 Key 使用可搜索下拉，也可手动输入筛选。默认查看近期数据，需要长期趋势时调整时间窗口。管理面板状态写入 `?tab=`，刷新页面后保留当前面板，也可分享对应面板链接。

用量日志和请求动态直接展示客户端 IP，支持复制及点击筛选。IP 管理的活跃 IP 表可筛选高频低 Token、高频低积分，设置最少请求数、平均消耗上限及 IP 地址；均值按成功用量记录统计，未返回 Token 的请求不作为低消耗。入口次数包含鉴权拒绝、限流和封禁；点击 IP 可查看用量明细。IP 仅从受信任代理的转发头读取，反向代理应覆盖 `X-Real-IP`；直连使用连接来源地址。

反向代理必须让后端看到真实客户端 IP，否则所有请求看起来都是代理的 IP，登录防爆破会退化成「全局锁」——任何人打几次错误密码就能把管理员锁在门外。nginx 站点配置（放进反代本服务的 `location` 块）：

```nginx
location / {
    proxy_pass http://127.0.0.1:8790;
    proxy_set_header Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;              # 覆写，后端只信任这个
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;                                   # 流式响应必需
}
```

后端默认只信任 `127.0.0.1,::1` 直连对端提交的转发头（`ADMIN_TRUSTED_PROXY_IPS` 可改为 CIDR 列表）；不可信来源自报的 `X-Real-IP` / `X-Forwarded-For` 一律忽略。登录失败计数按「(IP, 用户名) 对 + 同 IP 全局 + 设备/浏览器指纹」三个维度独立累计：攻击者只能锁住「自己的 IP × 他猜的用户名」，锁不到管理员的来源 IP。

调用详情先加载摘要，展开某次请求后按需读取正文，每页最多 64 KiB；翻页替换当前预览，关闭详情取消正在加载的预览。完整正文通过独立 JSONL 下载，不在页面中同时展开或复制多份大正文。记录时复用受限的原始字节缓存，避免重新序列化完整请求。

调用详情支持下载 JSONL（用量、入站请求、转换后各次实际发往上游的请求和诊断）。在用量日志打开“记录请求详情”即时生效，默认关闭，仅记录开启后进入的 JSON 推理请求。请求内容仅存服务进程内存，1 小时过期，最多 200 条或 64 MiB，达到上限淘汰最旧记录；重启全部清空，开关恢复环境初始值。单请求入站及所有上游尝试合计上限 8 MiB，单份正文由 `ADMIN_LOG_PAYLOAD_MAX_MB` 控制（默认 8，范围 1–8），超出截断并明确标记。请求头采用保留名单并隐藏凭据，正文按实际序列化内容保存，可能包含完整对话；仅管理员可以读取。关闭停止新请求采集，已有缓存可手动清空。历史未记录、过期或淘汰的内容无法恢复。`ADMIN_LOG_REQUEST_PAYLOAD=1` 可改变重启后的初始开关。多进程部署各进程的开关和缓存独立，需使用单工作进程进行此项临时排查。

### 客户端参数与跨部署同步

Windows 或源码部署的“客户端参数”页面可探测本机官方客户端版本和请求参数，查看每项取值来源，按需保存、重置或切换来源策略。`auto` 优先现场探测，适合装有官方客户端的电脑；`saved` 优先已保存档案，适合没有官方安装包的服务器。显式环境变量优先于这两种策略，兼容参数名以源码为准。

探测分两路并逐项标注来源：**安装包**（版本号、commit、部署形态、风控渠道号等，读自官方安装目录）与**本机系统**（os / arch / 内核版本 / CPU 核数 / 内存 / 时区 / 内嵌 Node 版本，仅在装了官方客户端的机器上采纳，取值公式与官方源码一致）。客户端档案只用于展示、同步和需要它的客户端业务；普通模型请求不会自动添加未经实际请求证实的 `X-Stainless-*`、设备、IP、ACP 或链路追踪头。UA 仅在档案或环境中有明确值时使用，Python 的 TLS ClientHello 仍由运行时决定，不能宣称与 Electron 完全一致。

在“同步设置”中配置目标服务地址与设备同步密钥，可推送账号、模型配置和当前生效的客户端参数档案。接收端将档案设为 `saved`，避免服务器探测不到客户端时丢失已同步值。同步使用专门的设备密钥，不使用调用模型的 API Key；同步目标应是自己管理的服务，并使用 HTTPS 或受保护的网络。安卓端将对应区域用于本机服务设置，不提供完整桌面参数管理。

## 模型配置与模型分组

在模型配置页同步上游列表，按需启用/禁用单个模型或批量操作。将模型加入分组，再把 Key 绑定到该分组；绑定后只能调用分组内且全局启用的模型，越权会被拒绝。未绑定分组的 Key 按全局配置调用。模型调用权限还取决于实际账号的权益和上游限额。

“显示倍率”和“显示繁忙程度”控制 `GET /v1/models` 返回的 **`name`**，例如 `model-id · ×1.25 · 空闲`，不改变 `id`，也不隐藏管理表格中的状态列。客户端若仅展示 `id`，不会看到扩展名称。上游状态查询失败或数据缺失时显示“未知”。

模型健康/冷却界面可查看账号与模型的可用情况。主动检测会发送真实的短请求，展示响应头耗时、首字延迟、片段间隔抖动及当次健康状态，消耗少量额度。繁忙程度来自上游容量信息，延迟与抖动来自实际调用观测，二者不能互相替代。

## 自动化与定时任务

账号池的“刷新全部”也通过一次 POST 提交后台任务，显示进度与成功/失败/跳过汇总。账号停用、正在对话或受保护时跳过，可稍后重试。上游接口按账号鉴权，由服务端依次请求，浏览器无需逐账号发送刷新请求。

### 任务类型

| 任务标识 | 功能 |
| --- | --- |
| `refresh_balances` | 刷新账号池总积分和余额 |
| `refresh_credits` | 更新逐账号积分包与到期快照 |
| `sync_models` | 同步上游模型列表 |
| `daily_checkin` | 查询每日积分打卡状态并领取可领取奖励 |
| `refresh_growth_tasks` | 更新成长任务列表与状态 |
| `run_growth_tasks` | 参与、执行可自动化成长任务并领取奖励 |
| `run_growth_plan` | 巡检活跃、宠物旅行、地图奖励、免费抽奖与盲盒等成长计划活动 |
| `keepalive_tokens` | 按配置的整点节流刷新账号登录态 |

在“定时任务”中新建或编辑任务，可保留系统预设，或选择固定间隔（分钟、小时、天、周）、每天、每周指定执行日、每月指定日期。日历周期按北京时间（UTC+8）执行，每月遇到不存在的日期时在月末执行。保存后重新计算下次执行时间，默认任务名中的时间描述随周期更新，自定义名称保留。可点击“立即运行”，并查看上一轮执行摘要、逐账号结果和失败原因。每日签到支持设置停止领取时间；活动已结束时会停止相应领取操作。奖励是否到账以上游返回和刷新后的余额为准。

### 成长任务与成长计划

成长任务页面展示一次性任务的参与、执行、领取状态。支持的任务按策略执行，不支持或条件不满足的任务会提示原因；批量执行通过后台任务提供进度，页面关闭不会把已经发出的操作当作未执行。

成长计划页面展示连续登录天数、能量、月度活跃地图、年度热力图、宠物与领取记录。支持手动签到、开盲盒、领取旅行礼物、兑换已达标档位及使用已有免费抽奖次数。日常巡检跳过已完成项目，对未知结果避免盲目重复操作；实物奖品需在官方成长中心填写地址。

自动任务与手动操作共享资源协调和执行状态；账号正在对话、处于冷却或出口保护期间，维护任务会跳过或顺延。低频调度有助于减少后台操作与正常对话争用同一账号。新积分到账后自动刷新账号池的余额与到期快照。

## API 接入与客户端配置

在 API Keys 页创建密钥并选择连接地址。两类 SDK 的 Base URL 约定不同：

| 协议 | Base URL 示例 | 请求路径 |
| --- | --- | --- |
| OpenAI Chat / Responses | `http://127.0.0.1:8790/v1` | `/v1/chat/completions`、`/v1/responses` |
| Anthropic Messages | `http://127.0.0.1:8790` | `/v1/messages` |

OpenAI 使用 `Authorization: Bearer <API Key>`；Anthropic 可使用 `x-api-key`。启用模型通过 `GET /v1/models` 获取。模型配置中的“显示倍率”和“显示繁忙程度”分别控制接口返回的 `name`，例如 `model-id · ×1.25 · 空闲`；关闭后不拼接对应部分，模型 `id` 保持不变。管理表格仍显示完整状态。

模型检测会发送实际的短请求，展示响应头耗时、首字延迟、片段间隔抖动和当次健康状态，会消耗少量额度；结果代表检测时的账号与网络状况。

### 共享网关端点

| 方法与路径 | 用途 |
| --- | --- |
| `GET /v1/models` | 当前 Key 有权使用的启用模型 |
| `GET /v1/balance` | 当前 Key 可用积分的同步快照与网关额度 |
| `GET /user/balance` | DeepSeek 官方余额接口兼容（`/v1/user/balance` 同义） |
| `POST /v1/chat/completions` | OpenAI Chat，支持流式和非流式 |
| `POST /v1/responses` | OpenAI Responses，面向 Responses 兼容客户端 |
| `POST /v1/messages` | Anthropic Messages，包含工具消息与思考参数兼容处理 |
| `POST /v1/messages/count_tokens` | Anthropic token 数量本地估算，不请求上游模型 |
| `POST /v1/images/generations` | AI 文生图，JSON 请求 |
| `POST /v1/images/edits` | AI 图生图，支持单张图片上传或 JSON 图片来源 |

`GET /v1/models` 保留上游的模型名称、输入与输出上限、能力及思考档位，并提供 `context_length`、`context_window`、`max_input_tokens`、`max_output_tokens`、`reasoning_supported_efforts` 和 `reasoning_default_effort`。上游没有提供的容量返回空值，不填入猜测值。DSH 的模型发现流程可读取上下文与输出上限；是否采用思考档位取决于客户端自身支持。

`GET /v1/balance` 使用同一个 API Key，通过 Bearer 或 `X-API-Key` 鉴权。`balance` / `balance_remain` 是该 Key 可访问的活跃账号余额与 Key 剩余额度两者的较小值；无限额度 Key 使用账号余额。`upstream_balance_remain` 是对应账号范围的上游积分快照，`key_quota` 单独说明网关额度。绑定 Key 只统计绑定账号；公共 Key 排除其他 Key 独占的账号，同 UID 不重复累计。`shared_pool=true` 表示余额由公共 Key 共享，并非该 Key 的独占额度。`synced_at` 是全部纳入账号中最早的同步时间；存在未同步账号时为 `null` 且 `snapshot_complete=false`。此查询不触发逐账号上游刷新，需在管理页或定时任务刷新余额后读取。额度耗尽的有效 Key 仍可查询，返回可用余额 0；停用 Key 返回 403。

`GET /user/balance` 是 DeepSeek 官方余额接口（`https://api.deepseek.com/user/balance`）的兼容实现，`/v1/user/balance` 为同义别名，供 base_url 已带 `/v1` 的 DeepSeek 系客户端与渠道余额检测工具直接使用。返回 DeepSeek 的固定形状（金额为字符串）：

```json
{
  "is_available": true,
  "balance_infos": [{
    "currency": "CREDITS",
    "total_balance": "4200.00",
    "granted_balance": "0.00",
    "topped_up_balance": "4200.00"
  }]
}
```

口径与 `/v1/balance` 一致：`total_balance` 是该 Key 实际可用积分（账号池余额与 Key 剩余额度取小，无限额度 Key 用账号余额），`is_available` 表示可用积分大于 0。上游没有「赠金 / 充值」的区分，`granted_balance` 恒为 `"0.00"`；记账单位是积分而不是人民币，因此 `currency` 如实标注为 `CREDITS` 而非冒充 `CNY`。同样不触发逐账号上游刷新。

独立 converter 另外提供 `/v1/balance`、`/user/balance` 和 `/health`，挂载时路径为 `/gw/v1/balance`、`/gw/user/balance` 和 `/gw/health`；其余额属于本机桌面登录账号。

### OpenAI 兼容客户端与 SDK

Cherry Studio、ZCode、LobeChat、NextChat、Open WebUI 等客户端选择 OpenAI 兼容接入，Base URL 填 `http://127.0.0.1:8790/v1`，API Key 使用后台创建的真实密钥，模型从 `/v1/models` 选择。

```bash
curl -N http://127.0.0.1:8790/v1/chat/completions \
  -H "Authorization: Bearer <API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model":"<MODEL_ID>","stream":true,"messages":[{"role":"user","content":"你好"}]}'
```

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8790/v1",
    api_key=os.environ["WB2API_API_KEY"],
)
result = client.chat.completions.create(
    model=os.environ["WB2API_MODEL"],
    messages=[{"role": "user", "content": "你好"}],
)
print(result.choices[0].message.content)
```

SDK 示例需自行安装 `openai`。从其他设备访问时，用 API Keys 页面复制的局域网或服务器地址替换 `127.0.0.1`，保留实际端口。

### AI 生图

生图使用同一套 API Key、模型启停与分组权限、独占账号绑定和并发保护。模型需要明确指定，不能使用 `auto`；例如 `hunyuan-image-alpha`。其他生图模型是否支持某个端点、尺寸、数量和风格，以上游实际能力为准。

模型列表使用 `IMAGE` / `TEXT` 标签区分输出类型。图片模型按上游返回的 `usage.credit` 实际积分记账，不套用文本模型的每千 token 倍率；目录未给价格不代表免费。2026-10-23 单张 `hunyuan-image-alpha` 实测文生图和图生图均返回 5.71 积分，此数字仅为当次实测，不是固定价格。

```python
image = client.images.generate(
    model="hunyuan-image-alpha",
    prompt="白色背景上的蓝色圆形",
    n=1,
    response_format="url",
)
print(image.data[0].url)

with open("input.png", "rb") as source:
    edited = client.images.edit(
        model="hunyuan-image-alpha",
        image=source,
        prompt="把圆形改成绿色，保留白色背景",
    )
print(edited.data[0].url)
```

`/v1/images/generations` 使用 JSON，`/v1/images/edits` 可使用 `multipart/form-data` 上传 `image` 文件，也可使用 JSON 传入一个 HTTPS `image_url` 或 base64 `image_data`。当前支持一张输入图片，单图最大 20 MiB，请求体最大 32 MiB；不支持 mask、流式和多图输入。参数支持 `model`、`prompt`、`n`、`size`、`quality`、`style`、`response_format`、`user`，具体取值仍由模型校验。

返回格式支持 `url`（默认）和 `b64_json`。上游返回 URL 时，网关可将腾讯 COS 图片转换为 base64；转换失败仍返回 HTTP 200，在 `warning.code` 中标记 `image_conversion_failed`，并在 `data` 中保留已生成图片地址，避免 SDK 因 5xx 自动重复生图。成功生成的图片按真实上游积分记账。网络超时或 5xx 不自动重发生图，避免结果不明时重复生成和扣费。用量记录区分 `images/generations` 与 `images/edits`。

### Codex CLI（Responses）

在现有配置中合并一个 provider 和 profile，模型填写后台当前启用且账号有权限的 ID：

也可使用 [wb2api 客户端配置示例](codex-wb2api.example.toml)。示例中的 `WB2API_API_KEY` 是客户端自定义变量，不会修改服务端已有环境变量名。

```toml
# ~/.codex/config.toml
[model_providers.wb]
name = "wb"
base_url = "http://127.0.0.1:8790/v1"
wire_api = "responses"
env_key = "WB2API_API_KEY"

[profiles.wb]
model = "<MODEL_ID>"
model_provider = "wb"
```

```bash
export WB2API_API_KEY='<API_KEY>'
codex --profile wb
```

PowerShell 使用 `$env:WB2API_API_KEY='<API_KEY>'` 设置环境变量。共享网关必须使用有效 Key，不能用任意占位字符串代替。

### Claude Code / CC Switch（Anthropic）

原有 Opus / Sonnet / Haiku 映射及思考档位保持不变。兼容 `thinking.effort`、`output_config.effort`、`reasoning_effort`、`reasoningEffort` 和 `reasoning.effort`；只传 `thinking.type=adaptive` 或 `enabled` 而未传档位时，沿用默认 `high`，明确关闭时保持关闭。用量日志记录实际转发档位。客户端界面选择若未进入请求体，网关无法还原该选择。

Anthropic Base URL 填 `http://127.0.0.1:8790`，不附加 `/v1` 或 `/v1/messages`，由 SDK 拼接路径。将当前进程的连接变量设置为：

```bash
export ANTHROPIC_BASE_URL='http://127.0.0.1:8790'
export ANTHROPIC_AUTH_TOKEN='<API_KEY>'
export ANTHROPIC_MODEL='<MODEL_ID>'
```

CC Switch 中对应填写 Anthropic 服务地址、后台 API Key 和模型。可直接使用上游模型 ID；使用 `claude-opus-*` / `claude-sonnet-*` / `claude-haiku-*` 别名时，服务端按 `ADMIN_ANTHROPIC_MODEL_*` 配置映射，实际模型仍需通过全局启用、Key 分组和账号权限检查。别名只做请求映射，不代表上游提供同名 Claude 模型。

## 管理接口总览

管理接口使用 `POST /api/login` 返回的 JWT，通过 `X-Admin-Token` 发送。它与 `/v1/*` 的 API Key 不同，不要混用。

| 模块 | 主要接口 | 用途 |
| --- | --- | --- |
| 后台登录 | `POST /api/login` | 获取管理会话 |
| 手机号登录 | `/api/login/start`、`send`、`verify`、`status/{sid}` | 创建、发送和验证手机号会话 |
| 账号池 | `GET/POST /api/accounts`、`POST /api/accounts/batch` | 列表、新增和批量导入 |
| 账号操作 | `/api/accounts/{id}/refresh`、`PATCH/DELETE /api/accounts/{id}` | 刷新、启停与删除 |
| 凭证导出 | `POST /api/accounts/export`、`GET /api/accounts/{id}/export` | 批量/单账号导出，支持 `format=client` 或 `backup` |
| API Key | `GET/POST /api/keys`、`PATCH/DELETE /api/keys/{id}` | 密钥、限额、账号绑定与分组 |
| 模型配置 | `/api/models/configs`、`sync`、`batch-toggle` | 模型列表、同步与批量启停 |
| 模型显示与检测 | `/api/models/display`、`busy`、`speed-test` | 名称开关、繁忙程度与真实请求检测 |
| 模型分组 | `/api/groups`、`/api/groups/{id}` | 分组及模型权限 |
| 用量统计 | `GET /api/stats/usage` | 总览、模型、端点、Key 和趋势 |
| 用量日志 | `/api/logs`、`/api/logs/accounts`、`/api/logs/summary` | 分页日志、账号筛选与汇总 |
| 积分到期 | `/api/credits/expiry`、`/api/credits/usage`、`/api/accounts/credits/sync` | 到期与消耗构成、重新采集 |
| 定时任务 | `/api/schedules`、`/api/schedules/{id}`、`/api/schedules/{id}/run` | 任务管理与立即执行 |
| 成长任务 | `/api/growth/tasks`、`accept`、`run`、`claim`、`plans` | 任务详情、参与、执行、领奖与策略 |
| 成长计划 | `/api/growth-center/accounts`、`/api/growth-center/accounts/{id}` | 账号快照、操作和领取记录 |
| 应用版本 | `GET /api/app/version`、`GET /api/app/releases` | 当前版本与平台更新信息 |

参数和方法以对应 `admin/routers/` 源码为准。需要交互式 API 文档时，可在受控环境设置 `ADMIN_ENABLE_DOCS=1` 后访问 `/docs`；默认关闭。

## 环境变量

完整配置和注释见 [.env.example](.env.example)。源码部署从项目根目录 `.env` 读取；EXE/APK 使用自身数据目录，不读取项目部署配置。

| 配置 | 用途与取值 |
| --- | --- |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Web 管理账号与密码，源码部署必须设置 |
| `ADMIN_JWT_SECRET` / `ADMIN_JWT_EXPIRE_HOURS` | 管理会话签名密钥与有效期，密钥使用至少 32 字节随机值 |
| `ADMIN_HOST` / `ADMIN_PORT` | `main.py` 监听地址与端口，默认 `0.0.0.0:8790` |
| `ADMIN_DATABASE_URL` | SQLAlchemy 连接串，支持 SQLite 或 MySQL；模板默认 MySQL |
| `ADMIN_REDIS_URL` | Redis 连接地址，内存限流模式可留空 |
| `ADMIN_GATEWAY_LIMIT_BACKEND` | `auto` / `redis` / `memory`，按部署选择 |
| `ADMIN_GATEWAY_RATE_PER_MINUTE` / `ADMIN_GATEWAY_MAX_CONCURRENT` | 每 Key 的分钟请求量与并发准入上限 |
| `ADMIN_GATEWAY_WAIT_TIMEOUT` / `ADMIN_GATEWAY_REQUEST_TIMEOUT` | Key 并发空位等待默认 `15` 秒（上限 `30`）；推理请求最长 `160` 秒，含排队、上传、上游处理和流式返回 |
| `ADMIN_GATEWAY_QUEUE_TIMEOUT` | 请求内部排队上限，默认 `90` 秒，覆盖等上游发车间隔与等可用账号，独立于 Key 并发等待 |
| `ADMIN_JOIN_MAX_CONCURRENT` | 注册接口最大并发数，超过后快速返回重试提示，默认 `4` |
| `ADMIN_BACKEND` | 上游服务地址；保留实际域名，默认值以配置模板和源码为准 |
| `ADMIN_CLIENT_AUTH_DIR` | 扫描和切换本机账号时读取的凭证目录 |
| `ADMIN_ACCOUNT_SELECT` | `expiry_weighted`（默认）/ `throughput` / `expiring` / `oldest` / `remain` / `lru` / `weighted`；控制台保存的策略优先 |
| `ADMIN_POOL_MAX_IN_FLIGHT` | 单账号最大在途请求数，默认 `3` |
| `ADMIN_POOL_MAX_ROTATE` / `ADMIN_POOL_MAX_TOTAL_ATTEMPTS` | 单请求的换号与总尝试次数上限，默认均为 `6` |
| `ADMIN_POOL_RETRY_WINDOW` | 输出前重试窗口，默认 `90` 秒，不中断已经活跃的流 |
| `ADMIN_SESSION_STICKY` / `ADMIN_SESSION_STICKY_TTL` | 会话粘性及空闲有效期，默认开启、`1800` 秒 |
| `ADMIN_STREAM_CONNECT_TIMEOUT` / `ADMIN_STREAM_IDLE_TIMEOUT` | 上游连接与流静默阶段超时，默认 `15` / `180` 秒；请求总时限另见网关配置 |
| `ADMIN_STREAM_WRITE_TIMEOUT` / `ADMIN_STREAM_POOL_TIMEOUT` | 写请求与等待连接池超时，默认 `60` / `20` 秒 |
| `ADMIN_KEEPALIVE_ENABLED` / `ADMIN_KEEPALIVE_HOURS` / `ADMIN_KEEPALIVE_ACCOUNT_GAP` | token 保活开关、执行整点及逐账号间隔 |
| `ADMIN_LOGIN_MAX_ATTEMPTS` / `ADMIN_LOGIN_IP_MAX_ATTEMPTS` | 登录防爆破阈值：`(IP, 用户名)` 对默认 `5` 次；同 IP 全局默认取其 3 倍。任何人打错密码只锁「他的 IP × 他猜的用户名」，锁不到管理员的 IP |
| `ADMIN_BAN_RECHECK_ENABLED` / `MIN_INTERVAL` / `MAX_INTERVAL` | 风控封号（11140 等）自动复核：真实模型调用探测，通过即自动解封；退避间隔默认 `1800`–`21600` 秒 |
| `ADMIN_ANTHROPIC_MODEL_OPUS` / `SONNET` / `HAIKU` | 三档模型别名映射，后三项均使用 `ADMIN_ANTHROPIC_MODEL_` 前缀 |
| `CONVERTER_API_KEY` | 可选 `/gw` 的独立访问密钥，本机凭证目录另按源码中的兼容参数配置 |
| `CONVERTER_DESENSITIZE` / `CONVERTER_LOG` | 内嵌 converter 的文本兼容处理与日志位置 |
| `ADMIN_ENABLE_DOCS` / `ADMIN_DEV_TOOLS` | 交互式 API 文档与本机开发辅助功能开关 |

`ADMIN_CLIENT_AUTH_DIR` 用于后台扫描/切换账号，独立 converter 的凭证目录另行配置，二者不能混为一项。服务用户与桌面登录用户不同的 Windows 部署，应填写该用户可读的绝对路径，避免 `%LOCALAPPDATA%` 指向系统账户目录。

DSH 等客户端通过 `<system-reminder>` 注入 `Instructions from: …/AGENTS.md:` 时，网关会把对应正文单独提取保留，不参与可选文本脱敏、运行时摘要或 Responses 历史截断；同一请求里的多份规则按出现顺序保留。已确认导致上游拒绝的固定身份句和模板短语仍执行必要的兼容归一，避免 system 消息中的规则触发 `11128`，项目指令不会整段删除。来源标题中的本地路径会省略，其余运行时元数据继续按原配置处理，消息角色与图片附件保持原有语义。此处理覆盖 Chat、Responses 和 Messages 接口，无需手动复制规则到每个会话。

## 稳定性与流量治理

| 机制 | 行为 | 实现位置 |
| --- | --- | --- |
| 账号选择 | 先按积分到期紧迫度筛选，再执行余额、LRU 或加权等策略 | `admin/pool.py`、`admin/credits.py` |
| 在途租约 | 单账号并发上限，结束或失败时释放资源 | `admin/pool.py`、`admin/routers/proxy.py` |
| 公平容量队列 | 按 Key 轮转选号与原子占位；相同等待范围合并探测，释放时立即唤醒；最多 512 个等待者、每 Key 64 个 | `admin/capacity_queue.py` |
| 吞吐优先策略 | 先选并发占用比例最低的账号，同负载保留粘性，再考虑积分到期与闲置 | `admin/routing_policy.py`、`admin/routers/proxy.py` |
| 会话粘性 | 尽量让同会话使用同一账号，降低多轮切换 | `admin/pool.py` |
| 独占账号 | 公共池与绑定 Key 使用不同的账号范围 | `admin/account_binding.py` |
| 错误分类 | 区分凭证、余额、模型限额、账号限速、出口保护和网络错误 | `admin/routers/proxy.py`、`admin/health.py` |
| 模型冷却 | 按账号与模型记录限制，单模型受限不等于账号所有模型停用 | `admin/model_limits.py`、`AccountModelCooldown` |
| 重试与退避 | 输出前在次数和时间预算内处理可恢复错误，遵守上游重置时间和 `Retry-After` | `admin/routers/proxy.py` |
| 出口保护 | 多账号出现相同出口风险时停止无效轮转 | `admin/pool.py` |
| 后台维护协调 | 优先使用空闲账号，冷却、占用或出口保护时跳过/顺延 | `admin/maintenance.py`、`admin/scheduler.py` |
| token 保活 | 串行节流续期，回写刷新后的凭证 | `admin/scheduler.py`、`converter.py` |
| 分级超时 | 连接、读取、写入、连接池分别设置；流式按静默时间判断 | `admin/routers/proxy.py` |

增加换号次数可能延长失败请求的等待时间，不能消除上游限额。已经向客户端输出的流无法通过服务端换号无缝续接；上游中途失败时，客户端仍可能需要重试。

多人共享 Key 时，可在积分到期页面把自动选号切到“吞吐优先”，并在 Key 页面为共享 Key 单独设置并发和 RPM。全局默认仍是每 Key 4 路并发、60 次/分钟；调度优化不会自动扩大现有权限。可先从共享 Key 16 路并发、240 次/分钟开始观察，再根据健康账号数、首字延迟、429 和排队 P95 调整；单账号默认 3 路和每账号 75ms 启动间隔继续保留。公平排队让不同 Key 轮流获得新空位，已启动的长请求不会被抢占；满池时仍需要等待，要求硬隔离可使用独占账号绑定。

账号租约和容量公平队列均为进程内状态，建议单个网关 worker 管理共享池。Redis 可共享 Key 准入和账号发车时钟，但不会把账号租约变成跨进程租约。多 worker 部署不能把每进程上限误认为全局账号上限。日志中的 `account_queue_wait`、`gateway_queue_wait`、`upstream_queue_wait` 可分别识别账号容量、Key 准入和发车间隔的等待。

## 日志与常见问题

使用 `main.py` 时查看 `logs/`，Windows 版查看 `%LOCALAPPDATA%\***\logs/`，Docker 使用 `docker compose --env-file .env.docker logs --tail=100 admin`。用量日志可结合账号、模型、端点、耗时和错误码定位单次失败。

| 现象 | 检查方向 |
| --- | --- |
| 管理登录返回 503 | 是否设置 `ADMIN_USERNAME`、`ADMIN_PASSWORD`，服务是否已重启读取新配置 |
| 管理接口 401 | Web 管理会话是否过期；本地应用重新打开工作台，远程浏览器重新登录 |
| `/v1/*` 返回 401 | 是否携带后台生成的 API Key，是否把管理 JWT 当成 API Key |
| Key 配额耗尽 / 402 | 检查 Key 积分上限、已用量和账号余额 |
| 模型 403 或不可用 | 检查全局启用、Key 分组、独占账号范围及上游模型权限 |
| 503 / 无可用账号 | 查看账号启停、余额、租约、模型冷却、重置时间与出口保护 |
| 短信发送受限 | 等待本地倒计时及上游限制恢复，避免重复发送 |
| 新积分未马上显示 | 核对任务是否真正领取成功，查看余额与到期快照的刷新时间及顺延原因 |
| 加密 `.info` 导入/导出失败 | 检查是否具备匹配版本的官方加密能力；迁移可选系统明文备份或手机号登录 |
| 局域网无法连接 | 使用实际 `192.168.*` 等地址和端口，检查系统防火墙；`0.0.0.0` 是监听地址，不能作为其他设备的目标 IP |
| 长对话被打断 | 查看流静默超时和上游错误，避免把活跃流改成固定总时长限制 |
| 安卓退后台后服务中断 | 检查通知、后台运行、电池优化和厂商省电限制；系统强行停止后需重新打开 |
| 更新未提示 | 检查 GitHub 网络、正式 Release、平台文件名与 SHA256；无本平台安装包的版本只显示公告 |

需要检查本机运行依赖时执行 `python wb_envcheck.py --fast`；去掉 `--fast` 可进一步执行设备能力等慢检查。它可能输出本机路径和环境信息，对外反馈时先遮盖个人信息；提交 Issue 请提供脱敏后的错误类型、模型和复现步骤，不附账号凭证或数据库。

## 技术栈与已知限制

- 后端：Python、FastAPI、SQLAlchemy、httpx；数据库支持 SQLite / MySQL，限流支持内存 / Redis。
- Web UI：HTML、JavaScript、Tailwind 样式及本地字体/图标资源，无需 Node 前端构建即可运行。
- Windows：PyWebView / WebView2、托盘和 PyInstaller 单文件打包；Android：原生 Java 外壳、WebView、Chaquopy Python 运行时及前台服务。
- 管理端使用 JWT；API Key 使用哈希校验，明文只在创建时展示。数据库中的账号凭证与备份仍属于敏感数据，需限制访问。
- 上游未返回实际积分消耗时可能使用估算，不能把本地统计当作上游账单的精确替代；活动奖励、模型、倍率和限额也可能随上游变化。
- 配额在请求结束时记账，并发在途请求可能造成短暂超额；单进程协调不等于跨 worker 的分布式账号租约。
- 本项目不附带官方账号、私有密钥或设备 SDK。缺少本机设备能力时部分接口可降级运行，但不能保证上游接受所有请求。
- 客户端后台运行仍受操作系统限制，更新也依赖 GitHub 网络；未配置强制更新。

## 文档与构建

- [文档公开范围与迁移约定](docs/PUBLICATION.md)
- [EXE / APK 构建与发布](BUILD.md)：根目录 `build.bat` 可统一构建两端
- [通用工具脚本](scripts/README.md)：守护进程、凭证配置与 ASAR 工具
- [1.0.4 更新公告](docs/RELEASE_NOTES_1.0.4.md)
- [1.0.3 更新公告](docs/RELEASE_NOTES_1.0.3.md)
- [1.0.2 更新公告](docs/RELEASE_NOTES_1.0.2.md)

## 协议与致谢

本项目依据 [MIT License](LICENSE) 发布，为独立项目。

项目实现以 Python 为主。第三方依赖及其许可声明保留在对应文件和资源中；旧仓库地址及内部研究记录不随本次迁移公开。

项目早期代码和文档由 [DeepSeek](https://platform.deepseek.com) 与 [DSH](https://github.com/deepseek-ai/deepseek-harness) 协助完成。
