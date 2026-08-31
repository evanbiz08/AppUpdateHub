# 🚀 AppUpdateHub (FigmaRestore 版本中心)

基于 GitHub 作为静态托管的极简版本控制与更新分发仓库。

---

## 📄 版本配置文件 (`versions/figma-restore.json`)

仅需维护 4 个最核心字段：

```json
{
  "name": "FigmaRestore",
  "version": "1.0.0",
  "url": "http://10.0.0.1/downloads/FigmaRestore-v1.0.0.zip",
  "notes": "🎉 FigmaRestore 1.0.0 首次正式发布\n1. 支持双运行引擎模式 (本机 Google 账号 / 个人专属 Gemini API Key)\n2. 智能识别并解析 Figma 设计稿结构，自动生成 Android XML 布局与样式\n3. 自动同步设计稿切图与资源，极速轻量 (2.3MB)",
  "force": false
}
```

- **`version`**: 当前最新版本号（如 `1.0.0`）；
- **`url`**: 你的内网或自建下载链接；
- **`notes`**: 本次更新的说明内容；
- **`force`**: 是否强制用户升级（`true`/`false`）。

---

## 🌐 客户端如何获取数据

客户端应用直接向 GitHub 发起一次普通的 HTTP GET 请求：

- **GitHub Raw 官方地址**：
  `https://raw.githubusercontent.com/evanbiz08/AppUpdateHub/main/versions/figma-restore.json`

- **国内高速 CDN 镜像（推荐）**：
  `https://cdn.jsdelivr.net/gh/evanbiz08/AppUpdateHub@main/versions/figma-restore.json`

---

## 🚀 如何发布新版本

后续有新版本时，**直接在 GitHub 网页上打开** `versions/figma-restore.json` 修改版本号、内网下载链接和更新日志，点击保存提交即可！
