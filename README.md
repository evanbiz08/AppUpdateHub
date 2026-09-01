# AppUpdateHub — FigmaRestore Sparkle Feed

该仓库的 `version_controller` 分支托管 FigmaRestore 的 Sparkle 2 更新源。

## 发布文件

- `appcast.xml`
- `downloads/FigmaRestore-v<version>.zip`
- `downloads/FigmaRestore-v<version>.md`
- `downloads/*.delta`（存在可生成增量更新的旧版本时）

Feed 地址：

`https://raw.githubusercontent.com/evanbiz08/AppUpdateHub/version_controller/appcast.xml`

## 发布新版本

在 FigmaRestore 仓库执行：

```bash
./Scripts/release.sh 1.0.1 ./ReleaseNotes/1.0.1.md
```

脚本会构建应用、生成 EdDSA 签名的 ZIP 与 `appcast.xml`，但不会 commit 或 push。
