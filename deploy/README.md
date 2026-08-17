# 会议卡密系统 · 部署指南（极空间 NAS + GitHub Pages）

把后端跑在你的极空间 NAS（Docker），前端托管到 GitHub Pages，用自有域名访问。

## 架构

```
GitHub Pages (meet.yourdomain.com)   ← 静态前端（h5/static/）
        │  fetch(API_BASE + /api/...)
        ▼
极空间 NAS · Docker 容器              ← Flask 后端 + Playwright + Chromium
        │  cpolar / Cloudflare Tunnel 暴露公网
        ▼
api.yourdomain.com  →  容器内部 localhost:5000
        │
        ▼
5 个腾讯会议账号（容器内登录一次，profiles 持久化）
```

- 前端与后端跨域，后端已开 `Access-Control-Allow-Origin: *`，无需额外配置。
- 后端只在 `deploy/config/` 里读凭证，登录态只在 `deploy/data/` 里，**绝不进镜像、不进 git**。

---

## 0. 前置条件

- 极空间已装 Docker（应用中心装，或 SSH 后 `docker -v` 验证）。架构已确认为 x86_64。
- 你本机 Windows 上已有 `python/accounts.json` 和 `python/.env`（含账号密码与腾讯会议凭证）。

---

## 1. 把项目放到 NAS

方式 A（推荐）：在 NAS 上 `git clone` 你的仓库到 `/mnt/media/meeting-bot/`。  
方式 B：用 极空间 File Station / SCP 把整个 `meeting-bot` 目录传上去。

最终假设路径为 `/mnt/media/meeting-bot/`。

---

## 2. 放凭证（一次性）

```bash
cd /mnt/media/meeting-bot
mkdir -p deploy/config deploy/data
cp /你本机/meeting-bot/python/accounts.json deploy/config/accounts.json
cp /你本机/meeting-bot/python/.env           deploy/config/.env
```

> `accounts.json` / `.env` 含密码与密钥，不要提交到 GitHub。它们只存在于 NAS 本地的 `deploy/config/`。

---

## 3. 构建并运行（无头建会）

```bash
cd /mnt/media/meeting-bot/deploy
docker compose build
docker compose up -d
```

浏览器开 `http://<NAS_IP>:5000` 应能看到页面（同源可直接用）。

---

## 4. 首次登录 5 个账号（一次性，必做）

建会需要登录态，而登录要人眼扫码/输密码，所以走「虚拟桌面(noVNC)」：

```bash
docker compose -f docker-compose.login.yml up -d
```

1. 浏览器开 `http://<NAS_IP>:6080` → 看到空虚拟桌面。
2. 另开终端（SSH 或 极空间 Docker 终端），对 5 个账号各执行一次：
   ```bash
   docker exec -it meeting-bot-login python /app/python/create_via_web.py --login admin1783592634
   docker exec -it meeting-bot-login python /app/python/create_via_web.py --login wemeeting8280568
   docker exec -it meeting-bot-login python /app/python/create_via_web.py --login wemeeting8280570
   docker exec -it meeting-bot-login python /app/python/create_via_web.py --login wemeeting8295133
   docker exec -it meeting-bot-login python /app/python/create_via_web.py --login wemeeting8295134
   ```

3. 在 noVNC 桌面里完成扫码/输密码登录，登录窗口可关。
4. 全部登完后，回到启动 login 容器的终端按 `Ctrl+C`，然后：
   ```bash
   docker compose -f docker-compose.login.yml down
   docker compose up -d
   ```

登录态已写入 `deploy/data/profiles`，重启容器不丢。

---

## 5. 暴露到公网（二选一）

### 方案 A：Cloudflare Tunnel（免费、稳定、自带 HTTPS）

前提：你有一个域名托管在 Cloudflare。

```bash
# NAS 上用 Docker 跑 cloudflared
docker run -d --name cloudflared cloudflare/cloudflared tunnel --url http://localhost:5000
# 首次需登录授权：docker run --rm cloudflared login
# 之后建议建「命名隧道」把 api.yourdomain.com 指向它（见 Cloudflare 文档）
```

在 Cloudflare DNS 给 `api.yourdomain.com` 加 CNAME 指向该 tunnel。

### 方案 B：cpolar（国产、上手快）

```bash
# 极空间 Docker 拉 cpolar 镜像，或直接按官网装
cpolar http 5000
```

免费版给随机公网域名；付费可绑定 `api.yourdomain.com`。cpolar 官网有「极空间」专题教程。

---

## 6. 前端放到 GitHub Pages

1. 新建仓库（如 `meeting-frontend`），把 `h5/static/` 下文件（`index.html`、`admin.html`、`config.js` 等）推上去。
2. 仓库 Settings → Pages → 选 main 分支 / root → 开启。
3. 绑定自定义域名 `meet.yourdomain.com`（CNAME 指向 `你的用户名.github.io`）。
4. 编辑 `config.js`：
   ```js
   window.API_BASE = "https://api.yourdomain.com";
   ```
   前端就会跨域调用你的 NAS 后端（后端已开 CORS `*`）。

---

## 7. 环境变量

| 变量                                             | 说明                           | 默认                 |
| ---------------------------------------------- | ---------------------------- | ------------------ |
| `PORT`                                         | 监听端口                         | 5000               |
| `CARDS_DB`                                     | 卡密库 sqlite 路径                | /app/data/cards.db |
| `APPID` / `SDKID` / `SECRET_ID` / `SECRET_KEY` | 腾讯会议 REST 凭证（在 .env）         | —                  |
| `HOST_KEY`                                     | 主持人密钥                        | —                  |
| `MEETING_ACCOUNTS` / `MEETING_USERIDS`         | 账号配置（在 .env / accounts.json） | —                  |
| `ADMIN_TOKEN`                                  | 管理员生成卡密令牌                    | —                  |

---

## 8. 日常运维

- 看日志：`docker logs -f meeting-bot`
- 重启：`docker compose restart`
- 升级代码：NAS 上 `git pull` → `docker compose build` → `docker compose up -d`
- 登录过期：重跑第 4 步（只登过期的账号即可）

## 常见问题

- 建会报未登录 / 跳登录页：登录态过期，重跑登录流程。
- 无头 Chromium 崩溃：已加 `--no-sandbox --disable-dev-shm-usage`；仍崩就把 compose 里 `shm_size` 调到 `512mb`。
- 端口被占：改 compose `ports` 左侧（主机端口）。
- 密码里有 `$` 等特殊字符：`deploy/config/.env` 里用双引号包住，例如 `SECRET_KEY="ab$cd"`。
