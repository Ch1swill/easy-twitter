# 🐦 Easy Twitter

一个 Telegram Bot，自动下载 Twitter/X 视频 — 转发链接即下载，还能自动监控点赞并批量下载。

## ✨ 功能亮点

- 🔗 **链接下载** — 给 Bot 发一条 Twitter/X 链接，自动下载视频到本地
- ❤️ **自动点赞监控** — 自动轮询指定用户的点赞列表，发现新视频立即下载
- 🔄 **GraphQL 参数自动发现** — 启动时从 X 前端 JS 提取最新 API 参数，定时 + 失败时自动刷新，无需手动维护
- 📦 **Docker 一键部署** — 开箱即用，配置全在 `docker-compose.yaml` 里
- 🗃️ **SQLite 去重** — 已下载记录持久化，重启不重复
- ⚡ **异步队列** — 多 worker 并发下载，不会因为点赞太多卡住
- 🧹 **自动清理** — 下载完成后可自动删除 Bot 端的状态消息

## 🚀 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/Ch1swill/easy-twitter.git
cd easy-twitter
```

### 2. 配置

编辑 `docker-compose.yaml`，填入你的 Telegram Bot Token：

```yaml
environment:
  BOT_TOKEN: "你的_Telegram_Bot_Token"
```

### 3. 放入 Cookies（可选，自动点赞功能需要）

将浏览器导出的 **Netscape 格式** cookies 文件放到 `secrets/cookies.txt`：

```bash
mkdir -p secrets
# 将导出的 cookies.txt 复制到这里
cp ~/Downloads/cookies.txt secrets/cookies.txt
```

> 💡 **如何导出 Cookies？**
> 1. 用 Chrome/Firefox 登录 [x.com](https://x.com)
> 2. 安装浏览器扩展 [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
> 3. 在 x.com 页面点击扩展图标 → 导出为 Netscape 格式
> 4. 保存到 `secrets/cookies.txt`

### 4. 启动

```bash
docker compose up -d
```

搞定！给 Bot 发一条 Twitter/X 链接试试 🎉

## ⚙️ 配置说明

所有配置都在 `docker-compose.yaml` 的 `environment` 中设置：

### 必填项

| 变量 | 说明 |
|---|---|
| `BOT_TOKEN` | Telegram Bot API Token（从 [@BotFather](https://t.me/BotFather) 获取） |

### 常用选项

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ALLOWED_USERS` | _(空=不限)_ | 允许使用的 Telegram 用户 ID，逗号分隔 |
| `HTTP_PROXY` | _(空)_ | HTTP/SOCKS5 代理，同时用于 Telegram API 和 yt-dlp |
| `AUTO_LIKES_ENABLED` | `false` | 是否启用自动点赞监控 |
| `AUTO_LIKES_TARGET_USER` | _(空)_ | 要监控的 X 用户名（不带 @） |
| `AUTO_LIKES_POLL_INTERVAL` | `300` | 轮询间隔（秒），最小 30 |
| `BOT_CLEANUP_MODE` | `minimal` | 下载完成后消息处理：`minimal`（替换为完成提示）/ `delete`（删除消息） |

### 高级选项

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AUTO_LIKES_MAX_PER_ROUND` | `20` | 每轮最多获取的点赞数 |
| `AUTO_LIKES_WORKERS` | `2` | 并发下载 worker 数 |
| `AUTO_DISCOVER_REFRESH_HOURS` | `24` | GraphQL 参数自动刷新间隔（小时） |
| `AUTO_LIKES_RETRY_MAX` | `3` | 下载失败最大重试次数 |

> 📖 完整高级选项请参考 [.env.example](.env.example)

## 📂 数据目录

```
easy-twitter/
├── data/
│   ├── downloads/    # 📥 下载的视频文件
│   ├── logs/         # 📋 运行日志
│   └── state/        # 🗃️ SQLite 数据库（去重记录）
└── secrets/
    └── cookies.txt   # 🍪 X/Twitter Cookies（可选）
```

## 🛠️ 常用命令

```bash
# 启动
docker compose up -d

# 查看日志
docker compose logs -f easy-twitter

# 停止
docker compose down

# 更新到最新版
docker compose pull && docker compose up -d

# 重新构建（代码有改动时）
docker compose up -d --build
```

## 🤖 Bot 命令

| 命令 | 说明 |
|---|---|
| `/start` | 显示欢迎消息 |
| `/help` | 使用帮助 |
| `/list` | 查看最近 10 个下载文件 |
| _发送链接_ | 直接发送 Twitter/X 链接即可下载 |

## 💻 本地开发（不使用 Docker）

```bash
# 安装依赖
pip install -r requirements.txt

# 确保 ffmpeg 已安装
# Windows: winget install ffmpeg
# macOS: brew install ffmpeg
# Linux: apt install ffmpeg

# 复制配置
cp .env.example .env
# 编辑 .env 填入 BOT_TOKEN 等配置

# 启动
python twitter.py
```

## 📄 许可证

[MIT License](LICENSE) © 2026 Ch1swill
