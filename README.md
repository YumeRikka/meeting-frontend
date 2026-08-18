# 会议卡密系统（meeting-bot）

腾讯会议自动建会 + 卡密（邀请码）管理系统：网页端创建/取消会议、卡密生成与管理；后端 Flask 调度 5 个腾讯会议账号，用 Playwright 自动预定会议。支持网页建会（web）与 REST 建会两种方式，建会前先查询各账号会议列表、冲突预检自动挑选空闲账号。

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `h5/` | Flask 后端（`app.py`）+ 前端静态页（`static/index.html` 建会页、`admin.html` 卡密管理页） |
| `python/` | 建会核心：`tencent_meeting.py`（调度/冲突预检）、`create_via_web.py`（浏览器自动化）、`parse_meeting.py`（文本解析） |
| `tests/` | unittest 回归测试（零依赖） |
| `deploy/` | NAS(Docker) 部署产物与指南，见 [`deploy/README.md`](deploy/README.md) |

## 本机（Windows）启动 / 停止

| 操作 | 命令 | 说明 |
| --- | --- | --- |
| 启动 | `run_web.bat` | 启动前自动清理旧进程 → 激活项目 `.venv` → 起 Flask（5000 端口），日志实时显示在控制台 |
| 停止 | `stop_web.bat` | 一键停掉后端，干净无残留 |
| 清理 | `kill_web.bat` | 杀掉占用 5000 端口的进程及所有 `app.py` 进程（`run_web.bat` 启动前会自动调用） |
| 回归测试 | `run_tests.bat` | 跑 `tests/` 全部用例 |
| 公网隧道 | `run_tunnel.bat` | 起 Cloudflare 命名隧道 `meeting-api` → `localhost:5000` |

> ⚠️ 直接点 × 关控制台窗口不可靠：Playwright/Chromium 子进程可能残留、越积越多，旧进程占住 5000 端口会让新改动"看起来不生效"。请用 `stop_web.bat` 停止，或直接重跑 `run_web.bat`（自带清理）。

### 日志怎么看

- 控制台实时输出，同时落盘 `h5/app.log`（`.gitignore` 已忽略）。
- 建会每一步带账号名与原因：`冲突预检：301:冲突 / 302:空闲 … => 预检选中空闲账号 302(302)`、`[建会] ▶ 开始用账号 302(302) 建会（方式=web）`、`✗ 账号 302 建会失败，切换到下一个账号。原因：…`。建会卡住时，日志最后一行就是卡点。

## 依赖与配置

- **唯一 Python 环境**：项目根 `.venv`（Python 3.13，依赖锁版本于 `requirements.txt`）。禁止用系统裸 Python，避免版本漂移。
- 凭证（不入 git）：`python/.env`（腾讯会议 REST 凭证、账号配置、`ADMIN_TOKEN`）、`python/accounts.json`（5 个账号密码）。
- 浏览器登录态：`python/profiles/{userid}/`（Playwright 持久化，Windows 本机可用；跨平台/容器需重登）。

## 部署到 NAS

见 [`deploy/README.md`](deploy/README.md)：Docker 构建、noVNC 首次登录 5 个账号、Cloudflare Tunnel 暴露公网、前端托管 GitHub Pages。
