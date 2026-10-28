# wb2api 1.14.0

## 迁移说明

项目源码与客户端更新源已迁移至 [xiaofan6ya/wb2api](https://github.com/xiaofan6ya/wb2api)。由于先前版本内置的是旧更新地址，1.13.0 及更早版本需手动安装本版一次；之后客户端会从新仓库检查更新。

Windows 版本可覆盖安装并保留原有本地数据。Android 保持原应用标识与签名，可覆盖升级并保留应用数据。后续安装包文件名统一使用 `wb2api` 前缀。

## 更新内容

- 管理后台、Windows 界面、Android 应用名称统一为 wb2api。
- 增加可信代理客户端 IP 识别、IP 访问控制和请求活动统计，并提供 API Key 来源 IP 查看与访问限制。
- 增加客户端设备标识管理和阻止控制，便于按实际接入来源治理 API 使用。
- 增加 API Key 并发限制以及账号调度策略和上游请求启动间隔配置。
- 修复数据库错误信息处理时的安全过滤，并补充服务启动、账号调度、IP 管理、并发限制和更新匹配回归。
- 更新检查使用新仓库，Windows 与 Android 分别选择对应安装包，并校验下载摘要。

## 验证

- 生产服务已部署并确认 1.14.0 正常响应；管理接口鉴权保持有效。
- 真实验证 Chat Completions、Responses、Messages 的流式和非流式调用；临时测试 Key 已删除。
- 70 项定向回归和 10 项维护检查通过。
- Windows EXE 在隔离空数据目录通过原生启动冒烟。
- Android release APK 编译、lint、包内源码与版本检查通过；签名与上一版一致。未进行设备安装测试。

## 安装包

- Windows x64: `wb2api-1.14.0-windows-x64.exe`
- Android arm64: `wb2api-1.14.0-android-arm64.apk`
- 两个平台均提供独立 `.sha256` 文件。
