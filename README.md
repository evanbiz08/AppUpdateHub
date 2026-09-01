# AppUpdateHub — FigmaRestore Sparkle Feed

该仓库的 `version_controller` 分支统一托管多个 macOS 应用的 Sparkle 2 更新源。

## 本机发布控制台

在 Finder 中双击 `start.command`：

1. Terminal 启动仅监听 `127.0.0.1` 的本地服务；
2. 浏览器自动打开 AppUpdateHub；
3. 选择应用、填写版本和更新内容、拖入 ZIP；
4. 点击“发布更新”，自动校验、签名、生成 appcast、commit 并 push；
5. 关闭 Terminal 或按 `Control-C` 即停止，不会常驻后台。

浏览器不能直接执行 Git，因此不要直接双击 `index.html`。发布前仓库必须保持 clean，控制台不会夹带已有修改。

应用列表与各自的 feed、下载目录、Sparkle 工具和 Keychain 账户统一配置在 `apps.json`。每个新项目应使用独立的 `downloadsPath` 与 `feedPath`。

## 发布文件

- `appcast.xml`
- `downloads/FigmaRestore-v<version>.zip`
- `downloads/FigmaRestore-v<version>.md`
- `downloads/*.delta`（存在可生成增量更新的旧版本时）

Feed 地址：

`https://raw.githubusercontent.com/evanbiz08/AppUpdateHub/version_controller/appcast.xml`

## 发布新版本

也可以继续在 FigmaRestore 仓库使用命令行发布：

```bash
./Scripts/release.sh 1.0.1 ./ReleaseNotes/1.0.1.md
```

脚本会构建应用、生成 EdDSA 签名的 ZIP 与 `appcast.xml`，但不会 commit 或 push。
